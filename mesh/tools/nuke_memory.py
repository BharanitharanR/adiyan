#!/usr/bin/env python3
"""
Wipes RAG (the knowledge base and every ingested book's pages), every
registered client, and the long-term memory bank (mem0) back to a clean
slate - a genuinely irreversible, owner-driven action, same category as
mesh/tools/set_default_model.py but with real user data at stake instead
of a config default, so this one requires typed confirmation and never
runs unattended.

What this wipes (Qdrant collections, all under QDRANT_URL):
  - adiyan_knowledge_base      (uploaded documents - RAG)
  - adiyan_book_pages          (ingested books for AdiyanReader - RAG)
  - adiyan_conversation_memory (mem0's long-term memory bank)
  - adiyan_coaching_memory     (if present - a second mem0-style store)

And (MongoDB, db 'adiyan'):
  - orchestrator_clients       (every registered client - deregisters everyone)

What this deliberately does NOT touch:
  - Short-term chat history (mesh/lib/chat_cache.py) - already purely
    in-process, cleared by restarting orchestrator, nothing to wipe here.
  - AdiyanReader's own reading-job progress (adiyan_reader_jobs/
    adiyan_reader_questions in Mongo) - a book's ingested pages ARE
    wiped (it's RAG), but which page a job was up to is progress
    tracking, not RAG/memory - ask for a separate tool if you want that
    reset too.
  - Scheduler jobs, micro_habits entries, dashboard config (config_sdk's
    own agent_config collection) - none of these are RAG, clients, or
    memory.
  - The owner's own access - is_owner() checks the connected WhatsApp
    session's phone number directly, never orchestrator_clients, so
    deregistering every client never locks the owner out.

Run from the repo root:
    python3 -m mesh.tools.nuke_memory
"""
import asyncio
import sys

from pymongo import MongoClient
from qdrant_client import QdrantClient

from mesh.memory.constants import QDRANT_URL
from mesh.orchestrator.db import COLLECTION_NAME as CLIENTS_COLLECTION, MONGO_DB_NAME, MONGO_URL

QDRANT_COLLECTIONS_TO_WIPE = [
    'adiyan_knowledge_base',
    'adiyan_book_pages',
    'adiyan_conversation_memory',
    'adiyan_coaching_memory',
]

CONFIRM_PHRASE = 'NUKE MY DATA'


async def main() -> None:
    qdrant = QdrantClient(url=QDRANT_URL)
    existing_collections = {c.name for c in qdrant.get_collections().collections}
    targets = [name for name in QDRANT_COLLECTIONS_TO_WIPE if name in existing_collections]

    mongo = MongoClient(MONGO_URL)
    clients_count = mongo[MONGO_DB_NAME][CLIENTS_COLLECTION].count_documents({})

    print('This will PERMANENTLY delete:')
    for name in targets:
        try:
            count = qdrant.count(collection_name=name).count
        except Exception:
            count = '?'
        print(f'  - Qdrant collection {name!r} ({count} points)')
    for name in QDRANT_COLLECTIONS_TO_WIPE:
        if name not in existing_collections:
            print(f'  - Qdrant collection {name!r} - already absent, nothing to do')
    print(f'  - {clients_count} registered client(s) in MongoDB {MONGO_DB_NAME}.{CLIENTS_COLLECTION}')
    print()
    print('This does NOT touch: reading-job progress, scheduler jobs, micro_habits, dashboard config,')
    print('or the owner\'s own access (is_owner() never depends on the clients table).')
    print()
    print('This cannot be undone.')
    print()

    typed = input(f'Type exactly "{CONFIRM_PHRASE}" to proceed, anything else to abort: ')
    if typed != CONFIRM_PHRASE:
        print('Aborted - nothing was deleted.')
        return

    print()
    for name in targets:
        qdrant.delete_collection(collection_name=name)
        print(f'  deleted Qdrant collection {name!r}')

    result = mongo[MONGO_DB_NAME][CLIENTS_COLLECTION].delete_many({})
    print(f'  deregistered {result.deleted_count} client(s)')

    print()
    print('Done. RAG, memory bank, and client registrations are wiped.')
    print('Restart the mesh (or at least memory/orchestrator) so no process holds a stale in-memory reference:')
    print('  ./mesh/start_all.sh restart memory orchestrator')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nAborted - nothing was deleted.')
        sys.exit(1)
