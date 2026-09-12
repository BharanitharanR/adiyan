"""
Phase 2 of the memory-graph rebuild: the actual memory schema, built only
on mesh/lib/graph_client.py's generic primitives (create_node/get_node/
create_relationship/run_query) - this is the first file in the mesh that
knows what a "Fact" or "Identity" is. Node/relationship shape mirrors the
design reviewed in memory-graph-model.html this session:

  (:Fact)-[:ABOUT]->(:Identity)        who/what the fact concerns
  (:Fact)-[:STATED_BY]->(:Identity)    who told Adiyan this
  (:Fact)-[:VISIBLE_TO]->(:Identity)   who is allowed to be told this back
  (:Fact)-[:SUPERSEDES]->(:Fact)       a correction replacing an older fact
  (:Fact)-[:SOURCED_FROM]->(:Document) a fact extracted from an uploaded file

`about` and `visible_to` are deliberately independent (see the artifact's
"Priya B hasn't paid fees" example) - a fact can be about someone without
ever being shown to them.

A "global" fact (visible to every registered contact, e.g. "office hours
are 9-6") is just a VISIBLE_TO edge to one sentinel Identity node with
key='global' - not a separate code path, so facts_visible_to() below never
needs an `if tier == 'global'` branch.

Every identity referenced here (about/stated_by/visible_to) is a stable
identity key (mesh/orchestrator/db.py's resolve_identity_key output), never
a raw display name - same discipline that module already enforces, now
carried into the graph.

Tested in isolation from every agent, orchestrator, and mesh/memory/ - see
test_graph_store.py, which only imports this module and graph_client.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from neo4j import Driver

from mesh.lib import graph_client

GLOBAL_IDENTITY_KEY = 'global'


def upsert_identity(driver: Driver, key: str, display_name: Optional[str] = None) -> None:
    """Idempotent - calling this for an identity that already exists just
    refreshes display_name (a contact's WhatsApp pushName can change),
    never creates a duplicate node. MERGE, not graph_client.create_node -
    this is the one place in the mesh that needs "create if absent," which
    Phase 1 deliberately left out of the generic primitives (see that
    module's own docstring: CREATE only, no opinion on identity)."""
    graph_client.run_query(
        driver,
        'MERGE (i:Identity {key: $key}) '
        'SET i.display_name = coalesce($display_name, i.display_name)',
        key=key, display_name=display_name,
    )


def record_fact(
    driver: Driver,
    text: str,
    about: Optional[str] = None,
    stated_by: Optional[str] = None,
    visible_to: Sequence[str] = (),
    supersedes: Optional[str] = None,
    source_document: Optional[str] = None,
) -> str:
    """Writes one Fact node plus every relationship the caller supplied.
    `about`/`stated_by`/`visible_to`/`supersedes` take identity keys (or,
    for supersedes, a fact id) - not display names.

    Every identity referenced here must already exist (upsert_identity
    first) - record_fact deliberately does not auto-create identities, so
    a typo'd identity key fails loudly (zero rows matched on the MATCH
    clause, relationship silently not created) rather than growing a
    phantom node. Callers should upsert_identity for every key they pass
    before calling this.

    Returns the new fact's id (a plain uuid, not Neo4j's internal element
    id - stable across process/database restarts, unlike element ids)."""
    fact_id = str(uuid.uuid4())
    graph_client.create_node(driver, 'Fact', {
        'id': fact_id,
        'text': text,
        'created_at': datetime.now(timezone.utc).isoformat(),
    })
    if about:
        graph_client.create_relationship(
            driver, 'Fact', {'id': fact_id}, 'ABOUT', 'Identity', {'key': about},
        )
    if stated_by:
        graph_client.create_relationship(
            driver, 'Fact', {'id': fact_id}, 'STATED_BY', 'Identity', {'key': stated_by},
        )
    for viewer_key in visible_to:
        graph_client.create_relationship(
            driver, 'Fact', {'id': fact_id}, 'VISIBLE_TO', 'Identity', {'key': viewer_key},
        )
    if supersedes:
        graph_client.create_relationship(
            driver, 'Fact', {'id': fact_id}, 'SUPERSEDES', 'Fact', {'id': supersedes},
        )
    if source_document:
        graph_client.create_relationship(
            driver, 'Fact', {'id': fact_id}, 'SOURCED_FROM', 'Document', {'source_filename': source_document},
        )
    return fact_id


def record_document(driver: Driver, source_filename: str, user_context: Optional[str] = None) -> None:
    """One Document node per uploaded file - source_filename is the natural
    key (matches memory_index.py's own kb_documents table today), so
    re-ingesting the same filename updates user_context rather than
    duplicating the node."""
    graph_client.run_query(
        driver,
        'MERGE (d:Document {source_filename: $source_filename}) '
        'SET d.user_context = coalesce($user_context, d.user_context)',
        source_filename=source_filename, user_context=user_context,
    )


def facts_visible_to(driver: Driver, identity_key: str, include_superseded: bool = False) -> List[Dict[str, Any]]:
    """Every fact this identity can see - visible_to this exact identity,
    OR visible_to the 'global' sentinel. Superseded facts (something newer
    points SUPERSEDES at them) are excluded by default - include_superseded
    exists only for debugging/history views, never the live recall path."""
    superseded_clause = '' if include_superseded else (
        'AND NOT EXISTS { MATCH (:Fact)-[:SUPERSEDES]->(f) } '
    )
    return graph_client.run_query(
        driver,
        'MATCH (f:Fact)-[:VISIBLE_TO]->(v:Identity) '
        'WHERE v.key IN [$identity_key, $global_key] '
        + superseded_clause +
        'RETURN DISTINCT f.id AS id, f.text AS text, f.created_at AS created_at '
        'ORDER BY f.created_at',
        identity_key=identity_key, global_key=GLOBAL_IDENTITY_KEY,
    )
