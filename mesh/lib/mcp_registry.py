"""
Mongo-backed registry of MCP servers and their tools - one collection per
registered MCP server, named `mcp_registry__<mcp_id>`. Three document
shapes per collection, distinguished by `doc_type`:

    {doc_type: "connection_config", url, transport, auth}   - one per collection
    {doc_type: "group_meta", name, description}             - one per collection
    {doc_type: "tool", name, description, schema}           - one per real tool

See docs/TOOL_RESOLUTION_DESIGN.md for the full design this implements.

Uses a raw `pymongo.AsyncMongoClient` rather than config_sdk's Beanie-backed
AgentConfig model - these collections are dynamically named (one per MCP
server, not known ahead of time), which doesn't fit a single typed Document
class the way config_sdk's fixed `agent_config` collection does. Same
database as config_sdk (`adiyan_config`) so this lives alongside every
other piece of Adiyan's config rather than a separate deployment to manage,
but its own collections, namespaced by the `mcp_registry__` prefix so they
can never collide with `agent_config` or anything else already in that
database.

Same graceful-degradation contract as config_sdk.py: if Mongo is
unreachable, every read returns empty and every write is silently skipped
- a down registry must not take an agent's ReAct loop down, it only means
that agent temporarily has no MCP tools to delegate to.
"""
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from pymongo import AsyncMongoClient

logger = logging.getLogger('MCPRegistry')

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB', 'adiyan_config')
COLLECTION_PREFIX = 'mcp_registry__'

_init_lock = asyncio.Lock()
_client: Optional[AsyncMongoClient] = None
_client_loop: Optional[Any] = None
_unavailable = False


def _collection_name(mcp_id: str) -> str:
    return f'{COLLECTION_PREFIX}{mcp_id}'


async def _get_db():
    """Returns the database handle, or None if Mongo is unreachable - same
    per-loop client-reuse guard as config_sdk._ensure_initialized(), for the
    same reason (a process that first calls in from one asyncio loop and
    later from another, e.g. a one-off asyncio.run() before uvicorn's own
    loop starts serving, would otherwise reuse a client bound to a dead
    loop)."""
    global _client, _client_loop, _unavailable
    if _unavailable:
        return None
    current_loop = asyncio.get_running_loop()
    if _client is not None and _client_loop is current_loop:
        return _client[MONGO_DB_NAME]
    async with _init_lock:
        if _client is not None and _client_loop is current_loop:
            return _client[MONGO_DB_NAME]
        try:
            client = AsyncMongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
            await client.admin.command('ping')
            _client = client
            _client_loop = current_loop
            logger.info(f'MCP registry connected to MongoDB at {MONGO_URL!r}, db {MONGO_DB_NAME!r}')
            return _client[MONGO_DB_NAME]
        except Exception as e:
            _unavailable = True
            logger.warning(f'MCP registry could not reach MongoDB ({MONGO_URL!r}) - registry reads/writes disabled: {e}')
            return None


async def list_registered_mcp_ids() -> List[str]:
    """Every mcp_id with a registry collection on file, derived from
    collection names rather than a separate index document - one less thing
    that can drift out of sync with what actually exists."""
    db = await _get_db()
    if db is None:
        return []
    names = await db.list_collection_names()
    prefix_len = len(COLLECTION_PREFIX)
    return [n[prefix_len:] for n in names if n.startswith(COLLECTION_PREFIX)]


async def get_connection_config(mcp_id: str) -> Optional[Dict[str, Any]]:
    db = await _get_db()
    if db is None:
        return None
    return await db[_collection_name(mcp_id)].find_one({'doc_type': 'connection_config'}, {'_id': 0})


async def get_group_metas(mcp_id: str) -> List[Dict[str, Any]]:
    """One MCP server can register more than one group - confirmed live:
    mongodb-mcp-server bundles real data-query tools (find, aggregate...)
    together with MongoDB's own unrelated product-documentation search
    (search-knowledge, list-knowledge-sources) behind one connection.
    Lumping both under one description repeats, one level down, the exact
    "too many unrelated options in one flat list" problem this registry
    exists to avoid at the group level - so a server's tools are split
    into as many group_meta docs (each carrying its own group_id) as its
    real, distinct purposes, not always exactly one."""
    db = await _get_db()
    if db is None:
        return []
    cursor = db[_collection_name(mcp_id)].find({'doc_type': 'group_meta'}, {'_id': 0})
    return [doc async for doc in cursor]


async def get_all_group_metas() -> List[Dict[str, Any]]:
    """One {mcp_id, group_id, name, description} entry per registered
    group - this is exactly what Step 1 of resolution queries, across
    every collection at once."""
    results = []
    for mcp_id in await list_registered_mcp_ids():
        for meta in await get_group_metas(mcp_id):
            results.append({'mcp_id': mcp_id, **meta})
    return results


async def get_tools(mcp_id: str, group_id: str) -> List[Dict[str, Any]]:
    db = await _get_db()
    if db is None:
        return []
    cursor = db[_collection_name(mcp_id)].find({'doc_type': 'tool', 'group_id': group_id}, {'_id': 0})
    return [doc async for doc in cursor]


async def upsert_connection_config(
    mcp_id: str, url: str, transport: str, auth: Optional[Dict[str, Any]] = None,
    backend_connection_string: Optional[str] = None,
) -> bool:
    """backend_connection_string is distinct from `url` - `url` is how
    Adiyan reaches the MCP server itself; backend_connection_string is
    what some servers (mongodb-mcp-server confirmed live) need passed to
    their OWN `connect` tool before any data tool works, naming the real
    resource behind that server (e.g. a mongodb:// URI) - a second,
    unrelated address, not a duplicate of the first."""
    db = await _get_db()
    if db is None:
        return False
    doc = {
        'doc_type': 'connection_config', 'url': url, 'transport': transport,
        'auth': auth or {'type': 'none'}, 'backend_connection_string': backend_connection_string,
    }
    await db[_collection_name(mcp_id)].replace_one({'doc_type': 'connection_config'}, doc, upsert=True)
    return True


async def replace_groups(mcp_id: str, groups: List[Dict[str, Any]]) -> bool:
    """Replaces every group_meta and tool doc for this mcp_id wholesale -
    `groups` is [{group_id, name, description, tools: [{name, description,
    schema}, ...]}, ...]. Wholesale, same reasoning as the old
    replace_tools(): a sync should reflect this server's current real
    grouping, not accumulate stale group_ids or tools it no longer has.
    connection_config is untouched - it belongs to the server, not to any
    one group carved out of it."""
    db = await _get_db()
    if db is None:
        return False
    collection = db[_collection_name(mcp_id)]
    await collection.delete_many({'doc_type': {'$in': ['group_meta', 'tool']}})
    docs = []
    for group in groups:
        docs.append({
            'doc_type': 'group_meta', 'group_id': group['group_id'],
            'name': group['name'], 'description': group['description'],
        })
        for t in group['tools']:
            docs.append({'doc_type': 'tool', 'group_id': group['group_id'], **t})
    if docs:
        await collection.insert_many(docs)
    return True
