"""Example Agent's AgentSkill catalog - same single-source-of-truth pattern
as every other agent's skills_catalog.py (see mesh/journal/skills_catalog.py
for the smallest real one this was copied from).

description/examples are Mongo-backed via config_sdk, so they're editable
from the config dashboard without a restart - the orchestrator picks up an
edited description the next time it routes a message, since this whole
function is rebuilt on every call rather than cached at import time.

This is the one place the orchestrator actually reads to decide "does this
conversation belong to Example Agent, or to someone else?" - the
description below IS the routing logic, in plain English, not a
regex or a keyword list."""
from typing import Any, Dict, List

from a2a.types import AgentSkill

from mesh.example_agent.constants import AGENT_ID
from mesh.lib import config_sdk

_DEFAULT_DESCRIPTIONS: Dict[str, str] = {
    'log_micro_habit': (
        "Record a short micro-habit journal entry for the owner - a quick note "
        "of a small habit they did or skipped today (e.g. 'did 10 pushups', "
        "'no sugar today', 'read for 15 min'), saved to their personal habit log. "
        "This stores an entry; it does not generate reflection questions."
    ),
    'list_micro_habits': (
        "Report and interpret the micro-habit entries the owner has logged - "
        "read-only. Handles 'what did I log today', 'show my micro habits', and "
        "'what do my habits say about me' / 'interpret my micro habits'. Returns "
        "the stored entries and, when there are any, a short plain summary of the "
        "pattern. It never creates or changes anything."
    ),
}

_DEFAULT_EXAMPLES: Dict[str, List[str]] = {
    'log_micro_habit': [
        "did my 10 pushups today",
        "log habit: no screens after 9pm",
        "micro habit check-in - meditated 5 minutes",
        "skipped my walk today",
    ],
    'list_micro_habits': [
        "what micro habits have I logged today",
        "show me my micro habits",
        "have I logged a habit this week",
        "list my recent habit entries",
    ],
}

_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'log_micro_habit': {
        'name': 'Log Micro Habit', 'tags': ['habit', 'journal', 'tracking'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
    },
    'list_micro_habits': {
        'name': 'List Micro Habits', 'tags': ['habit', 'journal', 'tracking', 'read'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
    },
}

async def get_skills() -> List[AgentSkill]:
    """Rebuilt on every call - config_sdk's own short-TTL cache keeps this
    cheap while still picking up a dashboard edit within one cache window,
    not only at process startup."""
    skills = []
    for skill_id, structure in _STRUCTURE.items():
        description = await config_sdk.get_constant(
            AGENT_ID, f'skill_{skill_id}_description', _DEFAULT_DESCRIPTIONS[skill_id],
        )
        examples = await config_sdk.get_constant(
            AGENT_ID, f'skill_{skill_id}_examples', _DEFAULT_EXAMPLES[skill_id],
        )
        skills.append(AgentSkill(id=skill_id, description=description, examples=examples, **structure))
    return skills
