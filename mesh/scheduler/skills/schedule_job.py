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
from langchain_ollama import ChatOllama
from llama_index.embeddings.ollama import OllamaEmbedding
from pydantic import BaseModel

from mesh.lib import permissions
from mesh.lib.config import load_runtime_config
from mesh.lib.mcp_client import call_tool
from mesh.lib.paths import state_db_path
from mesh.scheduler import db
from mesh.scheduler.constants import AGENT_ID, AGENT_URL, CRON_TRIGGER_URL

OLLAMA_URL = 'http://localhost:11434'
EMBED_MODEL = 'nomic-embed-text'

# mesh/scheduler/ - the code directory holding runtime_config.json, not to be
# confused with ~/.Adiyan/agents/scheduler/ (runtime data - see mesh/lib/paths.py).
AGENT_CODE_DIR = Path(__file__).parent.parent


class ResolvedSchedule(BaseModel):
    cron_expression: str  # standard 5-field cron, e.g. "0 18 * * 0"


class TargetNotResolvableError(Exception):
    """target isn't 'self' - resolving anything else needs a client/group
    directory this agent has no access to. Not silently guessed at."""
    def __init__(self, target: str):
        self.target = target
        super().__init__(f"Cannot resolve target '{target}' - only 'self' is currently supported.")


async def _resolve_schedule(description: str) -> str:
    """description -> a real cron expression, via its own LLM stage. Mirrors
    the old codebase's services/schedule_parser.py rather than folding this
    into extract_parameters - a wrong classification and a wrong schedule
    parse are different failure modes worth being able to isolate."""
    cfg = load_runtime_config(AGENT_CODE_DIR)['resolve_schedule']
    model = ChatOllama(model=cfg['model'], base_url=OLLAMA_URL, temperature=cfg['temperature'])
    structured = model.with_structured_output(ResolvedSchedule)
    result = structured.invoke(
        f'Convert this into a standard 5-field cron expression (minute hour day month weekday). '
        f'Respond with only the cron expression.\n\nDescription: "{description}"'
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

    existing = db.find_similar_job(conn, embedding)
    if existing is not None:
        return {
            'job_id': existing['id'],
            'routine_name': existing['name'],
            'created_new_routine': False,
            'resolved_schedule': existing['resolved_schedule'],
            'next_run_at': existing['next_run_at'],
        }

    cron_expression = await _resolve_schedule(description)
    next_run_at = _next_run_at(cron_expression)

    job = db.create_job(
        conn, name=name, description=description, target=target,
        resolved_schedule=cron_expression, next_run_at=next_run_at,
        embedding=embedding, expects_response=expects_response,
        response_window_minutes=response_window_minutes,
    )

    # Service token - see delete_job.py's identical comment: the caller's
    # own right to schedule_job was already checked upstream.
    token = permissions.mint_token('scheduler', 'service')
    await call_tool(CRON_TRIGGER_URL, 'register_trigger', {
        'job_id': job['id'],
        'invoke_at': next_run_at,
        'target_agent_url': AGENT_URL,
        'skill_id': 'run_routine',
        'params': {},
    }, token=token)

    return {
        'job_id': job['id'],
        'routine_name': job['name'],
        'created_new_routine': True,
        'resolved_schedule': cron_expression,
        'next_run_at': next_run_at,
    }
