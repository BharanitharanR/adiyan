"""
compute_share - A2A server entrypoint. No config_sdk/Mongo dependency
(deliberately, for this POC) - host/port/model come from environment
variables via constants.py, since two live instances (standing in for
two different users' machines) run from this same codebase with
different identities.

Run two instances from the repo root, in separate terminals:
    COMPUTE_SHARE_DISPLAY_NAME=alice COMPUTE_SHARE_PORT=8460 python3 -m mesh.compute_share.server
    COMPUTE_SHARE_DISPLAY_NAME=bob   COMPUTE_SHARE_PORT=8461 python3 -m mesh.compute_share.server

See README.md for the full walkthrough.
"""
from mesh.compute_share import availability
from mesh.compute_share.agent_executor import ComputeShareAgentExecutor
from mesh.compute_share.constants import AGENT_ID, AVAILABILITY_PORT, DISPLAY_NAME, HOST, PORT, STORAGE_ID
from mesh.compute_share.skills_catalog import get_skills
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path

if __name__ == '__main__':
    import asyncio
    skills = asyncio.run(get_skills())

    # Started before serve() (which blocks) - its own thread, its own
    # socket, so it's already answering by the time anyone could possibly
    # ask, and stays independent of whatever the main A2A server below
    # does from here on. See availability.py's own docstring for why this
    # needs to be genuinely separate, not just started early.
    availability.start_server(HOST, AVAILABILITY_PORT)

    agent_card = adiyan_card(
        name=f'Compute Share ({DISPLAY_NAME})',
        description='Shares or offloads LLM inference between Adiyan instances that opt in.',
        skills=skills,
        host=HOST,
        port=PORT,
    )
    serve(
        agent_card=agent_card,
        executor=ComputeShareAgentExecutor(),
        host=HOST,
        port=PORT,
        tasks_db_path=tasks_db_path(STORAGE_ID),
        agent_id=AGENT_ID,
        skills_refresher=get_skills,
        # Never a destination Orchestrator's router.py should pick for raw
        # user text - only ever reached via a fixed URL from another
        # agent's own code (mesh/lib/agent_sdk.py's ask()). See
        # bootstrap.build_app()'s own docstring for the live bug this
        # closes: run_inference's description alone was enough to win a
        # routing decision for an ordinary WhatsApp message.
        register_with_agent_registry=False,
    )
