from datetime import date

from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import unlinked


def _flat(output: str) -> str:
    return " ".join(output.split())


def _found(conn, title):
    note_id = conn.execute("SELECT id FROM notes WHERE title = ?", (title,)).fetchone()["id"]
    return unlinked.for_note(conn, note_id)


def test_entries_that_name_a_note_without_linking_it(conn):
    add_note(conn, "John Doe", aliases=["John"])
    add_entry(conn, date(2024, 1, 1), text="Lunch with John. Then work.", path="/vault/a.md")
    add_entry(conn, date(2024, 1, 2), text="John again.", links=["John Doe"], path="/vault/b.md")
    add_entry(conn, date(2024, 1, 3), text="Saw JOHN DOE at the shop!", path="/vault/c.md")
    add_entry(conn, date(2024, 1, 4), text="Johnny called. Johnstown trip.", path="/vault/d.md")
    found = _found(conn, "John Doe")
    assert [(m.entry_date, m.name, m.snippet) for m in found] == [
        ("2024-01-03", "JOHN DOE", "Saw JOHN DOE at the shop!"),
        ("2024-01-01", "John", "Lunch with John."),
    ]


def test_a_name_inside_another_notes_longer_name_does_not_count(conn):
    add_note(conn, "John", kind="people")
    add_note(conn, "John Lee", kind="people")
    add_entry(conn, date(2024, 1, 1), text="Dinner with John Lee.", path="/vault/a.md")
    add_entry(conn, date(2024, 1, 2), text="John Lee and John both came.", path="/vault/b.md")
    assert [m.entry_date for m in _found(conn, "John")] == ["2024-01-02"]
    assert [m.entry_date for m in _found(conn, "John Lee")] == ["2024-01-02", "2024-01-01"]


def test_short_names_are_skipped(conn):
    add_note(conn, "Al")
    add_entry(conn, date(2024, 1, 1), text="Al came round.", path="/vault/a.md")
    assert _found(conn, "Al") == []


def test_long_sentences_are_cut_around_the_name():
    text = "word " * 80 + "John was there " + "word " * 80 + "."
    start = text.index("John")
    snippet = unlinked._snippet(text, start, start + 4)
    assert "John was there" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) <= unlinked.SNIPPET_CHARS + 2


def test_by_note_counts_and_filters_by_kind(conn):
    add_note(conn, "John Doe", aliases=["John"])
    add_note(conn, "Bear Mountain", kind="locations")
    add_entry(conn, date(2024, 1, 1), text="John and I climbed Bear Mountain.", path="/vault/a.md")
    add_entry(conn, date(2024, 1, 5), text="John phoned.", path="/vault/b.md")
    rows = unlinked.by_note(conn)
    assert [(r.title, r.count, r.latest) for r in rows] == [
        ("John Doe", 2, "2024-01-05"), ("Bear Mountain", 1, "2024-01-01")]
    assert [r.title for r in unlinked.by_note(conn, "locations")] == ["Bear Mountain"]


def test_unlinked_command(tmp_path, config, conn):
    add_note(conn, "John Doe", aliases=["John"])
    add_entry(conn, date(2024, 1, 1), text="Lunch with John.", path="/vault/a.md")
    listing = _flat(invoke_cli(tmp_path, config, "unlinked").output)
    assert "John Doe │ people │ 1 │ 2024-01-01" in listing
    detail = invoke_cli(tmp_path, config, "unlinked", "john")
    assert detail.exit_code == 0, detail.output
    assert "Lunch with John." in _flat(detail.output)
    assert invoke_cli(tmp_path, config, "unlinked", "Nobody").exit_code == 1
