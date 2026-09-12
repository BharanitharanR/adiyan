"""
Manual isolated test for mesh/lib/graph_store.py - Phase 2 of the memory-
graph rebuild. Same convention as test_graph_client.py: no pytest, run by
hand, read the PASS/FAIL lines.

Only imports graph_store + graph_client - no agent, no orchestrator, no
mem0. Uses the fitness-coach/two-Priyas scenario from the
memory-graph-model.html artifact this session, including its complexity
layers (a global fact, a supersede, a document-sourced fact), so this test
doubles as a runnable version of that design.

Requires the same local Neo4j as test_graph_client.py. Wipes the whole
database before and after.

    python -m mesh.lib.test_graph_store
"""
from mesh.lib import graph_client, graph_store


def check(label: str, condition: bool) -> None:
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {label}')
    if not condition:
        raise SystemExit(1)


def texts(facts):
    return {f['text'] for f in facts}


def main() -> None:
    driver = graph_client.connect()
    graph_client.wipe_all(driver)

    try:
        owner, priya_a, priya_b = 'owner', 'priya_a', 'priya_b'
        graph_store.upsert_identity(driver, owner, 'Coach (owner)')
        graph_store.upsert_identity(driver, priya_a, 'Priya A')
        graph_store.upsert_identity(driver, priya_b, 'Priya B')
        graph_store.upsert_identity(driver, graph_store.GLOBAL_IDENTITY_KEY)

        # about != visible_to: a fact about Priya B, visible only to the owner
        graph_store.record_fact(
            driver, 'Priya B has not paid September fees',
            about=priya_b, stated_by=owner, visible_to=[owner],
        )
        check(
            'a fact about someone is not automatically visible to them',
            'Priya B has not paid September fees' not in texts(graph_store.facts_visible_to(driver, priya_b)),
        )
        check(
            'the owner can see the fact they were told in confidence',
            'Priya B has not paid September fees' in texts(graph_store.facts_visible_to(driver, owner)),
        )

        # supersession: a correction replaces the old fact for its viewers
        old_fact_id = graph_store.record_fact(
            driver, 'Recovering from knee surgery, avoid squats',
            about=priya_a, stated_by=owner, visible_to=[owner, priya_a],
        )
        check(
            'before the correction, Priya A sees the knee-surgery note',
            'Recovering from knee surgery, avoid squats' in texts(graph_store.facts_visible_to(driver, priya_a)),
        )
        graph_store.record_fact(
            driver, 'Cleared for squats again',
            about=priya_a, stated_by=owner, visible_to=[owner, priya_a],
            supersedes=old_fact_id,
        )
        visible_to_a = texts(graph_store.facts_visible_to(driver, priya_a))
        check('after the correction, the old fact is hidden', 'Recovering from knee surgery, avoid squats' not in visible_to_a)
        check('after the correction, the new fact is visible', 'Cleared for squats again' in visible_to_a)
        check(
            'include_superseded=True still surfaces the superseded fact for history/debugging',
            'Recovering from knee surgery, avoid squats' in texts(graph_store.facts_visible_to(driver, priya_a, include_superseded=True)),
        )

        # global facts: visible to every registered identity, no per-identity edges needed
        graph_store.record_fact(
            driver, 'Studio is closed on public holidays',
            stated_by=owner, visible_to=[graph_store.GLOBAL_IDENTITY_KEY],
        )
        check(
            'a global fact reaches Priya A without an explicit edge to her',
            'Studio is closed on public holidays' in texts(graph_store.facts_visible_to(driver, priya_a)),
        )
        check(
            'a global fact reaches Priya B too',
            'Studio is closed on public holidays' in texts(graph_store.facts_visible_to(driver, priya_b)),
        )

        # document-sourced fact
        graph_store.record_document(driver, 'physio_report.pdf', user_context='Physio clearance note for training')
        graph_store.record_fact(
            driver, 'Physio cleared full range of motion as of Sept 2026',
            about=priya_a, stated_by=owner, visible_to=[owner, priya_a],
            source_document='physio_report.pdf',
        )
        sourced = graph_client.run_query(
            driver,
            'MATCH (f:Fact)-[:SOURCED_FROM]->(d:Document) '
            'WHERE f.text = $text RETURN d.source_filename AS filename, d.user_context AS context',
            text='Physio cleared full range of motion as of Sept 2026',
        )
        check('a fact ingested from a document links back to it', sourced == [
            {'filename': 'physio_report.pdf', 'context': 'Physio clearance note for training'},
        ])

        # unknown identity key: relationship silently not created, not a phantom node
        graph_store.record_fact(driver, 'orphan fact', visible_to=['nonexistent_identity'])
        check(
            'a typo identity key creates no phantom Identity node',
            graph_client.get_node(driver, 'Identity', {'key': 'nonexistent_identity'}) is None,
        )

        print('\nAll Phase 2 graph_store checks passed.')
    finally:
        graph_client.wipe_all(driver)
        graph_client.close()


if __name__ == '__main__':
    main()
