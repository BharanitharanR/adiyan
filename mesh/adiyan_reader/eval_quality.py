"""
Post-delivery audio-quality eval for AdiyanReader - now that ABJ has a real,
paying customer, every page's actual voice-note gets checked, not just
trusted. The loop: take the OGG/Opus audio that was just sent, transcribe it
back to text via Voicebox's own Whisper endpoint (already running natively
as this mesh's own 'voicebox' component - see mesh/start_all.sh's own entry
- no second STT dependency needed), then ask a small LLM whether that
transcript still means the same thing as the text actually handed to TTS.
A percentage below the configured threshold doesn't block delivery - the
customer already has their page by the time this runs - it's recorded for
someone to notice a real quality regression, not a live gate.

Deliberately fire-and-forget from the caller's side (read_next_page.py's
_synthesize_and_send_page): a task reference is kept in _PENDING_EVALS
purely so asyncio doesn't garbage-collect it mid-flight (a bare
asyncio.create_task() call with no other reference is a well-known real
footgun - the task can vanish before it finishes), not because anything
ever awaits it. Every failure mode (Voicebox unreachable, transcription
error, LLM error) is caught and logged, never raised - an eval failing to
run is a monitoring gap, not a reason a page failed to read.
"""
import logging
from typing import Any, Dict, Optional, Set

import httpx
from pydantic import BaseModel, Field

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID
from mesh.adiyan_reader.tts import VOICEBOX_URL
from mesh.lib import config_sdk
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.errors import describe_exception
from mesh.lib.paths import state_db_path

logger = logging.getLogger('AdiyanReaderEval')

_agent = AdiyanAgent(AGENT_ID)

# Kept alive purely so a fire-and-forget asyncio.Task isn't garbage
# collected before it finishes - see this module's own top docstring.
_PENDING_EVALS: Set[Any] = set()

# Speech-to-text now lives in mesh/lib/stt.py, shared with other agents and
# plugins; these names stay so this module reads (and behaves) as before.
from mesh.lib.stt import DEFAULT_STT_MODEL as _DEFAULT_STT_MODEL  # noqa: E402


async def _transcribe(audio_bytes: bytes, stt_model: str, voicebox_url: str) -> str:
    from mesh.lib.stt import transcribe
    return await transcribe(audio_bytes, stt_model, voicebox_url, filename='page.ogg', mimetype='audio/ogg')


class _MatchScore(BaseModel):
    match_percentage: int = Field(
        ge=0, le=100,
        description=(
            "0-100: how well the transcribed audio's MEANING matches the original text's "
            "meaning - not word-for-word identity. Minor rewording, filler words a real "
            "transcription introduces, or small STT mistakes on proper nouns shouldn't tank "
            "the score if the actual content and sense of the passage came through intact. "
            "Score low only when real content is missing, wrong, or garbled beyond recognition."
        ),
    )


async def _score_match(original_text: str, transcript: str, cfg: Dict[str, Any]) -> int:
    prompt = (
        "Original text (what the narrator was supposed to say):\n"
        f'"""{original_text}"""\n\n'
        "Transcribed audio (what a speech-to-text model heard the narrator actually say):\n"
        f'"""{transcript}"""\n\n'
        "Score how well the transcribed audio's meaning matches the original text's meaning, "
        "0-100."
    )
    # think=False - explicit, not the model's own default: this is a single
    # bounded scoring judgment, not a task that benefits from a visible
    # reasoning trace, and skipping it keeps every eval call fast enough to
    # run on every single page without becoming its own bottleneck.
    result = await _agent.ask(
        prompt, stage='eval_audio_quality', model=cfg['model'], temperature=cfg.get('temperature', 0.1),
        schema=_MatchScore, think=False,
    )
    return result.match_percentage


async def evaluate(
    original_text: str, audio_bytes: bytes, reading_job_id: str, source_filename: str, page_number: int,
) -> None:
    """The real body, run as a detached task - see this module's own
    docstring. Never raises; every outcome (including "couldn't run the
    eval at all") is either recorded via db.record_eval_result or logged
    and dropped, so a broken eval pipeline can never surface as a customer-
    facing failure."""
    try:
        if not await config_sdk.get_constant(
            AGENT_ID, 'eval_enabled', True,
            description='Whether every page\'s generated audio gets a post-delivery quality eval (transcribe-back-and-compare). On by default now that ABJ has real customers.',
        ):
            return

        voicebox_url = await config_sdk.get_constant(AGENT_ID, 'voicebox_url', VOICEBOX_URL)
        stt_model = await config_sdk.get_constant(
            AGENT_ID, 'eval_stt_model', _DEFAULT_STT_MODEL,
            description='Voicebox Whisper model used to transcribe generated audio back to text for the quality eval.',
        )
        threshold = await config_sdk.get_constant(
            AGENT_ID, 'eval_threshold_percent', 85,
            description='Semantic match percentage (audio transcript vs. the text it was supposed to speak) at or above which a page\'s audio is considered good quality.',
        )
        eval_cfg = await config_sdk.get_stage_config(
            AGENT_ID, 'eval_audio_quality', {'model': 'gemma4:e2b', 'temperature': 0.1, 'base_url': None},
            description='Model that judges semantic match between a page\'s original text and its transcribed-back audio, for the post-delivery quality eval.',
        )

        transcript = await _transcribe(audio_bytes, stt_model, voicebox_url)
        match_percentage = await _score_match(original_text, transcript, eval_cfg)
        passed = match_percentage >= threshold

        if not passed:
            logger.warning(
                f'Audio quality eval below threshold for {source_filename!r} page {page_number}: '
                f'{match_percentage}% < {threshold}%'
            )

        conn = db.connect(state_db_path(AGENT_ID))
        db.record_eval_result(
            conn, reading_job_id=reading_job_id, source_filename=source_filename, page_number=page_number,
            match_percentage=match_percentage, threshold=threshold, passed=passed, transcript=transcript,
        )
    except Exception as e:
        logger.warning(f'Audio quality eval failed for {source_filename!r} page {page_number}: {describe_exception(e)}')


def schedule(
    original_text: str, audio_bytes: bytes, reading_job_id: str, source_filename: str, page_number: int,
) -> None:
    """Fire-and-forget entry point for callers (read_next_page.py) -
    creates the task, stashes the reference in _PENDING_EVALS, and detaches
    a done-callback that removes it again so the set doesn't grow forever
    across a long-running reading agent's lifetime."""
    import asyncio
    task = asyncio.create_task(evaluate(original_text, audio_bytes, reading_job_id, source_filename, page_number))
    _PENDING_EVALS.add(task)
    task.add_done_callback(_PENDING_EVALS.discard)
