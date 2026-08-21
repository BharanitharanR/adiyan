"""
Analysis Agent - A2A server entrypoint. Same shape as mesh/journal/server.py.

Run from the repo root as `python -m mesh.analysis.server`. Memory Agent
(port 8423) should already be running - analyze_document calls it directly
(resolve_document, get_document_text).
"""
from mesh.analysis.agent_executor import AnalysisAgentExecutor
from mesh.analysis.constants import AGENT_ID, HOST, PORT
from mesh.analysis.skills_catalog import SKILLS
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing


if __name__ == '__main__':
    setup_tracing(AGENT_ID)

    agent_card = adiyan_card(
        name='Analysis Agent',
        description='Reads an entire uploaded document and analyzes, reviews, or synthesizes something from it per an instruction.',
        skills=SKILLS,
        host=HOST,
        port=PORT,
    )
    serve(
        agent_card=agent_card,
        executor=AnalysisAgentExecutor(),
        host=HOST,
        port=PORT,
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
    )
