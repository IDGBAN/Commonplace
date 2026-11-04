import io
import json
from datetime import date

import pytest
from conftest import add_entry, invoke_cli, plan_for

from journal_analyzer import cli, mcp_server, query, rollups, store, withhold

TRIP = "2024-03-02..2024-03-11"


def _trip(conn):
    for day in (1, 3, 8, 15):
        add_entry(conn, date(2024, 3, day), text=f"Day {day}.", summary=f"March {day}",
                  path=f"/vault/2024-03-{day:02d}.md")


@pytest.mark.parametrize(("typed", "message"), [
    ("2024-03-11..2024-03-02", "ends before it starts"),
    ("2024-01-01..2024-12-31", "is 366 days, and a span can be up to 92"),
    ("2024-02-30..2024-03-01", "isn't a date"),
])
def test_a_span_has_to_make_sense(typed, message):
    with pytest.raises(ValueError, match=message):
        store.parse_period(typed, ranges=True)


def test_spans_only_parse_where_theyre_asked_for():
    assert store.parse_period(TRIP, ranges=True) == ("range", TRIP, date(2024, 3, 2), date(2024, 3, 11))
    with pytest.raises(ValueError):
        store.parse_period(TRIP)


@pytest.mark.parametrize(("key", "title"), [
    (TRIP, "2 to 11 March 2024"),
    ("2024-02-28..2024-03-03", "28 February to 3 March 2024"),
    ("2024-12-30..2025-01-02", "30 December 2024 to 2 January 2025"),
    ("2024-03-04..2024-03-04", "4 March 2024"),
])
def test_span_titles(key, title):
    assert store.period_title("range", key) == title


def test_writing_one_and_keeping_it_current(config, conn, fake_client):
    _trip(conn)
    fake_client.chat_response = "A trip to Lisbon."
    assert rollups.write_range(config, conn, fake_client, TRIP)
    row = store.get_rollup(conn, "range", TRIP)
    assert (row["summary"], row["entry_count"], row["stale"]) == ("A trip to Lisbon.", 2, 0)
    assert "Period 2024-03-02..2024-03-11 entries:" in fake_client.chat_calls[-1][1][1]["content"]
    assert not rollups.write_range(config, conn, fake_client, "2023-01-01..2023-01-05")

    # a change inside the span marks it; one outside doesn't
    store.mark_rollups_stale(conn, date(2024, 3, 15))
    assert not store.get_rollup(conn, "range", TRIP)["stale"]
    store.mark_rollups_stale(conn, date(2024, 3, 8))
    assert store.get_rollup(conn, "range", TRIP)["stale"]

    plan = rollups.plan_rollups(config, conn, ("range",))
    assert [(p.kind, p.key, p.reason) for p in plan] == [("range", TRIP, "stale")]
    fake_client.chat_response = "Rewritten."
    counts = rollups.generate_rollups(config, conn, fake_client, ("range",))
    assert counts["range"] == 1 and store.get_rollup(conn, "range", TRIP)["summary"] == "Rewritten."


def test_rollup_keeps_spans_with_entries_and_drops_empty_ones(config, conn, fake_client):
    _trip(conn)
    store.upsert_rollup(conn, "range", TRIP, "Kept.", "[]", 5.0, 2)
    store.upsert_rollup(conn, "range", "2023-01-01..2023-01-05", "Nothing left.", "[]", 5.0, 1)
    rollups.generate_rollups(config, conn, fake_client, ("range",))
    assert store.get_rollup(conn, "range", TRIP) is not None
    assert store.get_rollup(conn, "range", "2023-01-01..2023-01-05") is None


def test_watch_rewrites_a_stale_span_it_touched(config, conn, fake_client):
    _trip(conn)
    store.upsert_rollup(conn, "range", TRIP, "Old.", "[]", 5.0, 2)
    store.mark_rollups_stale(conn, date(2024, 3, 3))
    fake_client.chat_response = "New."
    written, waiting = rollups.refresh_days(config, conn, fake_client, {date(2024, 3, 3)})
    assert ("range", TRIP) in written and waiting == set()
    assert store.get_rollup(conn, "range", TRIP)["summary"] == "New."


def test_answers_about_those_days_read_it(conn):
    _trip(conn)
    store.upsert_rollup(conn, "range", TRIP, "The Lisbon trip.", "[]", 5.0, 2)
    store.upsert_rollup(conn, "range", "2024-03-14..2024-03-20", "After the trip.", "[]", 5.0, 1)
    lines = query._digest_lines(plan_for("factual", date_from=date(2024, 3, 3), date_to=date(2024, 3, 9)), conn)
    assert lines == ["- [2 to 11 March 2024] The Lisbon trip."]
    assert query._digest_lines(plan_for("factual"), conn) == []


def test_journal_digest_writes_and_shows_a_span(tmp_path, config, conn, fake_client, monkeypatch):
    _trip(conn)
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    fake_client.chat_response = "Lisbon."
    result = invoke_cli(tmp_path, config, "digest", TRIP)
    assert result.exit_code == 0, result.output
    assert "2 to 11 March 2024" in result.output and f"journal digest {TRIP} --refresh" in result.output
    result = invoke_cli(tmp_path, config, "digest", TRIP, "--refresh", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert "Writing 1 digest" in result.stderr
    assert (data["type"], data["entries"], data["digest"]["summary"]) == ("range", 2, "Lisbon.")
    assert (data["digest"]["start"], data["digest"]["end"]) == ("2024-03-02", "2024-03-11")

    result = invoke_cli(tmp_path, config, "digest", "2024-03-11..2024-03-02")
    assert result.exit_code == 1 and "ends before it starts" in result.output
    result = invoke_cli(tmp_path, config, "compare", TRIP)
    assert result.exit_code == 1 and "isn't a period" in result.output


def test_the_browser_lists_spans_and_the_days_in_them(conn, config):
    from journal_analyzer import browse

    _trip(conn)
    store.upsert_rollup(conn, "range", TRIP, "The Lisbon trip.", "[]", 5.0, 2)

    req = browse.Request(config, conn, {}, lambda c: None)
    listed = browse.digests(req)["digests"]["range"]
    assert [(d["key"], d["start"], d["end"]) for d in listed] == [(TRIP, "2024-03-02", "2024-03-11")]
    detail = browse.digest(req, "range", TRIP)
    assert [e["date"] for e in detail["entries"]] == ["2024-03-03", "2024-03-08"]
    day = conn.execute("SELECT id FROM entries WHERE entry_date = '2024-03-03'").fetchone()[0]
    assert {"type": "range", "key": TRIP, "exists": True, "stale": False, "summary": "The Lisbon trip."} in (
        browse.entry(req, str(day))["digests"]
    )


def test_mcp_reads_spans_and_withholds_ones_with_left_out_days(config, conn, fake_client):
    _trip(conn)
    add_entry(conn, date(2024, 3, 5), text="Rough night. #therapy", path="/vault/2024-03-05.md")
    store.upsert_rollup(conn, "range", TRIP, "The Lisbon trip.", "[]", 5.0, 2)
    store.upsert_rollup(conn, "range", "2024-03-14..2024-03-20", "After.", "[]", 5.0, 1)

    def digest(period):
        out = io.StringIO()
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "digest", "arguments": {"period": period}}}
        mcp_server.serve(config, io.StringIO(json.dumps(call) + "\n"), out, make_client=lambda c: fake_client)
        return json.loads(json.loads(out.getvalue())["result"]["content"][0]["text"])

    assert digest(TRIP)["digest"]["summary"] == "The Lisbon trip."
    config.mcp.exclude_tags = ["therapy"]
    assert digest(TRIP)["digest"] is None
    assert digest("2024-03-14..2024-03-20")["digest"]["summary"] == "After."
    assert withhold.active(config)
