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
schemaless: an old document simply doesn't have the key, and every reader
of it uses `doc.get('last_delivered_at')` rather than indexing directly.
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo import MongoClient
from pymongo.database import Database

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB_DATA', 'adiyan')
JOBS_COLLECTION = 'adiyan_reader_jobs'
QUESTIONS_COLLECTION = 'adiyan_reader_questions'
EVAL_RESULTS_COLLECTION = 'adiyan_reader_eval_results'

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
        'questions_enabled': True,
        'speech_rate': 1.0,
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


# How long a processing lock is honored before being treated as abandoned
# (a crashed run that never released it) - long enough to cover even a slow
# TTS synthesis + comprehension-question generation, short enough that a
# genuine crash doesn't wedge a reading job's "read next page" indefinitely.
_PROCESSING_LOCK_TTL_SECONDS = 300


def try_acquire_reading_lock(conn: Database, job_id: str) -> bool:
    """Atomically claims the right to process this job's next page right
    now - True if acquired, False if another read is already in flight (or
    started recently and hasn't released yet) for the same job.

    Confirmed live: a customer sending "read next page" four times in
    quick succession (four nearly-simultaneous webhook deliveries, all
    landing before the first one's TTS synthesis had finished) each read
    the same stale current_page and sent the exact same page number twice
    over WhatsApp - db.advance_page() doesn't run until AFTER synthesis and
    sending complete, which easily takes longer than the gap between two
    impatient follow-up messages. This lock exists specifically to make
    every request after the first WAIT (return a "still working on it"
    status) instead of silently duplicating that work.

    Self-healing via processing_since's own TTL, not a separate release-on-
    crash mechanism: a lock older than _PROCESSING_LOCK_TTL_SECONDS is
    treated as abandoned and can be reacquired, so a process that crashed
    mid-read doesn't wedge that reading job's "next page" forever."""
    now_ts = datetime.now(timezone.utc).timestamp()
    stale_before = now_ts - _PROCESSING_LOCK_TTL_SECONDS
    result = conn[JOBS_COLLECTION].find_one_and_update(
        {
            '_id': job_id,
            '$or': [
                {'processing_since': {'$exists': False}},
                {'processing_since': None},
                {'processing_since': {'$lt': stale_before}},
            ],
        },
        {'$set': {'processing_since': now_ts}},
    )
    return result is not None


def release_reading_lock(conn: Database, job_id: str) -> None:
    """Always called from a finally block by whoever acquired the lock -
    see read_next_page.py's and read_range.py's own run() functions."""
    conn[JOBS_COLLECTION].update_one({'_id': job_id}, {'$set': {'processing_since': None}})


def advance_page(conn: Database, job_id: str, new_page: int) -> None:
    conn[JOBS_COLLECTION].update_one(
        {'_id': job_id},
        {'$set': {'current_page': new_page, 'last_delivered_at': datetime.now(timezone.utc).isoformat()}},
    )


def set_reading_job_voice(conn: Database, job_id: str, voice: str) -> None:
    """voice='' clears an explicit override, going back to following
    default_voice live (see read_next_page.py's own resolution at read
    time) - the same "empty string means no explicit choice" convention
    create_reading_job()'s callers already use, not a schema change."""
    conn[JOBS_COLLECTION].update_one({'_id': job_id}, {'$set': {'voice': voice}})


def set_reading_job_speech_rate(conn: Database, job_id: str, rate: float) -> None:
    """A persistent per-job playback-speed multiplier (1.0 = normal), not a
    one-off - once a customer asks to slow down, every future page (nightly
    and on-demand alike) keeps that pace until they change it back. Applied
    at read_next_page.py's own tts.synthesize() call via job['speech_rate'],
    the same "resolved live from the job document, never frozen at request
    time" pattern set_reading_job_voice() already uses."""
    conn[JOBS_COLLECTION].update_one({'_id': job_id}, {'$set': {'speech_rate': rate}})


def deactivate_reading_job(conn: Database, job_id: str) -> None:
    """The book has run out of pages - stop re-registering the nightly
    trigger, but leave the document (and its question history) on file
    rather than deleting it. Also reused as-is by stop_reading.py for a
    customer-requested stop: tonight's already-registered one-shot cron
    trigger still fires once (cron_trigger has no cancel-in-place), but
    read_next_page.run() checks job['active'] before sending anything and
    returns 'inactive' with neither audio nor a re-registration - so the
    stop takes effect from the very next scheduled fire, not immediately
    mid-flight, with no separate trigger-removal step needed."""
    conn[JOBS_COLLECTION].update_one({'_id': job_id}, {'$set': {'active': False}})


def reset_reading_job_page(conn: Database, job_id: str) -> None:
    """Puts a job back to current_page=0 (the same starting state
    create_reading_job() gives a brand-new job) so the next read - on
    demand or nightly - delivers page 1 again. restart_reading.py's own
    docstring covers the real gap this closes. Doesn't touch active,
    voice, or questions_enabled - a restart is "read this again from the
    start," not a new job with a blank slate on every other setting."""
    conn[JOBS_COLLECTION].update_one({'_id': job_id}, {'$set': {'current_page': 0}})


def set_questions_enabled(conn: Database, job_id: str, enabled: bool) -> None:
    """Independent of `active` - lets a customer keep the nightly narration
    going while opting out of just the next-morning comprehension questions
    (stop_reading.py's questions_only=True path). Checked by
    read_next_page.py's own _schedule_quiz_and_next_reading() right before it
    would otherwise generate and schedule that page's questions; the nightly
    re-registration itself never looks at this flag, so narration is
    unaffected either way."""
    conn[JOBS_COLLECTION].update_one({'_id': job_id}, {'$set': {'questions_enabled': enabled}})


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


def record_eval_result(
    conn: Database, reading_job_id: str, source_filename: str, page_number: int,
    match_percentage: int, threshold: int, passed: bool, transcript: str,
) -> None:
    """One row per page's post-delivery audio-quality eval - see
    eval_quality.py's own module docstring for the transcribe-and-compare
    loop that produces these. Written after the fact, purely for
    monitoring/trend visibility - nothing reads this back to affect a live
    reading job."""
    conn[EVAL_RESULTS_COLLECTION].insert_one({
        '_id': str(uuid.uuid4()), 'reading_job_id': reading_job_id, 'source_filename': source_filename,
        'page_number': page_number, 'match_percentage': match_percentage, 'threshold': threshold,
        'passed': passed, 'transcript': transcript, 'created_at': datetime.now(timezone.utc).isoformat(),
    })
