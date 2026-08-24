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
import threading
import time

from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import state_db_path, tasks_db_path
from mesh.observability.tracing import setup_tracing
from mesh.scheduler import db
from mesh.scheduler.agent_executor import SchedulerAgentExecutor
from mesh.scheduler.constants import AGENT_ID, HOST, PORT
from mesh.scheduler.skills import run_routine
from mesh.scheduler.skills_catalog import SKILLS

logger = logging.getLogger('SchedulerServer')

# Confirmed live: a startup-only catch-up attempt that fails once (e.g.
# WhatsApp's session hadn't reconnected yet at that exact moment) leaves the
# job stuck permanently, with no error visible anywhere the user would
# check - find_overdue_jobs() only ever runs again on the next full process
# restart. Retrying periodically in the background is what actually
# recovers once whatever was down comes back, without needing a restart.
CATCH_UP_RETRY_SECONDS = 5 * 60


async def _catch_up_overdue_jobs() -> None:
    """See db.find_overdue_jobs()'s own docstring - catches anything
    cron_trigger's misfire handling silently dropped while this mesh was
    down. Best-effort per job: one failing job (e.g. WhatsApp not
    connected yet at this exact moment) must not block the others, stop
    the server from starting, or stop later jobs from being retried on
    the next pass - see CATCH_UP_RETRY_SECONDS above."""
    conn = db.connect(state_db_path(AGENT_ID))
    overdue = db.find_overdue_jobs(conn)
    for job in overdue:
        try:
            await run_routine.run(job_id=job['id'])
            logger.info(f"Caught up overdue job {job['id']} ({job['name']!r})")
        except Exception as e:
            logger.warning(f"Could not catch up overdue job {job['id']} ({job['name']!r}), will retry: {e}")


def _catch_up_retry_loop() -> None:
    while True:
        time.sleep(CATCH_UP_RETRY_SECONDS)
        try:
            asyncio.run(_catch_up_overdue_jobs())
        except Exception as e:
            logger.warning(f'Catch-up retry pass failed: {e}')


if __name__ == '__main__':
    # Must run before any LangChain call happens (auto_instrument patches
    # LangChain at call time) - so before serve(), not inside agent_executor.
    setup_tracing(AGENT_ID)

    asyncio.run(_catch_up_overdue_jobs())
    threading.Thread(target=_catch_up_retry_loop, daemon=True).start()

    agent_card = adiyan_card(
        name='Scheduler Agent',
        description='Owns scheduled jobs, durable routines, and trigger phrases for Adiyan.',
        skills=SKILLS,
        host=HOST,
        port=PORT,
    )
    serve(
        agent_card=agent_card,
        executor=SchedulerAgentExecutor(),
        host=HOST,
        port=PORT,
        tasks_db_path=tasks_db_path(AGENT_ID),
        agent_id=AGENT_ID,
    )
