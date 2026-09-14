"""
Local speech-to-text for incoming WhatsApp voice notes, via OpenAI's own
`whisper` CLI (Homebrew: `openai-whisper`) - already installed on this
machine, confirmed live to explicitly support Tamil ('ta') among its
language options. No cloud call, no API key: this is a subprocess, the
same "shell out to a local binary" shape mesh/adiyan_reader/tts.py already
uses for ffmpeg.

Deliberately NOT sarvam-1 or any Ollama-served model - sarvam-1 is a text
model with no audio input at all (confirmed against its own Ollama model
card: "8K context window - Text"). Transcription and Tamil-language
response generation are two separate stages of this pipeline; this module
is only the first one.

Whisper's own CLI, not the `openai-whisper` Python package imported
directly - confirmed live this session that only the Homebrew binary is
actually installed (`pip show openai-whisper` finds nothing in this
venv), so shelling out to the CLI already on PATH is what's actually
available, not a guess.
"""
import asyncio
import logging
import subprocess
import tempfile
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


def _run_whisper(audio_bytes: bytes, language: Optional[str], model: str) -> Optional[str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = Path(tmpdir) / 'voice_note.ogg'
        audio_path.write_bytes(audio_bytes)
        cmd = [
            'whisper', str(audio_path), '--model', model,
            '--output_format', 'txt', '--output_dir', tmpdir,
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

        txt_path = audio_path.with_suffix('.txt')
        if not txt_path.exists():
            logger.warning(f'whisper produced no {txt_path.name} output')
            return None
        return txt_path.read_text().strip()


async def transcribe_audio(
    audio_bytes: bytes, language: Optional[str] = None, model: str = DEFAULT_WHISPER_MODEL,
) -> Optional[str]:
    """Bytes of a real voice note (WhatsApp's own Opus-in-OGG encoding,
    same format mesh/adiyan_reader/tts.py already produces on the way out)
    -> transcribed text, or None on any failure (whisper missing/erroring,
    timeout, empty result) - the caller decides what silence means for its
    own domain, same contract every other tool call in this mesh follows.
    Never raises: a transcription failure must never crash the caller's
    whole message-handling flow over what is, structurally, the same kind
    of optional enhancement rewrite_for_speech()/add_emotion_tags() already
    fail open on elsewhere in this mesh.

    language: None (the default) lets Whisper auto-detect the spoken
    language from the audio itself - the right default for a sender who
    might speak Tamil, English, or a mix of both in one note. Pass an
    explicit code (e.g. 'ta') only when the caller already knows the
    language for certain and wants to skip detection.

    Blocking subprocess, run off the event loop via asyncio.to_thread -
    whisper itself has no async API, and this can take real wall-clock
    time (a 'small' model on CPU, not seconds)."""
    try:
        return await asyncio.to_thread(_run_whisper, audio_bytes, language, model)
    except Exception as e:
        logger.warning(f'transcribe_audio failed: {e}')
        return None
