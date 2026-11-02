from datetime import date

from conftest import add_entry, add_note

from journal_analyzer import browse, store


def _journal(conn):
    for title, kind in (("John", "people"), ("Jun", "people"), ("Park", "places"), ("Chess", "topics")):
        add_note(conn, title, kind=kind)
    days = [
        (date(2023, 5, 1), ["John", "Park"]), (date(2023, 5, 2), ["John", "Park"]),
        (date(2024, 1, 1), ["John", "Jun", "Park"]), (date(2024, 1, 2), ["John", "Jun"]),
        (date(2024, 1, 3), ["Chess"]),
    ]
    for day, links in days:
        add_entry(conn, day, links=links, path=f"/vault/{day}.md")


def _titles(conn, rows, *keys):
    names = {r["id"]: r["title"] for r in store.all_notes(conn)}
    return [(*(names[r[k]] for k in keys), r["n"]) for r in rows]


def test_the_most_linked_notes_and_the_pairs_that_share_entries(conn):
    _journal(conn)
    nodes, edges = store.note_graph(conn, limit=10, min_shared=2)
    assert [(r["title"], r["n"]) for r in nodes] == [("John", 4), ("Park", 3), ("Jun", 2), ("Chess", 1)]
    assert sorted(_titles(conn, edges, "a", "b")) == sorted([("John", "Park", 3), ("John", "Jun", 2)])


def test_only_shown_notes_are_joined(conn):
    _journal(conn)
    nodes, edges = store.note_graph(conn, limit=2, min_shared=1)
    assert [r["title"] for r in nodes] == ["John", "Park"]
    assert _titles(conn, edges, "a", "b") == [("John", "Park", 3)]


def test_a_range_and_a_kind_narrow_it(conn):
    _journal(conn)
    nodes, edges = store.note_graph(conn, 10, 1, date(2024, 1, 1), date(2024, 12, 31))
    assert {r["title"] for r in nodes} == {"John", "Jun", "Park", "Chess"}
    assert ("John", "Park", 1) in _titles(conn, edges, "a", "b")
    nodes, edges = store.note_graph(conn, 10, 1, kind="People")
    assert [r["title"] for r in nodes] == ["John", "Jun"]
    assert _titles(conn, edges, "a", "b") == [("John", "Jun", 2)]


def test_the_browser_route(conn, config):
    _journal(conn)
    req = browse.Request(config, conn, {"limit": "3", "min": "3"}, lambda c: None)
    data = browse.graph(req)
    assert [n["title"] for n in data["nodes"]] == ["John", "Park", "Jun"]
    assert len(data["edges"]) == 1 and data["min_shared"] == 3
    assert {k["kind"] for k in data["kinds"]} == {"people", "places", "topics"}
    req = browse.Request(config, conn, {"from": "2030-01-01"}, lambda c: None)
    assert browse.graph(req)["nodes"] == []
