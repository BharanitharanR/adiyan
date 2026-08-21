"""
Scheduler Agent's own domain data - jobs, keyed by id, with the embedding
used for dedup matching stored alongside each row. This is state.db (see
mesh/lib/paths.py's state_db_path) - fixed relationships, not A2A task
bookkeeping (that's tasks.db, a separate file/concern entirely).

Dedup approach mirrors the old codebase's services/routine_store.py: cosine
similarity over embeddings, 0.72 floor - carried over as a floor worth
re-tuning from real usage here too, not re-derived from scratch.
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


def find_similar_job(conn: sqlite3.Connection, embedding: List[float]) -> Optional[Dict[str, Any]]:
    """Returns the closest existing job at/above SIMILARITY_FLOOR, or None.
    Linear scan - fine at the row counts one owner's scheduled jobs will
    ever reach; revisit if that assumption stops holding."""
    best_row, best_score = None, 0.0
    for row in conn.execute('SELECT * FROM jobs'):
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


def delete_job(conn: sqlite3.Connection, job_id: str) -> None:
    """Only removes this agent's own domain row. The matching cron_trigger
    registration is a separate store entirely - callers must also cancel
    that themselves (see mesh/scheduler/skills/delete_job.py), or the job
    keeps firing against a row that no longer exists."""
    conn.execute('DELETE FROM jobs WHERE id = ?', (job_id,))
    conn.commit()
