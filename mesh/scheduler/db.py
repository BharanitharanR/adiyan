"""
Scheduler Agent's own domain data - jobs, keyed by id, with the embedding
used for dedup matching stored alongside each document.

Storage: MongoDB (collection `scheduler_jobs` in the `adiyan` database),
not the old SQLite state.db. Ported 2026-09-10 - see the runaway '* * * * *'
incident; also lets an operator inspect/repair jobs with the same Mongo
tooling the rest of the deployment already uses. The A2A task lifecycle
store (tasks.db) is a separate concern and stays on SQLite via the a2a SDK.

Function signatures still take `conn` as the first argument - it is the
Mongo collection handle returned by connect(), passed through unchanged so
existing call sites (`conn = db.connect(...)`, `db.create_job(conn, ...)`)
did not all have to change in the port. The functions remain synchronous
(pymongo's sync client): the jobs collection is tiny - a handful of
recurring routines - so a blocking call from an async handler costs
nothing measurable, same as the old sqlite3 calls did.

Dedup approach: cosine similarity over embeddings (0.72 floor, carried over
from the old codebase's services/routine_store.py, still worth re-tuning
from real usage) AND an exact resolved_schedule match - see
find_similar_job()'s own docstring for why schedule has to be part of the
check too, not just description similarity.
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np
from pymongo import MongoClient
from pymongo.collection import Collection

SIMILARITY_FLOOR = 0.72

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB_DATA', 'adiyan')
COLLECTION_NAME = 'scheduler_jobs'

_client: Optional[MongoClient] = None


def connect(_state_db_path: Any = None) -> Collection:
    """Returns the `scheduler_jobs` collection. The argument is ignored -
    kept so existing call sites that pass state_db_path(AGENT_ID) don't all
    have to change in the sqlite->mongo move. The client is created once
    and reused."""
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    return _client[MONGO_DB_NAME][COLLECTION_NAME]


def _doc_to_job(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Mongo `_id` -> `id`, so callers keep reading `job['id']` exactly as
    they did with the sqlite row. `expects_response` comes back as a real
    bool (sqlite stored 0/1); everything else is stored in its final shape."""
    if doc is None:
        return None
    job = dict(doc)
    job['id'] = job.pop('_id')
    job['expects_response'] = bool(job.get('expects_response', False))
    return job


def _cosine(a: List[float], b: List[float]) -> float:
    a_arr, b_arr = np.array(a), np.array(b)
    denom = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    return float(np.dot(a_arr, b_arr) / denom) if denom else 0.0


def find_similar_job(
    conn: Collection, embedding: List[float], resolved_schedule: str,
    requester_chat_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Returns the closest existing job at/above SIMILARITY_FLOOR that also
    runs on the exact same schedule, or None.

    Confirmed live: name/description similarity alone isn't enough to call
    two jobs "the same" - "log my journal every day at 8am" scored 0.7455
    against an existing "Daily Progress Log" job at 6pm (both mention
    logging something daily), crossed the old bare floor, and got returned
    as if it were that job - silently not creating the new one, at the
    wrong time entirely. The embedding never encoded *when* a job runs,
    only what it's about, so schedule was never actually being checked.
    Requiring an exact resolved_schedule match alongside the similarity
    floor is what actually captures "this is a repeat of an existing job,"
    not just "these two descriptions are topically similar." The Mongo
    query is the cheap early filter on schedule; the cosine comparison only
    runs over that already-narrowed set.

    requester_chat_id scopes the candidate set to the SAME person's own
    jobs - without this, two different Verticals customers asking for the
    exact same phrased, exact same timed reminder would dedupe onto a
    SINGLE job, and only the first one to ask would ever actually get
    notified. None matches the legacy owner-only jobs that predate this
    field (never mixed with a real customer's own jobs)."""
    best_doc, best_score = None, 0.0
    for doc in conn.find({'resolved_schedule': resolved_schedule, 'requester_chat_id': requester_chat_id}):
        score = _cosine(embedding, doc['embedding'])
        if score > best_score:
            best_doc, best_score = doc, score
    if best_doc is not None and best_score >= SIMILARITY_FLOOR:
        return _doc_to_job(best_doc)
    return None


def find_job_by_name(
    conn: Collection, embedding: List[float], requester_chat_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Returns the closest existing job at/above SIMILARITY_FLOOR, searched
    across ALL jobs regardless of schedule.

    This is for job_lookup.resolve_job()'s name_or_phrase path (delete_job,
    run_routine) - a caller looking a job up by name doesn't know its
    schedule in advance, so there's nothing to pre-filter on. That's the
    opposite situation from find_similar_job()'s dedup check, which already
    has a target resolved_schedule in hand and uses it to narrow the
    candidate set before comparing embeddings.

    requester_chat_id: same scoping reasoning as find_similar_job - "run my
    morning routine" must only ever be able to match a job THIS person
    created, never another customer's (or the owner's) similarly-named one."""
    best_doc, best_score = None, 0.0
    for doc in conn.find({'requester_chat_id': requester_chat_id}):
        score = _cosine(embedding, doc['embedding'])
        if score > best_score:
            best_doc, best_score = doc, score
    if best_doc is not None and best_score >= SIMILARITY_FLOOR:
        return _doc_to_job(best_doc)
    return None


def create_job(
    conn: Collection,
    name: str,
    description: str,
    target: str,
    resolved_schedule: str,
    next_run_at: str,
    embedding: List[float],
    expects_response: bool = False,
    response_window_minutes: Optional[int] = None,
    vertical_id: Optional[str] = None,
    requester_chat_id: Optional[str] = None,
) -> Dict[str, Any]:
    # vertical_id/requester_chat_id default to None - a job created before
    # Verticals customers could reach Scheduler at all (the owner's own
    # habit/reflection routines) - run_routine.py reads a None vertical_id
    # as "deliver via notify_owner, exactly as it always has."
    # requester_chat_id is who the job actually belongs to for dedup/lookup
    # scoping (see find_similar_job/find_job_by_name) - for an owner job
    # this is the owner's own chat_id, not None, so two DIFFERENT people
    # never collide even when vertical_id happens to match (or is None for
    # both).
    job_id = str(uuid.uuid4())
    conn.insert_one({
        '_id': job_id,
        'name': name,
        'description': description,
        'target': target,
        'resolved_schedule': resolved_schedule,
        'next_run_at': next_run_at,
        'expects_response': bool(expects_response),
        'response_window_minutes': response_window_minutes,
        'embedding': list(embedding),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'vertical_id': vertical_id,
        'requester_chat_id': requester_chat_id,
    })
    return get_job(conn, job_id)


def get_job(conn: Collection, job_id: str) -> Optional[Dict[str, Any]]:
    return _doc_to_job(conn.find_one({'_id': job_id}))


def list_all(conn: Collection) -> List[Dict[str, Any]]:
    """Every job, id-normalized. list_jobs.py used to read this with a raw
    `SELECT * FROM jobs`; that lives here now that the store is Mongo."""
    return [_doc_to_job(doc) for doc in conn.find({})]


def update_next_run(conn: Collection, job_id: str, next_run_at: str) -> None:
    conn.update_one({'_id': job_id}, {'$set': {'next_run_at': next_run_at}})


def delete_job(conn: Collection, job_id: str) -> None:
    """Only removes this agent's own domain document. The matching
    cron_trigger registration is a separate store entirely - callers must
    also cancel that themselves (see mesh/scheduler/skills/delete_job.py),
    or the job keeps firing against a document that no longer exists."""
    conn.delete_one({'_id': job_id})
