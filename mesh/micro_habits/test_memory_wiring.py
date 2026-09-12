"""
Manual isolated test for Phase 4 - memory wired into one real agent
(micro_habits). Same convention as the Phase 1-3 scripts: no pytest, run
by hand, read the PASS/FAIL lines.

What this covers that the Phase 3 test doesn't: the real token path. It
mints an actual permission token exactly the way
mesh/orchestrator/skills/handle_message.py does
(`permissions.mint_token(chat_id, tier)`), verifies it the way
agent_executor.py does, and checks that the identity that comes out the
far end is the same key orchestrator itself would have stored. That
round-trip is the load-bearing claim of Phase 4 - that identity already
travels with every A2A call and needed no new plumbing.

Does NOT start the agent's A2A server or send a real A2A message - that
needs the whole mesh up, which is exactly the dependency these isolated
tests exist to avoid. It exercises the executor's logic, not its
transport.

Requires the same local Neo4j as the earlier scripts. Wipes the graph
before and after.

    python -m mesh.micro_habits.test_memory_wiring
"""
from mesh.lib import graph_client, memory_hook, permissions
from mesh.lib.identity import resolve_identity_key
from mesh.orchestrator import db as orchestrator_db


def check(label: str, condition: bool) -> None:
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {label}')
    if not condition:
        raise SystemExit(1)


def main() -> None:
    driver = graph_client.connect()
    graph_client.wipe_all(driver)

    try:
        chat_id = '14259744452672@lid'

        # The exact round-trip Phase 4 depends on: orchestrator mints a
        # token with the raw chat_id as subject, the agent verifies it and
        # derives an identity key - and that key matches what orchestrator
        # itself stores as orchestrator_clients._id.
        token = permissions.mint_token(chat_id, 'standard')
        claims = permissions.verify_token(token)
        identity_key = memory_hook.identity_from_claims(claims)
        check('a minted token verifies on the agent side', claims is not None)
        check(
            'the identity derived in the agent matches orchestrator’s own key for the same chat_id',
            identity_key == orchestrator_db.resolve_identity_key(chat_id) == resolve_identity_key(chat_id),
        )

        # An owner-tier phone-form caller normalizes identically on both sides
        owner_chat_id = '919361315379@c.us'
        owner_claims = permissions.verify_token(permissions.mint_token(owner_chat_id, 'owner'))
        check(
            'a phone-form owner token also resolves to the same key both sides agree on',
            memory_hook.identity_from_claims(owner_claims) == orchestrator_db.resolve_identity_key(owner_chat_id),
        )

        # A scheduled/service call carries no person - memory must be skipped
        service_claims = permissions.verify_token(permissions.mint_token('orchestrator', 'service'))
        check(
            'a scheduled/service call yields no identity, so no phantom Identity node',
            memory_hook.identity_from_claims(service_claims) is None,
        )

        # The executor's own pre-step and post-step, run in order
        memory_hook.ensure_identity(identity_key)
        check(
            'first run: no context yet for a brand-new identity',
            memory_hook.fetch_context(identity_key) == '',
        )
        memory_hook.record_fact(
            'Logged a micro habit: cleaned the soaked utensils tonight',
            about=identity_key, stated_by=identity_key, visible_to=[identity_key],
        )
        context = memory_hook.fetch_context(identity_key)
        check('after logging, the habit is in this identity’s own context', 'soaked utensils' in context)

        # A different contact who happens to share a display name sees none of it -
        # the exact collision that started this whole redesign.
        other_chat_id = '42795289067704@lid'   # also contact_name 'Bharani' in real data
        other_key = memory_hook.identity_from_claims(
            permissions.verify_token(permissions.mint_token(other_chat_id, 'standard'))
        )
        memory_hook.ensure_identity(other_key)
        check(
            'a second contact sharing the same display name sees none of the first one’s facts',
            memory_hook.fetch_context(other_key) == '',
        )

        print('\nAll Phase 4 wiring checks passed.')
    finally:
        graph_client.wipe_all(driver)
        graph_client.close()


if __name__ == '__main__':
    main()
