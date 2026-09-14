"""
Local speech-to-text for incoming WhatsApp voice notes, via OpenAI's own
`whisper` CLI (Homebrew: `openai-whisper`) - already installed on this
machine, confirmed live to explicitly support Tamil ('ta') among its
language options. No cloud call, no API key: this is a subprocess, the
same "shell out to a local binary" shape mesh/adiyan_reader/tts.py already
uses for ffmpeg.

Deliberately NOT sarvam-1 or any Ollama-served model - sarvam-1 is a text
model with no audio input at all (confirmed against its own Ollama model
card: "8K context window - Text"). Transcription and the humanize step's
own reply-language choice are two separate stages of this pipeline; this
module is only the first one - see TranscriptionResult.language's own
docstring for how the second stage actually uses what this one detects.

Whisper's own CLI, not the `openai-whisper` Python package imported
directly - confirmed live this session that only the Homebrew binary is
actually installed (`pip show openai-whisper` finds nothing in this
venv), so shelling out to the CLI already on PATH is what's actually
available, not a guess.
"""
import asyncio
import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger('AudioTranscribe')

# 'large-v3' (~1.55B params, confirmed against Whisper's own
# available_models() list - not the ambiguous 'large' alias, whose target
# version isn't pinned) - upgraded from 'small' (~242M, confirmed by
# directly loading and counting that checkpoint's own state dict) on the
# owner's own request, to test whether more capacity helps general
# accuracy and/or the confirmed code-switching failure (a Tamil+English
# voice note transcribed with the English portion mangled into Tamil
# phonetics - see mesh/orchestrator/skills/handle_message.py's own
# summon-phrase-variants comment for the related, separate fix that
# came out of the same testing). Real cost: large-v3 is ~6x 'small''s
# parameter count, CPU-only on this machine - expect real extra seconds
# to minutes per voice note, not the ~15-35s 'small' measured at.
DEFAULT_WHISPER_MODEL = 'large-v3'

# Whisper's own ISO-639-1-ish code -> display name mapping, extracted
# directly from the installed CLI's own package (whisper.tokenizer.LANGUAGES,
# confirmed live via `/opt/homebrew/.../openai-whisper/.../bin/python3 -c
# "from whisper.tokenizer import LANGUAGES; print(LANGUAGES)"` since that
# package isn't importable from this venv - see this module's own docstring
# on why the CLI is shelled out to instead) rather than hand-typed, to avoid
# exactly the kind of transcription error a hand-typed Tamil/Devanagari
# string already caused elsewhere this session. Names are lowercase as
# Whisper itself stores them; language_display_name() below title-cases for
# use in a natural-language prompt instruction.
WHISPER_LANGUAGES = {
    'en': 'english', 'zh': 'chinese', 'de': 'german', 'es': 'spanish', 'ru': 'russian',
    'ko': 'korean', 'fr': 'french', 'ja': 'japanese', 'pt': 'portuguese', 'tr': 'turkish',
    'pl': 'polish', 'ca': 'catalan', 'nl': 'dutch', 'ar': 'arabic', 'sv': 'swedish',
    'it': 'italian', 'id': 'indonesian', 'hi': 'hindi', 'fi': 'finnish', 'vi': 'vietnamese',
    'he': 'hebrew', 'uk': 'ukrainian', 'el': 'greek', 'ms': 'malay', 'cs': 'czech',
    'ro': 'romanian', 'da': 'danish', 'hu': 'hungarian', 'ta': 'tamil', 'no': 'norwegian',
    'th': 'thai', 'ur': 'urdu', 'hr': 'croatian', 'bg': 'bulgarian', 'lt': 'lithuanian',
    'la': 'latin', 'mi': 'maori', 'ml': 'malayalam', 'cy': 'welsh', 'sk': 'slovak',
    'te': 'telugu', 'fa': 'persian', 'lv': 'latvian', 'bn': 'bengali', 'sr': 'serbian',
    'az': 'azerbaijani', 'sl': 'slovenian', 'kn': 'kannada', 'et': 'estonian',
    'mk': 'macedonian', 'br': 'breton', 'eu': 'basque', 'is': 'icelandic', 'hy': 'armenian',
    'ne': 'nepali', 'mn': 'mongolian', 'bs': 'bosnian', 'kk': 'kazakh', 'sq': 'albanian',
    'sw': 'swahili', 'gl': 'galician', 'mr': 'marathi', 'pa': 'punjabi', 'si': 'sinhala',
    'km': 'khmer', 'sn': 'shona', 'yo': 'yoruba', 'so': 'somali', 'af': 'afrikaans',
    'oc': 'occitan', 'ka': 'georgian', 'be': 'belarusian', 'tg': 'tajik', 'sd': 'sindhi',
    'gu': 'gujarati', 'am': 'amharic', 'yi': 'yiddish', 'lo': 'lao', 'uz': 'uzbek',
    'fo': 'faroese', 'ht': 'haitian creole', 'ps': 'pashto', 'tk': 'turkmen',
    'nn': 'nynorsk', 'mt': 'maltese', 'sa': 'sanskrit', 'lb': 'luxembourgish',
    'my': 'myanmar', 'bo': 'tibetan', 'tl': 'tagalog', 'mg': 'malagasy', 'as': 'assamese',
    'tt': 'tatar', 'haw': 'hawaiian', 'ln': 'lingala', 'ha': 'hausa', 'ba': 'bashkir',
    'jw': 'javanese', 'su': 'sundanese', 'yue': 'cantonese',
}


def language_display_name(code: Optional[str]) -> Optional[str]:
    """'ta' -> 'Tamil', for feeding into a natural-language reply-language
    instruction (mesh/orchestrator/humanize.py's own `language` param) -
    a model reliably understands a real language name, not an ISO code.
    None if code is None/empty/unrecognized (a Whisper CLI version could
    detect a language newer than WHISPER_LANGUAGES has, though none has
    been observed live) - the caller then falls back to not pinning a
    reply language at all, same as no language was ever detected."""
    if not code:
        return None
    name = WHISPER_LANGUAGES.get(code.lower())
    return name.title() if name else None


@dataclass
class TranscriptionResult:
    text: str
    # Whisper's own detected 2-3 letter code (e.g. 'ta', 'en', 'hi') - the
    # same detection that already had to run correctly for `text` itself to
    # come out right, just no longer discarded. None only if the CLI's own
    # json output is missing a language field entirely (shouldn't happen
    # given a real transcription succeeded, but degrades to "unknown"
    # rather than crashing).
    language: Optional[str]


def _run_whisper(audio_bytes: bytes, language: Optional[str], model: str) -> Optional[TranscriptionResult]:
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = Path(tmpdir) / 'voice_note.ogg'
        audio_path.write_bytes(audio_bytes)
        cmd = [
            'whisper', str(audio_path), '--model', model,
            # json, not txt - confirmed live via the installed CLI's own
            # output that json's top-level 'language' field is exactly
            # Whisper's own detected spoken language, the same detection
            # already driving `text` itself; 'txt' output discarded this
            # entirely, forcing every downstream reply-language decision
            # to either guess or hardcode one language for every voice
            # note regardless of what was actually spoken (confirmed live:
            # this was hardcoded to 'Tamil' - see handle_message.py's own
            # comment on why that was wrong for anyone speaking anything
            # else).
            '--output_format', 'json', '--output_dir', tmpdir,
            '--verbose', 'False',
        ]
        if language:
            # Omitted entirely (not passed as e.g. 'auto') when language is
            # None - confirmed live this session that forcing the wrong
            # language badly mangles transcription in EITHER direction: a
            # real English voice note forced through --language ta came
            # back as Tamil-script gibberish approximating the English
            # sounds ("என் அதார் நம்பர் என்று கேட்கிறேன்" for "What is my
            # Aadhaar number?"), and a real Tamil note with English
            # technical terms mixed in suffered the same way under the
            # same forced flag. Whisper's own language auto-detection
            # (this CLI's actual default when --language is never given)
            # exists specifically to avoid this - trust it instead of
            # assuming every voice note is Tamil.
            cmd += ['--language', language]
        try:
            subprocess.run(
                cmd,
                check=True, capture_output=True, timeout=300,
            )
        except subprocess.CalledProcessError as e:
            logger.warning(f'whisper exited non-zero: {e.stderr.decode(errors="replace")[:500]}')
            return None
        except subprocess.TimeoutExpired:
            logger.warning('whisper timed out after 300s')
            return None

        json_path = audio_path.with_suffix('.json')
        if not json_path.exists():
            logger.warning(f'whisper produced no {json_path.name} output')
            return None
        result = json.loads(json_path.read_text())
        text = (result.get('text') or '').strip()
        if not text:
            return None
        return TranscriptionResult(text=text, language=result.get('language'))


async def transcribe_audio(
    audio_bytes: bytes, language: Optional[str] = None, model: str = DEFAULT_WHISPER_MODEL,
) -> Optional[TranscriptionResult]:
    """Bytes of a real voice note (WhatsApp's own Opus-in-OGG encoding,
    same format mesh/adiyan_reader/tts.py already produces on the way out)
    -> a TranscriptionResult (text + Whisper's own detected language), or
    None on any failure (whisper missing/erroring, timeout, empty result) -
    the caller decides what silence means for its own domain, same
    contract every other tool call in this mesh follows. Never raises: a
    transcription failure must never crash the caller's whole message-
    handling flow over what is, structurally, the same kind of optional
    enhancement rewrite_for_speech()/add_emotion_tags() already fail open
    on elsewhere in this mesh.

    language: None (the default) lets Whisper auto-detect the spoken
    language from the audio itself - the right default for a sender who
    might speak Tamil, English, or a mix of both in one note. Pass an
    explicit code (e.g. 'ta') only when the caller already knows the
    language for certain and wants to skip detection. This is the
    transcription-language HINT, not the same thing as the returned
    result's own `.language` field (Whisper's actual detection, used by
    the caller to decide what language to REPLY in - two different
    decisions this module deliberately keeps separate).

    Blocking subprocess, run off the event loop via asyncio.to_thread -
    whisper itself has no async API, and this can take real wall-clock
    time (a 'small' model on CPU, not seconds)."""
    try:
        return await asyncio.to_thread(_run_whisper, audio_bytes, language, model)
    except Exception as e:
        logger.warning(f'transcribe_audio failed: {e}')
        return None
