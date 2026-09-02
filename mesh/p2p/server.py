"""
p2p - A2A server entrypoint. Two roles in one process, same "dual
purpose" shape as mesh/compute_share/server.py (its own availability
listener alongside the main A2A server):

  - CLIENT role: this agent's own A2A skill (dispatch) - what
    mesh/inference_router/skills/complete.py calls when it decides to
    offload, instead of running Ollama locally. See skills/dispatch.py.

  - WORKER role: mesh/p2p/p2p_app.py's own raw UDP listener + heartbeat
    announcer against the self-hosted matchmaker - what makes THIS
    machine discoverable and answerable by a peer's own p2p agent.
    Started on a genuinely separate thread with its own event loop
    (asyncio.run() inside a daemon thread), not sharing the A2A server's
    loop - same reasoning compute_share's availability.py documents for
    its own separate thread: the UDP worker's own event loop must never
    be delayed by whatever the A2A server is doing, and vice versa.

Run from the repo root as `python -m mesh.p2p.server`.
"""
import asyncio
import threading

from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.p2p.agent_executor import P2PAgentExecutor
from mesh.p2p.constants import AGENT_ID, CAPABILITIES, HOST, PORT, UDP_PORT
from mesh.p2p.p2p_app import start_worker_endpoint
from mesh.p2p.skills_catalog import get_skills


def _start_worker_thread(port: int, capabilities: list) -> None:
    def _run() -> None:
        asyncio.run(start_worker_endpoint(port, capabilities))
    threading.Thread(target=_run, daemon=True, name='p2p_udp_worker').start()


if __name__ == '__main__':
    skills = asyncio.run(get_skills())

    # Started before serve() (which blocks) - see this module's own
    # docstring on why this needs a genuinely separate thread/loop.
    _start_worker_thread(UDP_PORT, CAPABILITIES)

    agent_card = adiyan_card(
        name='P2P',
        description='Discovers and dispatches offloaded LLM inference to peers via a self-hosted matchmaker.',
        skills=skills,
        host=HOST,
        port=PORT,
    )
    serve(
        agent_card=agent_card,
        executor=P2PAgentExecutor(),
        host=HOST,
        port=PORT,
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        skills_refresher=get_skills,
        # Never a destination Orchestrator's router.py should pick for raw
        # user text - only ever reached via a fixed URL from
        # mesh/inference_router/skills/complete.py, same reasoning
        # mesh/compute_share/server.py's own serve() call documents.
        register_with_agent_registry=False,
    )
