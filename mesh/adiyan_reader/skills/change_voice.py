"""
change_voice's real body - sets or clears one reading job's explicit voice
override. Phone_number-first, same disambiguation contract as
stop_reading.py/restart_reading.py/reread_page.py's own DataPart-only
lookups - book_reference, when given, is matched against that phone's own
ACTIVE jobs only, never guessed. voice itself is checked against
available_voices, never trusted blind.

Reworked from a raw reading_job_id parameter (confirmed live: that shape had
never actually been reachable from any real conversation - a caller would
have had to already know a real reading_job_id, which no orchestrator-side
classify/extract step could ever produce, so this skill was dead code from a
customer's perspective) to phone_number + optional book_reference, the same
pattern every other customer-facing adiyan_reader skill already uses.
"""
from typing import Any, Dict, List, Optional

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID
from mesh.adiyan_reader.tts import VOICES
from mesh.lib import config_sdk
from mesh.lib.paths import state_db_path


def _title(job: Dict[str, Any]) -> str:
    return job['source_filename'].split('/', 1)[-1].rsplit('.', 1)[0].replace('_', ' ')


def _match_job(jobs: List[Dict[str, Any]], book_reference: str) -> Optional[Dict[str, Any]]:
    needle = book_reference.strip().lower()
    for job in jobs:
        if needle in _title(job).lower() or needle in job['source_filename'].lower():
            return job
    return None


async def _resolve_voice(voice: str) -> Any:
    """(normalized_voice, error_result_or_None). Pulled out of run() so
    all_books=True can validate the voice exactly once, before touching any
    job, rather than re-validating (and risking a different answer from
    config_sdk's own TTL cache) once per book in the loop."""
    normalized_voice = voice.strip().lower()
    if normalized_voice == '':
        return '', None
    available_voices = await config_sdk.get_constant(
        AGENT_ID, 'available_voices', list(VOICES),
        description='The Orpheus voice names this deployment allows readers to pick from.',
    )
    lower_available = [v.lower() for v in available_voices]
    if normalized_voice not in lower_available:
        return None, {
            'status': 'invalid_voice', 'available_voices': available_voices,
            'result_summary': f"\"{voice}\" isn't one of the available voices - you've got: {', '.join(available_voices)}.",
        }
    return lower_available[lower_available.index(normalized_voice)], None


async def run(
    phone_number: str, voice: str, book_reference: Optional[str] = None, all_books: bool = False,
) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    jobs = db.get_active_reading_jobs_by_phone(conn, phone_number)
    if not jobs:
        return {'status': 'no_active_job', 'result_summary': "No active reading job for this number - nothing to change the voice on."}

    # all_books: the answer to this skill's own "more than one active book -
    # which one?" prompt when the caller says "all"/"both"/"all of them" -
    # confirmed live as a real gap: that exact reply had nowhere to go
    # before (no voice name in it for THIS skill's own classify to latch
    # onto), so orchestrator's own pending-clarification handling
    # (handle_message.py) is what actually turns "for all" into this flag -
    # see that module's own docstring on the mechanism.
    if all_books:
        normalized_voice, error = await _resolve_voice(voice)
        if error is not None:
            return error
        for job in jobs:
            db.set_reading_job_voice(conn, job['id'], normalized_voice)
        titles = [_title(j) for j in jobs]
        display_voice = normalized_voice.capitalize() if normalized_voice else 'the default'
        return {
            'status': 'changed_all', 'titles': titles, 'voice': normalized_voice or 'default',
            'result_summary': f"Switched all {len(titles)} of your books to {display_voice} voice.",
        }

    if book_reference:
        job = _match_job(jobs, book_reference)
        if job is None:
            return {
                'status': 'not_found',
                'active_books': [_title(j) for j in jobs],
                'result_summary': f"\"{book_reference}\" isn't among your active books.",
            }
    elif len(jobs) == 1:
        job = jobs[0]
    else:
        return {
            'status': 'ambiguous',
            'active_books': [_title(j) for j in jobs],
            'result_summary': 'More than one active book - which one should I change the voice for?',
        }

    normalized_voice, error = await _resolve_voice(voice)
    if error is not None:
        return error

    db.set_reading_job_voice(conn, job['id'], normalized_voice)
    title = _title(job)
    display_voice = normalized_voice.capitalize() if normalized_voice else 'the default'
    return {
        'status': 'changed', 'title': title, 'voice': normalized_voice or 'default',
        'result_summary': f"Switched {title} to {display_voice} voice - you'll hear it from the next page on.",
    }
