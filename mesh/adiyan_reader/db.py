"""
AdiyanReader's own domain data - reading_jobs (one per book a contact is
being read, tracking current_page) and questions (comprehension questions
generated right after a page is read out, preloaded here and dispatched
the next morning by a separate scheduled fire - see skills/dispatch_
questions.py).

Storage: MongoDB (collections `adiyan_reader_jobs` and
`adiyan_reader_questions`, in the shared `adiyan` database - same server
mesh/scheduler/db.py and mesh/mcp/cron_trigger/server.py already use), not
the old SQLite state.db. Ported 2026-09-11, same reasoning as the
scheduler port: one place to inspect/back up/repair, and no per-agent
SQLite file to lose track of.

connect() returns the Mongo DATABASE handle, not a single collection - this
agent has two collections, and every calling file (server.py, every
skills/*.py) already treats `conn` as an opaque object it just passes
through to db.py's own functions, so nothing outside this file changed.

Still synchronous (plain pymongo MongoClient), same reasoning as the
scheduler port: the job/question count here is small, and a blocking call
from an async handler costs nothing measurable.

No more _migrate()/_MIGRATIONS/ALTER TABLE - that machinery existed only
to add a column (`last_delivered_at`) to pre-existing SQLite rows. Mongo is
schemaless: an old document simply doesn't have the key, and
find_overdue_reading_jobs() below reads it as `doc.get(...) or
doc['created_at']` instead.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from pymongo import MongoClient
from pymongo.database import Database

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB_DATA', 'adiyan')
JOBS_COLLECTION = 'adiyan_reader_jobs'
QUESTIONS_COLLECTION = 'adiyan_reader_questions'

_client: Optional[MongoClient] = None


def connect(_state_db_path: Any = None) -> Database:
    """Returns the shared `adiyan` database (holding both this agent's
    collections). The argument is ignored - kept so existing call sites
    passing state_db_path(AGENT_ID) didn't have to change in the
    sqlite->mongo move. The client is created once and reused."""
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    return _client[MONGO_DB_NAME]


def _doc_to_job(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Mongo `_id` -> `id`, and `last_delivered_at`/`voice` default to what
    the old SQLite schema's NULL/'' meant, so callers see the same shape
    they always did."""
    if doc is None:
        return None
    job = dict(doc)
    job['id'] = job.pop('_id')
    job.setdefault('last_delivered_at', None)
    return job


def _doc_to_question(doc: Dict[str, Any]) -> Dict[str, Any]:
    q = dict(doc)
    q['id'] = q.pop('_id')
    return q


def create_reading_job(
    conn: Database, phone_number: str, source_filename: str, voice: str,
) -> Dict[str, Any]:
    job_id = str(uuid.uuid4())
    conn[JOBS_COLLECTION].insert_one({
        '_id': job_id,
        'phone_number': phone_number,
        'source_filename': source_filename,
        'voice': voice,
        'current_page': 0,
        'active': True,
        'last_delivered_at': None,
        'created_at': datetime.now(timezone.utc).isoformat(),
    })
    return get_reading_job(conn, job_id)


def get_reading_job(conn: Database, job_id: str) -> Optional[Dict[str, Any]]:
    return _doc_to_job(conn[JOBS_COLLECTION].find_one({'_id': job_id}))


def get_active_reading_job(conn: Database, phone_number: str, source_filename: str) -> Optional[Dict[str, Any]]:
    """The existing in-progress job for this exact phone+book pair, if one
    exists - start_reading.py checks this before ever calling
    create_reading_job(), so asking to start a book that's already being
    read (e.g. a duplicate "read me this book" message) resumes the
    existing job instead of silently spinning up a second one reading the
    same book to the same number in parallel."""
    return _doc_to_job(conn[JOBS_COLLECTION].find_one({
        'phone_number': phone_number, 'source_filename': source_filename, 'active': True,
    }))


def get_active_reading_jobs_by_phone(conn: Database, phone_number: str) -> List[Dict[str, Any]]:
    """Every active reading job for this phone number, most recently
    created first - the read_now skill's own lookup for "send me the next
    page right now" (mesh/adiyan_reader/skills/read_now.py), which has no
    book name to disambiguate with (unlike start_reading, which always
    gets an explicit source_filename). One active job is the common case
    and resolves unambiguously; the caller decides what to do with more
    than one (read_now.py picks the most recent rather than guessing which
    book "now" means)."""
    cursor = conn[JOBS_COLLECTION].find(
        {'phone_number': phone_number, 'active': True},
    ).sort('created_at', -1)
    return [_doc_to_job(doc) for doc in cursor]


def advance_page(conn: Database, job_id: str, new_page: int) -> None:
    conn[JOBS_COLLECTION].update_one(
        {'_id': job_id},
        {'$set': {'current_page': new_page, 'last_delivered_at': datetime.now(timezone.utc).isoformat()}},
    )


def find_overdue_reading_jobs(conn: Database, stale_after_hours: float = 30.0) -> List[Dict[str, Any]]:
    """Every active job whose most recent real signal of life - the last
    page actually delivered, or its own creation if it's never had a first
    night yet - is older than stale_after_hours. Checked once at
    AdiyanReader's own startup (see mesh/adiyan_reader/server.py), same
    reasoning as mesh/scheduler/db.py's own find_overdue_jobs(): catches a
    nightly fire that mcp/cron_trigger's own misfire handling silently
    dropped while this mesh was down (see mcp/cron_trigger/server.py's
    MISFIRE_GRACE_SECONDS docstring for the mechanism this compensates for
    - that one only covers up to 6 hours of downtime, this catches
    whatever slips past it).

    30 hours, not 24 - a job re-registers itself for the next literal
    midnight UTC after it fires (see read_next_page.py), not "24 hours
    from last delivery," so the real gap between two consecutive on-time
    deliveries already varies by several hours depending on what time of
    day the job was first created. 30 hours gives that natural variance
    room without also catching a job that's merely running a few hours
    late tonight but not actually missed.

    The COALESCE(last_delivered_at, created_at) SQLite did is a plain `or`
    here - Mongo has no NULL-coalescing filter operator worth reaching for
    over such a small candidate set, so this filters in Python rather than
    an aggregation pipeline."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_after_hours)).isoformat()
    overdue = []
    for doc in conn[JOBS_COLLECTION].find({'active': True}):
        last_signal = doc.get('last_delivered_at') or doc['created_at']
        if last_signal < cutoff:
            overdue.append(_doc_to_job(doc))
    return overdue


def set_reading_job_voice(conn: Database, job_id: str, voice: str) -> None:
    """voice='' clears an explicit override, going back to following
    default_voice live (see read_next_page.py's own resolution at read
    time) - the same "empty string means no explicit choice" convention
    create_reading_job()'s callers already use, not a schema change."""
    conn[JOBS_COLLECTION].update_one({'_id': job_id}, {'$set': {'voice': voice}})


def deactivate_reading_job(conn: Database, job_id: str) -> None:
    """The book has run out of pages - stop re-registering the nightly
    trigger, but leave the document (and its question history) on file
    rather than deleting it."""
    conn[JOBS_COLLECTION].update_one({'_id': job_id}, {'$set': {'active': False}})


def add_questions(
    conn: Database, reading_job_id: str, page_number: int,
    question_texts: List[str], dispatch_at: str,
) -> None:
    if not question_texts:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn[QUESTIONS_COLLECTION].insert_many([
        {
            '_id': str(uuid.uuid4()), 'reading_job_id': reading_job_id, 'page_number': page_number,
            'question_text': q, 'dispatch_at': dispatch_at, 'sent': False, 'created_at': now,
        }
        for q in question_texts
    ])


def get_pending_questions(conn: Database, reading_job_id: str, page_number: int) -> List[Dict[str, Any]]:
    """Only this exact page's own batch - dispatch_questions.py is fired
    with a specific (reading_job_id, page_number) by the one-shot trigger
    read_next_page.py registered for it, not a generic "whatever's due"
    sweep - see that module's own docstring for why."""
    cursor = conn[QUESTIONS_COLLECTION].find({
        'reading_job_id': reading_job_id, 'page_number': page_number, 'sent': False,
    })
    return [_doc_to_question(doc) for doc in cursor]


def mark_questions_sent(conn: Database, reading_job_id: str, page_number: int) -> None:
    conn[QUESTIONS_COLLECTION].update_many(
        {'reading_job_id': reading_job_id, 'page_number': page_number},
        {'$set': {'sent': True}},
    )
