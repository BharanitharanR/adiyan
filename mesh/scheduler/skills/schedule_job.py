"""
schedule_job's real body - the piece that ties together skill_router's
(eventual) extracted params, state.db, the resolve_schedule LLM stage, and
cron_trigger.register_trigger.

Scope, deliberately: only target='self' is resolved. Any other target
('everyone', a named group, a specific other client) requires a client/group
directory Scheduler Agent has no access path to - the shared_state question
tabled earlier in this build, still open. Raising rather than guessing.

Called with already-extracted parameters (name, description, target, ...) -
this module doesn't do classification or extraction itself; that's
skill_router.py's job, not yet wired to this file since skill_router.py is
still a stub.
"""
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from croniter import croniter
from llama_index.embeddings.ollama import OllamaEmbedding
from pydantic import BaseModel

from mesh.lib import config_sdk, permissions
from mesh.lib.agent_sdk import AdiyanAgent
from mesh.lib.config import load_runtime_config, load_seed_config
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path
from mesh.scheduler import db
from mesh.scheduler.constants import AGENT_ID, AGENT_URL, CRON_TRIGGER_URL

# Embeddings only, not a completion - ask() has no embedding capability, so
# OllamaEmbedding stays a direct call, same reasoning as everywhere else in
# this migration that a raw model call surviving isn't an oversight.
OLLAMA_URL = 'http://localhost:11434'
EMBED_MODEL = 'nomic-embed-text'

# mesh/scheduler/ - the code directory holding runtime_config.json, not to be
# confused with ~/.Adiyan/agents/scheduler/ (runtime data - see mesh/lib/paths.py).
AGENT_CODE_DIR = Path(__file__).parent.parent
_SEED = load_seed_config(AGENT_CODE_DIR)

_agent = AdiyanAgent(AGENT_ID)


def _seeded(key: str) -> Dict[str, Any]:
    return _SEED.get(key, {'value': '', 'description': ''})


class ResolvedSchedule(BaseModel):
    cron_expression: str  # standard 5-field cron, e.g. "0 18 * * 0"


class TargetNotResolvableError(Exception):
    """target isn't 'self' - resolving anything else needs a client/group
    directory this agent has no access to. Not silently guessed at."""
    def __init__(self, target: str):
        self.target = target
        super().__init__(f"Cannot resolve target '{target}' - only 'self' is currently supported.")


class ScheduleTooFrequentError(Exception):
    """The resolved cron would fire more than once an hour. Code-side
    backstop for resolve_schedule_prompt_template's own once-per-hour rule -
    a model that ignores the prompt (2026-09-10: 'starting now' resolved to
    '* * * * *', every minute, and spammed the owner's chat for hours)
    stops here instead of at cron_trigger.register_trigger."""
    def __init__(self, cron_expression: str):
        self.cron_expression = cron_expression
        super().__init__(
            f"Refusing to schedule {cron_expression!r} - it fires more than once an hour. "
            "The minute field must be a single literal 0-59."
        )


def _reject_if_subhourly(cron_expression: str) -> None:
    """Raise ScheduleTooFrequentError unless the minute field is one fixed
    value. '*', a step ('*/5'), a list ('0,30') or a range ('0-15') in the
    minute field all mean 'more than once an hour' - none of which any
    legitimate reminder needs, and all of which turn a bad parse into a
    flood."""
    fields = cron_expression.split()
    minute = fields[0] if fields else '*'
    if minute == '*' or any(c in minute for c in ('/', ',', '-')):
        raise ScheduleTooFrequentError(cron_expression)
    try:
        if not (0 <= int(minute) <= 59):
            raise ScheduleTooFrequentError(cron_expression)
    except ValueError:
        raise ScheduleTooFrequentError(cron_expression)


async def _resolve_schedule(description: str) -> str:
    """description -> a real cron expression, via its own LLM stage. Mirrors
    the old codebase's services/schedule_parser.py rather than folding this
    into extract_parameters - a wrong classification and a wrong schedule
    parse are different failure modes worth being able to isolate."""
    cfg = await config_sdk.get_stage_config(
        AGENT_ID, 'resolve_schedule', load_runtime_config(AGENT_CODE_DIR)['resolve_schedule'],
    )
    seeded = _seeded('resolve_schedule_prompt_template')
    template = await config_sdk.get_constant(
        AGENT_ID, 'resolve_schedule_prompt_template', seeded['value'], description=seeded['description'],
    )
    try:
        prompt = template.format(description=description)
    except Exception:
        prompt = seeded['value'].format(description=description)
    result = await _agent.ask(
        prompt, stage='resolve_schedule', model=cfg['model'], temperature=cfg['temperature'], schema=ResolvedSchedule,
    )
    return result.cron_expression


def _next_run_at(cron_expression: str) -> str:
    return croniter(cron_expression, datetime.now(timezone.utc)).get_next(datetime).isoformat()


async def _embed(text: str) -> list:
    embedder = OllamaEmbedding(model_name=EMBED_MODEL, base_url=OLLAMA_URL)
    return await embedder.aget_text_embedding(text)


async def run(
    name: str,
    description: str,
    target: str,
    expects_response: bool = False,
    response_window_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    if target != 'self':
        raise TargetNotResolvableError(target)

    conn = db.connect(state_db_path(AGENT_ID))
    embedding = await _embed(f'{name} {description}')

    # Resolved before the dedup check now, not after - see
    # db.find_similar_job()'s own docstring for why "same schedule" has to
    # be part of what "duplicate" means, not just description similarity.
    cron_expression = await _resolve_schedule(description)
    _reject_if_subhourly(cron_expression)
    next_run_at = _next_run_at(cron_expression)

    existing = db.find_similar_job(conn, embedding, cron_expression)
    if existing is not None:
        return {
            'job_id': existing['id'],
            'routine_name': existing['name'],
            'created_new_routine': False,
            'resolved_schedule': existing['resolved_schedule'],
            'next_run_at': existing['next_run_at'],
        }

    job = db.create_job(
        conn, name=name, description=description, target=target,
        resolved_schedule=cron_expression, next_run_at=next_run_at,
        embedding=embedding, expects_response=expects_response,
        response_window_minutes=response_window_minutes,
    )

    # Service token - see delete_job.py's identical comment: the caller's
    # own right to schedule_job was already checked upstream.
    token = permissions.mint_token('scheduler', 'service')
    cron_trigger_url = await config_sdk.get_constant(
        AGENT_ID, 'cron_trigger_url', CRON_TRIGGER_URL,
        description='URL of the cron_trigger MCP server that actually fires scheduled jobs at their due time.',
    )
    await call_tool(cron_trigger_url, 'register_trigger', {
        'job_id': job['id'],
        'invoke_at': next_run_at,
        'target_agent_url': AGENT_URL,
        'skill_id': 'run_routine',
        # See run_routine.py's own identical comment - cron_trigger no
        # longer injects its registration-level job_id into fired params
        # (it calls call_agent() now, params pass through as given), so
        # run_routine's own job_id parameter has to be explicit here.
        'params': {'job_id': job['id']},
    }, token=token)

    return {
        'job_id': job['id'],
        'routine_name': job['name'],
        'created_new_routine': True,
        'resolved_schedule': cron_expression,
        'next_run_at': next_run_at,
    }
