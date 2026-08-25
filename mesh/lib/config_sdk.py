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
teams) eventually sits in front of Mongo, only this module's internals
change to call that service's API instead of Mongo directly. No calling
agent's code changes at all.

Two-layer resolution: every (agent_id, key) has a PLATFORM_VERTICAL default
- the seeded config every platform agent (Orchestrator, Analysis, ...) ships
with - and, optionally, a per-vertical override document keyed by the same
agent_id under a real vertical_id. A business-vertical agent (Adiyan
marketplace - to be introduced) can override a platform agent's own prompt
(e.g. Orchestrator's humanize tone) for its own vertical without touching
what every other deployment sees. Resolution checks the vertical layer
first (if one was asked for), then falls back to platform, exactly like a
normal config-override hierarchy: vertical > platform > caller's own
hardcoded default (which only ever seeds the *platform* layer - a vertical
override is always an explicit write, never implied by a read that missed).

Auto-seeding: the first time any agent asks for a stage/constant that
doesn't exist yet in Mongo, whatever `default` the caller supplied (its own
current local runtime_config.json value, or a hardcoded prompt string) is
written back as the new on-file PLATFORM value - migrating existing
hardcoded config/prompts into Mongo happens automatically, one read at a
time, not via a separate migration script.

Graceful degradation: exactly like mem0_backend.py and registry_client.py -
if Mongo is unreachable, every read falls back to the caller's own default
and every write is silently skipped. A missing/down config store must never
take an agent down; it only means edits made via the dashboard/WhatsApp tool
aren't picked up until Mongo is reachable again. Connection is attempted
once per process - if it fails, every later call fails fast without
retrying (this mesh's existing "restart to recover" convention already
covers coming back, same reasoning router.py's old load-once model had,
just not the part of that model that caused the Orchestrator/Analysis Agent
staleness bug - there's nothing to go stale here, since a fresh read
happens every _ttl_seconds()).
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from beanie import Document, init_beanie
from pydantic import Field
from pymongo import AsyncMongoClient

logger = logging.getLogger('ConfigSDK')

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB', 'adiyan_config')

# The seeded-defaults layer every agent_id always has. A real vertical_id
# (a marketplace agent's own identity) is anything else - see this module's
# own docstring on the two-layer resolution order.
PLATFORM_VERTICAL = 'platform'

# How long a cached agent document is trusted before the next read
# transparently refreshes it from Mongo - configurable, same reasoning as
# registry_client.py's AGENT_REGISTRY_REFRESH_SECONDS.
DEFAULT_CACHE_TTL_SECONDS = 30.0

# Not a real agent_id - a reserved one this module uses to store deployment-
# wide control state (currently just active_vertical_id) in the same
# collection/document shape everything else already uses, rather than a
# second Document class. See get_active_vertical_id()/set_active_vertical_id().
CONTROL_AGENT_ID = '_mesh_control'


class AgentConfig(Document):
    agent_id: str
    vertical_id: str = PLATFORM_VERTICAL
    stages: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    constants: Dict[str, Any] = Field(default_factory=dict)
    # Human-readable "what does this setting do" text, keyed the same as
    # constants - a separate map, not folded into constants itself, so the
    # dashboard's list of editable values never has to filter out its own
    # documentation. Platform-only by design, same as a constant's seeded
    # default - what a field MEANS doesn't vary per vertical the way its
    # VALUE does, so this is never looked up through _resolve_vertical().
    descriptions: Dict[str, str] = Field(default_factory=dict)

    class Settings:
        name = 'agent_config'


_init_lock = asyncio.Lock()
_initialized = False
_unavailable = False
# Which running loop the cached AsyncMongoClient was created on - confirmed
# live this matters: a process that calls into config_sdk from more than
# one event loop over its lifetime (e.g. server.py's own
# asyncio.run(_load_startup_config()) before serve() even starts uvicorn's
# own, separate loop) got 'Cannot use AsyncMongoClient in different event
# loop' on every call from the second loop onward, since _initialized
# short-circuited straight past ever creating a new client for it. Compared
# against asyncio.get_running_loop() on every call now, not just once.
_initialized_loop: Optional[Any] = None

# Cache key is (agent_id, vertical_id) - a platform doc and a vertical
# override for the same agent_id are cached independently. Cleared on
# reinit too - a doc cached by one loop's client isn't safe to keep serving
# once that client is torn down.
_cache: Dict[Tuple[str, str], AgentConfig] = {}
_cache_at: Dict[Tuple[str, str], float] = {}


def _ttl_seconds() -> float:
    return float(os.environ.get('CONFIG_SDK_CACHE_TTL_SECONDS', str(DEFAULT_CACHE_TTL_SECONDS)))


async def _ensure_initialized() -> bool:
    """True if Mongo/Beanie is ready to use on the CURRENT running loop -
    see this module's own docstring on why a failed connection isn't
    retried per-call, and the _initialized_loop comment above on why
    "already initialized" alone isn't sufficient once a process might call
    in from more than one loop."""
    global _initialized, _unavailable, _initialized_loop, _cache, _cache_at
    current_loop = asyncio.get_running_loop()
    if _initialized and _initialized_loop is current_loop:
        return True
    if _unavailable:
        return False
    async with _init_lock:
        if _initialized and _initialized_loop is current_loop:
            return True
        if _unavailable:
            return False
        try:
            client = AsyncMongoClient(MONGO_URL, serverSelectionTimeoutMS=3000)
            await client.admin.command('ping')
            await init_beanie(database=client[MONGO_DB_NAME], document_models=[AgentConfig])
            _initialized = True
            _initialized_loop = current_loop
            _cache = {}
            _cache_at = {}
            logger.info(f'Config SDK connected to MongoDB at {MONGO_URL!r}, db {MONGO_DB_NAME!r}')
            return True
        except Exception as e:
            _unavailable = True
            logger.warning(f'Config SDK could not reach MongoDB ({MONGO_URL!r}) - falling back to local defaults: {e}')
            return False


async def _get_agent_doc(agent_id: str, vertical_id: str) -> Optional[AgentConfig]:
    cache_key = (agent_id, vertical_id)
    now = time.time()
    cached = _cache.get(cache_key)
    if cached is not None and (now - _cache_at.get(cache_key, 0)) < _ttl_seconds():
        return cached
    if not await _ensure_initialized():
        return cached  # stale-but-present beats nothing, if Mongo just went down transiently
    try:
        doc = await AgentConfig.find_one(AgentConfig.agent_id == agent_id, AgentConfig.vertical_id == vertical_id)
    except Exception as e:
        logger.warning(f'Config SDK read failed for {cache_key!r}, using cache/defaults: {e}')
        return cached
    if doc is not None:
        _cache[cache_key] = doc
        _cache_at[cache_key] = now
    return doc


async def _upsert(
    agent_id: str, vertical_id: str, *,
    stage: Optional[Tuple[str, Dict[str, Any]]] = None, constant: Optional[Tuple[str, Any]] = None,
    description: Optional[Tuple[str, str]] = None,
) -> Optional[AgentConfig]:
    """stage=(name, value) or constant=(key, value), exactly one of those
    two - description=(key, text) may accompany either, seeded once (never
    overwrites an existing entry for that key, same "a read that missed
    only ever seeds, doesn't overwrite" rule the value itself follows).
    Returns the updated document, or None if Mongo is unavailable or the
    write itself failed - callers treat either the same way (write didn't
    stick, keep using the in-memory default this call started from)."""
    if not await _ensure_initialized():
        return None
    cache_key = (agent_id, vertical_id)
    try:
        doc = await AgentConfig.find_one(AgentConfig.agent_id == agent_id, AgentConfig.vertical_id == vertical_id)
        if doc is None:
            doc = AgentConfig(agent_id=agent_id, vertical_id=vertical_id)
        if stage is not None:
            doc.stages[stage[0]] = stage[1]
        if constant is not None:
            doc.constants[constant[0]] = constant[1]
        if description is not None and description[0] not in doc.descriptions:
            doc.descriptions[description[0]] = description[1]
        await (doc.insert() if doc.id is None else doc.save())
        _cache[cache_key] = doc
        _cache_at[cache_key] = time.time()
        return doc
    except Exception as e:
        logger.warning(f'Config SDK write failed for {cache_key!r}: {e}')
        return None


async def _resolve_vertical(agent_id: str, vertical_id: Optional[str]) -> Optional[str]:
    """A caller-supplied vertical_id always wins. Otherwise, whatever's
    currently activated deployment-wide (see set_active_vertical_id())
    applies automatically - this is what makes activating a vertical
    change every agent's behavior at once without touching a single
    call site in handle_message.py/analyze.py/etc. Guarded against
    CONTROL_AGENT_ID itself, or get_active_vertical_id()'s own
    get_constant() call would recurse into this forever."""
    if vertical_id:
        return vertical_id
    if agent_id == CONTROL_AGENT_ID:
        return None
    return await get_active_vertical_id()


async def get_stage_config(
    agent_id: str, stage_name: str, default: Dict[str, Any], vertical_id: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """{model, temperature, timeout, ...} for one agent's one pipeline
    stage. The effective vertical (explicit vertical_id, or whatever's
    currently activated deployment-wide - see _resolve_vertical()) is
    checked first - falls back to the platform layer, then to `default`,
    which only ever seeds the *platform* layer (see this module's own
    docstring on why a vertical override is always an explicit write).

    description, if given, is backfilled onto an already-seeded stage that
    predates this parameter existing, not just written alongside a
    brand-new one - a call site that starts passing description= a version
    later than its first deploy still gets it stored, on the very next
    call, not only on a fresh install."""
    effective_vertical = await _resolve_vertical(agent_id, vertical_id)
    if effective_vertical and effective_vertical != PLATFORM_VERTICAL:
        vertical_doc = await _get_agent_doc(agent_id, effective_vertical)
        if vertical_doc is not None and stage_name in vertical_doc.stages:
            return vertical_doc.stages[stage_name]

    platform_doc = await _get_agent_doc(agent_id, PLATFORM_VERTICAL)
    if platform_doc is not None and stage_name in platform_doc.stages:
        if description is not None and stage_name not in platform_doc.descriptions:
            await _upsert(agent_id, PLATFORM_VERTICAL, description=(stage_name, description))
        return platform_doc.stages[stage_name]

    await _upsert(
        agent_id, PLATFORM_VERTICAL, stage=(stage_name, default),
        description=(stage_name, description) if description is not None else None,
    )
    return default


async def get_constant(
    agent_id: str, key: str, default: Any, vertical_id: Optional[str] = None, description: Optional[str] = None,
) -> Any:
    """Any other hardcoded constant or prompt template - same layered
    resolution (effective vertical, explicit or currently activated, then
    platform), auto-seed-on-first-miss behavior, and description-backfill
    as get_stage_config()."""
    effective_vertical = await _resolve_vertical(agent_id, vertical_id)
    if effective_vertical and effective_vertical != PLATFORM_VERTICAL:
        vertical_doc = await _get_agent_doc(agent_id, effective_vertical)
        if vertical_doc is not None and key in vertical_doc.constants:
            return vertical_doc.constants[key]

    platform_doc = await _get_agent_doc(agent_id, PLATFORM_VERTICAL)
    if platform_doc is not None and key in platform_doc.constants:
        if description is not None and key not in platform_doc.descriptions:
            await _upsert(agent_id, PLATFORM_VERTICAL, description=(key, description))
        return platform_doc.constants[key]

    await _upsert(
        agent_id, PLATFORM_VERTICAL, constant=(key, default),
        description=(key, description) if description is not None else None,
    )
    return default


async def get_active_vertical_id() -> Optional[str]:
    """The deployment-wide active vertical, or None if the deployment is
    running plain platform defaults (the default state). Same caching as
    every other read here - a fresh check happens at most every
    _ttl_seconds()."""
    return await get_constant(
        CONTROL_AGENT_ID, 'active_vertical_id', None,
        description='Which business-vertical deployment is active, if any. Empty means every agent runs on plain platform defaults.',
    )


async def set_active_vertical_id(vertical_id: Optional[str]) -> bool:
    """Activates a vertical deployment-wide (every later get_stage_config/
    get_constant call that doesn't pass its own vertical_id now resolves
    against it automatically) - or pass None to deactivate, reverting to
    plain platform defaults. True on success. This is the one write every
    'confirmed by owner or support' activation flow (Config Agent's
    activate_vertical skill, the dashboard's activation control) actually
    calls - see mesh/CONFIG_ARCHITECTURE.md."""
    return await set_constant(CONTROL_AGENT_ID, 'active_vertical_id', vertical_id)


async def load_stage_configs(
    agent_id: str, defaults: Dict[str, Dict[str, Any]], vertical_id: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """{stage_name: {model, temperature, timeout}} for every stage in
    `defaults` at once - convenience wrapper for a caller (e.g.
    handle_message.py) that currently does one load_runtime_config() call
    and wants a drop-in async replacement rather than one get_stage_config()
    call per stage."""
    return {name: await get_stage_config(agent_id, name, value, vertical_id) for name, value in defaults.items()}


async def set_stage_config(
    agent_id: str, stage_name: str, value: Dict[str, Any], vertical_id: str = PLATFORM_VERTICAL,
) -> bool:
    """Explicit write, for the dashboard/WhatsApp edit path - True on
    success. Unlike get_stage_config()'s auto-seed, this always overwrites,
    since it's a deliberate update, not a first-read default. Pass a real
    vertical_id to write a vertical-specific override instead of changing
    the platform default every deployment sees."""
    return await _upsert(agent_id, vertical_id, stage=(stage_name, value)) is not None


async def set_constant(agent_id: str, key: str, value: Any, vertical_id: str = PLATFORM_VERTICAL) -> bool:
    return await _upsert(agent_id, vertical_id, constant=(key, value)) is not None


async def list_agent_ids(vertical_id: str = PLATFORM_VERTICAL) -> List[str]:
    """Every real agent_id with a config document on file for this layer
    (platform by default) - for the Config Agent (WhatsApp/NLP resolution
    against real agent_ids, not guessed ones) and the dashboard's agent
    picker. Excludes CONTROL_AGENT_ID - it holds deployment-wide control
    state, not a real agent's config, and has no business showing up next
    to 'orchestrator'/'analysis' in a picker. Empty if Mongo is unreachable."""
    if not await _ensure_initialized():
        return []
    try:
        docs = await AgentConfig.find(AgentConfig.vertical_id == vertical_id).to_list()
        return [d.agent_id for d in docs if d.agent_id != CONTROL_AGENT_ID]
    except Exception as e:
        logger.warning(f'Config SDK could not list agent ids: {e}')
        return []


async def get_full_config(agent_id: str, vertical_id: str = PLATFORM_VERTICAL) -> Optional[Dict[str, Any]]:
    """{'stages': {...}, 'constants': {...}, 'descriptions': {...}} for one
    agent's one layer (platform by default), or None if it has no config
    document yet. 'descriptions' is keyed the same as constants/stages -
    the dashboard cross-references by key, not by position. Bypasses the
    read cache deliberately - a caller asking for the *entire* config
    (Config Agent answering a question, the dashboard rendering a page)
    wants what's actually on file right now, not a value that might be up
    to _ttl_seconds() stale. Does not merge layers - the dashboard's job is
    to show each layer distinctly (a support person editing "Orchestrator"
    should see the platform default, not a value silently blended with
    some vertical's override), not this module's."""
    if not await _ensure_initialized():
        return None
    try:
        doc = await AgentConfig.find_one(AgentConfig.agent_id == agent_id, AgentConfig.vertical_id == vertical_id)
    except Exception as e:
        logger.warning(f'Config SDK could not read full config for {(agent_id, vertical_id)!r}: {e}')
        return None
    if doc is None:
        return None
    return {'stages': doc.stages, 'constants': doc.constants, 'descriptions': doc.descriptions}


async def list_vertical_ids(agent_id: str) -> List[str]:
    """Every vertical_id that has its own override document for this
    agent_id (excluding the platform layer itself) - lets the dashboard
    show "this agent has N vertical overrides" without needing to know
    real vertical ids in advance."""
    if not await _ensure_initialized():
        return []
    try:
        docs = await AgentConfig.find(AgentConfig.agent_id == agent_id).to_list()
        return [d.vertical_id for d in docs if d.vertical_id != PLATFORM_VERTICAL]
    except Exception as e:
        logger.warning(f'Config SDK could not list vertical ids for {agent_id!r}: {e}')
        return []
