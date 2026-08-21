"""
Orchestrator Agent - A2A server entrypoint. Same shape as every other
mesh/ agent's server.py.

Run from the repo root as `python -m mesh.orchestrator.server`. The
whatsapp MCP server (port 8425) should already be running - it pushes
incoming messages here, and handle_message replies through its
send_message tool.
"""
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing
from mesh.orchestrator import router
from mesh.orchestrator.agent_executor import OrchestratorAgentExecutor
from mesh.orchestrator.constants import AGENT_ID, HOST, PORT
from mesh.orchestrator.skills_catalog import SKILLS


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    # Blocking - loads which agents/skills exist from the Agent Registry
    # before Orchestrator starts accepting real traffic. See router.py's
    # module docstring for why this is once-at-startup, not per-message.
    router.load_agent_pool()

    agent_card = adiyan_card(
        name='Orchestrator Agent',
        description='Routes an incoming message to the right agent and replies back.',
        skills=SKILLS,
        host=HOST,
        port=PORT,
    )
    serve(
        agent_card=agent_card,
        executor=OrchestratorAgentExecutor(),
        host=HOST,
        port=PORT,
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
    )
