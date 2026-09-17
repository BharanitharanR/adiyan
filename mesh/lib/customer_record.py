"""
Per-vertical customer records - one JSON envelope per (vertical_id,
identity_key), the durable answer to "does Adiyan actually have a record of
this customer" that mem0/the memory graph don't reliably give (both are
fact-extraction stores that can legitimately write nothing for a turn when
nothing new was "learned" - see this session's own live-verified finding).

Deliberately schemaless on the platform side - a business's own fields
(subscription plan, order status, payment history, whatever a coach vs a
restaurant vs a realtor actually needs to track) are never fixed by this
module. Each vertical decides what belongs in `customer_section`/
`owner_section`; the platform only guarantees the envelope and the
customer/owner write-authority split:

  {
    vertical_id, identity_key, created_at, updated_at,
    customer_section: {...},  # LLM-inferred from ordinary conversation
    owner_section: {...},     # written ONLY by an explicit owner command
  }

customer_section is freely additive - low-risk, since it only ever reflects
what the customer themselves said. owner_section is the one place a
consequential fact (payment confirmed, active/inactive) can live, and it is
never injected into a customer-facing prompt (see customer_record_hook.py) -
a customer can never read it back, let alone talk it into a false state the
way they could an LLM-inferred field.

Own Mongo/Beanie connection, not routed through config_sdk.py - this is
customer data, not agent configuration, and every other cross-cutting store
in this mesh (mem0_backend.py, graph_client.py, scheduler/db.py) already
manages its own connection rather than funneling through config_sdk's
deliberately-private schema. Same graceful-degradation contract as
config_sdk.py though: a down Mongo must never take an agent's reply down
with it, so every read/write here fails soft.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from beanie import Document, init_beanie
from pydantic import Field
from pymongo import AsyncMongoClient

logger = logging.getLogger('CustomerRecord')

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB', 'adiyan_config')


class CustomerRecord(Document):
    vertical_id: str
    identity_key: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    customer_section: Dict[str, Any] = Field(default_factory=dict)
    owner_section: Dict[str, Any] = Field(default_factory=dict)

    class Settings:
        name = 'customer_records'


_init_lock = asyncio.Lock()
_initialized = False
_unavailable = False
_initialized_loop: Optional[Any] = None


async def _ensure_initialized() -> bool:
    """Same per-loop re-init reasoning as config_sdk.py's own
    _ensure_initialized() - a process that calls in from more than one
    event loop over its lifetime needs a fresh client per loop, not a
    cached one from whichever loop happened to initialize first."""
    global _initialized, _unavailable, _initialized_loop
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
            await init_beanie(database=client[MONGO_DB_NAME], document_models=[CustomerRecord])
            _initialized = True
            _initialized_loop = current_loop
            logger.info(f'CustomerRecord connected to MongoDB at {MONGO_URL!r}, db {MONGO_DB_NAME!r}')
            return True
        except Exception as e:
            _unavailable = True
            logger.warning(f'CustomerRecord could not reach MongoDB ({MONGO_URL!r}): {e}')
            return False


async def get_or_create(vertical_id: str, identity_key: str) -> Optional[CustomerRecord]:
    """The record for this (vertical, customer), creating a blank one on
    first contact - same upsert-on-first-touch shape as memory_hook's own
    ensure_identity(). None only when Mongo is unreachable; callers treat
    that as "nothing to inject/update this turn," never as an error to
    surface to the sender."""
    if not await _ensure_initialized():
        return None
    try:
        record = await CustomerRecord.find_one(
            CustomerRecord.vertical_id == vertical_id, CustomerRecord.identity_key == identity_key,
        )
        if record is None:
            record = CustomerRecord(vertical_id=vertical_id, identity_key=identity_key)
            await record.insert()
        return record
    except Exception as e:
        logger.warning(f'CustomerRecord read/create failed for ({vertical_id!r}, {identity_key!r}): {e}')
        return None


async def merge_customer_section(vertical_id: str, identity_key: str, updates: Dict[str, Any]) -> bool:
    """Shallow-merges `updates` into customer_section - additive, never
    destructive (an empty/missing field in `updates` leaves the existing
    value alone, it does not clear it). This is the ONLY write path
    intended for LLM-inferred content - see this module's own docstring on
    why owner_section never goes through here."""
    if not updates:
        return True
    if not await _ensure_initialized():
        return False
    try:
        record = await get_or_create(vertical_id, identity_key)
        if record is None:
            return False
        record.customer_section.update(updates)
        record.updated_at = datetime.now(timezone.utc)
        await record.save()
        return True
    except Exception as e:
        logger.warning(f'CustomerRecord customer_section merge failed for ({vertical_id!r}, {identity_key!r}): {e}')
        return False


_MAX_NOTES = 20


async def append_customer_note(vertical_id: str, identity_key: str, note: str) -> bool:
    """Appends one free-text note to customer_section['notes'] - the one
    LLM-inferred field this module ships with by default (every other
    customer_section field is business-defined and merged via
    merge_customer_section instead). List-append, not merge_customer_section's
    shallow dict update, since a plain .update() would silently replace the
    whole notes list with a single-element one every time. Capped at
    _MAX_NOTES (oldest dropped first) - same reasoning
    customer_record_hook.py's own _MAX_SECTION_CHARS caps injection size,
    just at write time instead of read time, so a long-lived customer's
    record doesn't grow the Mongo document without bound either."""
    if not note or not note.strip():
        return True
    if not await _ensure_initialized():
        return False
    try:
        record = await get_or_create(vertical_id, identity_key)
        if record is None:
            return False
        notes = record.customer_section.get('notes')
        if not isinstance(notes, list):
            notes = []
        notes.append(note.strip())
        record.customer_section['notes'] = notes[-_MAX_NOTES:]
        record.updated_at = datetime.now(timezone.utc)
        await record.save()
        return True
    except Exception as e:
        logger.warning(f'CustomerRecord note append failed for ({vertical_id!r}, {identity_key!r}): {e}')
        return False


async def merge_owner_section(vertical_id: str, identity_key: str, updates: Dict[str, Any]) -> bool:
    """Shallow-merges `updates` into owner_section. Callers MUST have
    already verified the caller is the owner (mesh/lib/permissions.py's
    tier check) before reaching this - this function itself has no
    authority check, the same "gate before, not inside, the storage layer"
    convention config_sdk.py's own writes follow."""
    if not updates:
        return True
    if not await _ensure_initialized():
        return False
    try:
        record = await get_or_create(vertical_id, identity_key)
        if record is None:
            return False
        record.owner_section.update(updates)
        record.updated_at = datetime.now(timezone.utc)
        await record.save()
        return True
    except Exception as e:
        logger.warning(f'CustomerRecord owner_section merge failed for ({vertical_id!r}, {identity_key!r}): {e}')
        return False
