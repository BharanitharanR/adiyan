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
from mesh.lib.bootstrap import serve
from mesh.lib.card import adiyan_card
from mesh.lib.paths import tasks_db_path
from mesh.observability.tracing import setup_tracing
from mesh.scheduler.agent_executor import SchedulerAgentExecutor
from mesh.scheduler.constants import AGENT_ID, HOST, PORT
from mesh.scheduler.skills_catalog import SKILLS


if __name__ == '__main__':
    # Must run before any LangChain call happens (auto_instrument patches
    # LangChain at call time) - so before serve(), not inside agent_executor.
    setup_tracing(AGENT_ID)

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
