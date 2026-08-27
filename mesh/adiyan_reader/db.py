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
from datetime import datetime, timezone
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


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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
    conn.execute('UPDATE reading_jobs SET current_page = ? WHERE id = ?', (new_page, job_id))
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
