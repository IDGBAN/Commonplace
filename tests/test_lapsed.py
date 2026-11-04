from datetime import date

import pytest
from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import store


def _flat(output: str) -> str:
    return " ".join(output.split())


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2024, 9, 15), 6, date(2024, 3, 15)),
        (date(2024, 3, 31), 1, date(2024, 2, 29)),
        (date(2024, 1, 10), 1, date(2023, 12, 10)),
        (date(2024, 5, 31), 12, date(2023, 5, 31)),
    ],
)
def test_months_before(day, months, expected):
    assert store.months_before(day, months) == expected


@pytest.fixture
def friends(conn):
    add_note(conn, "Jun Park")
    add_note(conn, "John Doe")
    add_note(conn, "Old Office", kind="locations")
    for i in range(6):
        add_entry(conn, date(2022, 2 + i, 1), links=["Jun Park", "Old Office"], path=f"/vault/j{i}.md")
    add_entry(conn, date(2023, 1, 5), links=["Jun Park"], path="/vault/j-last.md")
    for i in range(6):
        add_entry(conn, date(2024, 3 + i, 1), links=["John Doe"], path=f"/vault/s{i}.md")
    return conn


def test_lapsed_notes_were_linked_often_and_not_lately(friends):
    rows = store.lapsed_notes(friends, 5, date(2024, 3, 1))
    assert [(r.title, r.count, r.last, r.busiest_year, r.busiest_count) for r in rows] == [
        ("Jun Park", 7, "2023-01-05", "2022", 6),
        ("Old Office", 6, "2022-07-01", "2022", 6),
    ]
    assert [r.title for r in store.lapsed_notes(friends, 5, date(2024, 3, 1), "people")] == ["Jun Park"]
    assert [r.title for r in store.lapsed_notes(friends, 7, date(2024, 3, 1))] == ["Jun Park"]


def test_lapsed_command(tmp_path, config, friends):
    result = invoke_cli(tmp_path, config, "lapsed", "--type", "people")
    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert "not in the 6 months before 2024-08-01" in output
    assert "Jun Park │ people │ 7 │ 2022 (6) │ 2023-01-05 │ 18 months" in output
    assert "John Doe" not in output
    quiet = _flat(invoke_cli(tmp_path, config, "lapsed", "--min", "50").output)
    assert "Nothing you linked 50 or more times has gone 6 months without a link." in quiet


def test_lapsed_with_no_entries(tmp_path, config, conn):
    assert "No entries indexed yet" in _flat(invoke_cli(tmp_path, config, "lapsed").output)
