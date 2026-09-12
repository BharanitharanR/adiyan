"""
Orchestrator's own domain data - registered WhatsApp contacts, keyed by an
identity key (see resolve_identity_key - NOT the raw chat_id a webhook
happens to report). Mirrors the legacy config/database.py's clients table
shape (contact_name, is_whitelisted), scoped to what the WhatsApp rules
engine actually needs.

Storage: MongoDB (collection `orchestrator_clients`, in the shared `adiyan`
database - the same one mesh/scheduler/db.py and mesh/adiyan_reader/db.py
already use), not the old SQLite state.db. Ported 2026-09-11, same
reasoning as those two: one place to inspect/back up/repair. The document's
own `_id` IS the identity key (resolve_identity_key's output) - no
separate chat_id field to keep in sync with the key it's stored under.

No more _migrate()/_MIGRATIONS/the ALTER TABLE machinery, or
_migrate_identity_keys() re-scanning every row on every connect() call -
both existed only to evolve pre-existing SQLite rows forward (a new
column; a new keying scheme). Mongo needs neither: a document either has a
key or doesn't, and every write from here on already stores under the
correct, normalized identity key.
"""
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pymongo import MongoClient
from pymongo.collection import Collection

# Moved to mesh/lib/identity.py when agents began deriving the same key
# from their own A2A token's `sub` claim - re-exported here (rather than
# updating rules_engine's two `db.resolve_identity_key(...)` call sites to
# import it separately) because this module's own contract is still "the
# clients collection, keyed by identity key," and the key's definition
# belongs with it.
from mesh.lib.identity import resolve_identity_key  # noqa: F401

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB_DATA', 'adiyan')
COLLECTION_NAME = 'orchestrator_clients'

_client: Optional[MongoClient] = None


def connect(_state_db_path: Any = None) -> Collection:
    """Returns the `orchestrator_clients` collection. The argument is
    ignored - kept so existing call sites passing state_db_path(AGENT_ID)
    didn't have to change in the sqlite->mongo move. The client is created
    once and reused."""
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    return _client[MONGO_DB_NAME][COLLECTION_NAME]


def is_whitelisted(conn: Collection, chat_id: str) -> bool:
    doc = conn.find_one({'_id': chat_id})
    return bool(doc and doc.get('is_whitelisted'))


def add_client(conn: Collection, chat_id: str, contact_name: Optional[str]) -> None:
    """Upsert, same as the legacy add_client - re-registering an already-
    unregistered contact just flips is_whitelisted back on, doesn't error.
    registered_at is set only on first-ever insert ($setOnInsert) - a
    re-registration updates contact_name/is_whitelisted/updated_at but
    never resets when this contact originally joined, same as the old
    ON CONFLICT clause deliberately left registered_at out of its SET
    list."""
    now = datetime.now(timezone.utc).isoformat()
    conn.update_one(
        {'_id': chat_id},
        {
            '$set': {'contact_name': contact_name, 'is_whitelisted': True, 'updated_at': now},
            '$setOnInsert': {'registered_at': now, 'metadata': {}},
        },
        upsert=True,
    )


def remove_client(conn: Collection, chat_id: str) -> None:
    """Soft delete, same as the legacy remove_client - flips the flag,
    doesn't drop the document, so history/re-registration stays intact."""
    conn.update_one(
        {'_id': chat_id},
        {'$set': {'is_whitelisted': False, 'updated_at': datetime.now(timezone.utc).isoformat()}},
    )


def get_metadata(conn: Collection, chat_id: str) -> Dict[str, Any]:
    """Arbitrary per-client fields with no schema of their own yet - e.g. a
    future preferred-language, timezone, or plan tier. Empty dict for an
    unregistered chat_id or a document that's never had metadata set."""
    doc = conn.find_one({'_id': chat_id})
    return (doc or {}).get('metadata') or {}


def update_metadata(conn: Collection, chat_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow-merges `updates` into the client's existing metadata (new keys
    added, matching keys overwritten, everything else untouched) and returns
    the merged result. No-op if chat_id isn't a registered client."""
    current = get_metadata(conn, chat_id)
    current.update(updates)
    result = conn.update_one(
        {'_id': chat_id},
        {'$set': {'metadata': current, 'updated_at': datetime.now(timezone.utc).isoformat()}},
    )
    return current if result.matched_count else {}
