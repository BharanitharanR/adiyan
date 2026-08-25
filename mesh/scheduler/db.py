"""
Scheduler Agent's own domain data - jobs, keyed by id, with the embedding
used for dedup matching stored alongside each row. This is state.db (see
mesh/lib/paths.py's state_db_path) - fixed relationships, not A2A task
bookkeeping (that's tasks.db, a separate file/concern entirely).

Dedup approach: cosine similarity over embeddings (0.72 floor, carried over
from the old codebase's services/routine_store.py, still worth re-tuning
from real usage) AND an exact resolved_schedule match - see
find_similar_job()'s own docstring for why schedule has to be part of the
check too, not just description similarity.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

SIMILARITY_FLOOR = 0.72

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    target TEXT NOT NULL,
    resolved_schedule TEXT NOT NULL,
    next_run_at TEXT NOT NULL,
    expects_response INTEGER NOT NULL DEFAULT 0,
    response_window_minutes INTEGER,
    embedding TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def _cosine(a: List[float], b: List[float]) -> float:
    a_arr, b_arr = np.array(a), np.array(b)
    denom = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    return float(np.dot(a_arr, b_arr) / denom) if denom else 0.0


def find_similar_job(conn: sqlite3.Connection, embedding: List[float], resolved_schedule: str) -> Optional[Dict[str, Any]]:
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
    not just "these two descriptions are topically similar." Only scans
    rows that already match on schedule - cheap early filter before the
    embedding comparison, not just a post-hoc check."""
    best_row, best_score = None, 0.0
    for row in conn.execute('SELECT * FROM jobs WHERE resolved_schedule = ?', (resolved_schedule,)):
        score = _cosine(embedding, json.loads(row['embedding']))
        if score > best_score:
            best_row, best_score = row, score
    if best_row is not None and best_score >= SIMILARITY_FLOOR:
        return dict(best_row)
    return None


def create_job(
    conn: sqlite3.Connection,
    name: str,
    description: str,
    target: str,
    resolved_schedule: str,
    next_run_at: str,
    embedding: List[float],
    expects_response: bool = False,
    response_window_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    job_id = str(uuid.uuid4())
    conn.execute(
        'INSERT INTO jobs (id, name, description, target, resolved_schedule, next_run_at, '
        'expects_response, response_window_minutes, embedding, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (job_id, name, description, target, resolved_schedule, next_run_at,
         int(expects_response), response_window_minutes, json.dumps(embedding),
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return get_job(conn, job_id)


def get_job(conn: sqlite3.Connection, job_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
    return dict(row) if row else None


def update_next_run(conn: sqlite3.Connection, job_id: str, next_run_at: str) -> None:
    conn.execute('UPDATE jobs SET next_run_at = ? WHERE id = ?', (next_run_at, job_id))
    conn.commit()


def find_overdue_jobs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Every job whose next_run_at has already passed - checked once at
    Scheduler Agent's own startup (see mesh/scheduler/server.py) to catch
    anything cron_trigger's own misfire handling silently dropped while
    this mesh was down (see mcp/cron_trigger/server.py's
    MISFIRE_GRACE_SECONDS docstring for the mechanism). A recurring job's
    own next-fire computation is always relative to 'now' at the moment it
    fires, so catching up once here - not once per missed occurrence -
    is enough to get it current again; this isn't a queue of backlogged
    reminders to replay."""
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute('SELECT * FROM jobs WHERE next_run_at < ?', (now,)).fetchall()
    return [dict(row) for row in rows]


def delete_job(conn: sqlite3.Connection, job_id: str) -> None:
    """Only removes this agent's own domain row. The matching cron_trigger
    registration is a separate store entirely - callers must also cancel
    that themselves (see mesh/scheduler/skills/delete_job.py), or the job
    keeps firing against a row that no longer exists."""
    conn.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
    conn.commit()
