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

# 'small' - a deliberate middle ground, not the CLI's own default (which
# tries every model in turn). Tamil and the other lower-resource Indic
# languages need more capacity than 'tiny'/'base' give reliably, but
# 'medium'/'large' cost real extra minutes per voice note on CPU-only
# hardware with no confirmed benefit measured yet for this specific use
# case. Revisit with real transcription-quality testing before assuming
# this is the right trade-off long-term.
DEFAULT_WHISPER_MODEL = 'small'


def _run_whisper(audio_bytes: bytes, language: str, model: str) -> Optional[str]:
    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = Path(tmpdir) / 'voice_note.ogg'
        audio_path.write_bytes(audio_bytes)
        try:
            subprocess.run(
                [
                    'whisper', str(audio_path),
                    '--language', language, '--model', model,
                    '--output_format', 'txt', '--output_dir', tmpdir,
                    '--verbose', 'False',
                ],
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
    audio_bytes: bytes, language: str = 'ta', model: str = DEFAULT_WHISPER_MODEL,
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

    Blocking subprocess, run off the event loop via asyncio.to_thread -
    whisper itself has no async API, and this can take real wall-clock
    time (a 'small' model on CPU, not seconds)."""
    try:
        return await asyncio.to_thread(_run_whisper, audio_bytes, language, model)
    except Exception as e:
        logger.warning(f'transcribe_audio failed: {e}')
        return None
