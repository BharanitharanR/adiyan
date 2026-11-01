"""
Skills as data: the classifier-facing definition of a skill (name, description,
examples, tags) lives in one Mongo collection, `skills`, and one query returns
everything a classify() call needs to choose between them. Editing a
description in Compass changes how the next message is classified, with no
deploy.

Only the hosted-model path reads this (see handle_message.py's
_classify_reading_message); the local-Ollama path keeps its hardcoded per-skill
pools untouched. What a skill DOES stays in code - a document here can change
how a skill is described, not give it a handler, so a skill_id with no handler
is ignored by the caller rather than acted on.

One document per scope::skill_id. `scope` groups skills that are classified
together (e.g. 'reading'). Seeding is insert-only: the code-defined defaults
are written for ids that don't exist yet and never overwrite a document, so an
edit made in the collection survives restarts. Any failure reading the
collection (or an empty result) falls back to the code defaults, so the
classifier never has nothing to choose from.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import List

from a2a.types import AgentSkill
from pymongo import ASCENDING, UpdateOne

from mesh.lib import mongo_shared

logger = logging.getLogger('SkillStore')

MONGO_URL = os.environ.get('ADIYAN_MONGO_URL', 'mongodb://localhost:27017')
MONGO_DB_NAME = os.environ.get('ADIYAN_MONGO_DB_DATA', 'adiyan')
COLLECTION = 'skills'

_seeded_scopes: set = set()


def _collection():
    return mongo_shared.database()[COLLECTION]


def _to_document(scope: str, order: int, skill: AgentSkill) -> dict:
    return {
        '_id': f'{scope}::{skill.id}', 'scope': scope, 'skill_id': skill.id, 'order': order,
        'name': skill.name, 'description': skill.description,
        'examples': list(skill.examples), 'tags': list(skill.tags), 'enabled': True,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }


def _to_skill(document: dict) -> AgentSkill:
    return AgentSkill(
        id=document['skill_id'], name=document.get('name') or document['skill_id'],
        description=document['description'], tags=list(document.get('tags') or []),
        examples=list(document.get('examples') or []),
        input_modes=['text/plain'], output_modes=['application/json'],
    )


def _seed_missing(scope: str, defaults: List[AgentSkill]) -> None:
    _collection().bulk_write([
        UpdateOne({'_id': f'{scope}::{skill.id}'}, {'$setOnInsert': _to_document(scope, order, skill)}, upsert=True)
        for order, skill in enumerate(defaults)
    ], ordered=False)


def _query(scope: str) -> List[AgentSkill]:
    documents = _collection().find({'scope': scope, 'enabled': True}).sort([('order', ASCENDING)])
    return [_to_skill(d) for d in documents]


async def load_skills(scope: str, defaults: List[AgentSkill]) -> List[AgentSkill]:
    """Every enabled skill in `scope`, in one query. `defaults` are written to
    the collection for any id it lacks (once per process), and are returned
    as-is if the collection can't be read or comes back empty."""
    try:
        if scope not in _seeded_scopes:
            await asyncio.to_thread(_seed_missing, scope, defaults)
            _seeded_scopes.add(scope)
        skills = await asyncio.to_thread(_query, scope)
    except Exception as e:
        logger.warning(f'skill collection unavailable for scope {scope!r}, using built-in defaults: {e}')
        return defaults
    return skills or defaults
