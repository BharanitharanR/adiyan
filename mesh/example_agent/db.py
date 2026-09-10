"""
Per-agent persistence template - the pattern for an agent that needs to
store its own domain data (a journal, a habit log, saved preferences,
anything).

Example Agent's own skill (roll_dice) doesn't persist anything, so nothing
here is actually called by this agent. It's in the scaffold as the
reference: copy this file into your agent, change AGENT_ID's value (which
drives the collection name), and call it from your skill in skills/.

Shape, and why:
  - MongoDB, not SQLite. Same server/database the rest of the deployment
    already runs (ADIYAN_MONGO_URL, database 'adiyan') - one place to back
    up, inspect, and repair. config_sdk uses a separate 'adiyan_config'
    database for config; domain data goes in 'adiyan', one collection per
    agent, named from AGENT_ID so two agents can't collide.
  - Synchronous (plain pymongo MongoClient). Agent skills are `async def`,
    but a per-agent collection is small and a blocking call from an async
    handler costs nothing measurable. Going async brings the event-loop-
    caching complexity config_sdk.py has to carry; not worth it here.
  - The collection is created by Mongo on the first insert. Nothing to set
    up ahead of time. Add a create_index() call in connect() if you later
    want a fast lookup (it's idempotent, safe to run every start).

See mesh/scheduler/db.py for the same pattern used in production, including
cosine-similarity dedup over an embedding field.
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pymongo import MongoClient
from pymongo.collection import Collection

from mesh.example_agent.constants import AGENT_ID

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB_DATA', 'adiyan')
# One collection per agent, named from AGENT_ID. Rename nothing here when you
# copy this file - changing AGENT_ID in constants.py is what moves the data.
COLLECTION_NAME = f'{AGENT_ID}_entries'

_client: Optional[MongoClient] = None


def connect() -> Collection:
    """Returns this agent's own collection. The client is created once and
    reused for the life of the process."""
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    return _client[MONGO_DB_NAME][COLLECTION_NAME]


def _doc_to_record(doc: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Mongo `_id` -> `id`, so callers never see the raw `_id` key."""
    if doc is None:
        return None
    record = dict(doc)
    record['id'] = record.pop('_id')
    return record


def add_entry(conn: Collection, **fields: Any) -> Dict[str, Any]:
    """Insert one record. `fields` is whatever your skill wants to store -
    e.g. add_entry(conn, entry='did 10 pushups', mood='good'). A uuid id
    and a UTC created_at timestamp are added for you. Returns the stored
    record (id-normalized)."""
    doc_id = str(uuid.uuid4())
    doc = {
        '_id': doc_id,
        'created_at': datetime.now(timezone.utc).isoformat(),
        **fields,
    }
    conn.insert_one(doc)
    return _doc_to_record(doc)


def get_entry(conn: Collection, entry_id: str) -> Optional[Dict[str, Any]]:
    return _doc_to_record(conn.find_one({'_id': entry_id}))


def list_entries(conn: Collection, limit: int = 20) -> List[Dict[str, Any]]:
    """Most recent first. created_at is an ISO-8601 UTC string, so a plain
    string sort is also a chronological one."""
    cursor = conn.find({}).sort('created_at', -1).limit(limit)
    return [_doc_to_record(doc) for doc in cursor]


def delete_entry(conn: Collection, entry_id: str) -> None:
    conn.delete_one({'_id': entry_id})
