#!/usr/bin/env python3
"""
Prints the dynamically-registered agents mesh/start_all.sh should launch on
top of its own hardcoded core set - one `name|port|module` line each, the
exact format of a COMPONENTS entry in that script.

Source is the `run_the_agent` collection (config_sdk.list_runnables()),
which an agent's own server.py writes to at startup via
config_sdk.register_runnable() - wired into the example_agent scaffold, so
every agent copied from it self-registers the first time you run it.

start_all.sh calls this AFTER the core mesh is already up, so a
newly-registered agent's own startup (registry call, config read, an
inference_router call) has something to talk to.

Exit 0 and print nothing when the config store is unreachable or empty -
start_all.sh then just runs its core set, no error.

    python -m mesh.tools.runnable_agents
"""
import asyncio

from mesh.lib import config_sdk


async def _main() -> None:
    for row in await config_sdk.list_runnables():
        # Same 3-field pipe format start_all.sh's COMPONENTS array uses.
        print(f"{row['agent_id']}|{row['port']}|{row['module']}")


if __name__ == '__main__':
    asyncio.run(_main())
