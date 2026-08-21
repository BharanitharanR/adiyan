"""
Journal Agent - A2A server entrypoint. Same shape as mesh/scheduler/server.py.

Run from the repo root as `python -m mesh.journal.server`. Memory Agent
(port 8423) should already be running - craft_reflection_prompt calls it
directly.
"""
from mesh.journal.agent_executor import JournalAgentExecutor
from mesh.journal.constants import AGENT_ID, HOST, PORT
from mesh.journal.skills_catalog import SKILLS
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    agent_card = adiyan_card(
        name='Journal Agent',
        description="Crafts a tailored journaling question for one person, personalized from what's known about them if anything is.",
        skills=SKILLS,
        host=HOST,
        port=PORT,
    )
    serve(
        agent_card=agent_card,
        executor=JournalAgentExecutor(),
        host=HOST,
        port=PORT,
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
    )
