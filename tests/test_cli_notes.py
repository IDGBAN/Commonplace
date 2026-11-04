from datetime import date

from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import cli
from journal_analyzer.models import QueryPlan


def test_note_shows_properties_summary_links_and_linking_entries(tmp_path, config, conn):
    add_note(
        conn, "John Doe", aliases=["John"], properties={"Relationship": "Friend"},
        summary="Climbing buddy.", links=["Bear Mountain"],
    )
    add_note(conn, "Bear Mountain", kind="locations")
    add_entry(conn, date(2024, 1, 15), links=["John Doe"], summary="Hiked with John.")
    result = invoke_cli(tmp_path, config, "note", "john")
    assert result.exit_code == 0, result.output
    for expected in (
        "John Doe",
        "Relationship: Friend",
        "Climbing buddy.",
        "Linked from 1 entries",
        "links to: Bear Mountain",
        "2024-01-15",
    ):
        assert expected in result.output


def test_note_lists_candidates_when_a_name_is_ambiguous(tmp_path, config, conn):
    add_note(conn, "John Doe")
    add_note(conn, "John Lee")
    result = invoke_cli(tmp_path, config, "note", "john")
    assert result.exit_code == 1
    assert "John Doe" in result.output and "John Lee" in result.output


def test_note_reports_an_unknown_name(tmp_path, config, conn):
    result = invoke_cli(tmp_path, config, "note", "nobody")
    assert result.exit_code == 1
    assert "No reference note" in result.output


def test_entities_names_the_kinds_when_a_kind_has_no_matches(tmp_path, config, conn):
    add_note(conn, "John")
    add_entry(conn, date(2024, 1, 15), links=["John"])
    result = invoke_cli(tmp_path, config, "entities", "--type", "places")
    assert result.exit_code == 0
    assert "Kinds: people" in result.output


def test_ask_prints_lowercase_wikilinks_intact(
    tmp_path, config, conn, fake_client, monkeypatch
):
    # unescaped, rich eats [garden-project] as a style tag
    add_note(conn, "garden-project", kind="topics")
    add_entry(conn, date(2024, 1, 15), links=["garden-project"], summary="Dug the beds.")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You dug the beds for the garden-project."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "ask", "How is the garden-project going?")
    assert result.exit_code == 0, result.output
    assert "Sources: [[2024-01-15]] [[garden-project]]" in result.output
