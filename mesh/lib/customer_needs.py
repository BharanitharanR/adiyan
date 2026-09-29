"""
Per-customer "needs" event log - every workflow completion (a booking, an
order, a cancellation) a business's n8n automation actually produced,
searchable afterward.

Deliberately separate from mesh/lib/customer_record.py: that module is a
CURRENT-STATE snapshot per customer, shallow-merged in place, with no way
to represent "three separate bookings happened" without the second
overwriting the first. This module is an APPEND-ONLY log instead - one
entry per workflow completion, grouped by the date it happened, never
overwritten - because mesh/analysis/skills/analyze.py's trigger_workflow
tool was confirmed live this session to write its n8n result NOWHERE: the
booking/order/cancellation confirmation only ever existed in that turn's
chat reply text, so a business owner had no way to ask "show me today's
bookings" afterward - every "created" record evaporated the moment its
confirmation was sent.

Grouped by date (not a flat list) specifically so a business owner's own
"summarize what happened this week" question can walk one key per day
directly, instead of scanning a flat list and re-deriving the grouping
itself every time.

Same Mongo/Beanie conventions as customer_record.py: own connection, fails
soft (a down Mongo must never take a customer's reply down with it), one
document per (vertical_id, identity_key).
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from beanie import Document, init_beanie
from pydantic import Field
from pymongo import AsyncMongoClient

logger = logging.getLogger('CustomerNeeds')

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB', 'adiyan_config')


class CustomerNeed(Document):
    vertical_id: str
    identity_key: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # 'YYYY-MM-DD' -> list of {time, workflow_name, request, result},
    # oldest entry first within a date - never a flat list, so listing or
    # summarizing by day never needs to re-derive the grouping.
    notes_by_date: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)

    class Settings:
        name = 'customer_needs'


_init_lock = asyncio.Lock()
_initialized = False
_unavailable = False
_initialized_loop: Optional[Any] = None


async def _ensure_initialized() -> bool:
    """Same per-loop re-init reasoning as customer_record.py's own copy -
    a process that calls in from more than one event loop over its
    lifetime needs a fresh client per loop, not a cached one from whichever
    loop happened to initialize first."""
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
            await init_beanie(database=client[MONGO_DB_NAME], document_models=[CustomerNeed])
            _initialized = True
            _initialized_loop = current_loop
            logger.info(f'CustomerNeeds connected to MongoDB at {MONGO_URL!r}, db {MONGO_DB_NAME!r}')
            return True
        except Exception as e:
            _unavailable = True
            logger.warning(f'CustomerNeeds could not reach MongoDB ({MONGO_URL!r}): {e}')
            return False


async def _get_or_create(vertical_id: str, identity_key: str) -> Optional[CustomerNeed]:
    if not await _ensure_initialized():
        return None
    try:
        record = await CustomerNeed.find_one(
            CustomerNeed.vertical_id == vertical_id, CustomerNeed.identity_key == identity_key,
        )
        if record is None:
            record = CustomerNeed(vertical_id=vertical_id, identity_key=identity_key)
            await record.insert()
        return record
    except Exception as e:
        logger.warning(f'CustomerNeed read/create failed for ({vertical_id!r}, {identity_key!r}): {e}')
        return None


# Cap at write time, same reasoning as customer_record.py's own _MAX_NOTES -
# a long-lived customer/vertical's log must not grow its Mongo document
# without bound.
_MAX_ENTRIES_PER_DATE = 50
_MAX_DATES = 90


async def record_need(
    vertical_id: str, identity_key: str, workflow_name: str, request: str, result: Dict[str, Any],
) -> bool:
    """Appends one workflow-completion entry under today's date key. Called
    from trigger_workflow the moment n8n returns a successful result - this
    is the one write this module ships with; there is no update or delete,
    an entry is a historical fact once it happened."""
    if not await _ensure_initialized():
        return False
    try:
        record = await _get_or_create(vertical_id, identity_key)
        if record is None:
            return False
        now = datetime.now(timezone.utc)
        date_key = now.date().isoformat()
        entries = record.notes_by_date.get(date_key)
        if not isinstance(entries, list):
            entries = []
        entries.append({
            'time': now.isoformat(), 'workflow_name': workflow_name, 'request': request, 'result': result,
        })
        record.notes_by_date[date_key] = entries[-_MAX_ENTRIES_PER_DATE:]
        if len(record.notes_by_date) > _MAX_DATES:
            oldest_first = sorted(record.notes_by_date)
            for old_key in oldest_first[:len(record.notes_by_date) - _MAX_DATES]:
                del record.notes_by_date[old_key]
        record.updated_at = now
        await record.save()
        return True
    except Exception as e:
        logger.warning(f'CustomerNeed record_need failed for ({vertical_id!r}, {identity_key!r}): {e}')
        return False


async def list_needs(
    vertical_id: str, identity_key: Optional[str] = None, date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Every entry matching this vertical (optionally scoped to one
    customer, optionally to one date), sorted oldest-first by date then by
    time within a date - the shape a "summarize this week" or "show me
    today's bookings" owner question needs to walk directly, each entry
    carrying its own identity_key and date so the caller never has to
    re-derive either."""
    if not await _ensure_initialized():
        return []
    try:
        if identity_key:
            records = await CustomerNeed.find(
                CustomerNeed.vertical_id == vertical_id, CustomerNeed.identity_key == identity_key,
            ).to_list()
        else:
            records = await CustomerNeed.find(CustomerNeed.vertical_id == vertical_id).to_list()
    except Exception as e:
        logger.warning(f'CustomerNeed list_needs failed for {vertical_id!r}: {e}')
        return []

    out: List[Dict[str, Any]] = []
    for record in records:
        for date_key, entries in record.notes_by_date.items():
            if date and date_key != date:
                continue
            for entry in entries:
                out.append({'identity_key': record.identity_key, 'date': date_key, **entry})
    out.sort(key=lambda e: (e['date'], e.get('time', '')))
    return out
