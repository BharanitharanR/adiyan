"""
Per-agent last-activity timestamps in Mongo - the signal
mesh/tools/offload_idle_agents.py uses to decide which agents have gone
unused long enough to stop, the offload half of Adiyan's scale-to-zero (see
that module's own docstring; mesh/lib/process_control.py is the wake half).

record_activity() is called from exactly one place, mesh/lib/bootstrap.py's
_PlatformWiredExecutor.execute() - the single point every agent's every
incoming request already passes through, so no individual agent needs to
call this itself, the same lever that module already uses for its other
platform-wide hooks (see its own top docstring).

Uses its own small AsyncMongoClient, cached at module scope. Safe to cache
here - unlike the background-thread trap mesh/lib/bootstrap.py's own
docstring warns about for config_sdk.py's client - because every caller of
record_activity() runs inside an agent's own request handler, always on
uvicorn's own event loop, never a separate thread spinning up its own loop.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from pymongo import AsyncMongoClient

logger = logging.getLogger('agent_activity')

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB', 'adiyan_config')
COLLECTION_NAME = 'agent_activity'

_client: Optional[AsyncMongoClient] = None


def _collection():
    global _client
    if _client is None:
        _client = AsyncMongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
    return _client[MONGO_DB_NAME][COLLECTION_NAME]


async def record_activity(agent_id: str) -> None:
    """Best-effort - a Mongo hiccup here must never fail the real request
    it's piggybacking on, so failures are logged and swallowed, never
    raised."""
    try:
        await _collection().update_one(
            {'_id': agent_id},
            {'$set': {'last_active_at': datetime.now(timezone.utc)}},
            upsert=True,
        )
    except Exception as e:
        logger.warning(f"Could not record activity for {agent_id!r}: {e}")
