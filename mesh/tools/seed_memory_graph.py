"""
Seeds the memory graph with the fitness-coach demo scenario and LEAVES it
there, so it can be explored visually in Neo4j Browser
(http://localhost:7474). The Phase 1-4 test scripts all wipe the graph in
their own teardown - deliberately, so a test never depends on what a
previous run left behind - which means they prove the code works but show
nothing afterwards. This fills that gap.

Not a test and not part of any runtime path: nothing in the mesh imports
this. It's a development/demo tool, the same category as
mesh/scheduler/test_client.py.

    python -m mesh.tools.seed_memory_graph          # seed the demo data
    python -m mesh.tools.seed_memory_graph --wipe   # remove everything

The scenario is the one from the memory-graph-model artifact: a coach with
two clients who share the first name Priya, including the fact that is
ABOUT one of them but visible only to the coach.
"""
import sys

from mesh.lib import graph_client, graph_store

OWNER = '919812345600@c.us'
PRIYA_A = '919812345601@lid'
PRIYA_B = '919812345699@lid'


def seed() -> None:
    driver = graph_client.connect()

    graph_store.upsert_identity(driver, graph_store.GLOBAL_IDENTITY_KEY)
    graph_store.upsert_identity(driver, OWNER, 'Coach (owner)')
    graph_store.upsert_identity(driver, PRIYA_A, 'Priya A')
    graph_store.upsert_identity(driver, PRIYA_B, 'Priya B')

    # ABOUT one person, VISIBLE_TO someone else entirely - the case a flat
    # visible_to array on a single document tends to blur.
    graph_store.record_fact(
        driver, 'Priya B has not paid September fees',
        about=PRIYA_B, stated_by=OWNER, visible_to=[OWNER],
    )

    knee = graph_store.record_fact(
        driver, 'Recovering from knee surgery, avoid squats',
        about=PRIYA_A, stated_by=PRIYA_A, visible_to=[OWNER, PRIYA_A],
    )
    # A correction: the older fact stays in the graph but drops out of
    # every live read (facts_visible_to filters superseded facts).
    graph_store.record_fact(
        driver, 'Cleared for squats again',
        about=PRIYA_A, stated_by=PRIYA_A, visible_to=[OWNER, PRIYA_A],
        supersedes=knee,
    )

    graph_store.record_fact(
        driver, 'Training for the Mumbai marathon in October',
        about=PRIYA_B, stated_by=PRIYA_B, visible_to=[OWNER, PRIYA_B],
    )
    graph_store.record_fact(
        driver, 'Studio is closed on public holidays',
        stated_by=OWNER, visible_to=[graph_store.GLOBAL_IDENTITY_KEY],
    )

    graph_store.record_document(
        driver, 'physio_report.pdf', user_context='Physio clearance note for training',
    )
    graph_store.record_fact(
        driver, 'Physio cleared full range of motion as of Sept 2026',
        about=PRIYA_A, stated_by=OWNER, visible_to=[OWNER, PRIYA_A],
        source_document='physio_report.pdf',
    )

    print('Seeded. Open http://localhost:7474 and run:  MATCH (n) RETURN n')
    print()
    for label, key in (('Coach', OWNER), ('Priya A', PRIYA_A), ('Priya B', PRIYA_B)):
        facts = graph_store.facts_visible_to(driver, key)
        print(f'{label} ({key}) can see {len(facts)}:')
        for f in facts:
            print(f'   - {f["text"]}')
        print()
    graph_client.close()


def wipe() -> None:
    driver = graph_client.connect()
    graph_client.wipe_all(driver)
    graph_client.close()
    print('Graph wiped.')


if __name__ == '__main__':
    wipe() if '--wipe' in sys.argv else seed()
