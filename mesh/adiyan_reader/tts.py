"""
Local, free text-to-speech via the Orpheus model, served through this
deployment's own Ollama instance (a separate model, legraphista/Orpheus:
3b-ft-q4_k_m, from the qwen3 models every other agent uses for reasoning -
Ollama loads/unloads it on demand like any other model, no second inference
engine needed).

Adapted from mesh/voice/Orpheus-TTS-Local/TTS.py's own generate() - that
script is a raw CLI tool (argparse, an interactive input loop, local audio
playback via sounddevice) built for a person running it by hand at a
terminal, not something an A2A agent calls headlessly. This module keeps
only the actual synthesis path (Ollama call -> SNAC decode -> audio bytes),
with model/voice/generation params passed in as an explicit cfg dict
(config_sdk-driven at the call site - mesh/adiyan_reader/skills/
read_next_page.py) instead of argparse flags or module-level globals.

The Ollama call itself goes through mesh/lib/agent_sdk.py's ask(raw=True)
(see _generate_tokens() below), not a private httpx client of this
module's own - the same platform hook every other LLM call in the mesh
already gets: every prompt/response logged centrally
(~/.Adiyan/logs/llm_calls.log), the model name dashboard-editable via
config_sdk. Moved there specifically to get that logging on the one call
shape (raw, streamed, unparsed tokens) that used to bypass it entirely.

Confirmed live this session: Ollama's /api/generate chat-templates the
prompt by default, which breaks Orpheus's expected raw
"<|audio|>voice: text<|eot_id|>" format entirely - the model replies with
ordinary conversational text instead of the <custom_token_N> audio codes
SNAC needs to decode. raw=True on every request here is not optional; the
same bug was patched into the cloned TTS.py script directly too.

Output is Opus-encoded OGG, not the raw WAV Orpheus/SNAC produce - OpenWA's
own send-audio docs are explicit that a WhatsApp voice note (PTT - the mic
bubble + waveform UI) needs audio/ogg;codecs=opus for reliable playback,
confirmed live: ffmpeg transcodes the WAV losslessly-enough for speech in
under 100ms, negligible next to the actual TTS generation time.
"""
import asyncio
import json
import logging
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from mesh.adiyan_reader.constants import AGENT_ID
from mesh.lib import config_sdk
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_seed_config

_agent = AdiyanAgent(AGENT_ID)

_SEED = load_seed_config(Path(__file__).parent)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})

logger = logging.getLogger('AdiyanReaderTTS')

SPECIAL_START = '<|audio|>'
SPECIAL_END = '<|eot_id|>'
CUSTOM_TOKEN_PREFIX = '<custom_token_'
SAMPLE_RATE = 24000

VOICES = ('tara', 'leah', 'jess', 'leo', 'dan', 'mia', 'zac', 'zoe')
DEFAULT_VOICE = 'tara'

# A short, fixed demo line every voice sample uses - original text, not book
# content, so a sample never accidentally reads out someone's actual page.
# Generated once per voice, then cached on disk (get_or_create_voice_sample
# below): the voice models and this line are both static, so regenerating
# per customer request would be pure waste of an Ollama round-trip.
VOICE_SAMPLE_TEXT = "Hello, I'm one of the voices here at Audio Book Junkie. I'll be reading your books to you, one page every night."
_VOICE_SAMPLE_CACHE_DIR = Path(__file__).parent / 'data' / 'voice_samples'


async def get_or_create_voice_sample(voice: str, cfg: Dict[str, Any]) -> bytes:
    """Opus/OGG bytes for `voice` speaking VOICE_SAMPLE_TEXT - read from
    the on-disk cache if a previous request already generated it, otherwise
    synthesized once and cached for every request after. voice_samples.py
    is the only real caller today (see its own docstring)."""
    _VOICE_SAMPLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _VOICE_SAMPLE_CACHE_DIR / f'{voice}.ogg'
    if cache_path.exists():
        return cache_path.read_bytes()
    audio = await synthesize(VOICE_SAMPLE_TEXT, voice, cfg)
    cache_path.write_bytes(audio)
    return audio

# A local Voicebox instance (github.com/jamiepine/voicebox), run natively as
# its own mesh component - not Docker, see mesh/start_all.sh's own voicebox
# entry - offering an alternative synthesize path via its Chatterbox Turbo
# engine, chosen (over Voicebox's other 6 engines) specifically because it's
# the only one that actually performs inline paralinguistic tags rather than
# reading them as literal text, matching what add_emotion_tags() below
# already does for Orpheus. Selected via the 'engine' key in the
# synthesize_speech stage config (config_sdk-driven, same as every other
# knob here) - 'orpheus' (default, the path below this comment) or
# 'chatterbox_turbo'. Both paths coexist deliberately: switching back is a
# config change, not a code revert.
VOICEBOX_URL = 'http://127.0.0.1:17493'
_VOICEBOX_POLL_INTERVAL_SECONDS = 1.0
_VOICEBOX_POLL_TIMEOUT_SECONDS = 180.0

# Chatterbox Turbo's own recognized paralinguistic tags (square brackets) -
# a different vocabulary and syntax than the <angle-bracket> tokens
# add_emotion_tags() below was originally tuned to produce for Orpheus.
# Translated here rather than re-tuning that prompt: the underlying model's
# actual judgment call ("does this sentence call for a tag") is identical
# either way, only the surface syntax the target engine expects differs.
# 'yawn' has no Chatterbox Turbo equivalent - dropped rather than mapped to
# a wrong-sounding substitute, the same "never invent" rule the rest of
# this pipeline already follows for content, applied here to delivery.
_ORPHEUS_TO_CHATTERBOX_TAGS = {
    'laugh': 'laugh', 'chuckle': 'chuckle', 'sigh': 'sigh', 'gasp': 'gasp',
    'cough': 'cough', 'sniffle': 'sniff', 'groan': 'groan',
}
_ORPHEUS_TAG_TRANSLATE_RE = re.compile(r'<(laugh|chuckle|sigh|gasp|yawn|cough|sniffle|groan)>')


def _translate_tags_for_chatterbox(text: str) -> str:
    def _sub(match: 're.Match[str]') -> str:
        mapped = _ORPHEUS_TO_CHATTERBOX_TAGS.get(match.group(1))
        return f'[{mapped}]' if mapped else ''
    return _ORPHEUS_TAG_TRANSLATE_RE.sub(_sub, text)


async def _synthesize_via_voicebox(text: str, profile_id: str, cfg: Dict[str, Any]) -> bytes:
    """Text -> WAV bytes via a local Voicebox instance's Chatterbox Turbo
    engine. Voicebox does its own sentence-aware chunking and crossfading
    internally (its /generate 'max_chunk_chars'/'crossfade_ms' params,
    defaults used here) - a whole page goes in as one string, unlike the
    Orpheus path below, which has to hand-chunk into short pieces itself
    (see _split_into_speech_chunks()'s own docstring for why THAT model
    specifically needs it)."""
    voicebox_url = cfg.get('voicebox_url') or VOICEBOX_URL
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(f'{voicebox_url}/generate', json={
            'profile_id': profile_id, 'text': text, 'engine': 'chatterbox_turbo',
        })
        response.raise_for_status()
        generation_id = response.json()['id']

        elapsed = 0.0
        payload: Dict[str, Any] = {}
        while elapsed < _VOICEBOX_POLL_TIMEOUT_SECONDS:
            status_response = await client.get(f'{voicebox_url}/generate/{generation_id}/status')
            status_response.raise_for_status()
            # Voicebox streams status as SSE ("data: {...}") even on a plain
            # GET - the last line is always the most current state.
            lines = [ln for ln in status_response.text.strip().split('\n') if ln.startswith('data: ')]
            if lines:
                payload = json.loads(lines[-1][len('data: '):])
            status = payload.get('status')
            if status == 'completed':
                break
            if status == 'failed':
                raise RuntimeError(f'Voicebox generation failed: {payload.get("error")}')
            await asyncio.sleep(_VOICEBOX_POLL_INTERVAL_SECONDS)
            elapsed += _VOICEBOX_POLL_INTERVAL_SECONDS
        else:
            raise RuntimeError(f'Voicebox generation {generation_id!r} timed out')

        audio_response = await client.get(f'{voicebox_url}/audio/{generation_id}')
        audio_response.raise_for_status()
        return audio_response.content

_snac_model = None
_snac_device: Optional[str] = None


def _ensure_snac():
    """Lazy singleton, loaded on first real call - torch/snac are heavy
    imports (multi-second load, matching this codebase's own established
    pattern for Docling/LlamaIndex in mesh/memory/memory_index.py), no
    reason to pay that cost just for `import mesh.adiyan_reader.tts` to
    succeed (a syntax check, a different skill's import chain, etc.)."""
    global _snac_model, _snac_device
    if _snac_model is not None:
        return _snac_model, _snac_device

    import torch
    from snac import SNAC

    device = 'cuda' if torch.cuda.is_available() else 'mps' if torch.backends.mps.is_available() else 'cpu'
    model = SNAC.from_pretrained('hubertsiuzdak/snac_24khz').eval().to(device)
    _snac_model, _snac_device = model, device
    logger.info(f'SNAC model loaded on {device}')
    return _snac_model, _snac_device


def clean_for_speech(text: str) -> str:
    """Docling's own markdown export (mesh/memory/memory_index.py's
    ingest_document_by_page) is a page's real text but with markdown
    structure baked in - "## Heading" markers, "<!-- image -->" picture
    placeholders, and similar formatting a human reader ignores by eye but
    an LLM-driven TTS model reads as literal input. Confirmed live:
    "<!-- image -->" got vocalized as the word "image" mid-sentence, and
    page 1 of a real book (title/subtitle/blurb, heavy with ## markers)
    read incompletely and inconsistently across repeated runs - stripped
    to plain prose here before it ever reaches Orpheus."""
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)  # <!-- image --> and similar
    # [Illustration: FIG. 18.--Overcast joint] and similar - Docling's other
    # image-placeholder shape, confirmed live on an image-heavy technical
    # manual (Elements of Plumbing, 37 of its 105 pages carry this markup):
    # unlike "<!-- image -->", these captions have real sentence-ending
    # periods inside them ("FIG. 18."), so they weren't just read as
    # literal bracket/colon noise - they also skewed looks_like_prose()'s
    # own punctuation-ratio heuristic toward "this is prose" on pages that
    # are actually just a run of figure captions with nothing narratable in
    # them at all.
    text = re.sub(r'\[Illustration:[^\]]*\]', '', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)  # markdown headings
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)  # *italic*
    # \_Ninth\_, and similar - Docling escapes underscores it uses for
    # markdown emphasis (backslash-underscore, not the bare *asterisk*
    # form the two lines above already handle), confirmed live on the same
    # image-heavy manual as the [Illustration: ...] fix above: read
    # verbatim, "\_Ninth\_," comes out as literal backslash/underscore
    # noise around the word instead of just "Ninth,". Escaped emphasis
    # first (paired \_..\_), then any leftover lone \_ that wasn't part of
    # a pair (an unmatched escape at a fragment boundary, same reasoning
    # _split_trailing_fragment() already documents for mid-sentence cuts).
    text = re.sub(r'\\_(.+?)\\_', r'\1', text)
    text = re.sub(r'\\_', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)  # collapse excess blank lines
    text = re.sub(r'[ \t]{2,}', ' ', text)  # collapse OCR'd multi-space/tab runs between words
    return text.strip()


_ROMAN_NUMERAL_MARKER_RE = re.compile(r'(?<!\w)[IVXLCDM]{1,4}\.\s')


def looks_like_prose(text: str) -> bool:
    """Confirmed live this session: a chapter-list/table-of-contents page
    (real example - "Past Pain: Dissolving the Pain-Body ... The Origin of
    Fear The Ego's Search for Wholeness") produced genuinely bad audio even
    with every other TTS fix applied (correct chunking, tuned
    repeat_penalty, pipelined decode). That's not a synthesis bug - the
    text itself has no sentence structure to read, just headings jammed
    together. No amount of TTS tuning fixes reading a word-jumble aloud.

    Heuristic, not a real NLP classifier: a page counts as prose if a real
    majority of its characters end up inside actual sentence-ending-
    punctuated pieces, not the word-boundary fallback
    _split_into_speech_chunks() falls back to for punctuation-less runs
    (see that function's own docstring on why that fallback exists at
    all - it keeps a heading page from becoming one giant unsplit chunk,
    but doesn't make heading fragments sound like real speech).

    A dedicated check for roman-numeral list markers ("CHAPTER I. ... II.
    The Use and Care of the Soldering Iron... III. ...") runs BEFORE the
    ratio heuristic below - confirmed live as a real blind spot the ratio
    check alone can't catch: a chapter title is itself a legitimate
    multi-word phrase, so a word-count floor doesn't distinguish "II. The
    Use and Care of the Soldering Iron" (a table-of-contents line) from an
    actual sentence of similar length - the period after "II." isn't a
    real sentence boundary, it's an enumeration marker, and the naive split
    lands on one at the end of nearly every piece by construction, scoring
    ~100% "real sentence chars" despite being pure listing structure. A
    real page of prose essentially never contains 3+ bare roman numerals
    each followed by a period - checked directly against two genuine prose
    pages (zero matches each) and the actual table-of-contents page this
    was found on (17 matches) - so a small count threshold cleanly
    separates the two without touching the ratio check the ORIGINAL bad
    case (heading fragments jammed together with no periods at all, no
    roman numerals in sight) still fails on its own."""
    cleaned = clean_for_speech(text)
    if not cleaned:
        return False
    if len(_ROMAN_NUMERAL_MARKER_RE.findall(cleaned)) >= 3:
        return False
    raw_sentences = re.split(r'(?<=[.!?])\s+', cleaned.replace('\n', ' ').strip())
    real_sentence_chars = sum(len(s) for s in raw_sentences if re.search(r'[.!?]\s*$', s.strip()))
    return real_sentence_chars >= len(cleaned) * 0.5


async def rewrite_for_speech(text: str, cfg: Dict[str, Any]) -> str:
    """Called for EVERY page, not gated behind a pre-check - the model
    itself now decides whether this page is real prose (told to return it
    completely unchanged) or a heading/table-of-contents page (told to
    describe it in 1-3 short spoken sentences instead). Confirmed live this
    session as a genuine, repeated blind spot in the regex-based
    looks_like_prose() heuristic this replaced: first a heading-jumble page
    with no periods at all, then - after that was patched - a table-of-
    contents page numbered with roman numerals ("CHAPTER I. ... II. The Use
    and Care of..."), where a chapter title being itself a legitimate
    multi-word phrase defeated a word-count fix too. Two different regex
    patches for two different failure shapes in one session is the pattern
    this function exists to stop repeating - "is this real prose" is
    exactly the kind of judgment call a model handles better than pattern-
    matching. Every page now pays one Ollama round-trip it didn't before;
    accepted deliberately in exchange for not re-litigating this heuristic
    a third time for whatever the next book's own formatting quirk turns
    out to be.

    Uses the reasoning model this agent already calls for comprehension-
    question generation (qwen3:8b-16k, a different model from Orpheus/SNAC -
    this is an ordinary chat completion, not audio).

    Strictly grounded, same "never invent" rule this whole mesh already
    follows for anything read back to a user (mesh/scheduler/skills/
    run_routine.py's _compose_generic, mesh/analysis/skills/analyze.py's
    strict_grounding, this agent's own _generate_questions) - told
    explicitly to describe, not embellish, and to say plainly when a page
    is just a table of contents rather than trying to narrate every
    heading. Falls back to clean_for_speech(text) unchanged on any failure
    - a page read exactly as it was ingested is still better than no page
    at all."""
    cleaned = clean_for_speech(text)
    try:
        seeded = _seeded('rewrite_for_speech_prompt_template')
        template = await config_sdk.get_constant(
            AGENT_ID, 'rewrite_for_speech_prompt_template', seeded['value'], description=seeded['description'],
        )
        try:
            prompt = template.format(cleaned=cleaned)
        except Exception:
            prompt = seeded['value'].format(cleaned=cleaned)
        rewritten = (await _agent.ask(
            prompt, stage='rewrite_for_speech', model=cfg['model'], temperature=cfg.get('temperature', 0.3),
        ) or '').strip()
        # Confirmed live: on the "return it unchanged" path, the model
        # sometimes echoes the prompt's own \"\"\"{cleaned}\"\"\" quoting
        # back around its answer instead of just returning the bare text -
        # harmless to the actual words, but those literal triple-quote
        # marks would otherwise get narrated too.
        rewritten = re.sub(r'^"""|"""$', '', rewritten).strip()
        return rewritten or cleaned
    except Exception as e:
        logger.warning(f'rewrite_for_speech failed, falling back to raw cleaned text: {e}')
        return cleaned


async def add_emotion_tags(text: str, cfg: Dict[str, Any]) -> str:
    """Inserts Orpheus's own emotion tags (<laugh> <chuckle> <sigh> <gasp>
    <yawn> <cough> <sniffle> <groan> - the literal text tokens this specific
    fine-tune recognizes, see this module's own docstring) inline into
    `text`, via a small text model - not Orpheus itself, which only ever
    turns text into audio, never edits its own input.

    Prompt-tuned live this session against three adversarial test sets
    (a page with genuine emotional beats, a page with none at all, and a
    page full of idiom/personification traps like "the wind sighed" or
    "he coughed up the cash") before being wired in here - see
    adiyan-primer-deck/'s own emotion-tagging artifact for the actual
    before/after examples that shaped every rule in the seeded template.
    gemma4:e2b (this stage's seeded default) was the only model of the
    three tested that passed the idiom-trap set cleanly; qwen3:4b and
    qwen3:8b both either corrupted the surrounding text or tagged fictional
    sounds (the wind, an engine, a pun) that no real person was making.

    Same fail-open shape as rewrite_for_speech() - any failure (bad model
    output, Ollama unreachable) falls back to the original, untagged text
    rather than blocking the whole reading pipeline over an optional
    enhancement. Never invents content: the prompt requires the model to
    return the page unchanged, word for word, if nothing in it actually
    calls for a tag - a page with zero real emotional beats should come
    back byte-identical."""
    try:
        seeded = _seeded('prompt_with_emotions_prompt_template')
        template = await config_sdk.get_constant(
            AGENT_ID, 'prompt_with_emotions_prompt_template', seeded['value'], description=seeded['description'],
        )
        try:
            prompt = template.format(page_text=text)
        except Exception:
            prompt = seeded['value'].format(page_text=text)
        tagged = (await _agent.ask(
            prompt, stage='add_emotion_tags', model=cfg['model'], temperature=cfg.get('temperature', 0.2),
        ) or '').strip()
        return tagged or text
    except Exception as e:
        logger.warning(f'add_emotion_tags failed, falling back to untagged text: {e}')
        return text


# Confirmed live this session, both directions: gemma4:e2b (this stage's
# seeded model) tags correctly - real tags, no word deletion, idiom traps
# correctly skipped - on a multi-sentence window up to ~450 chars. Below
# that, a single isolated TTS chunk (~50-70 chars, no surrounding dialogue
# visible) gives it no evidence a conversation is happening, so it
# under-tags real moments. Above ~580 chars it goes binary: low/default
# temperature makes it silently echo the page back unchanged, high
# temperature makes it willing to tag again but reintroduces the word-
# deletion bug. 400 is a deliberate safety margin under the observed
# ~450-580 char cliff, not the exact measured edge.
EMOTION_WINDOW_MAX_CHARS = 400


def _group_chunks_for_emotion_tagging(chunks: List[str], max_chars: int = EMOTION_WINDOW_MAX_CHARS) -> List[List[str]]:
    """Groups consecutive TTS chunks (each already <= MAX_CHUNK_CHARS from
    _split_into_speech_chunks) into windows for a single add_emotion_tags()
    call each - not one call per page (too long, see that constant's own
    docstring) and not one call per chunk (too little surrounding dialogue
    context for the "only tag during a real conversation" rule to have
    anything to work with)."""
    windows: List[List[str]] = []
    current: List[str] = []
    current_len = 0
    for chunk in chunks:
        added_len = len(chunk) + (1 if current else 0)  # +1 for the joining space
        if current and current_len + added_len > max_chars:
            windows.append(current)
            current, current_len = [], 0
            added_len = len(chunk)
        current.append(chunk)
        current_len += added_len
    if current:
        windows.append(current)
    return windows


_EMOTION_TAG_RE = re.compile(r'<(?:laugh|chuckle|sigh|gasp|yawn|cough|sniffle|groan)>')
_WORD_RE = re.compile(r"[A-Za-z']+")


def _accept_tagged_piece(original: str, piece: str) -> bool:
    """Decides whether one resplit piece is trustworthy enough to replace
    its corresponding original chunk - per CHUNK, not per window, and
    deterministic (checking which words are actually present), not
    another prompt asking the model to behave. Confirmed live this
    session, twice more even after multiple rounds of prompt tuning aimed
    at fixing this: gemma4:e2b sometimes still replaces the trigger word
    with its tag instead of adding the tag beside it (\"Professor
    McGonagall gasped.\" -> \"Professor McGonagall <gasp>.\", losing
    \"gasped\" entirely). No amount of prompt wording has made that fail
    reliably, so this catches it in code instead: every real word (letters
    only, case-insensitive - punctuation ignored on purpose, see below)
    that appeared in `original` must still appear in `piece` after
    stripping the tag(s) back out. Confirmed live: a naive word-COUNT
    check isn't enough here - stripping "<gasp>" out of "McGonagall
    <gasp>." leaves the trailing "." floating as its own whitespace-
    split token, which inflates the count enough to slip past a bare
    length comparison even though "gasped" itself is gone. Comparing
    actual word identities, not just how many tokens are left, is what
    catches that.

    A piece with no tag in it at all is also rejected here, even if it
    differs from `original` in some harmless way (whitespace, a
    fixed-up quote) - see _tag_chunks_windowed()'s own docstring for why
    that incidental drift should never propagate into a chunk that had
    nothing to tag in the first place."""
    if not _EMOTION_TAG_RE.search(piece):
        return False
    stripped = _EMOTION_TAG_RE.sub('', piece)
    from collections import Counter
    original_words = Counter(w.lower() for w in _WORD_RE.findall(original))
    piece_words = Counter(w.lower() for w in _WORD_RE.findall(stripped))
    return all(piece_words[word] >= count for word, count in original_words.items())


async def _tag_chunks_windowed(chunks: List[str], cfg: Dict[str, Any]) -> List[str]:
    """add_emotion_tags(), applied per-window instead of per-page or
    per-chunk - see _group_chunks_for_emotion_tagging()'s own docstring for
    why. Re-splits each window's tagged text back onto its original chunk
    boundaries using the same sentence regex _split_into_speech_chunks()
    uses (a tag is always inserted inside a sentence, never across one, so
    the sentence count should match the original chunk count going in).
    If it doesn't - the model merged, dropped, or otherwise reshaped a
    sentence - that window's original, untagged chunks are used instead
    rather than risk silently misaligning tagged text onto the wrong
    chunk. Same fail-open principle as add_emotion_tags() itself: a
    window that doesn't tag cleanly should never break the reading
    pipeline or scramble which sentence goes where.

    Each resplit piece is then accepted or rejected individually via
    _accept_tagged_piece(), not as a whole window - confirmed live this
    session: when only one sentence in a window actually gets a tag, the
    join-then-resplit roundtrip can still introduce small incidental
    drift (a double space collapsed, a missing quote closed) into the
    OTHER sentences in that same window, which never had anything to tag
    at all. Trusting the resplit per-chunk, only when that specific
    chunk actually gained a real tag, keeps every untagged sentence
    byte-identical to its original regardless of what happened elsewhere
    in the same window."""
    windows = _group_chunks_for_emotion_tagging(chunks)
    result: List[str] = []
    for window in windows:
        joined = ' '.join(window)
        tagged = await add_emotion_tags(joined, cfg)
        if tagged.strip() == joined.strip():
            result.extend(window)
            continue
        pieces = [p for p in re.split(r'(?<=[.!?])\s+', tagged.strip()) if p]
        if len(pieces) != len(window):
            logger.warning(
                f'add_emotion_tags window returned {len(pieces)} sentences for '
                f'{len(window)} original chunks - using untagged chunks for this window.',
            )
            result.extend(window)
            continue
        for original, piece in zip(window, pieces):
            result.append(piece if _accept_tagged_piece(original, piece) else original)
    return result


MAX_CHUNK_CHARS = 300

# 16-bit mono silence, inserted between chunks (not within one) to mask the
# splice between two independent SNAC decodes - see synthesize()'s own note.
_SILENCE_GAP_MS = 150
_SILENCE_GAP = b'\x00\x00' * int(SAMPLE_RATE * _SILENCE_GAP_MS / 1000)


def _split_into_speech_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> List[str]:
    """Confirmed live this session: Orpheus (a 3B model) reads a short
    sentence accurately and completely every time, but accuracy/coherence
    measurably degrades over a longer single generation - a real page
    (1500+ chars) came back garbled and incomplete even once the
    max_tokens truncation bug was fixed, at both q4 and q8 quantization.
    Splitting into shorter, sentence-respecting pieces and synthesizing
    each separately keeps every individual generation inside the range
    that actually worked reliably in testing, at the cost of one Ollama
    round-trip per chunk instead of one per page.

    Splits on sentence-ending punctuation, not python nltk/spacy - good
    enough for prose, and this module already avoids one more heavy
    optional dependency (see _ensure_snac()'s own reasoning for
    torch/snac already being the deliberate exception).

    Confirmed live this session: a heading-only page (title page, table of
    contents) has no "." / "!" / "?" anywhere, so the split below returns
    the ENTIRE page as one unbroken "sentence" - silently defeating the
    whole point of this function and sending one huge run-on chunk
    straight to Orpheus. That produced real garbled, looping audio on a
    live test (whisper transcript showed mangled proper nouns and
    repeated phrases). Any such piece gets a second, word-boundary split
    below so no chunk this function returns is ever over max_chars,
    punctuation or not."""
    raw_sentences = re.split(r'(?<=[.!?])\s+', text.replace('\n', ' ').strip())
    sentences: List[str] = []
    for raw in raw_sentences:
        if len(raw) <= max_chars:
            sentences.append(raw)
            continue
        words = raw.split(' ')
        piece = ''
        for word in words:
            candidate = f'{piece} {word}'.strip() if piece else word
            if len(candidate) > max_chars and piece:
                sentences.append(piece)
                piece = word
            else:
                piece = candidate
        if piece:
            sentences.append(piece)

    # One sentence, one chunk - deliberately not merged. Confirmed live this
    # session, twice: pairing even two short sentences into one chunk
    # measurably increased garbling (whisper transcript showed new
    # hallucinated words and mangled phrases that weren't there when the
    # same sentences ran alone). One-per-chunk isn't perfect either - this
    # model has no fixed random seed, so some run-to-run variance is
    # unavoidable regardless of chunk size - but it's the best-evidenced
    # option from actual testing, not a guess. max_chars only matters for
    # the word-boundary fallback above (sentence-less pages).
    return [s for s in sentences if s] or [text]


# Every chunk _split_into_speech_chunks() produces is <= MAX_CHUNK_CHARS
# (300) by construction, never an unbounded string - so the actual worst
# case this needs to cover is fixed and known, not something to estimate
# per call. Confirmed live this session that scaling the budget DOWN for
# a short chunk is exactly what causes truncation: a 75-character chunk
# needed more than max(1200, 75*16)=1200 tokens to finish naturally, while
# every chunk that got the full 6000 (long or short) has always completed
# well under it. There's no cost to over-provisioning num_predict -
# Ollama stops at a real completion regardless of how high the cap is set
# (confirmed by every successful synthesis this session finishing far
# short of 6000) - so there's no reason to ever request less than this
# for any chunk. Checked directly against Ollama's own model info for
# this model: num_ctx=32768, real trained context_length=131072 - 6000
# tokens of generation plus a ~200-token prompt uses under a fifth of the
# configured window, nowhere near a context overrun.
#
# What this does NOT protect against: a genuine repetition loop (the
# model looping on itself instead of ever reaching a natural stop) can
# still exhaust any fixed budget, however large - that's a different
# failure mode than undersized budget, and no number fixes it
# deterministically. _generate_tokens() still logs plainly when this
# happens so it surfaces as what it actually is, not a mysteriously cut
# off voice note.
ORPHEUS_MAX_TOKENS = 6000


def _turn_token_into_id(token_string: str, index: int) -> Optional[int]:
    token_string = token_string.strip()
    last_token_start = token_string.rfind(CUSTOM_TOKEN_PREFIX)
    if last_token_start == -1:
        return None
    last_token = token_string[last_token_start:]
    if not (last_token.startswith(CUSTOM_TOKEN_PREFIX) and last_token.endswith('>')):
        return None
    try:
        number_str = last_token[len(CUSTOM_TOKEN_PREFIX):-1]
        return int(number_str) - 10 - ((index % 7) * 4096)
    except ValueError:
        return None


def _convert_frame_to_audio(multiframe: List[int]) -> Optional[bytes]:
    """One 28-token frame -> raw PCM bytes, via SNAC's 3-codebook decode.
    Verbatim logic from TTS.py's convert_to_audio() - this is SNAC's own
    codec structure (a fixed 7-tokens-per-timestep interleaving across 3
    hierarchical codebooks), not something to simplify without
    understanding the model's own token layout."""
    import torch

    if len(multiframe) < 7:
        return None
    model, device = _ensure_snac()

    codes_0: List[int] = []
    codes_1: List[int] = []
    codes_2: List[int] = []
    num_frames = len(multiframe) // 7
    frame = multiframe[:num_frames * 7]

    for j in range(num_frames):
        i = 7 * j
        codes_0.append(frame[i])
        codes_1.extend([frame[i + 1], frame[i + 4]])
        codes_2.extend([frame[i + 2], frame[i + 3], frame[i + 5], frame[i + 6]])

    codes = [
        torch.tensor(codes_0, device=device, dtype=torch.int32).unsqueeze(0),
        torch.tensor(codes_1, device=device, dtype=torch.int32).unsqueeze(0),
        torch.tensor(codes_2, device=device, dtype=torch.int32).unsqueeze(0),
    ]
    if any(bool(torch.any((c < 0) | (c > 4096))) for c in codes):
        return None

    with torch.inference_mode():
        audio_hat = model.decode(codes)

    audio_slice = audio_hat[:, :, 2048:4096].detach().cpu().numpy()
    audio_int16 = (audio_slice * 32767).astype('int16')
    return audio_int16.tobytes()


async def _generate_tokens(prompt: str, cfg: Dict[str, Any], max_tokens: int) -> List[str]:
    """Streams raw completion tokens from Ollama for the given already-
    formatted Orpheus prompt, via mesh/lib/agent_sdk.py's ask(raw=True) -
    the same platform hook every other LLM call in this mesh already gets
    (central logging in ~/.Adiyan/logs/llm_calls.log, dashboard-editable
    stage config), rather than this module's own private httpx client.
    raw=True inside ask() is still the one non-negotiable flag underneath
    - see this module's own docstring on why Ollama's default chat-
    templating breaks Orpheus's expected format entirely. max_tokens is
    ORPHEUS_MAX_TOKENS unless a caller explicitly overrides it via cfg -
    see that constant's own docstring for why a fixed ceiling replaced a
    per-chunk estimate that could (and did) undershoot for a short chunk."""
    return await _agent.ask(
        prompt, stage='synthesize_speech', model=cfg['model'], temperature=cfg.get('temperature', 0.6),
        raw=True, num_predict=max_tokens, top_p=cfg.get('top_p', 0.9),
        # Canopy Labs' own docs recommend 1.1 as the minimum-for-stability
        # default. Bumped to 1.3 here after live testing this session
        # kept producing exact-phrase repetition loops ("resh, resh,
        # resh...") even at 1.1, on both merged and single-sentence
        # chunks - an experiment based on this deployment's own observed
        # failures, not a documented Orpheus recommendation.
        repetition_penalty=cfg.get('repetition_penalty', 1.3),
    )


def _tokens_to_pcm_segments(tokens: List[str]) -> List[bytes]:
    """Raw PCM frames only, no WAV framing yet - synthesize() accumulates
    these across every text chunk before writing one combined WAV, so a
    multi-chunk page produces one continuous audio file, not several
    voice notes stitched by WAV headers."""
    buffer: List[int] = []
    count = 0
    segments: List[bytes] = []
    for token_text in tokens:
        token_id = _turn_token_into_id(token_text, count)
        if token_id is None or token_id <= 0:
            continue
        buffer.append(token_id)
        count += 1
        if count % 7 == 0 and count > 27:
            audio = _convert_frame_to_audio(buffer[-28:])
            if audio is not None:
                segments.append(audio)
    return segments


def _write_wav(pcm_segments: List[bytes]) -> bytes:
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        wav_path = f.name
    with wave.open(wav_path, 'wb') as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        for segment in pcm_segments:
            wav_file.writeframes(segment)

    wav_bytes = Path(wav_path).read_bytes()
    Path(wav_path).unlink(missing_ok=True)
    return wav_bytes


def _wav_to_opus_ogg(wav_bytes: bytes, rate: float = 1.0) -> bytes:
    """ffmpeg transcode, WAV -> Opus/OGG - see this module's own docstring
    for why WhatsApp's voice-note (PTT) UI needs this, not raw WAV.

    rate: playback-speed multiplier applied via ffmpeg's own atempo filter
    (1.0 = unchanged, 0.85 = noticeably slower without a pitch shift, unlike
    naively resampling). Engine-agnostic on purpose - neither Orpheus nor
    Chatterbox Turbo exposes a tempo knob of its own, so this is the one
    place "read it slower" can actually apply regardless of which engine
    rendered the audio. atempo only accepts 0.5-2.0 in a single filter
    instance; every real request from a customer ("slower"/"a bit slower")
    stays well inside that range, so chaining multiple atempo filters for
    a more extreme rate was never needed."""
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wav_f:
        wav_f.write(wav_bytes)
        wav_path = wav_f.name
    ogg_path = wav_path.replace('.wav', '.ogg')
    try:
        cmd = ['ffmpeg', '-y', '-i', wav_path]
        if rate != 1.0:
            cmd += ['-filter:a', f'atempo={rate}']
        cmd += ['-c:a', 'libopus', '-b:a', '32k', ogg_path]
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        return Path(ogg_path).read_bytes()
    finally:
        Path(wav_path).unlink(missing_ok=True)
        Path(ogg_path).unlink(missing_ok=True)


async def synthesize(
    text: str, voice: str, cfg: Dict[str, Any], emotion_cfg: Optional[Dict[str, Any]] = None,
    rate: float = 1.0,
) -> bytes:
    """Text -> Opus/OGG audio bytes, ready for OpenWAService.send_voice().

    Synthesized in sentence-respecting chunks (_split_into_speech_chunks),
    not as one call over the whole page - see that function's own
    docstring for why: a short chunk reads accurately and completely every
    time in testing, a long one measurably degrades.

    emotion_cfg: when given, _tag_chunks_windowed() groups this page's
    already-split chunks into ~400-char multi-sentence windows and runs
    add_emotion_tags() once per window, then re-splits the tagged result
    back onto the original chunk boundaries - not once on the whole page,
    and not once per individual chunk. Confirmed live this session:
    gemma4:e2b (the seeded model for this stage) needs a multi-sentence
    window to reliably recognize "this is a real conversation" (a single
    isolated chunk like "Professor McGonagall gasped." has no visible
    dialogue around it and gets silently skipped), but a whole page
    (1500+ chars) makes it either refuse outright or start deleting the
    words it tags - see _group_chunks_for_emotion_tagging()'s own
    docstring for the exact thresholds this was tuned against.

    Chunk *generation* runs sequentially, not concurrently - this already
    competes with real WhatsApp traffic for the same single-slot Ollama the
    rest of this mesh shares (see docs/RUNNING_RECORD.md's own account of
    that contention), so firing multiple chunks at Ollama in parallel would
    just queue behind itself, not go faster. But SNAC decoding (local
    CPU/GPU, not Ollama) doesn't need to wait for that queue: this pipelines
    it against the *next* chunk's generation instead of running strictly
    after it - while chunk N's tokens are being turned into audio on this
    machine, chunk N+1 is already streaming in from Ollama, instead of one
    completely idle CPU/GPU while the other sits idle.

    Raises if Orpheus produced no usable audio at all across every chunk
    (e.g. Ollama unreachable, or the model genuinely emitted nothing) -
    callers decide what that means for their own domain, same contract
    every other tool call in this mesh follows.

    cfg['engine'] == 'chatterbox_turbo' takes a completely different path
    (this module's own _synthesize_via_voicebox()) instead of everything
    below this docstring - see VOICEBOX_URL's own comment for why this
    exists as a config-selected alternative rather than a wholesale
    replacement. `voice` is ignored on that path (cfg['voicebox_profile_id']
    already carries a specific cloned voice identity), and emotion tagging
    still runs first, exactly as it does for Orpheus - only the tag
    SYNTAX handed to the engine differs (_translate_tags_for_chatterbox)."""
    if cfg.get('engine') == 'chatterbox_turbo':
        profile_id = cfg.get('voicebox_profile_id')
        if not profile_id:
            raise RuntimeError("engine='chatterbox_turbo' but no voicebox_profile_id configured.")
        text = clean_for_speech(text)
        chunks = _split_into_speech_chunks(text)
        if emotion_cfg is not None:
            chunks = await _tag_chunks_windowed(chunks, emotion_cfg)
        tagged_text = _translate_tags_for_chatterbox(' '.join(chunks))
        wav_bytes = await _synthesize_via_voicebox(tagged_text, profile_id, cfg)
        return await asyncio.to_thread(_wav_to_opus_ogg, wav_bytes, rate)

    if voice not in VOICES:
        logger.warning(f"Unknown voice {voice!r}, falling back to {DEFAULT_VOICE!r}")
        voice = DEFAULT_VOICE

    text = clean_for_speech(text)
    chunks = _split_into_speech_chunks(text)
    if emotion_cfg is not None:
        chunks = await _tag_chunks_windowed(chunks, emotion_cfg)
    all_segments: List[bytes] = []
    decode_task: Optional[asyncio.Task] = None
    flushed_any = False

    async def _flush_decode() -> None:
        nonlocal flushed_any
        segments = await decode_task
        if flushed_any and segments:
            # Each chunk is an independent SNAC decode - splicing them back
            # to back with zero gap produces an audible click/pop at every
            # chunk boundary (a phase discontinuity between two unrelated
            # generations). A short silence smooths the splice and reads as
            # a natural breath between sentences instead of a stitching seam.
            all_segments.append(_SILENCE_GAP)
        all_segments.extend(segments)
        flushed_any = True

    for chunk in chunks:
        prompt = f'{SPECIAL_START}{voice}: {chunk}{SPECIAL_END}'
        max_tokens = cfg.get('max_tokens') or ORPHEUS_MAX_TOKENS
        tokens = await _generate_tokens(prompt, cfg, max_tokens)
        if decode_task is not None:
            await _flush_decode()
        decode_task = asyncio.create_task(asyncio.to_thread(_tokens_to_pcm_segments, tokens))

    if decode_task is not None:
        await _flush_decode()

    if not all_segments:
        raise RuntimeError('Orpheus produced no audio for this text - check Ollama and the model name.')

    wav_bytes = await asyncio.to_thread(_write_wav, all_segments)
    return await asyncio.to_thread(_wav_to_opus_ogg, wav_bytes, rate)
