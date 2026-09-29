"""
reread_page's real body - lets a customer hear their CURRENT page again
("reread that", "with better clarity", "read it slower"), not the next one.
Same DataPart-only, phone_number-first contract as stop_reading.py/
restart_reading.py: book_reference, when given, is matched against that
phone's own ACTIVE jobs only.

clearer is a one-off nudge (Orpheus's own temperature/repetition_penalty
turned down for this single re-synthesis, see read_next_page.py's own
_synthesize_and_send_page() docstring); slower is a persistent per-job
speech_rate change (db.set_reading_job_speech_rate) - every future page,
not just this reread, keeps the slower pace until changed back. The two
can be combined in one request ("read that again, slower and clearer").
"""
from typing import Any, Dict, List, Optional

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID
from mesh.adiyan_reader.skills import read_next_page
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.paths import state_db_path

_agent = AdiyanAgent(AGENT_ID)

# Confirmed-comfortable "clearly slower without sounding broken" rate for
# both Orpheus and Chatterbox Turbo output - a fixed step, not a caller-
# supplied number, since "read it slower" doesn't come with a percentage
# attached and this mesh's own "never invent a value the caller didn't
# give" rule applies here too.
_SLOWER_RATE = 0.85
_NORMAL_RATE = 1.0


def _title(job: Dict[str, Any]) -> str:
    return job['source_filename'].split('/', 1)[-1].rsplit('.', 1)[0].replace('_', ' ')


def _match_job(jobs: List[Dict[str, Any]], book_reference: str) -> Optional[Dict[str, Any]]:
    needle = book_reference.strip().lower()
    for job in jobs:
        if needle in _title(job).lower() or needle in job['source_filename'].lower():
            return job
    return None


async def _reread_one(conn, job: Dict[str, Any], clearer: bool, slower: bool) -> Dict[str, Any]:
    """Reread of exactly one job's current page - pulled out of run() so
    all_books=True can call it once per active job without duplicating the
    slower-rate/resolve/resend logic."""
    title = _title(job)
    if job['current_page'] < 1:
        return {'title': title, 'ok': False, 'reason': 'nothing_read_yet'}

    if slower:
        db.set_reading_job_speech_rate(conn, job['id'], _SLOWER_RATE)
        job = db.get_reading_job(conn, job['id'])

    chat_id = await _agent.resolve_chat_id(job['phone_number'])
    if chat_id is None:
        return {'title': title, 'ok': False, 'reason': 'no_chat'}

    # job['current_page'], not +1 - this rereads what was already
    # delivered, the bookmark never moves.
    page_text = await read_next_page._synthesize_and_send_page(job, chat_id, job['current_page'], clearer=clearer)
    if page_text is None:
        # Confirmed possible, not just theoretical: the page that was
        # already read successfully once could still be affected by the
        # same kind of isolated ingestion gap _find_readable_page() guards
        # against elsewhere - vanishingly rare for a page that already
        # delivered fine the first time, but not impossible.
        return {'title': title, 'ok': False, 'reason': 'resend_failed'}

    return {'title': title, 'ok': True, 'page': job['current_page']}


async def run(
    phone_number: str, book_reference: Optional[str] = None,
    clearer: bool = False, slower: bool = False, all_books: bool = False,
) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    jobs = db.get_active_reading_jobs_by_phone(conn, phone_number)
    if not jobs:
        return {'status': 'no_active_job', 'result_summary': "No active reading job for this number - nothing to reread."}

    # The answer to this skill's own "more than one active book - which
    # one?" prompt when the caller says "all"/"both" - see
    # handle_message.py's own pending-clarification mechanism for how a
    # bare "for all" turns into this flag.
    if all_books:
        results = [await _reread_one(conn, job, clearer, slower) for job in jobs]
        succeeded = [r['title'] for r in results if r['ok']]
        if not succeeded:
            return {'status': 'failed', 'result_summary': "Couldn't reread any of your books right now."}
        pace_note = ' at a slower pace' if slower else ''
        return {
            'status': 'reread_all', 'titles': succeeded,
            'result_summary': f"Sent the current page of all {len(succeeded)} of your books again{pace_note}.",
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
            'result_summary': 'More than one active book - which one should I reread?',
        }

    result = await _reread_one(conn, job, clearer, slower)
    if not result['ok']:
        if result['reason'] == 'nothing_read_yet':
            return {'status': 'nothing_read_yet', 'title': result['title'], 'result_summary': f"Haven't read any of {result['title']} yet - nothing to reread."}
        if result['reason'] == 'no_chat':
            return {'status': 'failed', 'result_summary': f"Could not resolve WhatsApp chat for {phone_number}."}
        return {'status': 'failed', 'title': result['title'], 'result_summary': f"Couldn't reread that page of {result['title']} right now."}

    pace_note = ' at a slower pace' if slower else ''
    return {
        'reading_job_id': job['id'], 'status': 'reread', 'title': result['title'], 'page': result['page'],
        'result_summary': f"Sent page {result['page']} of {result['title']} again{pace_note}.",
    }
