"""
read_now's real body - "send me the next page right now," independent of
the nightly schedule. DataPart-only, phone_number only (no reading_job_id,
no source_filename) - a real caller identity, never something an
extraction schema should invent, same "no guessing a real key from prose"
rule start_reading.py's own docstring already documents.

Deliberately thin: this just resolves phone_number to the right
reading_job_id and then calls read_next_page.run() directly (a plain
Python call, not a second A2A round trip) - the actual reading/synthesis/
bookmark-advancing/quiz-scheduling logic lives in exactly one place, not
duplicated here. current_page is a single shared bookmark regardless of
what triggered the read (see read_next_page.run()'s own db.advance_page()
call) - an on-demand read at 3pm and tonight's already-scheduled cron fire
correctly pick up from the same page, never double-send or skip one.

cron_trigger's own register_trigger replaces a prior registration for the
same job_id (id=job_id, replace_existing=True - see mesh/mcp/cron_trigger/
server.py's own docstring) - so read_next_page.run()'s end-of-run
re-registration for "tomorrow, same hour" simply resets the clock when
this fires on demand, never stacking a duplicate nightly trigger.
"""
from typing import Any, Dict

from mesh.adiyan_reader import db
from mesh.adiyan_reader.constants import AGENT_ID
from mesh.adiyan_reader.skills import read_next_page
from mesh.lib.paths import state_db_path


async def run(phone_number: str) -> Dict[str, Any]:
    conn = db.connect(state_db_path(AGENT_ID))
    jobs = db.get_active_reading_jobs_by_phone(conn, phone_number)
    if not jobs:
        return {'status': 'no_active_job', 'result_summary': "No active reading job for this number - nothing to read."}

    # Most recent if more than one - "now" has no book name to disambiguate
    # with, unlike start_reading's own explicit source_filename. Good
    # enough default for tonight; a real "which book?" clarification would
    # need Orchestrator to ask, not something this skill can do on a
    # DataPart-only call.
    job = jobs[0]
    return await read_next_page.run(job['id'])
