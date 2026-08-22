"""
Memory Agent - A2A server entrypoint. Same shape as mesh/scheduler/server.py -
card built in code via mesh.lib.card, task store via mesh.lib.bootstrap.

Run from the repo root as `python -m mesh.memory.server`.
"""
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.memory.agent_executor import MemoryAgentExecutor
from mesh.memory.constants import AGENT_ID, HOST, PORT
from mesh.memory.skills_catalog import SKILLS
from mesh.observability.tracing import setup_tracing


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    agent_card = adiyan_card(
        name='Memory Agent',
        description="Looks up what's known about one person or business from their past conversations.",
        skills=SKILLS,
        host=HOST,
        port=PORT,
    )
    serve(
        agent_card=agent_card,
        executor=MemoryAgentExecutor(),
        host=HOST,
        port=PORT,
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
    )
