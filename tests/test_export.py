import json
from datetime import date

from conftest import add_entry, add_note

from journal_analyzer import export


def _seed(conn):
    add_note(conn, "Sara")
    add_entry(
        conn, date(2024, 1, 1), text="first day", mood=6.5, tags=["work"],
        links=["Sara"], summary="summary for 2024-01-01",
    )
    add_entry(
        conn, date(2024, 2, 1), text="second day", mood=6.5, tags=["rest", "family"],
        summary="summary for 2024-02-01",
    )


def test_collect_entries_carries_tags_and_linked_notes(conn):
    _seed(conn)
    entries = export.collect_entries(conn)
    assert [e["date"] for e in entries] == ["2024-01-01", "2024-02-01"]
    assert entries[0]["tags"] == ["work"]
    assert entries[0]["entities"] == [{"name": "Sara", "type": "people"}]
    assert entries[1]["entities"] == []
    assert entries[1]["tags"] == ["family", "rest"]


def test_raw_text_is_opt_in(conn):
    _seed(conn)
    assert "raw_text" not in export.collect_entries(conn)[0]
    assert export.collect_entries(conn, include_raw=True)[0]["raw_text"] == "first day"


def test_date_range_filters_both_ends(conn):
    _seed(conn)
    assert len(export.collect_entries(conn, date_from=date(2024, 1, 15))) == 1
    assert len(export.collect_entries(conn, date_to=date(2024, 1, 15))) == 1
    assert export.collect_entries(conn, date_from=date(2025, 1, 1)) == []


def test_to_json_round_trips(conn):
    _seed(conn)
    parsed = json.loads(export.to_json(export.collect_entries(conn)))
    assert len(parsed["entries"]) == 2
    assert parsed["entries"][0]["mood_score"] == 6.5


def test_to_markdown_uses_wikilink_headings(conn):
    _seed(conn)
    text = export.to_markdown(export.collect_entries(conn, include_raw=True))
    assert "## [[2024-01-01]]" in text
    assert "mood 6.5" in text
    assert "mentions: Sara" in text
    assert "first day" in text
    assert text.endswith("\n")


def test_to_markdown_handles_a_missing_mood(conn):
    add_entry(conn, date(2024, 3, 1), mood=None, summary="")
    assert "mood -" in export.to_markdown(export.collect_entries(conn))
