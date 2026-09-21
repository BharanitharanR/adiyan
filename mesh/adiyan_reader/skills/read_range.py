"""
read_range's real body - "read me a bunch of pages right now," in one burst,
as opposed to read_next_page.py's one-page-per-cron-fire pace. Covers both
requested shapes: an explicit "page 1 to 10" (start_page and end_page both
given) and a bare "read all" (both omitted, meaning "from wherever the
bookmark is, straight through to the end of the book").

Deliberately reuses read_next_page._synthesize_and_send_page() for the
actual fetch/TTS/send of each page - the exact same narration primitive the
nightly/on-demand single-page path uses - but skips
_schedule_quiz_and_next_reading() entirely for every page except the very
end of the burst: generating and scheduling a same-minute comprehension
quiz for each of, say, 10 pages read back-to-back tonight would fire 10
separate quiz messages at the same moment tomorrow morning, which nobody
asked for. Only the bookmark and the ongoing nightly schedule get updated,
once, after the burst finishes.

The bookmark only ever moves FORWARD here, never backward: replaying an
earlier range (e.g. "read page 1 to 10" again, well after current_page has
advanced past 10) must not reset the ongoing nightly reading back to page
11 - that would silently re-send pages the listener already heard. Only a
range that extends past the existing bookmark moves it, via
max(current_page, last_page_actually_sent).

Advanced ONCE PER PAGE, inside the loop, not once after the whole burst
completes - confirmed live as a real bug: a burst that failed partway
(TTS/send failure, network hiccup - realistic over many pages back to
back) never reached the old single post-loop db.advance_page() call at
all, silently discarding every page already sent that burst. A retry
after such a failure restarted from the ORIGINAL bookmark (e.g. page 10
again) despite having already voice-noted the listener through page 19.
Per-page advancement means a mid-burst failure only ever loses the one
page actually in flight when it failed, never the pages already
delivered.

Reaching a genuinely missing page (get_book_page's own found=False) is
treated exactly like read_next_page.run()'s own end-of-book case: the
job is deactivated and the same "we've finished" message is sent - whether
that missing page was hit via "read all" (the natural, expected way to
discover the book is over) or via an explicit range that happened to run
past the real end (e.g. "read page 1 to 1000" on a 200-page book).
"""
from typing import Any, Dict, List, Optional

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID
from mesh.adiyan_reader.skills import read_next_page
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.paths import state_db_path

_agent = AdiyanAgent(AGENT_ID)


async def run(phone_number: str, start_page: Optional[int] = None, end_page: Optional[int] = None) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    jobs = db.get_active_reading_jobs_by_phone(conn, phone_number)
    if not jobs:
        return {'status': 'no_active_job', 'result_summary': "No active reading job for this number - nothing to read."}

    # Most recent if more than one - same "no book name to disambiguate
    # with" default read_now.py's own docstring already documents.
    job = jobs[0]
    reading_job_id = job['id']

    chat_id = await _agent.resolve_chat_id(job['phone_number'])
    if chat_id is None:
        return {'reading_job_id': reading_job_id, 'status': 'failed', 'result_summary': f"Could not resolve WhatsApp chat for {job['phone_number']}."}

    if start_page is not None and start_page < 1:
        return {'reading_job_id': reading_job_id, 'status': 'failed', 'result_summary': 'start_page must be 1 or greater.'}
    if start_page is not None and end_page is not None and end_page < start_page:
        return {'reading_job_id': reading_job_id, 'status': 'failed', 'result_summary': 'end_page must not be before start_page.'}

    # Same lock read_next_page.run() acquires, and the same reason - a
    # burst read can take minutes end to end (many pages, each with its own
    # TTS synthesis), and a rapid-fire repeat of "read all" arriving mid-
    # burst must wait, not start a second overlapping burst racing the
    # first over the same bookmark.
    if not db.try_acquire_reading_lock(conn, reading_job_id):
        return {'reading_job_id': reading_job_id, 'status': 'busy', 'result_summary': "Still working on your last request - give it a moment."}

    try:
        page = start_page if start_page is not None else job['current_page'] + 1
        pages_sent: List[int] = []
        finished_book = False

        while end_page is None or page <= end_page:
            page_text = await read_next_page._synthesize_and_send_page(job, chat_id, page)
            if page_text is None:
                finished_book = True
                break
            pages_sent.append(page)
            # Persisted the moment this page is actually sent, not batched
            # until the whole burst finishes - see this module's own
            # docstring on why (a mid-burst failure must not discard pages
            # already delivered). Only ever moves forward, same invariant
            # the old post-loop write enforced.
            if page > job['current_page']:
                db.advance_page(conn, reading_job_id, page)
            page += 1

        if not pages_sent:
            if finished_book:
                db.deactivate_reading_job(conn, reading_job_id)
                await _agent.send_message_to(chat_id, f"We've finished reading {job['source_filename']} together - every page has been read out. 📖")
                return {'reading_job_id': reading_job_id, 'status': 'completed', 'result_summary': 'Book finished, reading job deactivated.'}
            return {'reading_job_id': reading_job_id, 'status': 'failed', 'result_summary': 'No pages were sent - check the requested range.'}

        last_page_sent = pages_sent[-1]

        if finished_book:
            db.deactivate_reading_job(conn, reading_job_id)
            await _agent.send_message_to(chat_id, f"We've finished reading {job['source_filename']} together - every page has been read out. 📖")
            return {
                'reading_job_id': reading_job_id, 'status': 'completed', 'pages_sent': pages_sent,
                'result_summary': f"Sent pages {pages_sent[0]}-{last_page_sent} of {job['source_filename']}, then the book finished.",
            }

        # Book isn't over - keep the ongoing nightly habit going from the
        # new bookmark (same "tomorrow, same hour" schedule a normal
        # single-page read would set), with no quiz generated for this
        # burst - see this module's own docstring on why.
        next_reading_at = await read_next_page._register_next_reading(reading_job_id)
        return {
            'reading_job_id': reading_job_id, 'status': 'completed', 'pages_sent': pages_sent,
            'next_reading_at': next_reading_at,
            'result_summary': f"Sent pages {pages_sent[0]}-{last_page_sent} of {job['source_filename']} as voice notes.",
        }
    finally:
        db.release_reading_lock(conn, reading_job_id)
