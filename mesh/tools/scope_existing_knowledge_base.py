#!/usr/bin/env python3
"""
One-off migration for the knowledge-base access-scoping fix (see
mesh/memory/memory_index.py's own module docstring for the confirmed-live
leak this closes: an unscoped search surfaced one registered client's
Aadhaar card to a different requester's question). Every document ingested
BEFORE that fix has no 'visibility'/'owner_identity' in its Qdrant chunk
payload or its kb_documents row - this tags every one of them, so scoped
reads (retrieve_knowledge_base, get_document_text, list_documents, ...)
have something real to filter on instead of silently treating an
unmigrated chunk as invisible to everyone but the owner.

Sets every existing document to visibility='private', owner_identity=OWNER_
IDENTITY (the owner's own explicit access, not any client's) - the owner's
own is_owner=True bypass already sees everything regardless of this value,
so what matters is that no *client* identity accidentally matches it. This
was the owner's own deliberate choice (offered three options: scope by the
existing per-document 'username' metadata, default everything to global, or
this one) specifically because the original 'username' tag was never
verified as a reliable/complete access boundary - collapsing everything to
owner-only first is the safe default; individual documents can be
re-marked 'global' or reassigned to a specific client's identity by hand
afterward if that's ever wanted.

Non-destructive (only sets payload/column fields, deletes nothing), but
still asks for typed confirmation - it silently changes what's searchable
by non-owner requesters going forward, from "everything" to "nothing until
re-tagged," which is a real behavior change worth a deliberate go-ahead
rather than running unattended.

Run from the repo root:
    python3 -m mesh.tools.scope_existing_knowledge_base
"""
import asyncio
import sqlite3
import sys

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, IsEmptyCondition, PayloadField

from mesh.memory.constants import AGENT_ID, QDRANT_URL
from mesh.memory.memory_index import KB_COLLECTION_NAME, _ensure_scope_columns
from mesh.lib.paths import state_db_path

CONFIRM_PHRASE = 'SCOPE MY DOCUMENTS'
OWNER_IDENTITY = 'owner'


def _unscoped_chunk_filter() -> Filter:
    # A chunk migrated in an earlier (interrupted) run already has
    # 'visibility' set - re-running this tool is then a safe no-op instead
    # of re-stamping everything, so it can be re-run after a partial
    # failure without needing to track progress itself.
    #
    # IsEmptyCondition, not IsNullCondition - confirmed live against the
    # real collection that every pre-existing chunk has NO 'visibility' key
    # in its payload at all (never explicitly set to null), and
    # IsNullCondition only matches a field explicitly present with a null
    # value, not one that's simply absent - it matched 0 of 1,599 real
    # points on the first attempt. IsEmptyCondition matches both "missing
    # entirely" and "present but null," which is the actual condition here.
    return Filter(must=[IsEmptyCondition(is_empty=PayloadField(key='visibility'))])


async def main() -> None:
    qdrant = QdrantClient(url=QDRANT_URL)
    existing_collections = {c.name for c in qdrant.get_collections().collections}
    if KB_COLLECTION_NAME not in existing_collections:
        print(f'Qdrant collection {KB_COLLECTION_NAME!r} does not exist - nothing to migrate.')
        return

    unscoped_filter = _unscoped_chunk_filter()
    unscoped_count = qdrant.count(collection_name=KB_COLLECTION_NAME, count_filter=unscoped_filter).count

    conn = sqlite3.connect(state_db_path(AGENT_ID))
    _ensure_scope_columns(conn)
    doc_rows = conn.execute(
        "SELECT source_filename FROM kb_documents WHERE owner_identity IS NULL"
    ).fetchall()

    print(f'This will tag {unscoped_count} un-scoped chunk(s) in Qdrant collection {KB_COLLECTION_NAME!r}')
    print(f'and {len(doc_rows)} un-scoped row(s) in the kb_documents index')
    print(f"as visibility='private', owner_identity={OWNER_IDENTITY!r} (visible only to the owner)")
    print("- until you explicitly re-mark a specific document 'global' or reassign its owner_identity.")
    print()
    print("This does NOT delete anything - only sets access-scoping fields on already-ingested documents.")
    print()

    if unscoped_count == 0 and not doc_rows:
        print('Nothing un-scoped found - already migrated (or nothing has been ingested yet). Nothing to do.')
        conn.close()
        return

    typed = input(f'Type exactly "{CONFIRM_PHRASE}" to proceed, anything else to abort: ')
    if typed != CONFIRM_PHRASE:
        print('Aborted - nothing was changed.')
        conn.close()
        return

    print()
    if unscoped_count:
        qdrant.set_payload(
            collection_name=KB_COLLECTION_NAME,
            payload={'visibility': 'private', 'owner_identity': OWNER_IDENTITY},
            points=unscoped_filter,
        )
        print(f'  tagged {unscoped_count} chunk(s) in Qdrant')

    if doc_rows:
        conn.execute(
            "UPDATE kb_documents SET visibility = 'private', owner_identity = ? WHERE owner_identity IS NULL",
            (OWNER_IDENTITY,),
        )
        conn.commit()
        print(f'  tagged {len(doc_rows)} row(s) in kb_documents')
    conn.close()

    print()
    print('Done. Every previously-unscoped document is now private to the owner.')
    print('Restart Memory Agent so no process holds a stale cached index reference:')
    print('  ./mesh/start_all.sh restart memory')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nAborted - nothing was changed.')
        sys.exit(1)
