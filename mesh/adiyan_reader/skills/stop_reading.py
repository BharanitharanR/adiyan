"""
stop_reading's real body - lets a customer stop AdiyanReader's nightly
narration for a book, or just its next-morning comprehension questions while
the narration itself keeps going, via a plain WhatsApp message. Same
DataPart-only, phone_number-first contract as read_now.py: book_reference,
when given, is the caller's own free-text wording, matched here against that
phone's own ACTIVE jobs only - never a filename the caller guessed, and never
Memory Agent's own fuzzy resolve_book (that one searches this person's whole
library; a book they're not currently being read isn't a valid stop target
at all).
"""
from typing import Any, Dict, List, Optional

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID
from mesh.lib.paths import state_db_path


def _title(job: Dict[str, Any]) -> str:
    return job['source_filename'].split('/', 1)[-1].rsplit('.', 1)[0].replace('_', ' ')


def _match_job(jobs: List[Dict[str, Any]], book_reference: str) -> Optional[Dict[str, Any]]:
    """Loose case-insensitive substring match against each active job's own
    title/filename - good enough to disambiguate a caller's own small
    handful of currently-active books. Not a real fuzzy search: resolve_book
    already did that proper matching once, at start_reading time, to decide
    which book this job even is."""
    needle = book_reference.strip().lower()
    for job in jobs:
        if needle in _title(job).lower() or needle in job['source_filename'].lower():
            return job
    return None


async def run(
    phone_number: str, book_reference: Optional[str] = None, questions_only: bool = False,
    all_books: bool = False,
) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    jobs = db.get_active_reading_jobs_by_phone(conn, phone_number)
    if not jobs:
        return {'status': 'no_active_job', 'result_summary': "No active reading job for this number - nothing to stop."}

    # The answer to this skill's own "more than one active book - which
    # one?" prompt when the caller says "all"/"both" - see
    # handle_message.py's own pending-clarification mechanism for how a
    # bare "for all" (no book name, nothing this skill's classify pool
    # could otherwise match) turns into this flag.
    if all_books:
        titles = [_title(j) for j in jobs]
        for job in jobs:
            if questions_only:
                db.set_questions_enabled(conn, job['id'], False)
            else:
                db.deactivate_reading_job(conn, job['id'])
        if questions_only:
            return {
                'status': 'questions_stopped_all', 'titles': titles,
                'result_summary': f"Stopped the comprehension questions for all {len(titles)} of your books - you'll still get the nightly reading.",
            }
        return {
            'status': 'stopped_all', 'titles': titles,
            'result_summary': f"Stopped all {len(titles)} of your books - no more nightly pages or questions for any of them.",
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
        # No book named, and nothing to disambiguate - same "only one
        # candidate, no need to ask" shortcut read_now.py's own docstring
        # documents for its identical phone-only lookup.
        job = jobs[0]
    else:
        return {
            'status': 'ambiguous',
            'active_books': [_title(j) for j in jobs],
            'result_summary': 'More than one active book - which one?',
        }

    title = _title(job)
    if questions_only:
        db.set_questions_enabled(conn, job['id'], False)
        return {
            'status': 'questions_stopped', 'title': title,
            'result_summary': f"Stopped the comprehension questions for {title} - you'll still get the nightly reading.",
        }

    db.deactivate_reading_job(conn, job['id'])
    return {
        'status': 'stopped', 'title': title,
        'result_summary': f"Stopped reading {title} - no more nightly pages or questions for it.",
    }
