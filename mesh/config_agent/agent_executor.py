"""
Config Agent's AgentExecutor. Same design as every other agent under mesh/
- see mesh/scheduler/agent_executor.py's module docstring for the
DataPart-fast-path / stranger-caller reasoning.
"""
from pathlib import Path
from typing import Any, Dict, Optional

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

from mesh.config_agent.constants import AGENT_ID
from mesh.config_agent.skills import (
    activate_vertical,
    deactivate_vertical,
    get_active_vertical,
    get_all_configs,
    onboard_mcp_server,
    query_config,
    update_config,
    update_stage_config,
)
from mesh.config_agent.skills_catalog import SKILLS
from mesh.lib import permissions
from mesh.lib.config import load_runtime_config
from mesh.lib.skill_router import route

AGENT_CODE_DIR = Path(__file__).parent


class QueryConfigParams(BaseModel):
    agent_id: Optional[str] = Field(default=None, description="Which agent's config, if named - leave unset if the caller wants a list of known agents.")
    key: Optional[str] = Field(default=None, description="Which specific stage or constant, if named - leave unset to return everything for that agent.")


class UpdateConfigParams(BaseModel):
    agent_id: str = Field(description='Which agent this setting belongs to.')
    key: str = Field(description='Which constant/toggle to change.')
    new_value: str = Field(description='The new value, as stated by the caller.')


class ActivateVerticalParams(BaseModel):
    vertical_id: str = Field(description='The vertical to switch this deployment onto.')


class NoParams(BaseModel):
    """deactivate_vertical/get_active_vertical take no arguments - still
    need a schema so extract() has something to run against, even though
    it'll extract nothing."""


EXTRACTION_SCHEMAS = {
    'query_config': QueryConfigParams,
    'update_config': UpdateConfigParams,
    'activate_vertical': ActivateVerticalParams,
    'deactivate_vertical': NoParams,
    'get_active_vertical': NoParams,
}

# get_all_configs/update_stage_config: DataPart-only, deliberately not in
# SKILLS/EXTRACTION_SCHEMAS above - the config dashboard's own structured
# calls (mesh/config_server/), never resolved from free text. See
# query_config.py's own module docstring on why stage settings specifically
# stay out of the NL path.
SKILL_HANDLERS = {
    'query_config': query_config.run,
    'update_config': update_config.run,
    'get_all_configs': get_all_configs.run,
    'update_stage_config': update_stage_config.run,
    'onboard_mcp_server': onboard_mcp_server.run,
    'activate_vertical': activate_vertical.run,
    'deactivate_vertical': deactivate_vertical.run,
    'get_active_vertical': get_active_vertical.run,
}


class ConfigAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        await updater.start_work()

        data_parts = get_data_parts(context.message.parts)
        if data_parts:
            payload = dict(data_parts[0])
            skill_id = payload.pop('skill_id', None)
            params: Dict[str, Any] = payload
        else:
            text = get_message_text(context.message)
            cfg = load_runtime_config(AGENT_CODE_DIR)
            try:
                skill_id, ambiguous, params = await route(
                    text, SKILLS, EXTRACTION_SCHEMAS,
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

        handler = SKILL_HANDLERS.get(skill_id)
        if handler is None:
            await updater.failed(new_text_message(f'Unknown skill_id: {skill_id}'))
            return

        claims = permissions.verify_token(context.metadata.get('token'))
        if not permissions.is_allowed(claims, f'{AGENT_ID}.{skill_id}'):
            await updater.reject(new_text_message('Not authorized for this.'))
            return

        try:
            result = await handler(**params)
        except Exception as e:
            await updater.failed(new_text_message(f'Could not process request: {e}'))
            return

        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not yet designed for this agent.')
