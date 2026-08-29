"""
AdiyanReader's own domain data - reading_jobs (one per book a contact is
being read, tracking current_page) and questions (comprehension questions
generated right after a page is read out, preloaded here and dispatched
the next morning by a separate scheduled fire - see skills/dispatch_
questions.py). Same state.db-not-tasks.db split every other agent's db.py
follows (see mesh/scheduler/db.py's own module docstring for why).
"""
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS reading_jobs (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    voice TEXT NOT NULL,
    current_page INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    reading_job_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    question_text TEXT NOT NULL,
    dispatch_at TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

# Columns added after the table's original release - ALTER TABLE'd in on
# connect() for any pre-existing state.db, same pattern as
# mesh/orchestrator/db.py's own _migrate(), since CREATE TABLE IF NOT
# EXISTS only applies to a table that doesn't exist yet.
_MIGRATIONS = [
    ("last_delivered_at", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(reading_jobs)')}
    for column, ddl in _MIGRATIONS:
        if column not in existing:
            conn.execute(f'ALTER TABLE reading_jobs ADD COLUMN {column} {ddl}')
    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def create_reading_job(
    conn: sqlite3.Connection, phone_number: str, source_filename: str, voice: str,
) -> Dict[str, Any]:
    job_id = str(uuid.uuid4())
    conn.execute(
        'INSERT INTO reading_jobs (id, phone_number, source_filename, voice, created_at) '
        'VALUES (?, ?, ?, ?, ?)',
        (job_id, phone_number, source_filename, voice, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return get_reading_job(conn, job_id)


def get_reading_job(conn: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute('SELECT * FROM reading_jobs WHERE id = ?', (job_id,)).fetchone()
    return dict(row) if row else None


def get_active_reading_job(conn: sqlite3.Connection, phone_number: str, source_filename: str) -> Optional[Dict[str, Any]]:
    """The existing in-progress job for this exact phone+book pair, if one
    exists - start_reading.py checks this before ever calling
    create_reading_job(), so asking to start a book that's already being
    read (e.g. a duplicate "read me this book" message) resumes the
    existing job instead of silently spinning up a second one reading the
    same book to the same number in parallel."""
    row = conn.execute(
        'SELECT * FROM reading_jobs WHERE phone_number = ? AND source_filename = ? AND active = 1',
        (phone_number, source_filename),
    ).fetchone()
    return dict(row) if row else None


def get_active_reading_jobs_by_phone(conn: sqlite3.Connection, phone_number: str) -> List[Dict[str, Any]]:
    """Every active reading job for this phone number, most recently
    created first - the read_now skill's own lookup for "send me the next
    page right now" (mesh/adiyan_reader/skills/read_now.py), which has no
    book name to disambiguate with (unlike start_reading, which always
    gets an explicit source_filename). One active job is the common case
    and resolves unambiguously; the caller decides what to do with more
    than one (read_now.py picks the most recent rather than guessing which
    book "now" means)."""
    rows = conn.execute(
        'SELECT * FROM reading_jobs WHERE phone_number = ? AND active = 1 ORDER BY created_at DESC',
        (phone_number,),
    ).fetchall()
    return [dict(r) for r in rows]


def advance_page(conn: sqlite3.Connection, job_id: str, new_page: int) -> None:
    conn.execute(
        'UPDATE reading_jobs SET current_page = ?, last_delivered_at = ? WHERE id = ?',
        (new_page, datetime.now(timezone.utc).isoformat(), job_id),
    )
    conn.commit()


def find_overdue_reading_jobs(conn: sqlite3.Connection, stale_after_hours: float = 30.0) -> List[Dict[str, Any]]:
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
    late tonight but not actually missed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=stale_after_hours)).isoformat()
    rows = conn.execute(
        'SELECT * FROM reading_jobs WHERE active = 1 AND COALESCE(last_delivered_at, created_at) < ?',
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_reading_job_voice(conn: sqlite3.Connection, job_id: str, voice: str) -> None:
    """voice='' clears an explicit override, going back to following
    default_voice live (see read_next_page.py's own resolution at read
    time) - the same "empty string means no explicit choice" convention
    create_reading_job()'s callers already use, not a schema change."""
    conn.execute('UPDATE reading_jobs SET voice = ? WHERE id = ?', (voice, job_id))
    conn.commit()


def deactivate_reading_job(conn: sqlite3.Connection, job_id: str) -> None:
    """The book has run out of pages - stop re-registering the nightly
    trigger, but leave the row (and its question history) on file rather
    than deleting it."""
    conn.execute('UPDATE reading_jobs SET active = 0 WHERE id = ?', (job_id,))
    conn.commit()


def add_questions(
    conn: sqlite3.Connection, reading_job_id: str, page_number: int,
    question_texts: List[str], dispatch_at: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        'INSERT INTO questions (id, reading_job_id, page_number, question_text, dispatch_at, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        [(str(uuid.uuid4()), reading_job_id, page_number, q, dispatch_at, now) for q in question_texts],
    )
    conn.commit()


def get_pending_questions(conn: sqlite3.Connection, reading_job_id: str, page_number: int) -> List[Dict[str, Any]]:
    """Only this exact page's own batch - dispatch_questions.py is fired
    with a specific (reading_job_id, page_number) by the one-shot trigger
    read_next_page.py registered for it, not a generic "whatever's due"
    sweep - see that module's own docstring for why."""
    rows = conn.execute(
        'SELECT * FROM questions WHERE reading_job_id = ? AND page_number = ? AND sent = 0',
        (reading_job_id, page_number),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_questions_sent(conn: sqlite3.Connection, reading_job_id: str, page_number: int) -> None:
    conn.execute(
        'UPDATE questions SET sent = 1 WHERE reading_job_id = ? AND page_number = ?',
        (reading_job_id, page_number),
    )
    conn.commit()
