#!/usr/bin/env python3
"""
Fast pre-flight check for mesh/lib/bootstrap.py - catches a broken shared
bootstrap before it takes down every agent simultaneously, the way it
actually did once: a `Starlette(..., on_startup=[...])` call that raised
`TypeError: unexpected keyword argument 'on_startup'` (the installed
Starlette version had removed it) went unnoticed by every `import mesh.X`
check run beforehand, because bootstrap.build_app()'s actual app-
construction code only ever ran inside `if __name__ == '__main__':` -
imports never reach it.

This builds a real Starlette app via bootstrap.build_app() for every real
agent (real agent_id/host/port/skills_refresher - a generic dummy
AgentCard/AgentExecutor, since it's the shared wiring under test here, not
any one agent's actual card content) and drives it through Starlette's own
TestClient, which triggers a real ASGI lifespan startup/shutdown - the
exact code path the on_startup bug lived in. Binds no real port, makes no
required network call (a Mongo/registry call inside the vertical poller
degrades gracefully if either is down, same as always - this is a
construction/wiring check, not a "is the mesh actually reachable" check).

Run from the repo root, any time mesh/lib/bootstrap.py (or anything it
imports) changes, before asking for a full mesh restart:
    python3 -m mesh.tools.smoke_test
"""
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import AgentSkill
from starlette.testclient import TestClient

from mesh.lib.bootstrap import build_app
from mesh.lib.card import adiyan_card


class _NoOpExecutor(AgentExecutor):
    """Never actually invoked - TestClient's lifespan trigger doesn't send
    a real request, it only starts/stops the app. Present because
    build_app() requires a real AgentExecutor instance to construct
    DefaultRequestHandler."""
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('smoke test never actually invokes this')

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError('smoke test never actually invokes this')


# (agent_id, host, port, skills_refresher_import_path or None) - real
# identity/network values per agent, so the vertical poller and
# skills_refresher wiring get exercised with what each agent actually
# passes to serve(), not a placeholder. Add a new agent here as it's built.
_AGENTS = [
    ('orchestrator', '127.0.0.1', 8426, 'mesh.orchestrator.skills_catalog.get_skills'),
    ('analysis', '127.0.0.1', 8427, 'mesh.analysis.skills_catalog.get_skills'),
    ('memory', '127.0.0.1', 8423, None),
    ('scheduler', '127.0.0.1', 8420, None),
    ('journal', '127.0.0.1', 8422, None),
    ('config_agent', '127.0.0.1', 8428, None),
]


def _resolve_skills_refresher(path: Optional[str]):
    if path is None:
        return None
    module_path, func_name = path.rsplit('.', 1)
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


def _dummy_card(agent_id: str, host: str, port: int, skills: List[AgentSkill]):
    return adiyan_card(name=f'{agent_id}-smoke-test', description='smoke test', skills=skills, host=host, port=port)


def main() -> int:
    failures = []
    for agent_id, host, port, refresher_path in _AGENTS:
        try:
            skills_refresher = _resolve_skills_refresher(refresher_path)
            card = _dummy_card(agent_id, host, port, skills=[])
            with tempfile.TemporaryDirectory() as tmp_dir:
                tasks_db_path = Path(tmp_dir) / 'tasks.db'
                app = build_app(
                    agent_card=card,
                    executor=_NoOpExecutor(),
                    host=host,
                    port=port,
                    tasks_db_path=tasks_db_path,
                    agent_id=f'{agent_id}_smoke_test',  # distinct from the real agent_id - never touches its real registry entry
                    skills_refresher=skills_refresher,
                )
                with TestClient(app):
                    pass  # __enter__/__exit__ alone trigger real lifespan startup/shutdown
            print(f'  ok   {agent_id}')
        except Exception as e:
            print(f'  FAIL {agent_id}: {e}')
            failures.append((agent_id, e))

    print()
    if failures:
        print(f'{len(failures)} of {len(_AGENTS)} agent(s) failed to build - do not restart the mesh yet.')
        return 1
    print(f'All {len(_AGENTS)} agents built and started cleanly.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
