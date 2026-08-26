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
made async (httpx, matching every other agent's own Ollama calls) instead
of TTS.py's synchronous requests.Session, with model/voice/generation
params passed in as an explicit cfg dict (config_sdk-driven at the call
site - mesh/adiyan_reader/skills/read_next_page.py) instead of argparse
flags or module-level globals.

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
import logging
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger('AdiyanReaderTTS')

SPECIAL_START = '<|audio|>'
SPECIAL_END = '<|eot_id|>'
CUSTOM_TOKEN_PREFIX = '<custom_token_'
SAMPLE_RATE = 24000

VOICES = ('tara', 'leah', 'jess', 'leo', 'dan', 'mia', 'zac', 'zoe')
DEFAULT_VOICE = 'tara'

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
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)  # markdown headings
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # **bold**
    text = re.sub(r'\*(.+?)\*', r'\1', text)  # *italic*
    text = re.sub(r'\n{3,}', '\n\n', text)  # collapse excess blank lines
    text = re.sub(r'[ \t]{2,}', ' ', text)  # collapse OCR'd multi-space/tab runs between words
    return text.strip()


MAX_CHUNK_CHARS = 220


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
    torch/snac already being the deliberate exception)."""
    sentences = re.split(r'(?<=[.!?])\s+', text.replace('\n', ' ').strip())
    chunks: List[str] = []
    current = ''
    for sentence in sentences:
        if not sentence:
            continue
        candidate = f'{current} {sentence}'.strip() if current else sentence
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


def _estimate_max_tokens(text: str) -> int:
    """Confirmed live this session: a fixed 1200-token cap (TTS.py's own
    CLI-tool default, sized for short interactive test phrases) silently
    truncated a real 303-character book page mid-sentence -
    done_reason:"length" at exactly eval_count:1200, not a natural stop.
    Roughly ~5-6 audio-code tokens per character of input held across
    several real test runs, but with real variance chunk to chunk (one
    252-char chunk still hit a x12 estimate live) - scaled generously
    (x16) with headroom, capped so one pathologically long chunk can't
    turn into a multi-minute generation on its own."""
    return min(6000, max(1200, len(text) * 16))


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
    formatted Orpheus prompt. raw=True is the one non-negotiable flag - see
    this module's own docstring. max_tokens is passed in explicitly
    (_estimate_max_tokens(), scaled to the actual input length) rather than
    read from cfg directly - see that function's own docstring for the real
    truncation this fixes."""
    payload = {
        'model': cfg['model'],
        'prompt': prompt,
        'raw': True,
        'stream': True,
        'options': {
            'num_predict': max_tokens,
            'temperature': cfg.get('temperature', 0.6),
            'top_p': cfg.get('top_p', 0.9),
            'repeat_penalty': cfg.get('repetition_penalty', 1.1),
        },
    }
    tokens: List[str] = []
    async with httpx.AsyncClient(timeout=cfg.get('timeout', 300.0)) as client:
        async with client.stream('POST', f"{cfg.get('base_url', 'http://localhost:11434')}/api/generate", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                import json
                data = json.loads(line)
                if data.get('response'):
                    tokens.append(data['response'])
                if data.get('done'):
                    if data.get('done_reason') == 'length':
                        logger.warning(
                            f'Orpheus generation hit max_tokens={max_tokens} before finishing '
                            f'({len(prompt)}-char prompt) - audio is likely cut off. Raise the cap '
                            "or check _estimate_max_tokens()'s scaling."
                        )
                    break
    return tokens


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


def _wav_to_opus_ogg(wav_bytes: bytes) -> bytes:
    """ffmpeg transcode, WAV -> Opus/OGG - see this module's own docstring
    for why WhatsApp's voice-note (PTT) UI needs this, not raw WAV."""
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wav_f:
        wav_f.write(wav_bytes)
        wav_path = wav_f.name
    ogg_path = wav_path.replace('.wav', '.ogg')
    try:
        subprocess.run(
            ['ffmpeg', '-y', '-i', wav_path, '-c:a', 'libopus', '-b:a', '32k', ogg_path],
            check=True, capture_output=True, timeout=30,
        )
        return Path(ogg_path).read_bytes()
    finally:
        Path(wav_path).unlink(missing_ok=True)
        Path(ogg_path).unlink(missing_ok=True)


async def synthesize(text: str, voice: str, cfg: Dict[str, Any]) -> bytes:
    """Text -> Opus/OGG audio bytes, ready for OpenWAService.send_voice().

    Synthesized in sentence-respecting chunks (_split_into_speech_chunks),
    not as one call over the whole page - see that function's own
    docstring for why: a short chunk reads accurately and completely every
    time in testing, a long one measurably degrades. Chunks run
    sequentially, not concurrently - this already competes with real
    WhatsApp traffic for the same single-slot Ollama the rest of this mesh
    shares (see mesh/RUNNING_RECORD.md's own account of that contention),
    so a page's worth of chunks queuing behind each other is the honest
    cost, not something to hide by firing them in parallel.

    Raises if Orpheus produced no usable audio at all across every chunk
    (e.g. Ollama unreachable, or the model genuinely emitted nothing) -
    callers decide what that means for their own domain, same contract
    every other tool call in this mesh follows."""
    if voice not in VOICES:
        logger.warning(f"Unknown voice {voice!r}, falling back to {DEFAULT_VOICE!r}")
        voice = DEFAULT_VOICE

    text = clean_for_speech(text)
    chunks = _split_into_speech_chunks(text)

    all_segments: List[bytes] = []
    for chunk in chunks:
        prompt = f'{SPECIAL_START}{voice}: {chunk}{SPECIAL_END}'
        max_tokens = cfg.get('max_tokens') or _estimate_max_tokens(chunk)
        tokens = await _generate_tokens(prompt, cfg, max_tokens)
        segments = await asyncio.to_thread(_tokens_to_pcm_segments, tokens)
        all_segments.extend(segments)

    if not all_segments:
        raise RuntimeError('Orpheus produced no audio for this text - check Ollama and the model name.')

    wav_bytes = await asyncio.to_thread(_write_wav, all_segments)
    return await asyncio.to_thread(_wav_to_opus_ogg, wav_bytes)
