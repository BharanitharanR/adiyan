"""
Shared job resolution - by job_id if the caller already knows it, or by
name_or_phrase via the same embedding infrastructure schedule_job.py uses
for dedup, here used for lookup instead. Extracted out of run_routine.py so
delete_job doesn't duplicate the exact same logic a second time.
"""
from typing import Any, Dict, Optional

from pymongo.collection import Collection

from mesh.scheduler import db
from mesh.scheduler.skills.schedule_job import _embed


class JobNotFoundError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)


async def resolve_job(
    conn: Collection,
    job_id: Optional[str],
    name_or_phrase: Optional[str],
    requester_chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    if job_id:
        # An exact id is already fully determined (cron_trigger's own fire
        # call, or a caller re-using an id it was handed earlier) - no
        # requester scoping needed, same as before this field existed.
        job = db.get_job(conn, job_id)
        if job is None:
            raise JobNotFoundError(f'No job with id {job_id}')
        return job
    embedding = await _embed(name_or_phrase)
    # requester_chat_id scopes name/phrase lookup to the caller's own jobs -
    # see db.find_job_by_name's own docstring for why a customer's "run my
    # morning routine" must never resolve to someone else's job of the
    # same name.
    job = db.find_job_by_name(conn, embedding, requester_chat_id=requester_chat_id)
    if job is None:
        raise JobNotFoundError(f"No routine matches '{name_or_phrase}'")
    return job
