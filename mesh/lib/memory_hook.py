"""
Phase 3 of the memory-graph rebuild: the platform-wide contract every agent
calls, instead of each one deciding for itself whether to wire memory in
(today's shape - recall_contact_memory is an OPTIONAL tool an agent's
ReAct loop may or may not invoke). Three functions, deliberately small:

    ensure_identity(identity_key, display_name)  - register/refresh a person
    fetch_context(identity_key, limit)           - deterministic recall, no LLM call
    record_fact(text, ...)                       - write one structured fact

This is the ONLY file that's allowed to call mesh/lib/graph_store.py from
outside mesh/lib/ itself - agent code never touches graph_store or
graph_client directly, the same boundary mesh/lib/config_sdk.py already
draws around Mongo for every agent's config reads.

The identity-key discipline this closes: `identity_key` here must already
be the stable key mesh/orchestrator/db.py's resolve_identity_key() produces
(the same value stored as orchestrator_clients' own _id) - never a raw
display name. This module does not resolve chat_id -> identity_key itself
(that stays orchestrator's own job, same as it is today for
orchestrator_clients) - it only refuses to silently accept a display name
standing in for one (see ensure_identity's docstring). display_name is
accepted separately, purely for the node's own readability, and is never
used as a lookup key anywhere in this module or in graph_store.

Tested in isolation from every agent's own server/executor code - see
test_memory_hook.py, which calls these three functions directly against a
real Neo4j with fake identity keys, no orchestrator or A2A traffic at all.
"""
import contextvars
import logging
from typing import Any, Dict, Optional, Sequence

from mesh.lib import graph_client, graph_store
from mesh.lib.identity import resolve_identity_key

logger = logging.getLogger(__name__)

# Phase 5's read-side wiring: mesh/lib/bootstrap.py's executor wrapper sets
# this once per incoming A2A request (identity resolved, context fetched),
# and mesh/lib/agent_sdk.py's AdiyanAgent.ask() reads it - the two ends of
# the platform hook, so every agent's own ask() call site gets the caller's
# known facts prepended without either file needing to know the other
# exists. A plain module-level ContextVar, not a parameter threaded through
# every skill/ask() call: the whole point is that no signature in between
# has to carry this.
#
# Default '', not None - every reader does `if memory_context:` rather than
# an `is not None` check, so a request with nothing set (no identity, graph
# down, or code running outside any request at all - e.g. this module's own
# test scripts) behaves identically to "nothing known," never a crash.
CURRENT_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar('adiyan_memory_context', default='')

# Token subjects with no real person behind them - a scheduled fire, a
# cron trigger, or one agent calling another. mint_token's own docstring
# names 'service' as the subject for "an internal machine caller with no
# WhatsApp identity behind it"; 'orchestrator' is what handle_message.py
# actually passes for its own service-tier calls. Neither should ever get
# an Identity node in the graph.
_MACHINE_SUBJECTS = {'service', 'orchestrator'}


def identity_from_claims(claims: Optional[Dict[str, Any]]) -> Optional[str]:
    """The identity key behind an A2A call, or None when there isn't a
    person behind it. This is the whole of Phase 4's per-agent logic, kept
    here rather than copied into each agent_executor - see this module's
    own docstring on why agent code shouldn't be making this decision.

    Reads `sub` from the verified token claims, which mint_token sets to
    the caller's real chat_id for a WhatsApp identity, and normalizes it
    through the same resolve_identity_key() orchestrator used on the way
    in - the same function, not a second implementation of the same idea.
    None for a missing/unverified token, and None for a machine caller
    (service tier or a machine subject), so nothing invents an identity
    for a cron fire."""
    if not claims:
        return None
    if claims.get('tier') == 'service':
        return None
    subject = claims.get('sub')
    if not subject or subject in _MACHINE_SUBJECTS:
        return None
    return resolve_identity_key(subject)


def ensure_identity(identity_key: str, display_name: Optional[str] = None) -> None:
    """Registers (or refreshes) one identity in the graph. Callers pass the
    already-resolved identity_key - the same value orchestrator/db.py's
    resolve_identity_key() produces and orchestrator_clients stores as its
    own _id - not the raw chat_id a webhook reported and never a bare
    contact_name. That resolution is deliberately NOT redone here: doing it
    twice, in two different modules, is exactly how the two are allowed to
    drift apart again.

    Degrades rather than raises if the graph is unreachable - same shape
    config_sdk already uses for a missing Mongo. An agent's real work
    (logging a habit, reading a page) must not fail because the memory
    tier is down; the user gets their answer, just without memory."""
    try:
        driver = graph_client.connect()
        graph_store.upsert_identity(driver, identity_key, display_name)
    except Exception as e:
        logger.warning(f'Memory graph unavailable, skipping ensure_identity for {identity_key}: {e}')


def fetch_context(identity_key: str, limit: int = 5) -> str:
    """Every structured fact this identity is allowed to see, most recent
    first, formatted as one ready-to-inject prompt block - or '' if there
    are none, so callers can safely do
    `'\\n\\n'.join(b for b in (fetch_context(...), other_block) if b)`
    without a special case for "nothing known yet."

    Deterministic - a plain graph query, no LLM call, no ReAct tool the
    model might choose not to invoke. This is the fix for the gap named
    earlier tonight: recall_contact_memory today is optional; this is not.

    Returns '' (not an exception) if the graph is unreachable - see
    ensure_identity's docstring on why memory degrades instead of
    failing the request it was meant to improve."""
    try:
        driver = graph_client.connect()
        facts = graph_store.facts_visible_to(driver, identity_key)
    except Exception as e:
        logger.warning(f'Memory graph unavailable, no context for {identity_key}: {e}')
        return ''
    if not facts:
        return ''
    recent = list(reversed(facts))[:limit]
    lines = [f'- {f["text"]}' for f in recent]
    return 'What you know about this person:\n' + '\n'.join(lines)


def record_fact(
    text: str,
    about: Optional[str] = None,
    stated_by: Optional[str] = None,
    visible_to: Sequence[str] = (),
    supersedes: Optional[str] = None,
    source_document: Optional[str] = None,
) -> Optional[str]:
    """Thin pass-through to graph_store.record_fact - kept as its own
    function (not "just call graph_store directly") so every write path
    goes through this one platform module, the same reason fetch_context
    exists instead of every agent importing graph_store's read function on
    its own. Every identity key passed here must already have been through
    ensure_identity - same discipline graph_store.record_fact itself
    already documents (a typo'd key fails loudly, no phantom node).

    Returns the new fact's id, or None if the graph was unreachable and
    nothing was written - a dropped fact is the acceptable cost of never
    failing the user's actual request over the memory tier being down."""
    try:
        driver = graph_client.connect()
        return graph_store.record_fact(
            driver, text,
            about=about, stated_by=stated_by, visible_to=visible_to,
            supersedes=supersedes, source_document=source_document,
        )
    except Exception as e:
        logger.warning(f'Memory graph unavailable, fact not recorded: {e}')
        return None
