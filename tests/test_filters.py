import json
from datetime import date

import pytest
from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import cli, query, store
from journal_analyzer.models import QueryPlan


def _flat(output: str) -> str:
    return " ".join(output.split())


@pytest.fixture
def journal(conn):
    add_note(conn, "John Doe", aliases=["John"])
    add_note(conn, "Tomas Reyes", aliases=["Tomas"])
    add_entry(conn, date(2024, 1, 10), mood=8.0, tags=["climbing"], links=["John Doe"],
              summary="Climbed with John", path="/vault/a.md")
    add_entry(conn, date(2024, 2, 10), mood=4.0, tags=["work"], links=["Tomas Reyes"],
              summary="Deadline with Tomas", path="/vault/b.md")
    add_entry(conn, date(2024, 3, 10), mood=6.0, tags=["climbing", "work"], links=["John Doe"],
              summary="Lunch climb with John", path="/vault/c.md")
    return conn


def _john(conn) -> int:
    return store.find_notes(conn, "John Doe")[0]["id"]


def _dates(conn, ids):
    return [r["entry_date"] for r in store.entries_by_ids(conn, ids)]


def test_a_note_filter_is_strict_and_drops_the_routers_names(journal):
    only = store.EntryFilter(note_ids=(_john(journal),))
    ids = store.filtered_entry_ids(journal, entity_names=["Tomas"], only=only)
    assert _dates(journal, ids) == ["2024-01-10", "2024-03-10"]
    nobody = store.EntryFilter(note_ids=(9999,))
    assert store.filtered_entry_ids(journal, entity_names=["John"], only=nobody) == []


def test_tag_and_note_filters_combine(journal):
    only = store.EntryFilter(tag="work", note_ids=(_john(journal),))
    assert _dates(journal, store.filtered_entry_ids(journal, only=only)) == ["2024-03-10"]
    months = store.monthly_mood(journal, None, None, store.EntryFilter(tag="climbing"))
    assert [(r["month"], r["avg_mood"]) for r in months] == [("2024-01", 8.0), ("2024-03", 6.0)]
    assert store.date_bounds(journal, None, None, store.EntryFilter(tag="work")) == (
        "2024-02-10", "2024-03-10")
    assert store.tag_exists(journal, "work") and not store.tag_exists(journal, "nope")


def test_answer_uses_the_dates_given_over_the_routers(journal, config, fake_client):
    fake_client.structured["QueryPlan"] = QueryPlan(
        intent="factual", date_from=date(2024, 1, 1), date_to=date(2024, 1, 31)
    )
    fake_client.chat_response = "ok"
    result = query.answer("anything", config, journal, fake_client, today=date(2024, 6, 1),
                          since=date(2024, 2, 1), until=date(2024, 3, 31))
    assert _dates(journal, result.read_entry_ids) == ["2024-02-10", "2024-03-10"]


def test_answer_reads_only_the_notes_entries(journal, config, fake_client):
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual", entities=["Tomas"])
    fake_client.chat_response = "ok"
    only = store.EntryFilter(note_ids=(_john(journal),))
    result = query.answer("What about February 10, 2024?", config, journal, fake_client,
                          today=date(2024, 6, 1), only=only)
    # the named day doesn't link John, so it stays out too
    assert _dates(journal, result.read_entry_ids) == ["2024-01-10", "2024-03-10"]
    assert _john(journal) in result.read_note_ids


def test_aggregate_answers_say_what_they_counted(journal, config, fake_client):
    fake_client.structured["QueryPlan"] = QueryPlan(intent="aggregate")
    seen = []
    fake_client.chat_response = lambda model, messages: seen.append(messages[-1]["content"]) or "ok"
    query.answer("How was my mood?", config, journal, fake_client, today=date(2024, 6, 1),
                 only=store.EntryFilter(tag="work"))
    assert "Only entries tagged 'work' are counted." in seen[0]
    assert "2024-01" not in seen[0]


def test_mood_about_a_note(tmp_path, config, journal):
    result = invoke_cli(tmp_path, config, "mood", "--about", "john")
    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert "entries linking John Doe" in output
    assert "2024-01" in output and "2024-02" not in output


def test_unknown_notes_and_tags_are_refused(tmp_path, config, journal):
    result = invoke_cli(tmp_path, config, "mood", "--about", "Nobody")
    assert result.exit_code == 1
    assert "No reference note called 'Nobody'" in _flat(result.output)
    result = invoke_cli(tmp_path, config, "search", "x", "--tag", "#Nope")
    assert result.exit_code == 1
    assert "No entry is tagged 'nope'" in _flat(result.output)


def test_an_ambiguous_note_lists_the_choices(tmp_path, config, journal):
    add_note(journal, "John Lee")
    result = invoke_cli(tmp_path, config, "export", "--about", "Joh")
    assert result.exit_code == 1
    assert "matches 2 notes" in _flat(result.output)


def test_export_by_tag(tmp_path, config, journal):
    result = invoke_cli(tmp_path, config, "export", "--tag", "climbing")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert [e["date"] for e in data["entries"]] == ["2024-01-10", "2024-03-10"]


def test_ask_and_chat_pass_the_filters_on(tmp_path, config, journal, fake_client, monkeypatch):
    seen = []

    def answer(question, cfg, conn, client, **kwargs):
        seen.append(kwargs)
        return query._finalize("ok", "fast_summary")

    monkeypatch.setattr(query, "answer", answer)
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "ask", "q", "--since", "2024-02-01", "--about", "Tomas")
    assert result.exit_code == 0, result.output
    assert seen[0]["since"] == date(2024, 2, 1) and seen[0]["until"] is None
    assert seen[0]["only"].note_ids == (store.find_notes(journal, "Tomas Reyes")[0]["id"],)
    result = invoke_cli(tmp_path, config, "chat", "--tag", "work", input="q\nexit\n")
    assert result.exit_code == 0, result.output
    assert seen[1]["only"].tag == "work"
