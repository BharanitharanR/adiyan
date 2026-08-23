"""
Central config/prompt SDK - the one place every agent under mesh/ reads its
stage settings (model/temperature/timeout), prompt templates, and any other
hardcoded constant from, instead of each agent's own local runtime_config.json
or an inline hardcoded string. Backed by MongoDB via Beanie (an async ODM on
top of pymongo's own native async client, chosen over hand-rolling raw
driver calls - typed documents, schema migrations).

Uses `pymongo.AsyncMongoClient`, not the separate `motor` package - Beanie
2.2.0 has dropped its motor dependency entirely in favor of pymongo's own
native async client (added in pymongo 4.9+, which absorbed what motor used
to provide). Confirmed the hard way: passing beanie a motor-wrapped database
raises `TypeError: MotorDatabase object is not callable` deep in its
Initializer - motor's compatibility shim and beanie 2.x's internals don't
agree on what `database.client` should be.

Schema is intentionally private to this module - a calling agent passes
agent_id/key and a default, and gets back whatever value is on file, with no
knowledge of documents/collections/fields. That's the seam this module is
built around: when a real config service (its own dashboard, used by support
teams - the stated later milestone) eventually sits in front of Mongo, only
this module's internals change to call that service's API instead of Mongo
directly. No calling agent's code changes at all.

Auto-seeding: the first time any agent asks for a stage/constant that
doesn't exist yet in Mongo, whatever `default` the caller supplied (its own
current local runtime_config.json value, or a hardcoded prompt string) is
written back as the new on-file value - migrating existing hardcoded
config/prompts into Mongo happens automatically, one read at a time, not via
a separate migration script.

Graceful degradation: exactly like mem0_backend.py and registry_client.py -
if Mongo is unreachable, every read falls back to the caller's own default
and every write is silently skipped. A missing/down config store must never
take an agent down; it only means edits made via the (future) dashboard/
WhatsApp tool aren't picked up until Mongo is reachable again. Connection is
attempted once per process - if it fails, every later call fails fast
without retrying (this mesh's existing "restart to recover" convention
already covers coming back, same reasoning router.py's old load-once model
had, just not the part of that model that caused the Orchestrator/Analysis
Agent staleness bug - there's nothing to go stale here, since a fresh read
happens every _ttl_seconds()).
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

from beanie import Document, init_beanie
from pydantic import Field
from pymongo import AsyncMongoClient

logger = logging.getLogger('ConfigSDK')

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB', 'adiyan_config')

# How long a cached agent document is trusted before the next read
# transparently refreshes it from Mongo - configurable, same reasoning as
# registry_client.py's AGENT_REGISTRY_REFRESH_SECONDS.
DEFAULT_CACHE_TTL_SECONDS = 30.0


class AgentConfig(Document):
    agent_id: str
    stages: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    constants: Dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = 'agent_config'


_init_lock = asyncio.Lock()
_initialized = False
_unavailable = False

_cache: Dict[str, AgentConfig] = {}
_cache_at: Dict[str, float] = {}


def _ttl_seconds() -> float:
    return float(os.environ.get('CONFIG_SDK_CACHE_TTL_SECONDS', str(DEFAULT_CACHE_TTL_SECONDS)))


async def _ensure_initialized() -> bool:
    """True if Mongo/Beanie is ready to use - see this module's own
    docstring on why a failed connection isn't retried per-call."""
    global _initialized, _unavailable
    if _initialized:
        return True
    if _unavailable:
        return False
    async with _init_lock:
        if _initialized:
            return True
        if _unavailable:
            return False
        try:
            client = AsyncMongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
            await client.admin.command('ping')
            await init_beanie(database=client[MONGO_DB_NAME], document_models=[AgentConfig])
            _initialized = True
            logger.info(f'Config SDK connected to MongoDB at {MONGO_URL!r}, db {MONGO_DB_NAME!r}')
            return True
        except Exception as e:
            _unavailable = True
            logger.warning(f'Config SDK could not reach MongoDB ({MONGO_URL!r}) - falling back to local defaults: {e}')
            return False


async def _get_agent_doc(agent_id: str) -> Optional[AgentConfig]:
    now = time.time()
    cached = _cache.get(agent_id)
    if cached is not None and (now - _cache_at.get(agent_id, 0)) < _ttl_seconds():
        return cached
    if not await _ensure_initialized():
        return cached  # stale-but-present beats nothing, if Mongo just went down transiently
    try:
        doc = await AgentConfig.find_one(AgentConfig.agent_id == agent_id)
    except Exception as e:
        logger.warning(f'Config SDK read failed for {agent_id!r}, using cache/defaults: {e}')
        return cached
    if doc is not None:
        _cache[agent_id] = doc
        _cache_at[agent_id] = now
    return doc


async def _upsert(
    agent_id: str, *, stage: Optional[Tuple[str, Dict[str, Any]]] = None, constant: Optional[Tuple[str, Any]] = None,
) -> Optional[AgentConfig]:
    """stage=(name, value) or constant=(key, value), exactly one. Returns
    the updated document, or None if Mongo is unavailable or the write
    itself failed - callers treat either the same way (write didn't stick,
    keep using the in-memory default this call started from)."""
    if not await _ensure_initialized():
        return None
    try:
        doc = await AgentConfig.find_one(AgentConfig.agent_id == agent_id)
        if doc is None:
            doc = AgentConfig(agent_id=agent_id)
        if stage is not None:
            doc.stages[stage[0]] = stage[1]
        if constant is not None:
            doc.constants[constant[0]] = constant[1]
        await (doc.insert() if doc.id is None else doc.save())
        _cache[agent_id] = doc
        _cache_at[agent_id] = time.time()
        return doc
    except Exception as e:
        logger.warning(f'Config SDK write failed for {agent_id!r}: {e}')
        return None


async def get_stage_config(agent_id: str, stage_name: str, default: Dict[str, Any]) -> Dict[str, Any]:
    """{model, temperature, timeout, ...} for one agent's one pipeline
    stage. Returns and auto-seeds `default` the first time this stage is
    asked for and Mongo has nothing on file yet."""
    doc = await _get_agent_doc(agent_id)
    if doc is not None and stage_name in doc.stages:
        return doc.stages[stage_name]
    await _upsert(agent_id, stage=(stage_name, default))
    return default


async def get_constant(agent_id: str, key: str, default: Any) -> Any:
    """Any other hardcoded constant or prompt template - same
    auto-seed-on-first-miss behavior as get_stage_config()."""
    doc = await _get_agent_doc(agent_id)
    if doc is not None and key in doc.constants:
        return doc.constants[key]
    await _upsert(agent_id, constant=(key, default))
    return default


async def load_stage_configs(agent_id: str, defaults: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{stage_name: {model, temperature, timeout}} for every stage in
    `defaults` at once - convenience wrapper for a caller (e.g.
    handle_message.py) that currently does one load_runtime_config() call
    and wants a drop-in async replacement rather than one get_stage_config()
    call per stage."""
    return {name: await get_stage_config(agent_id, name, value) for name, value in defaults.items()}


async def set_stage_config(agent_id: str, stage_name: str, value: Dict[str, Any]) -> bool:
    """Explicit write, for the (future) WhatsApp/dashboard edit path - True
    on success. Unlike get_stage_config()'s auto-seed, this always
    overwrites, since it's a deliberate update, not a first-read default."""
    return await _upsert(agent_id, stage=(stage_name, value)) is not None


async def set_constant(agent_id: str, key: str, value: Any) -> bool:
    return await _upsert(agent_id, constant=(key, value)) is not None
