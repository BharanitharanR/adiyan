"""
Orchestrator's own domain data - registered WhatsApp contacts, keyed by an
identity key (see resolve_identity_key - NOT the raw chat_id a webhook
happens to report). Mirrors the legacy config/database.py's clients table
shape (contact_name, is_whitelisted), scoped to what the WhatsApp rules
engine actually needs.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    chat_id TEXT PRIMARY KEY,
    contact_name TEXT,
    is_whitelisted INTEGER NOT NULL DEFAULT 1,
    registered_at TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT
)
"""

# Columns added after the table's original release - ALTER TABLE'd in on
# connect() for any pre-existing state.db, since CREATE TABLE IF NOT EXISTS
# only applies to a table that doesn't exist yet.
_MIGRATIONS = [
    ("metadata", "TEXT NOT NULL DEFAULT '{}'"),
    ("updated_at", "TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(clients)')}
    for column, ddl in _MIGRATIONS:
        if column not in existing:
            conn.execute(f'ALTER TABLE clients ADD COLUMN {column} {ddl}')
    conn.commit()


def resolve_identity_key(chat_id: str) -> str:
    """Phone digits when chat_id is already phone-form (@c.us) - the
    stable, WhatsApp-account-level identifier. Falls back to the raw
    chat_id (usually @lid) otherwise - a lid-form contact's phone number
    isn't safely resolvable (resolve_chat_id() is confirmed live to hang
    indefinitely - see mesh/mcp/whatsapp/server.py's get_own_phone()
    docstring), so the lid is the best available identity for them today.

    Every clients-table read/write goes through this, not the raw chat_id
    a webhook happens to report - confirmed live that the same contact can
    be addressed in different JID forms depending on which field you read
    (see openwa_receiver.py's is_self_chat fix), so comparing raw chat_id
    values directly is unreliable. Delivery (send_message) still uses the
    real, un-normalized chat_id - only identity lookups go through this."""
    if chat_id and chat_id.endswith('@c.us'):
        return chat_id.split('@')[0]
    return chat_id


def _migrate_identity_keys(conn: sqlite3.Connection) -> None:
    """One-time re-key of any pre-existing rows to resolve_identity_key's
    normalized shape - added when the clients table moved from keying on
    whatever raw chat_id form a webhook happened to report to a stable,
    phone-digits-when-resolvable key. Idempotent: a row already in its
    normalized form is a no-op UPDATE (new_key == old_key)."""
    rows = conn.execute('SELECT chat_id FROM clients').fetchall()
    for row in rows:
        old_key = row['chat_id']
        new_key = resolve_identity_key(old_key)
        if new_key != old_key:
            conn.execute('UPDATE clients SET chat_id = ? WHERE chat_id = ?', (new_key, old_key))
    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    _migrate(conn)
    _migrate_identity_keys(conn)
    return conn


def is_whitelisted(conn: sqlite3.Connection, chat_id: str) -> bool:
    row = conn.execute('SELECT is_whitelisted FROM clients WHERE chat_id = ?', (chat_id,)).fetchone()
    return bool(row and row['is_whitelisted'])


def add_client(conn: sqlite3.Connection, chat_id: str, contact_name: Optional[str]) -> None:
    """Upsert, same as the legacy add_client - re-registering an already-
    unregistered contact just flips is_whitelisted back on, doesn't error."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        'INSERT INTO clients (chat_id, contact_name, is_whitelisted, registered_at, updated_at) '
        'VALUES (?, ?, 1, ?, ?) '
        'ON CONFLICT(chat_id) DO UPDATE SET is_whitelisted=1, contact_name=excluded.contact_name, updated_at=excluded.updated_at',
        (chat_id, contact_name, now, now),
    )
    conn.commit()


def remove_client(conn: sqlite3.Connection, chat_id: str) -> None:
    """Soft delete, same as the legacy remove_client - flips the flag,
    doesn't drop the row, so history/re-registration stays intact."""
    conn.execute(
        'UPDATE clients SET is_whitelisted = 0, updated_at = ? WHERE chat_id = ?',
        (datetime.now(timezone.utc).isoformat(), chat_id),
    )
    conn.commit()


def get_metadata(conn: sqlite3.Connection, chat_id: str) -> Dict[str, Any]:
    """Arbitrary per-client fields with no schema of their own yet - e.g. a
    future preferred-language, timezone, or plan tier. Empty dict for an
    unregistered chat_id or a row that's never had metadata set."""
    row = conn.execute('SELECT metadata FROM clients WHERE chat_id = ?', (chat_id,)).fetchone()
    if not row or not row['metadata']:
        return {}
    return json.loads(row['metadata'])


def update_metadata(conn: sqlite3.Connection, chat_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merges `updates` into the client's existing metadata (new keys
    added, matching keys overwritten, everything else untouched) and returns
    the merged result. No-op if chat_id isn't a registered client."""
    current = get_metadata(conn, chat_id)
    current.update(updates)
    cursor = conn.execute(
        'UPDATE clients SET metadata = ?, updated_at = ? WHERE chat_id = ?',
        (json.dumps(current), datetime.now(timezone.utc).isoformat(), chat_id),
    )
    conn.commit()
    return current if cursor.rowcount else {}
