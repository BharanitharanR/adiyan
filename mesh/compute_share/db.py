"""compute_share's own domain data: the local table of peers this
instance knows are willing to take work, keyed by URL. Deliberately not
shared/global - see README.md for why a real deployment needs a public
registry instead of this direct-announce shortcut."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS peers (
    peer_url TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    announced_at TEXT NOT NULL
)
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def upsert_peer(conn: sqlite3.Connection, peer_url: str, model: str) -> None:
    conn.execute(
        'INSERT INTO peers (peer_url, model, announced_at) VALUES (?, ?, ?) '
        'ON CONFLICT(peer_url) DO UPDATE SET model = excluded.model, announced_at = excluded.announced_at',
        (peer_url, model, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def list_peers(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    return [dict(row) for row in conn.execute('SELECT * FROM peers ORDER BY announced_at ASC')]


def pick_peer(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    # Oldest-announced first, not most-recent - a crude round-robin so one
    # peer doesn't take every request just for being top of an unordered
    # scan. A real deployment would also weigh recent latency/failures;
    # this is deliberately the simplest thing that isn't "always the same
    # peer," for a POC.
    row = conn.execute('SELECT * FROM peers ORDER BY announced_at ASC LIMIT 1').fetchone()
    return dict(row) if row else None
