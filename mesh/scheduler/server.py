"""
Scheduler Agent - A2A server entrypoint.

The AgentCard is built here, in code, via mesh.lib.card.adiyan_card - not a
hand-maintained JSON file. A static card and a running server drift apart the
moment either changes alone; letting the SDK build and serve the card from
this same object removes that failure mode entirely.

Targets A2A protocol v1.0 (the current release - see
https://a2a-protocol.org/latest/specification/), whose normative definition is
Protocol Buffers (src/a2a/types/a2a_pb2.pyi in a2aproject/a2a-python), not the
older Pydantic/JSON-RPC-first v0.3 shape. No use of the SDK's compat/v0_3/
layer - there is no legacy v0.3 caller here to support.

Run from the repo root as `python -m mesh.scheduler.server`, not directly -
its imports are repo-root-relative (mesh.lib.*), matching how main.py already
imports agents.*/services.*, not the A2A helloworld sample's convention of
being run standalone from its own directory.
"""
import asyncio
import logging
from pathlib import Path

from mesh.lib import config_sdk
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing
from mesh.scheduler.agent_executor import SchedulerAgentExecutor
from mesh.scheduler.constants import AGENT_ID, HOST, PORT
from mesh.scheduler.skills_catalog import get_skills

logger = logging.getLogger('SchedulerServer')

AGENT_CODE_DIR = Path(__file__).parent

# Startup catch-up of overdue jobs was REMOVED on 2026-09-15, on the owner's
# own instruction, alongside the identical removal in
# mesh/adiyan_reader/server.py - see that file's own note for the confirmed-
# live incident behind it (repeated agent restarts during one debugging
# session re-fired every overdue job on each boot, sending unsolicited
# messages to four real recipients).
#
# The same failure shape applies here and is arguably worse: Scheduler's jobs
# send WhatsApp messages to whoever the routine targets, and the 2026-08-30
# lockdown that removed WhatsApp send from the shared 'service' tier exists
# because of an earlier runaway-message incident from this same agent. A
# mechanism that replays every overdue send on every process start is the
# wrong default for anything with that history.
#
# The trade accepted: a routine whose fire was genuinely missed while the mesh
# was down is not replayed - it simply runs at its next scheduled time.
# cron_trigger's own scheduled fire is now the only path that runs a routine.


async def _load_startup_config() -> dict:
    # Every key in seed_config.json goes into Mongo right now, not lazily
    # the first time whatever branch happens to touch it - see
    # mesh/lib/config_sdk.py's seed_from_file().
    await config_sdk.seed_from_file(AGENT_ID, AGENT_CODE_DIR)
    host = await config_sdk.get_constant(
        AGENT_ID, 'host', HOST,
        description='Which network interface this agent binds to. Changing this needs a restart to take effect.',
    )
    port = await config_sdk.get_constant(
        AGENT_ID, 'port', PORT,
        description='Which port this agent listens on. Changing this needs a restart to take effect.',
    )
    description = await config_sdk.get_constant(
        AGENT_ID, 'card_description',
        'Owns scheduled jobs, durable routines, and trigger phrases for Adiyan.',
        description='What this agent does, shown in its A2A agent card.',
    )
    skills = await get_skills()
    return {'host': host, 'port': port, 'description': description, 'skills': skills}


if __name__ == '__main__':
    # Must run before any LangChain call happens (auto_instrument patches
    # LangChain at call time) - so before serve(), not inside agent_executor.
    setup_tracing(AGENT_ID)

    startup = asyncio.run(_load_startup_config())

    agent_card = adiyan_card(
        name='Scheduler Agent',
        description=startup['description'],
        skills=startup['skills'],
        host=startup['host'],
        port=startup['port'],
    )
    serve(
        agent_card=agent_card,
        executor=SchedulerAgentExecutor(),
        host=startup['host'],
        port=startup['port'],
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
        skills_refresher=get_skills,
    )
