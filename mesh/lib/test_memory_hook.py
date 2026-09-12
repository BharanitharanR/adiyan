"""
Manual isolated test for mesh/lib/memory_hook.py - Phase 3 of the memory-
graph rebuild. Same convention as the Phase 1/2 test scripts: no pytest,
run by hand, read the PASS/FAIL lines.

Only imports memory_hook (+ graph_client for setup/teardown) - no agent
server, no orchestrator, no A2A traffic. Proves the platform contract
(ensure_identity / fetch_context / record_fact) behaves correctly using
fake identity keys, standing in for whatever orchestrator/db.py's
resolve_identity_key() would have produced in a real call.

Requires the same local Neo4j as the earlier test scripts. Wipes the whole
database before and after.

    python -m mesh.lib.test_memory_hook
"""
from mesh.lib import graph_client, memory_hook


def check(label: str, condition: bool) -> None:
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {label}')
    if not condition:
        raise SystemExit(1)


def main() -> None:
    driver = graph_client.connect()
    graph_client.wipe_all(driver)

    try:
        owner, priya_a = '919812345601@lid', '919812345602@lid'

        # ensure_identity is idempotent - two calls, no duplicate node
        memory_hook.ensure_identity(owner, 'Coach (owner)')
        memory_hook.ensure_identity(owner, 'Coach (owner)')
        identity_count = graph_client.run_query(
            driver, 'MATCH (i:Identity {key: $key}) RETURN count(i) AS n', key=owner,
        )[0]['n']
        check('ensure_identity is idempotent, no duplicate node on re-registration', identity_count == 1)

        memory_hook.ensure_identity(priya_a, 'Priya A')

        # fetch_context on someone with no facts yet returns '', not an error
        check('fetch_context returns empty string, not an error, before anything is recorded', memory_hook.fetch_context(priya_a) == '')

        # record_fact + fetch_context round-trip, formatted for direct prompt injection
        memory_hook.record_fact(
            'Prefers 6am training sessions',
            about=priya_a, stated_by=priya_a, visible_to=[owner, priya_a],
        )
        context = memory_hook.fetch_context(priya_a)
        check('fetch_context returns a prompt-ready block once a fact exists', context.startswith('What you know about this person:'))
        check('fetch_context includes the recorded fact text', 'Prefers 6am training sessions' in context)

        # a fact recorded about someone but not visible to them stays invisible via the hook too
        memory_hook.record_fact(
            'Missed the last two sessions without notice',
            about=priya_a, stated_by=owner, visible_to=[owner],
        )
        check(
            'a confidential fact recorded via the hook stays out of the subject’s own context',
            'Missed the last two sessions' not in memory_hook.fetch_context(priya_a),
        )
        check(
            'the same confidential fact is visible in the owner’s own context',
            'Missed the last two sessions' in memory_hook.fetch_context(owner),
        )

        # identity_from_claims: the whole of the per-agent wiring logic
        check(
            'a real WhatsApp caller’s token yields their identity key',
            memory_hook.identity_from_claims({'sub': '919812345601@lid', 'tier': 'standard'}) == '919812345601@lid',
        )
        check(
            'a @c.us subject is normalized to phone digits, same as orchestrator does',
            memory_hook.identity_from_claims({'sub': '919361315379@c.us', 'tier': 'owner'}) == '919361315379',
        )
        check(
            'a service-tier machine caller yields no identity',
            memory_hook.identity_from_claims({'sub': 'orchestrator', 'tier': 'service'}) is None,
        )
        check(
            'a machine subject yields no identity even on a non-service tier',
            memory_hook.identity_from_claims({'sub': 'service', 'tier': 'standard'}) is None,
        )
        check(
            'an unverified/missing token yields no identity',
            memory_hook.identity_from_claims(None) is None,
        )

        print('\nAll Phase 3 memory_hook checks passed.')
    finally:
        graph_client.wipe_all(driver)
        graph_client.close()


if __name__ == '__main__':
    main()
