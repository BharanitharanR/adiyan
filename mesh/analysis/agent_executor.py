"""
Analysis Agent's AgentExecutor. Same design as every other agent under
mesh/ - see mesh/scheduler/agent_executor.py's module docstring for the
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

from mesh.analysis.constants import AGENT_ID
from mesh.analysis.skills import analyze
from mesh.analysis.skills_catalog import get_skills
from mesh.lib import config_sdk, permissions
from mesh.lib.config import load_runtime_config
from mesh.lib.skill_router import route

AGENT_CODE_DIR = Path(__file__).parent


class AnalyseThisParams(BaseModel):
    instruction: str = Field(
        description="The full analysis/review/critique/synthesis instruction, as close to "
        "the caller's own wording as possible - do not paraphrase or shorten it."
    )


EXTRACTION_SCHEMAS = {'analyse_this': AnalyseThisParams}


class AnalysisAgentExecutor(AgentExecutor):
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

        if skill_id != 'analyse_this':
            await updater.failed(new_text_message(f'Unknown skill_id: {skill_id}'))
            return

        claims = permissions.verify_token(context.metadata.get('token'))
        if not permissions.is_allowed(claims, f'{AGENT_ID}.{skill_id}'):
            await updater.reject(new_text_message('Not authorized for this.'))
            return

        # This call's own token is who's REALLY asking - Orchestrator mints
        # it with the sender's real chat_id/tier (rules_engine.check()),
        # never with an internal service identity, for every path that
        # reaches analyse_this. Forwarded into analyze.run() so its ReAct
        # loop's own knowledge-base tools can pass the real identity on to
        # Memory Agent explicitly - see analyze.py's _make_tools() own
        # comment for why that forwarding has to be explicit rather than
        # left to Memory Agent inferring it from ITS OWN token (which would
        # instead see this agent's own service identity, sub='analysis').
        # setdefault, not an overwrite, in case a future direct caller ever
        # has a legitimate reason to pass its own already-correct values.
        claims = claims or {}
        params.setdefault('requester_id', claims.get('sub'))
        params.setdefault('is_owner', claims.get('tier') == 'owner')

        try:
            result = await analyze.run(**params)
        except Exception as e:
            await updater.failed(new_text_message(f'Could not analyze that: {e}'))
            return

        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not yet designed for this agent.')
