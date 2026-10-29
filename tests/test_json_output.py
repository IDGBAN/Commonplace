import json
from datetime import date

import pytest
from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import cli, store
from journal_analyzer.models import QueryPlan


@pytest.fixture
def journal(conn):
    add_note(conn, "John Doe", aliases=["John"], text="Friend from school.")
    add_entry(conn, date(2024, 1, 15), mood=8.0, tags=["hiking"], links=["John Doe"],
              text="Hiked with John.", summary="Hiked with John", path="/vault/2024-01-15.md")
    add_entry(conn, date(2024, 2, 1), mood=4.0, tags=["work"], text="Long day. John called.",
              summary="Long day", path="/vault/2024-02-01.md")
    return conn


def _json(tmp_path, config, *args, code=0):
    result = invoke_cli(tmp_path, config, *args, "--json")
    assert result.exit_code == code, result.output
    return json.loads(result.stdout)


def test_show_and_note(tmp_path, config, journal):
    day = _json(tmp_path, config, "show", "2024-01-15", "--raw")
    [entry] = day["entries"]
    assert (entry["date"], entry["mood"], entry["tags"]) == ("2024-01-15", 8.0, ["hiking"])
    assert entry["notes"][0]["title"] == "John Doe"
    assert entry["raw_text"] == "Hiked with John." and entry["metrics"] == {}

    note = _json(tmp_path, config, "note", "john")
    assert (note["title"], note["mentions"], note["first"]) == ("John Doe", 1, "2024-01-15")
    assert note["by_year"] == [{"year": "2024", "n": 1}]
    assert [e["date"] for e in note["entries"]] == ["2024-01-15"]
    assert "raw_text" not in note


def test_lists(tmp_path, config, journal):
    assert _json(tmp_path, config, "tags")["tags"] == [{"name": "hiking", "n": 1}, {"name": "work", "n": 1}]
    assert _json(tmp_path, config, "entities")["notes"] == [{"name": "John Doe", "kind": "people", "n": 1}]
    mood = _json(tmp_path, config, "mood", "--tag", "work")
    assert mood["tag"] == "work" and mood["months"] == [
        {"month": "2024-02", "avg_mood": 4.0, "entry_count": 1}]
    streaks = _json(tmp_path, config, "streaks")
    assert (streaks["entries"], streaks["first_day"]) == (2, "2024-01-15")
    stats = _json(tmp_path, config, "stats")
    assert stats["entries"] == 2 and stats["note_kinds"] == [{"kind": "people", "n": 1}]
    assert _json(tmp_path, config, "on-this-day", "--date", "2025-01-15")["entries"][0]["date"] == "2024-01-15"
    unlinked = _json(tmp_path, config, "unlinked", "John Doe")
    assert unlinked["entries"][0]["entry_date"] == "2024-02-01"


def test_metric_and_digest(tmp_path, config, journal):
    store.replace_entry_metrics(journal, 1, {"sleep": 7.0})
    assert _json(tmp_path, config, "metric")["metrics"][0]["key"] == "sleep"
    assert _json(tmp_path, config, "metric", "sleep")["months"] == [
        {"month": "2024-01", "avg": 7.0, "n": 1}]
    store.upsert_rollup(journal, "month", "2024-01", "Calm.", '["hiking"]', 8.0, 1)
    digest = _json(tmp_path, config, "digest", "2024-01")
    assert digest["digest"]["summary"] == "Calm." and digest["entries"] == 1


def test_errors_are_json_too(tmp_path, config, journal):
    assert _json(tmp_path, config, "show", "1999-01-01", code=1) == {"error": "No indexed entry on 1999-01-01."}
    assert "ISO date" in _json(tmp_path, config, "mood", "--since", "soon", code=1)["error"]
    add_note(journal, "John Lee")
    ambiguous = _json(tmp_path, config, "note", "Joh", code=1)
    assert {m["title"] for m in ambiguous["matches"]} == {"John Doe", "John Lee"}
    assert _json(tmp_path, config, "metric", "steps", code=1)["known"] == []


def test_cache_errors_are_json(tmp_path, config, journal):
    journal.execute("PRAGMA user_version = 99")
    journal.commit()
    assert "newer version" in _json(tmp_path, config, "stats", code=1)["error"]


def test_ask_and_search(tmp_path, config, journal, fake_client, monkeypatch):
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked with John on 2024-01-15."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    answer = _json(tmp_path, config, "ask", "When did I hike?")
    assert answer["question"] == "When did I hike?"
    assert answer["body"] == "You hiked with John on 2024-01-15."
    assert answer["stage"] == "fast_summary"
    [day] = [d for d in answer["sources"]["dates"] if d["date"] == "2024-01-15"]
    assert (day["id"], day["section"]) == (1, None)
    assert "hike" in day["quote"].lower()
    assert fake_client.streamed_calls == []  # nothing to stream into
    found = _json(tmp_path, config, "search", "hiked")
    assert found["query"] == "hiked" and found["entries"][0]["date"] == "2024-01-15"


def test_refresh_progress_stays_out_of_the_json(tmp_path, config, journal, fake_client, monkeypatch):
    fake_client.chat_response = "Written."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "digest", "2024-01", "--refresh", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["digest"]["summary"] == "Written."
    assert "Writing 1 digest" in result.stderr


def test_the_same_process_goes_back_to_tables(tmp_path, config, journal):
    _json(tmp_path, config, "show", "1999-01-01", code=1)
    result = invoke_cli(tmp_path, config, "show", "1999-01-01")
    assert result.output.strip() == "No indexed entry on 1999-01-01."
