"""
Manual isolated test for mesh/lib/graph_client.py - Phase 1 of the memory-
graph rebuild. No pytest/fixtures, same convention as
mesh/scheduler/test_client.py: a script you run by hand and read the
printed PASS/FAIL lines from.

Deliberately has ZERO memory-domain concepts (no Fact/Identity/visible_to)
- this only proves the graph primitives work, so a failure here can never
be confused with a bug in Phase 2's schema layer, which will have its own,
separate test script one layer up.

Requires a real Neo4j running locally (`/opt/homebrew/opt/neo4j/bin/neo4j
console` - see mesh/lib/graph_client.py's own docstring for credentials).
Wipes the whole database before and after running - do not point this at
anything but a throwaway local dev instance.

    python -m mesh.lib.test_graph_client
"""
from mesh.lib import graph_client


def check(label: str, condition: bool) -> None:
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {label}')
    if not condition:
        raise SystemExit(1)


def main() -> None:
    driver = graph_client.connect()
    graph_client.wipe_all(driver)

    try:
        # create_node + get_node round-trip
        graph_client.create_node(driver, 'Person', {'key': 'bharani', 'name': 'Bharani'})
        fetched = graph_client.get_node(driver, 'Person', {'key': 'bharani'})
        check('create_node + get_node round-trip', fetched == {'key': 'bharani', 'name': 'Bharani'})

        # get_node on a non-existent node returns None, not an error
        missing = graph_client.get_node(driver, 'Person', {'key': 'nobody'})
        check('get_node returns None for a miss', missing is None)

        # create_relationship between two existing nodes
        graph_client.create_node(driver, 'Book', {'key': 'crime_and_punishment', 'title': 'Crime and Punishment'})
        graph_client.create_relationship(
            driver,
            'Person', {'key': 'bharani'}, 'READING',
            'Book', {'key': 'crime_and_punishment'},
            properties={'page': 8},
        )
        rows = graph_client.run_query(
            driver,
            'MATCH (p:Person {key: $pkey})-[r:READING]->(b:Book) RETURN b.title AS title, r.page AS page',
            pkey='bharani',
        )
        check('create_relationship is traversable', rows == [{'title': 'Crime and Punishment', 'page': 8}])

        # a relationship type not created never matches
        no_rows = graph_client.run_query(
            driver,
            'MATCH (p:Person {key: $pkey})-[r:FINISHED]->(b:Book) RETURN b',
            pkey='bharani',
        )
        check('a relationship type never created has zero matches', no_rows == [])

        # run_query returns plain dicts, not live driver records
        all_people = graph_client.run_query(driver, 'MATCH (p:Person) RETURN p.name AS name')
        check('run_query result is JSON-serializable plain dicts', isinstance(all_people[0], dict))

        print('\nAll Phase 1 graph_client checks passed.')
    finally:
        graph_client.wipe_all(driver)
        graph_client.close()


if __name__ == '__main__':
    main()
