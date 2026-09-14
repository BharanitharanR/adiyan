#!/usr/bin/env python3
"""
Factory reset - wipes every piece of accumulated state so the deployment
comes back up as if Adiyan had just been installed: RAG (the knowledge
base and every ingested book's page), every registered client, the
long-term memory bank (mem0), AdiyanReader's reading-job progress,
Scheduler's jobs, micro_habits entries, and every agent's dashboard-edited
config (constants/stages revert to their seeded defaults - see
mesh/lib/config_sdk.py's own auto-seed behavior). A genuinely irreversible,
owner-driven action, same category as mesh/tools/set_default_model.py but
with real user data at stake instead of a config default, so this one
requires typed confirmation and never runs unattended.

What this wipes (Qdrant collections, all under QDRANT_URL):
  - adiyan_knowledge_base      (uploaded documents - RAG)
  - adiyan_book_pages          (ingested books for AdiyanReader - RAG)
  - adiyan_conversation_memory (mem0's long-term memory bank)
  - adiyan_coaching_memory     (if present - a second mem0-style store)

And (MongoDB, db 'adiyan'):
  - orchestrator_clients       (every registered client - deregisters everyone)
  - adiyan_reader_jobs         (which page each phone number is up to)
  - adiyan_reader_questions    (next-day comprehension quizzes)
  - scheduler_jobs             (every recurring/one-shot routine)
  - micro_habit_entries        (every logged habit entry)
  - agent_config               (every dashboard-edited constant/stage
                                 setting, for every agent - config_sdk
                                 re-seeds its own defaults on next read,
                                 same as a brand-new agent that's never
                                 been configured yet)
  - cron_trigger_jobs          (APScheduler's own one-shot fire timers -
                                 confirmed live this session that these
                                 reference reading_job_id directly, e.g. a
                                 book's next scheduled page read; wiping
                                 adiyan_reader_jobs above without also
                                 wiping this would leave stale timers set
                                 to fire against reading jobs that no
                                 longer exist)

And, on disk (~/.Adiyan/agents/memory/):
  - The memory/kb_documents raw-file store and its state.db's kb_documents
    index row-for-row - the knowledge base's OWN raw-upload side-store,
    left behind by the Qdrant wipe above alone (confirmed: deleting
    adiyan_knowledge_base leaves this table's rows pointing at vectors
    that no longer exist, and the original files still sitting on disk -
    not actually a clean slate, just an inconsistent one).

What this deliberately does NOT touch:
  - Short-term chat history (mesh/lib/chat_cache.py) - already purely
    in-process, cleared by restarting orchestrator, nothing to wipe here.
  - Which agents are installed/enabled (MongoDB's run_the_agent
    collection, config_sdk.py's RunnableAgent) - that's deployment
    topology (what mesh/start_all.sh launches), not user data or config
    values; a factory reset returns settings to defaults, it doesn't
    uninstall agents.
  - The owner's own access - is_owner() checks the connected WhatsApp
    session's phone number directly, never orchestrator_clients, so
    deregistering every client never locks the owner out.

Run from the repo root:
    python3 -m mesh.tools.nuke_memory
"""
import asyncio
import shutil
import sqlite3
import sys

from pymongo import MongoClient
from qdrant_client import QdrantClient

from mesh.lib.paths import kb_documents_dir, state_db_path
from mesh.memory.constants import QDRANT_URL
from mesh.orchestrator.db import COLLECTION_NAME as CLIENTS_COLLECTION, MONGO_DB_NAME, MONGO_URL

QDRANT_COLLECTIONS_TO_WIPE = [
    'adiyan_knowledge_base',
    'adiyan_book_pages',
    'adiyan_conversation_memory',
    'adiyan_coaching_memory',
]

# (collection name, one-line label for the confirmation summary)
MONGO_COLLECTIONS_TO_WIPE = [
    (CLIENTS_COLLECTION, 'registered client(s)'),
    ('adiyan_reader_jobs', 'reading-job(s)'),
    ('adiyan_reader_questions', 'pending comprehension quiz question(s)'),
    ('scheduler_jobs', 'scheduled routine(s)'),
    ('micro_habit_entries', 'logged micro-habit entrie(s)'),
    ('agent_config', "agent(s)' dashboard-edited config"),
    ('cron_trigger_jobs', 'scheduled one-shot fire timer(s)'),
]

CONFIRM_PHRASE = 'NUKE MY DATA'


async def main() -> None:
    qdrant = QdrantClient(url=QDRANT_URL)
    existing_collections = {c.name for c in qdrant.get_collections().collections}
    qdrant_targets = [name for name in QDRANT_COLLECTIONS_TO_WIPE if name in existing_collections]

    mongo = MongoClient(MONGO_URL)
    mongo_counts = {name: mongo[MONGO_DB_NAME][name].count_documents({}) for name, _ in MONGO_COLLECTIONS_TO_WIPE}

    kb_state_db = state_db_path('memory')
    kb_files_dir = kb_documents_dir('memory')
    kb_row_count = 0
    if kb_state_db.exists():
        conn = sqlite3.connect(kb_state_db)
        try:
            kb_row_count = conn.execute('SELECT COUNT(*) FROM kb_documents').fetchone()[0]
        except sqlite3.OperationalError:
            kb_row_count = 0  # table doesn't exist yet - nothing to wipe
        conn.close()

    print('This will PERMANENTLY delete:')
    for name in qdrant_targets:
        try:
            count = qdrant.count(collection_name=name).count
        except Exception:
            count = '?'
        print(f'  - Qdrant collection {name!r} ({count} points)')
    for name in QDRANT_COLLECTIONS_TO_WIPE:
        if name not in existing_collections:
            print(f'  - Qdrant collection {name!r} - already absent, nothing to do')
    for name, label in MONGO_COLLECTIONS_TO_WIPE:
        print(f'  - {mongo_counts[name]} {label} in MongoDB {MONGO_DB_NAME}.{name}')
    print(f'  - {kb_row_count} raw-file index row(s) and their files under {kb_files_dir}')
    print()
    print("This does NOT touch: which agents are installed/enabled, short-term chat history,")
    print("or the owner's own access (is_owner() never depends on the clients table).")
    print()
    print('This is a full factory reset - the deployment comes back up as if freshly installed.')
    print('This cannot be undone.')
    print()

    typed = input(f'Type exactly "{CONFIRM_PHRASE}" to proceed, anything else to abort: ')
    if typed != CONFIRM_PHRASE:
        print('Aborted - nothing was deleted.')
        return

    print()
    for name in qdrant_targets:
        qdrant.delete_collection(collection_name=name)
        print(f'  deleted Qdrant collection {name!r}')

    for name, label in MONGO_COLLECTIONS_TO_WIPE:
        result = mongo[MONGO_DB_NAME][name].delete_many({})
        print(f'  wiped {result.deleted_count} {label} from {name!r}')

    if kb_state_db.exists():
        conn = sqlite3.connect(kb_state_db)
        try:
            conn.execute('DELETE FROM kb_documents')
            conn.commit()
        except sqlite3.OperationalError:
            pass
        conn.close()
    if kb_files_dir.exists():
        shutil.rmtree(kb_files_dir)
        kb_files_dir.mkdir(parents=True, exist_ok=True)
    print(f'  cleared {kb_row_count} raw-file index row(s) and their files under {kb_files_dir}')

    print()
    print('Done. RAG, memory bank, client registrations, reading-job progress, scheduler jobs,')
    print('micro_habits, and every dashboard-edited config value are wiped - a clean slate.')
    print('Restart the whole mesh so no process holds a stale in-memory reference to anything wiped:')
    print('  ./mesh/start_all.sh restart')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nAborted - nothing was deleted.')
        sys.exit(1)
