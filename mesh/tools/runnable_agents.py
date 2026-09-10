#!/usr/bin/env python3
"""
Prints the agents mesh/start_all.sh should launch on top of its own
hardcoded core set - one `name|port|module` line each, the exact format of
a COMPONENTS entry in that script.

Two steps, every run:

  1. SWEEP THE FILESYSTEM. Every mesh/<dir>/ that has a constants.py
     defining AGENT_ID and PORT, plus a server.py, and isn't one of
     start_all.sh's own core components, gets a row upserted into the
     `run_the_agent` collection. This is what makes a brand-new agent
     start automatically without ever being run by hand first - dropping
     the directory in (e.g. via `python -m mesh.tools.new_agent`) is
     enough. AGENT_ID/PORT are read by static parse (ast), never by
     importing the agent, so a half-finished agent dir can't break the
     sweep or run its code.

  2. PRINT the enabled rows. An operator can still set enabled:false on a
     row to keep the directory but stop start_all.sh launching it; the
     sweep never flips that back (register_runnable only sets enabled on
     first insert).

Exit 0 and print nothing if the config store is unreachable - start_all.sh
then just runs its core set, no error.

    python -m mesh.tools.runnable_agents
"""
import ast
import asyncio
import re
from pathlib import Path

from mesh.lib import config_sdk

MESH = Path(__file__).resolve().parent.parent
START_ALL = MESH / 'start_all.sh'

# Directories under mesh/ that are not deployment agents: shared code, and
# the reference scaffold (a working die-roller you copy, not something a
# real deployment should be running).
NOT_AGENTS = {'lib', 'tools', 'mcp', 'nginx', 'qdrant', 'evals', 'voice', 'observability', 'example_agent'}


def _core_component_names() -> set:
    """The `name` field of every COMPONENTS entry in start_all.sh - the
    hardcoded core set, which must never be swept in as a dynamic agent."""
    if not START_ALL.exists():
        return set()
    return set(re.findall(r'"\s*(\w+)\s*\|', START_ALL.read_text()))


def _read_constant(tree: ast.Module, name: str):
    """Value of a module-level `name = <literal>` assignment, or None. Only
    literals - `PORT = int(os.environ.get(...))` (the core agents' pattern)
    returns None and that dir is skipped, which is correct: those are core
    components, already in start_all.sh."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    try:
                        return ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        return None
    return None


def _discover() -> list:
    """[{agent_id, module, port}] for every agent directory on disk that
    isn't a core component."""
    core = _core_component_names()
    found = []
    for const_path in MESH.glob('*/constants.py'):
        agent_dir = const_path.parent.name
        if agent_dir in core or agent_dir in NOT_AGENTS:
            continue
        if not (const_path.parent / 'server.py').exists():
            continue
        try:
            tree = ast.parse(const_path.read_text())
        except SyntaxError:
            continue
        agent_id = _read_constant(tree, 'AGENT_ID')
        port = _read_constant(tree, 'PORT')
        if not isinstance(agent_id, str) or not isinstance(port, int):
            continue
        if agent_id in core:
            continue
        found.append({'agent_id': agent_id, 'module': f'mesh.{agent_dir}.server', 'port': port})
    return found


async def _main() -> None:
    # Step 1: sweep the filesystem into run_the_agent (idempotent upsert).
    for agent in _discover():
        await config_sdk.register_runnable(agent['agent_id'], module=agent['module'], port=agent['port'])

    # Step 2: print every enabled row, in COMPONENTS format.
    for row in await config_sdk.list_runnables():
        print(f"{row['agent_id']}|{row['port']}|{row['module']}")


if __name__ == '__main__':
    asyncio.run(_main())
