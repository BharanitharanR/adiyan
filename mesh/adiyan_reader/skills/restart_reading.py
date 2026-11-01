"""
restart_reading's real body - lets a customer restart a book they're
currently being read from page 1, via a plain WhatsApp message like "start
all over again". Confirmed live as a real gap: without this skill, "start
all over again" had nothing to classify against except start_book_reading,
which took the whole phrase as a literal book title, searched for it on
Gutenberg, and told the customer it couldn't find a book called "start all
over again" - a real subscriber hit exactly this.

Same DataPart-only, phone_number-first contract as stop_reading.py:
book_reference, when given, is matched against that phone's own ACTIVE jobs
only, never guessed or resolved against the whole library - restarting a
book they're not currently reading isn't a valid target at all.
"""
from typing import Any, Dict, List, Optional

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID
from mesh.adiyan_reader.skills import read_next_page
from mesh.lib import config_sdk
from mesh.lib.paths import state_db_path


def _title(job: Dict[str, Any]) -> str:
    return job['source_filename'].split('/', 1)[-1].rsplit('.', 1)[0].replace('_', ' ')


def _match_job(jobs: List[Dict[str, Any]], book_reference: str) -> Optional[Dict[str, Any]]:
    """Same loose substring match stop_reading.py's own _match_job uses -
    resolve_book already did the real fuzzy matching once, at start_reading
    time, to decide which book this job even is."""
    needle = book_reference.strip().lower()
    for job in jobs:
        if needle in _title(job).lower() or needle in job['source_filename'].lower():
            return job
    return None


async def _restart_one(conn, job: Dict[str, Any], phone_number: str, is_owner: bool, remaining_budget: Optional[int]) -> Dict[str, Any]:
    """Reset + immediate-read for exactly one job - pulled out of run() so
    all_books=True can call it once per active job without duplicating the
    reset/best-effort-read logic. The RESET always happens, unconditionally
    - it's a state change, not a synthesis, so it never competes with the
    daily page budget. Only the immediate page-1 read is gated: with no
    budget left, this still resets the bookmark and reports "you'll get it
    fresh tonight" (the same fallback wording already used when the
    immediate read fails for any other reason), rather than refusing the
    whole restart outright."""
    title = _title(job)
    db.reset_reading_job_page(conn, job['id'])
    if not is_owner and remaining_budget is not None and remaining_budget <= 0:
        return {'title': title, 'page_sent': None, 'budget_hit': True}

    try:
        first_page = await read_next_page.run(job['id'])
    except Exception:
        first_page = {}
    if first_page.get('status') == 'completed' and 'page_sent' in first_page:
        if not is_owner:
            db.increment_daily_usage(conn, phone_number, 'pages')
        return {'title': title, 'page_sent': first_page['page_sent'], 'budget_hit': False}
    return {'title': title, 'page_sent': None, 'budget_hit': False}


async def run(
    phone_number: str, book_reference: Optional[str] = None, all_books: bool = False, is_owner: bool = False,
) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    jobs = db.get_active_reading_jobs_by_phone(conn, phone_number)
    if not jobs:
        return {'status': 'no_active_job', 'result_summary': "No active reading job for this number - nothing to restart."}

    # is_owner comes from agent_executor.py's own token check, never from a
    # customer-suppliable param. Read fresh once, before any resets, so
    # every book in an all_books=True restart competes for the SAME budget
    # snapshot rather than each _restart_one() call re-reading (and
    # decrementing against) a moving target.
    remaining_budget: Optional[int] = None
    if not is_owner:
        limit = await config_sdk.get_constant(
            AGENT_ID, 'daily_on_demand_page_limit', 10,
            description='Max on-demand pages (read now / next N / read all / restart / reread) a customer can pull per day (IST). Nightly reading is uncapped. The owner is exempt.',
        )
        remaining_budget = limit - db.get_daily_usage(conn, phone_number)['pages']

    # The answer to this skill's own "more than one active book - which
    # one?" prompt when the caller says "all"/"both" - see
    # handle_message.py's own pending-clarification mechanism for how a
    # bare "for all" turns into this flag.
    if all_books:
        results = []
        for job in jobs:
            r = await _restart_one(conn, job, phone_number, is_owner, remaining_budget)
            if remaining_budget is not None:
                remaining_budget -= 1 if r['page_sent'] is not None else 0
            results.append(r)
        titles = [r['title'] for r in results]
        any_capped = any(r['budget_hit'] for r in results)
        summary = f"Starting all {len(titles)} of your books over from page 1."
        if any_capped:
            summary = f"Reset all {len(titles)} of your books to page 1 - out of pages for today, so some will start fresh tonight instead of right now."
        return {'status': 'restarted_all', 'titles': titles, 'result_summary': summary}

    if book_reference:
        job = _match_job(jobs, book_reference)
        if job is None:
            return {
                'status': 'not_found',
                'active_books': [_title(j) for j in jobs],
                'result_summary': f"\"{book_reference}\" isn't among your active books.",
            }
    elif len(jobs) == 1:
        # No book named, nothing to disambiguate - same shortcut
        # stop_reading.py/read_now.py already use for their own identical
        # phone-only lookups.
        job = jobs[0]
    else:
        return {
            'status': 'ambiguous',
            'active_books': [_title(j) for j in jobs],
            'result_summary': 'More than one active book - which one should I restart?',
        }

    # Restarting reads as wanting page 1 right now, not waiting for
    # tonight's schedule - same "an explicit ask reads as wanting it
    # immediately" reasoning _start_book_reading()'s own docstring
    # documents for a fresh book start. Best-effort, actually caught inside
    # _restart_one() (confirmed live: an unresolvable chat_id raises
    # straight out of read_next_page.run(), which would otherwise blow past
    # this function's own "reset already succeeded" fallback instead of
    # reaching it) - if the immediate read fails for any reason, the
    # bookmark reset itself already succeeded, so the nightly schedule
    # still correctly picks up from page 1.
    result = await _restart_one(conn, job, phone_number, is_owner, remaining_budget)
    if result['page_sent'] is not None:
        return {
            'status': 'restarted', 'title': result['title'], 'page_sent': result['page_sent'],
            'result_summary': f"Starting {result['title']} over from page 1.",
        }
    return {
        'status': 'restarted', 'title': result['title'],
        'result_summary': f"Reset {result['title']} back to page 1 - you'll get it fresh tonight.",
    }
