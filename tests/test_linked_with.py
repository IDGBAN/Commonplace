import json
from datetime import date

from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import store


def _journal(conn):
    john = add_note(conn, "John Doe")
    add_note(conn, "Jane Doe")
    add_note(conn, "Bear Mountain", kind="locations")
    add_entry(conn, date(2023, 6, 1), links=["John Doe", "Jane Doe"], path="/vault/a.md")
    add_entry(conn, date(2024, 1, 1), links=["John Doe", "Jane Doe", "Bear Mountain"], path="/vault/b.md")
    add_entry(conn, date(2024, 2, 1), links=["John Doe", "Bear Mountain"], path="/vault/c.md")
    add_entry(conn, date(2024, 3, 1), links=["Jane Doe"], path="/vault/d.md")
    return john


def test_notes_linked_from_the_same_entries(conn):
    john = _journal(conn)
    together = [(r["title"], r["n"]) for r in store.notes_linked_with(conn, john, 10)]
    assert together == [("Bear Mountain", 2), ("Jane Doe", 2)]
    assert [r["title"] for r in store.notes_linked_with(conn, john, 1)] == ["Bear Mountain"]
    since = store.notes_linked_with(conn, john, 10, date_from=date(2024, 1, 15))
    assert [(r["title"], r["n"]) for r in since] == [("Bear Mountain", 1)]
    people = store.notes_linked_with(conn, john, 10, kind="People")
    assert [r["title"] for r in people] == ["Jane Doe"]


def test_note_and_entities_commands_show_them(tmp_path, config, conn):
    _journal(conn)
    conn.close()
    note = invoke_cli(tmp_path, config, "note", "John Doe")
    assert note.exit_code == 0, note.output
    assert "often linked with: Bear Mountain (2), Jane Doe (2)" in note.output

    listed = invoke_cli(tmp_path, config, "entities", "--with", "John Doe", "--json")
    assert listed.exit_code == 0, listed.output
    data = json.loads(listed.output)
    assert data["with"]["title"] == "John Doe"
    assert [(n["title"], n["n"]) for n in data["notes"]] == [("Bear Mountain", 2), ("Jane Doe", 2)]

    table = invoke_cli(tmp_path, config, "entities", "--with", "John Doe", "--since", "2024-01-15")
    assert "Bear Mountain" in table.output and "Jane Doe" not in table.output

    alone = invoke_cli(tmp_path, config, "entities", "--with", "Nobody")
    assert alone.exit_code == 1
