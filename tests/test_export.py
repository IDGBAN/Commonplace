import csv
import io
import json
from datetime import date

from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import export, store


def _seed(conn):
    add_note(conn, "John")
    add_entry(
        conn, date(2024, 1, 1), text="first day", mood=6.5, tags=["work"],
        links=["John"], summary="summary for 2024-01-01",
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
    assert entries[0]["entities"] == [{"name": "John", "type": "people"}]
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
    assert "mentions: John" in text
    assert "first day" in text
    assert text.endswith("\n")


def test_to_markdown_handles_a_missing_mood(conn):
    add_entry(conn, date(2024, 3, 1), mood=None, summary="")
    assert "mood -" in export.to_markdown(export.collect_entries(conn))


def _rows(text):
    return list(csv.reader(io.StringIO(text)))


def test_to_csv_has_a_row_per_entry_and_a_column_per_metric(conn):
    _seed(conn)
    first, second = (r[0] for r in conn.execute("SELECT id FROM entries ORDER BY entry_date"))
    store.replace_entry_metrics(conn, first, {"sleep": 7.5})
    store.replace_entry_metrics(conn, second, {"steps": 8000, "words": 3})
    rows = _rows(export.to_csv(export.collect_entries(conn)))
    assert rows[0] == [
        "date", "mood", "words", "tags", "notes", "sleep", "steps", "words (metric)",
        "summary", "details", "source_path",
    ]
    assert rows[1][:8] == ["2024-01-01", "6.5", "2", "work", "John", "7.5", "", ""]
    assert rows[2][:8] == ["2024-02-01", "6.5", "2", "family; rest", "", "", "8000", "3"]
    assert rows[1][8] == "summary for 2024-01-01"


def test_to_csv_keeps_text_out_of_spreadsheet_formulas(conn):
    add_entry(conn, date(2024, 3, 1), text="- [x] pack lunch\n=SUM(A1)", summary="=1+1", mood=None)
    entry = conn.execute("SELECT id FROM entries").fetchone()[0]
    store.replace_entry_metrics(conn, entry, {"balance": -3})
    rows = _rows(export.to_csv(export.collect_entries(conn, include_raw=True)))
    header, row = rows
    cells = dict(zip(header, row, strict=True))
    assert cells["summary"] == "'=1+1"
    assert cells["raw_text"] == "'- [x] pack lunch\n=SUM(A1)"
    # numbers stay numbers
    assert cells["balance"] == "-3"
    assert cells["mood"] == ""


def test_to_csv_quotes_commas_and_line_breaks(conn):
    add_entry(conn, date(2024, 3, 1), text="a, b\nc", summary='Said "hi", then left.')
    rows = _rows(export.to_csv(export.collect_entries(conn, include_raw=True)))
    assert rows[1][-1] == "a, b\nc"
    assert rows[1][-4] == 'Said "hi", then left.'


def test_csv_file_starts_with_a_byte_order_mark_for_excel(tmp_path, config, conn):
    _seed(conn)
    target = tmp_path / "out" / "journal.csv"
    result = invoke_cli(tmp_path, config, "export", "--format", "csv", "--out", str(target))
    assert result.exit_code == 0, result.output
    data = target.read_bytes()
    assert data.startswith(b"\xef\xbb\xbfdate,mood,")
    assert _rows(data.decode("utf-8-sig").replace("\r\n", "\n"))[1][0] == "2024-01-01"


def test_csv_on_stdout_has_no_byte_order_mark_or_blank_last_line(tmp_path, config, conn):
    _seed(conn)
    result = invoke_cli(tmp_path, config, "export", "--format", "csv")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("date,mood,")
    assert result.output.endswith("\n") and not result.output.endswith("\n\n")
