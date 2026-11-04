import io
import json
import sqlite3
from datetime import date

import pytest
from conftest import EMBED_DIM, add_entry, add_note

from journal_analyzer import db, mcp_server, store, withhold
from journal_analyzer.models import (
    ConfigError,
    Extraction,
    OllamaUnavailableError,
    ParsedEntry,
)
from journal_analyzer.parser import Section
from journal_analyzer.profile import TEXT_KEYS, text_hash


def _session(config, fake_client, *messages):
    out = io.StringIO()
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    mcp_server.serve(config, stdin, out, make_client=lambda cfg: fake_client)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _call(msg_id, tool, **arguments):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


def _payload(reply):
    result = reply["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _error(reply):
    assert reply["result"]["isError"] is True, reply
    return reply["result"]["content"][0]["text"]


THERAPY_TEXT = "Session with Dr Smith about the panic. #therapy"


def _journal(conn):
    john = add_note(conn, "John Doe", text="Friend from school.")
    add_note(conn, "Dr Smith", kind="health", text="My therapist since 2023.")
    add_note(conn, "Coping plan", kind="topics", text="Breathe first. #therapy")
    add_note(conn, "Lisbon", kind="events", links=["2024-03-04"], text="A trip.")
    hidden = add_entry(conn, date(2024, 3, 4), text=THERAPY_TEXT, mood=2, tags=["health"],
                       links=["John Doe"], summary="Panic session", path="/vault/2024-03-04.md")
    store.replace_entry_sections(conn, hidden, [Section("Morning", 1, 2, "quarterly panic review " * 20)], [None])
    # tagged only in the file's frontmatter
    store.upsert_file(conn, "/vault/2024-03-05.md", "h", "indexed")
    framed = store.save_entry(
        conn, ParsedEntry(date(2024, 3, 5), "Quiet day at home.", "/vault/2024-03-05.md"),
        Extraction(mood_score=3, summary="Quiet day", tags=["home"]), "m",
    )
    store.save_file_frontmatter(conn, "/vault/2024-03-05.md", {"tags": ["therapy"]})
    linked = add_entry(conn, date(2024, 5, 1), text="Called Dr Smith to reschedule.", mood=6,
                       links=["Dr Smith"], summary="Rescheduled", path="/vault/2024-05-01.md")
    add_entry(conn, date(2024, 7, 1), text="Beach with John Doe.", mood=8, tags=["beach"],
              links=["John Doe"], summary="Beach day", path="/vault/2024-07-01.md")
    for entry_id in (hidden, framed, linked):
        store.save_entry_vector(conn, entry_id, [0.5] * EMBED_DIM)
    for period_type, key in (("month", "2024-03"), ("month", "2024-05"), ("month", "2024-07"),
                             ("year", "2024"), ("week", "2024-W10")):
        store.upsert_rollup(conn, period_type, key, f"Digest of {key}.", "[]", 5.0, 1)
    for key in ("john doe", "dr smith", "lisbon"):
        conn.execute("INSERT INTO note_digests(link_key, summary, entry_count) VALUES (?, ?, 1)",
                     (key, f"How {key} figures."))
    conn.execute(
        "INSERT INTO profile_facts(text_hash, entry_date, category, fact, folded) VALUES (?, ?, ?, ?, 1)",
        (text_hash(THERAPY_TEXT), "2024-03-04", "health", "I see a therapist."),
    )
    for key, value in zip(TEXT_KEYS, ("I see a therapist.", "Al", "2024-08-01"), strict=True):
        db.set_meta(conn, key, value)
    conn.commit()
    return john


@pytest.fixture
def private(config, conn):
    _journal(conn)
    config.mcp.exclude_tags = ["therapy"]
    config.mcp.exclude_kinds = ["health"]
    return config


def test_left_out_entries_and_notes_dont_reach_any_tool(private, conn, fake_client):
    def offline(model, text, options=None):
        raise OllamaUnavailableError("Ollama isn't running.")

    fake_client.embed = offline
    replies = _session(
        private, fake_client,
        _call(1, "day", date="2024-03-04"),
        _call(2, "day", date="2024-03-05"),
        _call(3, "search", query="quarterly panic session"),
        _call(4, "tags"),
        _call(5, "mood"),
        _call(6, "note", name="Dr Smith"),
        _call(7, "notes"),
        _call(8, "broken_links"),
        _call(9, "day", date="2024-05-01"),
        _call(10, "note", name="Coping plan"),
    )
    assert "no entry on 2024-03-04" in _error(replies[0])
    assert "no entry on 2024-03-05" in _error(replies[1])
    found = _payload(replies[2])
    assert found["entries"] == [] and found["notes"] == []
    assert [t["name"] for t in _payload(replies[3])["tags"]] == ["beach"]
    assert [m["month"] for m in _payload(replies[4])["months"]] == ["2024-05", "2024-07"]
    assert "No note is called 'Dr Smith'" in _error(replies[5])
    assert {n["title"] for n in _payload(replies[6])["notes"]} == {"John Doe", "Lisbon"}
    # the link to a left-out note isn't listed as broken, which would give its name away
    assert _payload(replies[7])["links"] == []
    [rescheduled] = _payload(replies[8])["entries"]
    assert rescheduled["notes"] == []
    assert "No note is called 'Coping plan'" in _error(replies[9])


def test_what_was_written_from_them_is_left_out_too(private, conn, fake_client):
    replies = _session(
        private, fake_client,
        _call(1, "digest", period="2024-03"),
        _call(2, "digest", period="2024-05"),
        _call(3, "digest", period="2024-07"),
        _call(4, "digest", period="2024"),
        _call(5, "note", name="John Doe"),
        _call(6, "note", name="Lisbon"),
        _call(7, "profile"),
    )
    # March had the tagged entries, May an entry that links Dr Smith
    assert _payload(replies[0])["digest"] is None
    assert _payload(replies[1])["digest"] is None
    assert _payload(replies[2])["digest"]["summary"] == "Digest of 2024-07."
    assert _payload(replies[3])["digest"] is None
    john = _payload(replies[4])
    assert john["digest"] is None and john["mentions"] == 1
    # Lisbon's digest read the day it links
    assert _payload(replies[5])["digest"] is None
    shown = _payload(replies[6])
    assert shown["profile"] is None and shown["recent_facts"] == []


def test_the_cache_itself_is_never_changed(private, conn, fake_client):
    before = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    _session(private, fake_client, _call(1, "notes"))
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == before == 4
    assert db.get_meta(conn, TEXT_KEYS[0]) == "I see a therapist."


def test_the_copy_follows_changes_to_the_cache(private, conn):
    filtered = withhold.FilteredCache(private)
    first = filtered.connection()
    assert first.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
    assert filtered.connection() is first
    add_entry(conn, date(2024, 8, 1), summary="New day", path="/vault/2024-08-01.md")
    add_entry(conn, date(2024, 8, 2), text="More #therapy.", summary="Hidden", path="/vault/2024-08-02.md")
    again = filtered.connection()
    assert again is not first
    assert [r["entry_date"] for r in again.execute("SELECT entry_date FROM entries ORDER BY entry_date")] == [
        "2024-05-01", "2024-07-01", "2024-08-01",
    ]
    with pytest.raises(sqlite3.OperationalError, match=r"readonly|read-only"):
        again.execute("DELETE FROM entries")
    filtered.close()


def test_a_tag_the_settings_drop_still_leaves_its_entries_out(config, conn, fake_client):
    entry = add_entry(conn, date(2024, 1, 1), summary="Checkup", path="/vault/a.md")
    conn.execute("INSERT INTO entry_readings(entry_id, mood_score, tags) VALUES (?, 5, '[\"therapy\"]')", (entry,))
    conn.commit()
    config.mcp.exclude_tags = ["Therapy"]
    config.mcp = config.mcp.model_validate(config.mcp.model_dump())
    [reply] = _session(config, fake_client, _call(1, "day", date="2024-01-01"))
    assert "no entry on 2024-01-01" in _error(reply)


def test_without_raw_text_only_summaries_go_out(config, conn, fake_client):
    _journal(conn)
    add_entry(conn, date(2024, 9, 1), text="Saw John Doe at the market.", summary="Market",
              path="/vault/2024-09-01.md")
    config.mcp.raw_text = False
    day, note, unlinked = (_payload(r) for r in _session(
        config, fake_client, _call(1, "day", date="2024-07-01"), _call(2, "note", name="John Doe"),
        _call(3, "unlinked", name="John Doe"),
    ))
    assert "text" not in day["entries"][0] and day["entries"][0]["summary"] == "Beach day"
    assert "text" not in note and note["summary"] is None
    assert unlinked["entries"] and all("snippet" not in m for m in unlinked["entries"])


def test_only_the_listed_tools_are_offered(config, conn, fake_client):
    _journal(conn)
    config.mcp.tools = ["day", "mood"]
    listed, mood, refused = _session(
        config, fake_client,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        _call(2, "mood"),
        _call(3, "search", query="beach"),
    )
    assert [t["name"] for t in listed["result"]["tools"]] == ["day", "mood"]
    assert _payload(mood)["months"]
    assert "no tool called 'search'" in _error(refused)


def test_an_unknown_tool_in_the_config_is_refused(config):
    config.mcp.tools = ["day", "delete"]
    with pytest.raises(ConfigError, match="'delete', which isn't"):
        mcp_server.Server(config)


def test_the_server_says_what_it_leaves_out(private, conn, fake_client, capsys):
    private.mcp.raw_text = False
    _session(private, fake_client)
    err = capsys.readouterr().err
    assert "Leaving out 2 entries, 2 notes, 4 digests, 3 relationship digests and your profile" in err
    assert "the text of entries and notes" in err


def test_a_day_with_something_left_on_it_still_resolves(config, conn, fake_client):
    add_note(conn, "Lisbon", kind="events", links=["2024-03-04"], text="A trip.")
    add_entry(conn, date(2024, 3, 4), text="Morning. #therapy", summary="Hidden", path="/vault/a.md")
    add_entry(conn, date(2024, 3, 4), text="Evening walk.", summary="Walk", path="/vault/b.md")
    config.mcp.exclude_tags = ["therapy"]
    day, note, broken = (_payload(r) for r in _session(
        config, fake_client, _call(1, "day", date="2024-03-04"), _call(2, "note", name="Lisbon"),
        _call(3, "broken_links"),
    ))
    assert [e["summary"] for e in day["entries"]] == ["Walk"]
    assert broken["links"] == []
    assert note["title"] == "Lisbon"
