import io
import json
import os
import sys
import threading
from datetime import UTC, date, datetime, timedelta
from urllib.parse import quote

import pytest
from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import browse, cli, history, mcp_server
from journal_analyzer.config import Config
from journal_analyzer.models import Answer, QueryPlan

SECRET = "the answer mentions zebra-fourteen"


def _answer(text=SECRET):
    return Answer(text=f"{text}\n\nSources: [[2024-01-15]]", body=text, stage_used="fast_summary",
                  cited_dates=[date(2024, 1, 15)], cited_notes=["John Doe"])


@pytest.fixture
def kept(config):
    config.history.enabled = True
    return config


def test_nothing_is_kept_unless_its_turned_on(config):
    assert history.record(config, "q", _answer(), "ask", history.new_conversation()) is None
    assert not history.history_path(config).exists()
    # reading doesn't create it either
    assert history.recent(config) == [] and history.get(config, 1) is None
    assert not history.history_path(config).exists()


def test_a_kept_question_comes_back(kept):
    first = history.record(kept, "When did I hike?", _answer(), "ask", "a" * 32, scope="entries tagged hiking")
    found = history.get(kept, first)
    assert (found.question, found.scope, found.stage, found.source) == (
        "When did I hike?", "entries tagged hiking", "fast_summary", "ask",
    )
    assert found.answer.startswith(SECRET) and "Sources:" in found.answer
    assert found.sources == {"dates": [{"date": "2024-01-15", "section": None}], "notes": ["John Doe"]}
    assert history.history_path(kept).name == "journal-history.db"


def test_the_file_is_private(kept):
    history.record(kept, "q", _answer(), "ask", history.new_conversation())
    if sys.platform != "win32":
        assert os.stat(history.history_path(kept)).st_mode & 0o777 == 0o600


def test_deleting_overwrites_what_was_kept(kept):
    path = history.history_path(kept)
    keep_id = history.record(kept, "keep me", _answer("ordinary words"), "ask", history.new_conversation())
    gone = history.record(kept, "forget me", _answer(), "ask", history.new_conversation())
    assert SECRET.encode() in path.read_bytes()
    assert history.delete(kept, gone)
    assert not history.delete(kept, gone)
    data = path.read_bytes()
    assert SECRET.encode() not in data and b"forget me" not in data
    assert history.get(kept, keep_id) is not None
    # no -wal file to hold a copy of it
    assert not path.with_name(path.name + "-wal").exists()


def test_clearing_deletes_the_file(kept):
    path = history.history_path(kept)
    history.record(kept, "q", _answer(), "ask", history.new_conversation())
    # what a crash can leave
    path.with_name(path.name + "-journal").write_bytes(b"")
    assert history.clear(kept) == 1
    assert not path.exists() and not path.with_name(path.name + "-journal").exists()
    assert history.clear(kept) == 0


def test_only_the_newest_are_kept(kept):
    kept.history.keep = 2
    for i in range(4):
        history.record(kept, f"q{i}", _answer(), "ask", history.new_conversation())
    assert [k.question for k in history.recent(kept)] == ["q3", "q2"]


def test_old_ones_are_forgotten(kept, monkeypatch):
    kept.history.forget_after_days = 30
    old = history.record(kept, "old", _answer(), "ask", history.new_conversation())
    with history._writable(kept) as conn, conn:
        conn.execute("UPDATE asked SET asked_at = ? WHERE id = ?",
                     ((datetime.now(UTC) - timedelta(days=31)).isoformat(), old))
    # even with nothing new asked
    assert [k.question for k in history.recent(kept)] == []
    assert SECRET.encode() not in history.history_path(kept).read_bytes()


def test_a_path_inside_the_vault_is_refused(tmp_path, config):
    data = config.model_dump()
    data["history"] = {"enabled": True, "path": str(config.vault.path / "history.db")}
    with pytest.raises(ValueError, match="outside the vault"):
        Config.model_validate(data)


def test_the_last_chat_is_one_conversation(kept):
    history.record(kept, "lone", _answer(), "ask", history.new_conversation())
    talk = history.new_conversation()
    history.record(kept, "How was March?", _answer(), "chat", talk)
    history.record(kept, "and April?", _answer(), "chat", talk, standalone="How was April 2024?")
    turns = history.last_conversation(kept, "chat")
    assert [(t.question, t.standalone) for t in turns] == [("How was March?", None), ("and April?", "How was April 2024?")]


def test_ask_keeps_its_question(tmp_path, kept, conn, fake_client, monkeypatch):
    add_entry(conn, date(2024, 1, 15), text="Hiked with John.", summary="Hike", path="/vault/a.md")
    path = tmp_path / "config.toml"
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked on 2024-01-15."
    monkeypatch.setattr(cli, "OllamaClient", lambda c: fake_client)
    result = invoke_cli(tmp_path, kept, "ask", "When did I hike?", "--json")
    # invoke_cli writes a config without [history], so nothing is kept
    assert result.exit_code == 0 and json.loads(result.stdout)["history_id"] is None
    text = path.read_text(encoding="utf-8") + "[history]\nenabled = true\n"
    path.write_text(text, encoding="utf-8")
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, ["ask", "When did I hike?", "--json", "--config", str(path)])
    assert result.exit_code == 0, result.output
    kept_id = json.loads(result.stdout)["history_id"]
    assert history.get(kept, kept_id).answer.startswith("You hiked on 2024-01-15.")

    listed = CliRunner().invoke(cli.app, ["history", "--config", str(path)])
    assert listed.exit_code == 0 and "When did I hike?" in listed.output
    shown = CliRunner().invoke(cli.app, ["history", str(kept_id), "--config", str(path)])
    assert "You hiked on 2024-01-15." in shown.output
    refused = CliRunner().invoke(cli.app, ["history", "--clear", "--json", "--config", str(path)])
    assert refused.exit_code == 1 and "needs --yes" in json.loads(refused.stdout)["error"]
    kept_on = CliRunner().invoke(cli.app, ["history", "--clear", "--config", str(path)], input="n\n")
    assert kept_on.exit_code == 1 and history.history_path(kept).exists()
    cleared = CliRunner().invoke(cli.app, ["history", "--clear", "--config", str(path)], input="y\n")
    assert cleared.exit_code == 0 and "Deleted" in cleared.output
    assert not history.history_path(kept).exists()


def test_history_says_when_its_off(tmp_path, config):
    result = invoke_cli(tmp_path, config, "history")
    assert result.exit_code == 0 and "History is off" in result.output
    assert not history.history_path(config).exists()
    config.history.enabled = True
    history.record(config, "kept earlier", _answer(), "ask", history.new_conversation())
    result = invoke_cli(tmp_path, config, "history")
    flat = " ".join(result.output.split())
    assert "kept earlier" in flat and "'journal history --clear' deletes them" in flat
    assert "Question history: 1 question" in " ".join(invoke_cli(tmp_path, config, "stats").output.split())
    result = invoke_cli(tmp_path, config, "history", "--delete", "99")
    assert result.exit_code == 1 and "no question 99" in result.output


def test_chat_resume_needs_history(tmp_path, config):
    result = invoke_cli(tmp_path, config, "chat", "--resume", input="exit\n")
    assert result.exit_code == 1 and "History is off" in result.output


def test_chat_keeps_its_turns_and_picks_them_up_again(kept, conn, fake_client, capsys):
    add_entry(conn, date(2024, 3, 1), text="March.", summary="March", path="/vault/m.md")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "It was calm."
    inputs = iter(["How was March?", "exit"])
    real_input = cli.console.input
    cli.console.input = lambda prompt="": next(inputs)
    try:
        cli._chat_loop(kept, conn, fake_client)
        [turn] = history.last_conversation(kept, "chat")
        assert turn.question == "How was March?"
        seen = []

        def chat(model, messages):
            seen.append(messages[-1]["content"])
            return "It was calm."

        fake_client.chat_response = chat
        inputs = iter(["and April?", "exit"])
        cli._chat_loop(kept, conn, fake_client, earlier=[turn])
    finally:
        cli.console.input = real_input
    assert any("User: How was March?" in text for text in seen)
    turns = history.last_conversation(kept, "chat")
    assert [t.question for t in turns] == ["How was March?", "and April?"]


def test_mcp_never_keeps_questions(kept, conn, fake_client):
    add_entry(conn, date(2024, 1, 15), text="Hiked.", summary="Hike", path="/vault/a.md")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask", "arguments": {"question": "When?"}}}
    mcp_server.serve(kept, io.StringIO(json.dumps(call) + "\n"), io.StringIO(), make_client=lambda c: fake_client)
    assert not history.history_path(kept).exists()


def test_the_browser_keeps_its_answers_and_lists_them(kept, conn, fake_client):
    add_note(conn, "John Doe")
    add_entry(conn, date(2024, 1, 15), text="Hiked with John Doe.", summary="Hike", links=["John Doe"],
              path="/vault/a.md")
    kept.reranking.enabled = False
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked with [[John Doe]] on 2024-01-15."
    server = browse.make_server(kept, port=0, make_client=lambda cfg: fake_client)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import http.client

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
            c.request("GET", path)
            return c.getresponse().read().decode()

        talk = "b" * 32
        stream = get(f"/api/ask?q={quote('When did I hike?')}&conversation={talk}&tag=")
        assert '"history_id": 1' in stream
        listed = json.loads(get("/api/history"))
        assert listed["enabled"] and [i["question"] for i in listed["items"]] == ["When did I hike?"]
        assert "answer" not in listed["items"][0]
        item = json.loads(get("/api/history/1"))
        assert item["conversation"] == talk and item["sources"]["dates"][0]["id"] is not None
        assert item["sources"]["notes"] == [{"title": "John Doe", "id": 1}]
        assert "no question" in json.loads(get("/api/history/7"))["error"]
        # a conversation id that isn't one gets a new one
        get(f"/api/ask?q={quote('again?')}&conversation=nope")
        assert history.get(kept, 2).conversation != "nope"
    finally:
        server.shutdown()
        server.server_close()
