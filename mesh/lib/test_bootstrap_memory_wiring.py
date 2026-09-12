"""
Manual isolated test for Phase 5 - the platform-wide wiring in
mesh/lib/bootstrap.py (_MemoryWiredExecutor) and mesh/lib/agent_sdk.py
(ask()'s CURRENT_CONTEXT read). Same convention as the earlier phase
scripts: no pytest, run by hand, read the PASS/FAIL lines.

Does NOT build a real Starlette app or send a real A2A message - that's
mesh/tools/smoke_test.py's job (build_app() construction) and the
mesh-wide manual test (a real WhatsApp message) already run for Phase 4.
This isolates the one new mechanism Phase 5 actually adds: does the
executor wrapper set memory_hook.CURRENT_CONTEXT correctly around the
inner executor's own execute(), and does it always reset afterward - even
when the inner executor raises.

A fake AgentExecutor and a duck-typed stand-in for RequestContext (a plain
object with a `.metadata` dict) stand in for the real a2a types -
_MemoryWiredExecutor only ever reads `context.metadata.get('token')` and
otherwise passes `context` straight through untouched, so nothing else
about RequestContext's real shape matters here.

Requires the same local Neo4j as the earlier scripts (ensure_identity/
fetch_context inside the wrapper touch the graph for a real identity).
Wipes the graph before and after.

    python -m mesh.lib.test_bootstrap_memory_wiring
"""
import asyncio

from mesh.lib import graph_client, memory_hook, permissions
from mesh.lib.bootstrap import _MemoryWiredExecutor


def check(label: str, condition: bool) -> None:
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {label}')
    if not condition:
        raise SystemExit(1)


class _FakeContext:
    def __init__(self, token):
        self.metadata = {'token': token} if token else {}


class _RecordingExecutor:
    """Stands in for a real agent's AgentExecutor - records whatever
    CURRENT_CONTEXT held during its own execute(), so the test can assert
    on it without a real ask() call or a real LLM."""
    def __init__(self, raise_error: bool = False):
        self.seen_context = 'NEVER CALLED'
        self.raise_error = raise_error

    async def execute(self, context, event_queue):
        self.seen_context = memory_hook.CURRENT_CONTEXT.get()
        if self.raise_error:
            raise RuntimeError('simulated skill failure')

    async def cancel(self, context, event_queue):
        pass


async def main() -> None:
    driver = graph_client.connect()
    graph_client.wipe_all(driver)

    try:
        chat_id = '919812345601@lid'
        identity_key = memory_hook.identity_from_claims(
            permissions.verify_token(permissions.mint_token(chat_id, 'standard'))
        )
        memory_hook.ensure_identity(identity_key)
        memory_hook.record_fact(
            'Prefers 6am training sessions',
            about=identity_key, stated_by=identity_key, visible_to=[identity_key],
        )

        # Outside any wrapped call, the contextvar reads as its default.
        check('CURRENT_CONTEXT defaults to empty string outside any request', memory_hook.CURRENT_CONTEXT.get() == '')

        # A real caller: the wrapper should populate it for the inner executor.
        inner = _RecordingExecutor()
        wrapped = _MemoryWiredExecutor(inner)
        token = permissions.mint_token(chat_id, 'standard')
        await wrapped.execute(_FakeContext(token), event_queue=None)
        check('the inner executor saw a populated context during its own execute()', '6am training sessions' in inner.seen_context)
        check('CURRENT_CONTEXT resets to empty after a successful call', memory_hook.CURRENT_CONTEXT.get() == '')

        # A machine/service caller: no identity, so no context - and the
        # inner executor must still run normally.
        inner_service = _RecordingExecutor()
        wrapped_service = _MemoryWiredExecutor(inner_service)
        service_token = permissions.mint_token('orchestrator', 'service')
        await wrapped_service.execute(_FakeContext(service_token), event_queue=None)
        check('a service-tier caller gets an empty context, not an error', inner_service.seen_context == '')

        # A missing/malformed token: same as no identity, never a crash.
        inner_notoken = _RecordingExecutor()
        wrapped_notoken = _MemoryWiredExecutor(inner_notoken)
        await wrapped_notoken.execute(_FakeContext(None), event_queue=None)
        check('a missing token yields an empty context, not an exception', inner_notoken.seen_context == '')

        # The reset must happen even when the inner executor raises - a
        # ContextVar set here must never leak into the next task this same
        # worker process picks up.
        inner_failing = _RecordingExecutor(raise_error=True)
        wrapped_failing = _MemoryWiredExecutor(inner_failing)
        raised = False
        try:
            await wrapped_failing.execute(_FakeContext(token), event_queue=None)
        except RuntimeError:
            raised = True
        check('the inner executor’s own exception still propagates', raised)
        check('CURRENT_CONTEXT resets to empty even after the inner executor raised', memory_hook.CURRENT_CONTEXT.get() == '')

        print('\nAll Phase 5 bootstrap wiring checks passed.')
    finally:
        graph_client.wipe_all(driver)
        graph_client.close()


if __name__ == '__main__':
    asyncio.run(main())
