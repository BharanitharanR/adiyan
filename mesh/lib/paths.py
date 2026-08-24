"""
Per-agent runtime-data paths, shared by every agent under mesh/.

Encodes the code/runtime-data split settled early on: an agent's code lives in
this repo (mesh/<agent_id>/), but its generated, per-install state - task
bookkeeping, domain data, embeddings - lives under ~/.Adiyan/, never checked
into git, the same category as the existing ~/.Adiyan/routines/*.md files.
"""
from pathlib import Path

ADIYAN_HOME = Path.home() / '.Adiyan'


def agent_home(agent_id: str) -> Path:
    """Root runtime-data directory for one agent, e.g. ~/.Adiyan/agents/scheduler/.
    Creates it (and memory/) if missing."""
    home = ADIYAN_HOME / 'agents' / agent_id
    (home / 'memory').mkdir(parents=True, exist_ok=True)
    return home


def state_db_path(agent_id: str) -> Path:
    """This agent's own domain data - fixed relationships, e.g. Scheduler
    Agent's jobs/routines/trigger phrases. SQLite, not A2A task bookkeeping."""
    return agent_home(agent_id) / 'memory' / 'state.db'


def tasks_db_path(agent_id: str) -> Path:
    """A2A's own task-lifecycle bookkeeping (DatabaseTaskStore). Deliberately
    a separate file from state_db_path - different kind of state, same as
    settled when server.py was first written."""
    return agent_home(agent_id) / 'memory' / 'tasks.db'


def embeddings_path(agent_id: str) -> Path:
    """This agent's own growing experience/learnings store (per-invocation
    embeddings), as distinct from state_db_path's fixed relationships."""
    path = agent_home(agent_id) / 'memory' / 'embeddings'
    path.mkdir(parents=True, exist_ok=True)
    return path


def kb_documents_dir(agent_id: str) -> Path:
    """Where an agent's raw ingested source documents live, alongside their
    chunked/embedded form in whatever vector store it uses - the vectors
    alone can't answer "send me that document back," only "what does it
    say" (mesh/memory/memory_index.py's retrieve_knowledge_base()), so a
    later share needs the original bytes kept somewhere too."""
    path = agent_home(agent_id) / 'memory' / 'kb_documents'
    path.mkdir(parents=True, exist_ok=True)
    return path


def eval_reports_dir(agent_id: str) -> Path:
    """Where an eval runner's per-run reports land, e.g.
    ~/.Adiyan/agents/eval_engine/reports/. Runtime-generated output, same
    never-in-git category as state.db/tasks.db - a report is regenerated
    every run, not authored."""
    path = agent_home(agent_id) / 'reports'
    path.mkdir(parents=True, exist_ok=True)
    return path


def mcp_home(server_name: str) -> Path:
    """Root runtime-data directory for one MCP server, e.g.
    ~/.Adiyan/mcp/cron_trigger/. Separate top-level bucket from agents/ -
    an MCP server (like cron_trigger) has no AgentCard, no AgentExecutor,
    no reasoning of its own; it's a tool agents are clients of, not a peer,
    and its runtime data shouldn't be filed alongside genuine agents'."""
    home = ADIYAN_HOME / 'mcp' / server_name
    (home / 'memory').mkdir(parents=True, exist_ok=True)
    return home


def mcp_state_db_path(server_name: str) -> Path:
    """This MCP server's own persistent state, e.g. cron_trigger's
    APScheduler SQLAlchemyJobStore."""
    return mcp_home(server_name) / 'memory' / 'state.db'
