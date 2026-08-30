"""
Example Agent's AgentExecutor. Same design as every other agent under
mesh/ - copied from mesh/journal/agent_executor.py, the smallest real one,
and adapted for this agent's one skill.

Everything in this file except the two lines that mention roll_dice by
name is boilerplate every agent in this mesh shares verbatim: the A2A
task lifecycle, the DataPart fast-path (a caller who already knows exactly
which skill and params it wants skips the LLM routing entirely), the
plain-language routing fallback, and the permission check. A new agent
author copies this file and changes only what's marked below - they don't
design any of it themselves."""
from pathlib import Path
from typing import Any, Dict

from pydantic import BaseModel, Field

from a2a.helpers import (
    get_data_parts,
    get_message_text,
    new_data_part,
    new_task_from_user_message,
    new_text_message,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater

from mesh.example_agent.constants import AGENT_ID
from mesh.example_agent.skills import roll_dice
from mesh.example_agent.skills_catalog import get_skills
from mesh.lib import config_sdk, permissions
from mesh.lib.config import load_runtime_config
from mesh.lib.skill_router import route

AGENT_CODE_DIR = Path(__file__).parent


# --- Change this: one extraction schema per skill this agent has ---
class RollDiceParams(BaseModel):
    sides: int = Field(default=6, description="How many sides the die has. Default to 6 if the caller didn't say.")


EXTRACTION_SCHEMAS = {'roll_dice': RollDiceParams}
# --- end of what changes per-skill ---


class ExampleAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.start_work()

        # Fast path: a caller (another agent, or a UI) that already knows
        # exactly which skill and params it wants skips routing entirely -
        # this is how agent-to-agent calls in this mesh work, not just
        # human chat.
        data_parts = get_data_parts(context.message.parts)
        if data_parts:
            payload = dict(data_parts[0])
            skill_id = payload.pop('skill_id', None)
            params: Dict[str, Any] = payload
        else:
            # Plain-language path: the same skill_router.route() every
            # agent in this mesh uses, classifying against get_skills()'s
            # descriptions (skills_catalog.py) and extracting params against
            # EXTRACTION_SCHEMAS above. Nothing here is example-agent-
            # specific - this whole block is copy-pasted unchanged.
            text = get_message_text(context.message)
            cfg = await config_sdk.load_stage_configs(AGENT_ID, load_runtime_config(AGENT_CODE_DIR))
            skills = await get_skills()
            try:
                skill_id, ambiguous, params = await route(
                    text, skills, EXTRACTION_SCHEMAS,
                    classify_cfg=cfg['classify_skill'],
                    extract_cfg=cfg['extract_parameters'],
                )
            except Exception as e:
                await updater.failed(new_text_message(f'Could not process request: {e}'))
                return
            if skill_id is None and ambiguous:
                options = ', '.join(ambiguous)
                await updater.requires_input(
                    new_text_message(f'Did you mean one of: {options}? Please clarify which one.')
                )
                return
            if skill_id is None:
                await updater.reject(new_text_message("None of my skills match that request."))
                return

        # --- Change this: dispatch to whichever skill_id this agent supports ---
        if skill_id != 'roll_dice':
            await updater.failed(new_text_message(f'Unknown skill_id: {skill_id}'))
            return

        # Permission check - every call into this mesh goes through the
        # same permissions.is_allowed(), regardless of which agent it's
        # calling. A brand-new agent gets real access control for free,
        # just by including this one line.
        claims = permissions.verify_token(context.metadata.get('token'))
        if not permissions.is_allowed(claims, f'{AGENT_ID}.{skill_id}'):
            await updater.reject(new_text_message('Not authorized for this.'))
            return

        try:
            result = await roll_dice.run(**params)
        except Exception as e:
            await updater.failed(new_text_message(f'Could not roll the dice: {e}'))
            return
        # --- end of what changes per-skill ---

        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not yet designed for this agent.')
