"""
Dedup ledger for inbound WhatsApp webhooks - keyed on WhatsApp's own message
id (data['id']), never a content hash. A content hash keyed on chat_id+text
would treat a genuinely repeated message (e.g. sending "Hi" twice, on two
different days) as a duplicate and silently drop the second one - WhatsApp's
own message id has no such false-positive risk, since it's unique per
message by construction.

Exists because OpenWA redelivers a webhook when it doesn't get a timely
response (confirmed live: Adiyan's LLM calls routinely exceed whatever
window triggers a retry), and with no dedup anywhere in the pipeline, every
redelivery was processed as a brand-new message - confirmed live to produce
2-3 independent replies (and, without Scheduler's own separate embedding
dedup, would have created duplicate jobs) for a single message actually
sent once.
"""
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_messages (
    whatsapp_message_id TEXT PRIMARY KEY,
    seen_at INTEGER NOT NULL
)
"""

# How long a message id is kept before it's pruned - bounds table growth.
# Far longer than any realistic webhook-retry window, so it can never
# plausibly still be a live redelivery risk once it ages out.
RETENTION_SECONDS = 7 * 24 * 60 * 60


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    _prune(conn)
    return conn


def _prune(conn: sqlite3.Connection) -> None:
    cutoff = int(time.time()) - RETENTION_SECONDS
    conn.execute('DELETE FROM processed_messages WHERE seen_at < ?', (cutoff,))
    conn.commit()


def is_duplicate(conn: sqlite3.Connection, whatsapp_message_id: str) -> bool:
    """True if this exact WhatsApp message id has already been recorded.
    An empty/None id is never treated as a duplicate - fail open, not
    closed: reprocessing an unidentifiable message is a far smaller risk
    than silently dropping every message that happens to lack an id."""
    if not whatsapp_message_id:
        return False
    row = conn.execute(
        'SELECT 1 FROM processed_messages WHERE whatsapp_message_id = ?',
        (whatsapp_message_id,),
    ).fetchone()
    return row is not None


def mark_seen(conn: sqlite3.Connection, whatsapp_message_id: str) -> None:
    """No-op for an empty id. INSERT OR IGNORE: a concurrent duplicate
    marking of the same id is fine, not an error - the first writer wins,
    which is all that matters for correctness here."""
    if not whatsapp_message_id:
        return
    conn.execute(
        'INSERT OR IGNORE INTO processed_messages (whatsapp_message_id, seen_at) VALUES (?, ?)',
        (whatsapp_message_id, int(time.time())),
    )
    conn.commit()
