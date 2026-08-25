"""
dispatch_questions's real body - fired the next morning by the one-shot
cron_trigger read_next_page.py registered specifically for one
(reading_job_id, page_number) pair. Sends whatever comprehension questions
were preloaded for that exact page, not a generic "whatever's due" sweep -
each night's reading gets its own dedicated dispatch trigger, so there's
never ambiguity about which page's questions this fire is for.
"""
from typing import Any, Dict

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID, OPENWA_SESSION_NAME, OPENWA_URL
from mesh.lib.paths import state_db_path
from mesh.lib.utilities.whatsapp.openwa_service import OpenWAService


async def run(reading_job_id: str, page_number: int) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    job = db.get_reading_job(conn, reading_job_id)
    questions = db.get_pending_questions(conn, reading_job_id, page_number)
    if job is None or not questions:
        return {'reading_job_id': reading_job_id, 'status': 'nothing_to_send', 'result_summary': 'No pending questions for this page.'}

    lines = [f"Quick check on last night's reading (page {page_number}):", '']
    lines += [f'{i + 1}. {q["question_text"]}' for i, q in enumerate(questions)]
    text = '\n'.join(lines)

    openwa = OpenWAService(base_url=OPENWA_URL, api_key='', session_name=OPENWA_SESSION_NAME)
    chat_id = await openwa.resolve_chat_id(job['phone_number'])
    if chat_id is None:
        return {'reading_job_id': reading_job_id, 'status': 'failed', 'result_summary': f"Could not resolve WhatsApp chat for {job['phone_number']}."}

    await openwa.send_message(chat_id, text)
    db.mark_questions_sent(conn, reading_job_id, page_number)

    return {
        'reading_job_id': reading_job_id, 'status': 'completed',
        'questions_sent': len(questions), 'result_summary': f'Sent {len(questions)} comprehension questions for page {page_number}.',
    }
