"""
Shared job resolution - by job_id if the caller already knows it, or by
name_or_phrase via the same embedding infrastructure schedule_job.py uses
for dedup, here used for lookup instead. Extracted out of run_routine.py so
delete_job doesn't duplicate the exact same logic a second time.
"""
import sqlite3
from typing import Any, Dict, Optional

from mesh.scheduler import db
from mesh.scheduler.skills.schedule_job import _embed


class JobNotFoundError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)


async def resolve_job(
    conn: sqlite3.Connection,
    job_id: Optional[str],
    name_or_phrase: Optional[str],
) -> Dict[str, Any]:
    if job_id:
        job = db.get_job(conn, job_id)
        if job is None:
            raise JobNotFoundError(f'No job with id {job_id}')
        return job
    embedding = await _embed(name_or_phrase)
    job = db.find_job_by_name(conn, embedding)
    if job is None:
        raise JobNotFoundError(f"No routine matches '{name_or_phrase}'")
    return job
