"""
Shared speech-to-text: Voicebox's Whisper endpoint, for any agent or plugin.

Moved here from AdiyanReader's eval (mesh/adiyan_reader/eval_quality.py) so
other agents stop importing that module's private functions. Same behaviour:
'turbo' by default (about 8x faster than 'large' with little quality loss), and
a retry loop for the one-time 202 "still downloading" a Whisper model gives on
first use. Voicebox carries a local patch (patches/voicebox-long-transcription
.patch) so recordings longer than 30 s are transcribed in full.
"""
import asyncio
import logging

import httpx

logger = logging.getLogger('STT')

VOICEBOX_URL = 'http://127.0.0.1:17493'
DEFAULT_STT_MODEL = 'turbo'
TRANSCRIBE_TIMEOUT_SECONDS = 300.0
MODEL_DOWNLOAD_POLL_SECONDS = 15.0
MODEL_DOWNLOAD_MAX_ATTEMPTS = 20  # ~5 minutes ceiling for a first-time download


async def transcribe(audio_bytes: bytes, stt_model: str = DEFAULT_STT_MODEL, voicebox_url: str = VOICEBOX_URL,
                     filename: str = 'audio.ogg', mimetype: str = 'audio/ogg') -> str:
    """Text of a recording, via Voicebox. Raises on failure; the caller decides what that means."""
    async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT_SECONDS) as client:
        for attempt in range(MODEL_DOWNLOAD_MAX_ATTEMPTS):
            response = await client.post(
                f'{voicebox_url}/transcribe',
                files={'file': (filename, audio_bytes, mimetype)},
                data={'model': stt_model},
            )
            if response.status_code == 202:
                logger.info(f'Whisper model {stt_model!r} still downloading, waiting before retry (attempt {attempt + 1})')
                await asyncio.sleep(MODEL_DOWNLOAD_POLL_SECONDS)
                continue
            response.raise_for_status()
            return response.json()['text']
    raise RuntimeError(f'Whisper model {stt_model!r} never finished downloading within the retry window.')
