"""
Thin wrapper around the Neo4j Python driver - the only file in the mesh
that talks Bolt/Cypher directly. Phase 1 of the memory-graph rebuild (see
the design brainstorm this session): a bare graph-primitives client with
zero memory-domain concepts (no Fact/Identity/visible_to anywhere here) so
it can be tested in complete isolation from mesh/memory/ and from every
agent - the same separation mesh/lib/config_sdk.py already keeps from any
one agent's own domain data.

Storage: Neo4j Community (`brew install neo4j`), started the same way
Mongo/Qdrant are - a real background process this script assumes is
already running (see mesh/start_all.sh's own component list for that
wiring, added in a later phase), not something this module starts itself.

Auth stays ON (unlike Mongo's local no-auth default) - Neo4j ships secure-
by-default and disabling it means editing neo4j.conf outside this repo, a
change with no test coverage of its own. Credentials come from env vars
with the one local dev password as the fallback, same pattern
ADIYAN_MONGO_URL already uses for its own default.
"""
import os
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase, Driver

NEO4J_URI = os.environ.get('ADIYAN_NEO4J_URI', 'bolt://localhost:7687')
NEO4J_USER = os.environ.get('ADIYAN_NEO4J_USER', 'neo4j')
NEO4J_PASSWORD = os.environ.get('ADIYAN_NEO4J_PASSWORD', 'adiyan-graph-dev')

_driver: Optional[Driver] = None


def connect() -> Driver:
    """Returns the shared driver, creating it once. The driver itself pools
    connections internally - callers open a session per operation, not a
    driver per operation (see the functions below)."""
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver


def close() -> None:
    """Only needed by short-lived scripts (tests, one-off tools) - a long-
    running agent process leaves the driver open for its own lifetime."""
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def create_node(driver: Driver, label: str, properties: Dict[str, Any]) -> str:
    """Creates one node with the given label and properties, returns Neo4j's
    own internal element id. `properties` must include whatever the caller
    wants to look the node up by later (this layer has no opinion on IDs -
    that's the domain layer's job, one level up)."""
    with driver.session() as session:
        result = session.run(
            f'CREATE (n:{label} $props) RETURN elementId(n) AS id',
            props=properties,
        )
        return result.single()['id']


def get_node(driver: Driver, label: str, match: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """First node of `label` whose properties match every key/value in
    `match`, or None. Property-match, not element-id lookup - callers here
    identify nodes by their own domain keys (e.g. {'key': 'owner'}), not
    Neo4j's internal id.

    Builds a per-key equality WHERE clause (`n.k1 = $match.k1 AND ...`),
    not `WHERE n = $match` or the `MATCH (n:Label $match)` map-pattern
    shorthand - confirmed live both of those are wrong here: the former
    requires the node's ENTIRE property map to equal $match exactly (a
    caller passing a subset, the common case, matches nothing), and this
    Neo4j version's stricter (GQL-mode) parser rejects parameter maps
    inside MATCH patterns outright ("Parameter maps cannot be used in
    MATCH patterns"). Per-key WHERE equality works under both constraints
    and still only constrains the properties actually given."""
    where_clause = ' AND '.join(f'n.{key} = $match.{key}' for key in match)
    with driver.session() as session:
        result = session.run(
            f'MATCH (n:{label}) WHERE {where_clause} RETURN n',
            match=match,
        )
        record = result.single()
        return dict(record['n']) if record else None


def create_relationship(
    driver: Driver,
    from_label: str, from_match: Dict[str, Any],
    rel_type: str,
    to_label: str, to_match: Dict[str, Any],
    properties: Optional[Dict[str, Any]] = None,
) -> None:
    """Matches one existing node on each side by their own properties (not
    element id - see get_node) and creates a directed relationship between
    them. Raises via Neo4j's own error if either side doesn't match exactly
    one node - silently matching zero or many nodes would be a worse
    failure mode than a loud one here."""
    # Per-key WHERE equality, same reasoning as get_node's docstring - this
    # Neo4j version rejects parameter maps in MATCH patterns.
    from_clause = ' AND '.join(f'a.{key} = $from_match.{key}' for key in from_match)
    to_clause = ' AND '.join(f'b.{key} = $to_match.{key}' for key in to_match)
    with driver.session() as session:
        session.run(
            f'MATCH (a:{from_label}), (b:{to_label}) '
            f'WHERE {from_clause} AND {to_clause} '
            f'CREATE (a)-[r:{rel_type} $props]->(b)',
            from_match=from_match, to_match=to_match, props=properties or {},
        )


def run_query(driver: Driver, cypher: str, **params: Any) -> List[Dict[str, Any]]:
    """Escape hatch for anything the three helpers above don't cover
    (traversals, aggregations) - every row as a plain dict, so callers never
    hold a live driver record past the session closing."""
    with driver.session() as session:
        result = session.run(cypher, **params)
        return [dict(record) for record in result]


def wipe_all(driver: Driver) -> None:
    """Deletes every node and relationship. Test-only - never called from
    any agent or skill, just the isolated test script below and any future
    per-test cleanup."""
    with driver.session() as session:
        session.run('MATCH (n) DETACH DELETE n')
