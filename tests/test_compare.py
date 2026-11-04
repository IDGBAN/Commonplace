import json
from datetime import date

from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import compare, store


def _journal(conn):
    add_note(conn, "John Doe")
    add_note(conn, "Jane Doe")
    add_note(conn, "Office", kind="places")
    for day in range(1, 5):
        add_entry(conn, date(2024, 1, day), mood=4, tags=["work"], links=["Office", "John Doe"],
                  text="one two three", path=f"/vault/a{day}.md")
    for day in range(1, 9):
        add_entry(conn, date(2024, 2, day), mood=7, tags=["work"] if day < 3 else ["garden"],
                  links=["Jane Doe"] + (["John Doe"] if day < 5 else []), path=f"/vault/b{day}.md")


def _months(conn):
    return compare.compare(
        conn,
        ("2024-01", date(2024, 1, 1), date(2024, 1, 31)),
        ("2024-02", date(2024, 2, 1), date(2024, 2, 29)),
    )


def test_two_periods_side_by_side(conn):
    _journal(conn)
    result = _months(conn)
    assert (result.a.writing.entries, result.b.writing.entries) == (4, 8)
    assert (result.a.mood, result.b.mood) == (4.0, 7.0)
    assert [c.name for c in result.tags_down] == ["work"]
    work = result.tags_down[0]
    assert (work.a, work.b, work.a_share, work.b_share) == (4, 2, 1.0, 0.25)
    assert result.tags_up == []  # garden is new, not rising
    assert [c.name for c in result.notes_down] == ["John Doe"]
    assert [(c.name, c.kind) for c in result.notes_new] == [("Jane Doe", "people")]
    assert [c.name for c in result.notes_gone] == ["Office"]


def test_metrics_are_averaged_per_period(conn):
    first = add_entry(conn, date(2024, 1, 1), path="/vault/a.md")
    second = add_entry(conn, date(2024, 2, 1), path="/vault/b.md")
    store.replace_entry_metrics(conn, first, {"sleep": 6.0})
    store.replace_entry_metrics(conn, second, {"sleep": 8.0})
    result = _months(conn)
    assert (result.a.metrics, result.b.metrics) == ({"sleep": (6.0, 1)}, {"sleep": (8.0, 1)})


def test_the_period_before():
    assert compare.previous("year", date(2024, 1, 1)) == ("2023", date(2023, 1, 1), date(2023, 12, 31))
    assert compare.previous("month", date(2024, 3, 1)) == ("2024-02", date(2024, 2, 1), date(2024, 2, 29))
    assert compare.previous("month", date(2024, 1, 1))[0] == "2023-12"
    assert compare.previous("week", date(2024, 1, 1))[0] == "2023-W52"


def test_compare_command(tmp_path, config, conn):
    _journal(conn)
    conn.close()
    shown = invoke_cli(tmp_path, config, "compare", "2024-02")
    assert shown.exit_code == 0, shown.output
    text = " ".join(shown.output.split())
    assert "2024-01 and 2024-02" in text
    assert "Notes new in 2024-02" in text and "Jane Doe" in text
    assert "work │ 4 (100%) │ 2 (25%)" in text

    data = json.loads(invoke_cli(tmp_path, config, "compare", "2024-01", "2024-02", "--json").output)
    assert data["a"]["label"] == "2024-01" and data["b"]["writing"]["entries"] == 8
    assert data["notes_gone"][0]["name"] == "Office"

    lonely = invoke_cli(tmp_path, config, "compare", "2024-01", "2023-01")
    assert lonely.exit_code == 0 and "No entries in 2023-01" in lonely.output
    nothing = invoke_cli(tmp_path, config, "compare", "2020", "2021")
    assert nothing.exit_code == 1 and "No entries in 2020 or 2021" in nothing.output
    bad = invoke_cli(tmp_path, config, "compare", "spring")
    assert bad.exit_code == 1
