"""
resolve_book's real body - thin wrapper around memory_index.py's
find_book_by_reference() for the easy case (an exact/substring title match),
falling back to an LLM pick constrained to that same scoped candidate list
for everything else (a typo, a partial/paraphrased title). DataPart-only,
not advertised in skills_catalog.py (same reasoning as resolve_document.py's
own docstring) - this is meant to be called by Orchestrator resolving a
free-text book reference (mesh/orchestrator/skills/handle_message.py's
_start_book_reading()) before ever calling adiyan_reader's start_reading,
never classified from free text on Memory Agent's own card.

Deliberately separate from resolve_document.py: that one searches
kb_documents (chunk-ingested via ingest_document), which has zero
visibility into a page-ingested book (ingest_book/ingest_document_by_page
never writes a kb_documents row) - see find_book_by_reference()'s own
docstring for how this was found live.

The LLM fallback replaced a plain difflib fuzzy match (character-similarity
scoring against each candidate's raw display name) after a real, confirmed
bug: scoped to a customer whose only page-ingested book was "Crime and
Punishment", "read me pride and prejudice" resolved straight to Crime and
Punishment - both titles share the five-character connector " and " and
similar overall length, which is enough to inflate difflib's ratio() past
any cutoff loose enough to still catch a genuine typo. classify() doesn't
have that failure mode: it's given each candidate's actual title as a
plain-language description and picks by what the words mean, the same
constrained "pick one of these real ids, or none" mechanism this mesh
already uses for skill routing everywhere else - each candidate book is
represented as an AgentSkill (id=its real source_filename, description=its
title), so the model can only ever return a source_filename that's actually
in the caller's own scoped candidate list, never invent one.

vertical_id/requester_id/is_owner scope the match itself, not just later
reads of whatever it returns - same reasoning list_documents.py's own
docstring gives, and closing the same class of leak: confirmed live that an
unscoped match let one business's customer be handed back an entirely
different person's own uploaded book, the only candidate anywhere with a
similar-enough title. requester_id/is_owner are auto-injected from the
caller's own token claims (see mesh/memory/agent_executor.py's
_SCOPED_SKILLS) when this is called directly; vertical_id is not - it's
explicitly forwarded by _start_book_reading() the same way
recall_contact_memory's own vertical_id already is, never guessed from free
text.
"""
from typing import Any, Dict, List, Optional

from a2a.types import AgentSkill

from mesh.lib import config_sdk
from mesh.lib.skill_router import classify
from mesh.memory.constants import AGENT_ID, OLLAMA_URL, QDRANT_URL
from mesh.memory.memory_index import book_display_name, get_memory_index


async def _llm_pick(query: str, candidates: List[str]) -> Optional[str]:
    """None if nothing in `candidates` plausibly matches, or on any classify
    failure - same degrade-on-failure contract every other classify() call
    site in this mesh follows (a miss here just means resolve_book reports
    not-found, never an error surfaced to the customer). `candidates` is
    already the caller's own correctly-scoped list - this function trusts it
    completely and never widens it."""
    if not candidates:
        return None
    skills = [
        AgentSkill(
            id=source_filename, name=book_display_name(source_filename),
            description=f'The book "{book_display_name(source_filename)}".',
            examples=[], input_modes=['text/plain'], output_modes=['application/json'],
        )
        for source_filename in candidates
    ]
    model_cfg = await config_sdk.get_stage_config(
        # temperature=0.0, not the low-but-nonzero value used elsewhere in
        # this mesh: confirmed live this session that even 0.1 let a small
        # local model occasionally miss an obvious single-candidate typo
        # match ('crime and punishmnet' against a scoped list of exactly
        # one book, "crime and punishment") that it got right on a retry -
        # greedy decoding is the more deterministic choice for a pick-one-
        # of-a-short-list task like this, where there's no creative
        # variation worth preserving.
        AGENT_ID, 'resolve_book_match', {'model': 'qwen3:8b-16k', 'temperature': 0.0},
        description="Model/temperature for picking which of a customer's own candidate books a free-text reference means.",
    )
    valid_ids = {s.id for s in skills}

    async def _attempt() -> Optional[str]:
        try:
            choice = await classify(query, skills, model_cfg)
        except Exception:
            return None
        return choice.skill_id if choice.skill_id in valid_ids else None

    result = await _attempt()
    if result is not None:
        return result
    # One retry, not zero: confirmed live this session that this small
    # local model occasionally missed an obvious single-candidate typo
    # match ('crime and punishmnet' against a scoped list of exactly one
    # book) even at temperature=0.0, then got it right moments later with
    # the identical prompt - real inference non-determinism (unlike
    # deterministic string matching, an LLM call can't be made perfectly
    # reproducible just by lowering temperature), not a logic bug worth
    # chasing further. Same one-retry-then-accept-the-answer shape
    # fetch_public_domain_book.py's own _retry_once() already uses for
    # transient network flakiness - this is the model-call equivalent.
    return await _attempt()


async def run(
    query: str, vertical_id: Optional[str] = None, requester_id: Optional[str] = None, is_owner: bool = False,
) -> Dict[str, Any]:
    memory_index = get_memory_index(QDRANT_URL, OLLAMA_URL)
    if memory_index is None:
        return {'found': False, 'available': False}

    prefixes: List[str] = [p for p in (vertical_id, requester_id) if p]
    # The owner can resolve any book on file, same as list_books()'s own
    # is_owner behavior - no prefix filter rather than one that would
    # exclude their own platform-wide view.
    effective_prefixes = None if is_owner else prefixes
    source_filename = memory_index.find_book_by_reference(query, prefixes=effective_prefixes)
    if source_filename is None and query.strip():
        candidates = memory_index.list_page_ingested_books(prefixes=effective_prefixes)
        source_filename = await _llm_pick(query, candidates)
    if source_filename is None:
        return {'found': False, 'available': True}
    return {'found': True, 'available': True, 'source_filename': source_filename}
