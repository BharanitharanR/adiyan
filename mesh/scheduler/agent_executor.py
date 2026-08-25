"""
Scheduler Agent's AgentExecutor - the SDK-defined seam between an incoming
A2A task and this agent's own behavior.

Every caller is treated identically to a stranger who has only ever seen
this agent's AgentCard: no special-cased shortcut for Adiyan's own
orchestrator. The one legitimate fast path is a structured data Part
(Part.data) instead of free text - that's not a back-channel, it's a caller
(like cron_trigger, see mesh/mcp/cron_trigger/server.py) that already knows
exactly what it wants and says so directly, same as A2A's own Message model
allows for any caller. Free text still goes through skill_router's
classify -> extract, exactly as designed.
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

from mesh.lib import config_sdk, permissions
from mesh.lib.config import load_runtime_config
from mesh.lib.skill_router import route
from mesh.scheduler.constants import AGENT_ID
from mesh.scheduler.job_lookup import JobNotFoundError
from mesh.scheduler.skills import delete_job, list_jobs, run_routine, schedule_job
from mesh.scheduler.skills_catalog import get_skills

# mesh/scheduler/ - where runtime_config.json lives. Defined locally, same
# pattern as schedule_job.py's own AGENT_CODE_DIR, rather than reaching into
# that module's internals for a constant both files derive identically.
AGENT_CODE_DIR = Path(__file__).parent


class ScheduleJobParams(BaseModel):
    name: str = Field(
        description="A short (2-4 word) label for this job, drawn from what "
                     "it actually does - e.g. 'Weekly Recap', 'Journal Reminder', "
                     "'Morning Wakeup'. Not a generic word like 'scheduler' or "
                     "'reminder' - it should distinguish this job from any other."
    )
    description: str
    # Defaults to 'self', not left for the model to invent - an unconstrained
    # required string field led it to fabricate a target ("Wakeup Service")
    # for a message that named no target at all. Only 'self' is actually
    # supported today anyway (see schedule_job.TargetNotResolvableError), so
    # there was never a reason to let the model freely populate this.
    target: str = Field(
        default='self',
        description="Who receives it. Always output the literal string 'self' "
                     "when the caller means themselves - including 'me', 'myself', "
                     "'I', or no mention of a recipient at all. Only use "
                     "'everyone' or a named group if a different, specific "
                     "recipient is clearly named. Never output 'me' or 'I' "
                     "literally - translate them to 'self'.",
    )
    expects_response: bool = False
    response_window_minutes: Optional[int] = None
    # Deliberately NOT documented as something to fill from free text - the
    # extraction LLM has no way to turn "the book I uploaded" into a real
    # <username>/<filename> key without actually looking it up, and a
    # confident-sounding guess here would silently create a page-delivery
    # job against a document that doesn't exist. Left unset by the classify/
    # extract path; a caller that already resolved the real source_filename
    # (e.g. Orchestrator's own kb_pending upload flow, or Analysis Agent
    # after a real resolve_document call) passes it directly via DataPart.
    source_filename: Optional[str] = None


class RunRoutineParams(BaseModel):
    """Shape for a human caller ("run the office attendance check now").
    cron_trigger's own fire calls use job_id directly instead (see
    mesh/mcp/cron_trigger/server.py's _fire) - a DIFFERENT shape for the
    same skill_id, since that caller already knows the exact job, not a
    name/phrase to resolve. Both arrive as raw dicts via the DataPart/
    free-text branches below; this schema only governs the free-text path."""
    name_or_phrase: str = Field(
        description="ONLY the job's identifying name or phrase, stripped of "
                     "any command wrapper like 'run the', 'do the', 'now' - e.g. "
                     "for 'run the journal routine now', extract just 'journal', "
                     "not the full sentence. This gets matched against stored job "
                     "names by similarity, so extra words weaken the match."
    )


class DeleteJobParams(BaseModel):
    """Same name_or_phrase shape and same extraction guidance as
    RunRoutineParams - the two skills are only distinguished by skill_id
    (classify_skill's job), not by anything different in how this field is
    extracted."""
    name_or_phrase: str = Field(
        description="ONLY the job's identifying name or phrase, stripped of "
                     "any command wrapper like 'delete the', 'cancel', 'stop "
                     "reminding me about' - e.g. for 'cancel my morning wakeup "
                     "job', extract just 'morning wakeup'. This gets matched "
                     "against stored job names by similarity, so extra words "
                     "weaken the match."
    )


class ListJobsParams(BaseModel):
    # Same fix as ScheduleJobParams.target, for the same reason - a vague
    # phrase like "for users" was previously extracted as a literal filter
    # value ("users"), silently zeroing out real results. None (no filter,
    # show everything) is the correct default whenever the caller isn't
    # asking about one specific, named target.
    target: Optional[str] = Field(
        default=None,
        description="Filter to one specific target's jobs, e.g. 'self'. Leave "
                     "unset (the default) unless a specific target is clearly named - "
                     "vague phrasing like 'for users' is not a specific target.",
    )
    status: Optional[str] = None


EXTRACTION_SCHEMAS = {
    'schedule_job': ScheduleJobParams,
    'run_routine': RunRoutineParams,
    'delete_job': DeleteJobParams,
    'list_jobs': ListJobsParams,
}


class SchedulerAgentExecutor(AgentExecutor):
    """Implements Scheduler Agent's four skills: schedule_job, run_routine,
    delete_job, list_jobs. Public descriptions live in skills_catalog.py."""

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
            # Precise machine caller (e.g. cron_trigger) - already fully
            # determined, skip classify/extract entirely.
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
                # Without this, an Ollama/model failure here left the task
                # stuck forever - never reaching complete()/failed(), so the
                # caller just hangs until its own client-side timeout. Fail
                # the task explicitly instead.
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

        claims = permissions.verify_token(context.metadata.get('token'))
        if not permissions.is_allowed(claims, f'{AGENT_ID}.{skill_id}'):
            await updater.reject(new_text_message('Not authorized for this.'))
            return

        try:
            result = await self._dispatch(skill_id, params)
        except (schedule_job.TargetNotResolvableError, JobNotFoundError) as e:
            await updater.failed(new_text_message(str(e)))
            return
        except NotImplementedError as e:
            await updater.failed(new_text_message(str(e)))
            return
        except Exception as e:
            # run_routine reaches real network I/O (Journal Agent, cron_trigger,
            # WhatsApp) that schedule_job/list_jobs don't - any of those can
            # fail at runtime in ways worth surfacing cleanly, not crashing
            # execute() unhandled.
            await updater.failed(new_text_message(f'{skill_id} failed: {e}'))
            return

        await updater.add_artifact(parts=[new_data_part(result)])
        await updater.complete()

    async def _dispatch(self, skill_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if skill_id == 'schedule_job':
            return await schedule_job.run(**params)
        if skill_id == 'list_jobs':
            return list_jobs.run(**params)
        if skill_id == 'run_routine':
            return await run_routine.run(**params)
        if skill_id == 'delete_job':
            return await delete_job.run(**params)
        raise NotImplementedError(f'Unknown skill_id: {skill_id}')

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('Cancel not yet designed for in-flight scheduling tasks.')
