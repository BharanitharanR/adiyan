"""
Scheduler Agent's AgentSkill catalog - the single source both server.py (for
the public card) and agent_executor.py (for skill_router's classifier
prompt) read from. Split out to avoid a server <-> agent_executor cycle, and
so the card and the router can never quietly drift to describe different
skills.

description/examples are Mongo-backed (config_sdk) - same pattern as
mesh/memory/skills_catalog.py's get_skills(), including the same
vertical-override capability. id/name/tags/input_modes/output_modes stay
fixed - dispatch wiring, not prompt content.
"""
from typing import Any, Dict, List

from a2a.types import AgentSkill

from mesh.lib import config_sdk
from mesh.scheduler.constants import AGENT_ID

_DEFAULT_DESCRIPTIONS: Dict[str, str] = {
    'schedule_job': 'Create a recurring or one-time WhatsApp action from a natural-language time expression.',
    'run_routine': 'Re-run an existing named routine by name or trigger phrase.',
    'delete_job': 'Permanently cancel a scheduled job by name or phrase - it will not fire again.',
    'list_jobs': 'Report currently scheduled or pending jobs.',
}

_DEFAULT_EXAMPLES: Dict[str, List[str]] = {
    'schedule_job': [
        'Every Sunday at 6pm, send everyone the weekly recap',
        'Remind me every night to journal',
        'Send this to everyone this week who said yes to the poll',
    ],
    'run_routine': [
        'Run the office attendance check now',
        'reached p',
    ],
    'delete_job': [
        'Delete the journal reminder',
        'Cancel my morning wakeup job',
        'Stop reminding me about the office check',
    ],
    'list_jobs': [
        'What jobs are scheduled right now?',
        'Show me pending jobs for the gym group',
    ],
}

_STRUCTURE: Dict[str, Dict[str, Any]] = {
    'schedule_job': {
        'name': 'Schedule Job', 'tags': ['scheduling', 'cron', 'whatsapp'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
    },
    'run_routine': {
        'name': 'Run Routine', 'tags': ['scheduling', 'routines'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
    },
    'delete_job': {
        'name': 'Delete Job', 'tags': ['scheduling', 'delete', 'cancel'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
    },
    'list_jobs': {
        'name': 'List Jobs', 'tags': ['scheduling', 'status'],
        'input_modes': ['text/plain'], 'output_modes': ['application/json'],
    },
}


async def get_skills() -> List[AgentSkill]:
    """Rebuilt on every call - config_sdk's own short-TTL cache keeps this
    cheap while still picking up a dashboard/WhatsApp edit (or a vertical
    activation) within one cache window, not only at process startup."""
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
