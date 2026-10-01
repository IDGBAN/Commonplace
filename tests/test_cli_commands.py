import io
import sqlite3
import sys
from datetime import date, timedelta

import pytest
from conftest import add_entry, invoke_cli

from journal_analyzer import cli, db, query
from journal_analyzer.models import ModelRequestError, OllamaUnavailableError, QueryPlan


def _flat(output: str) -> str:
    # undo rich's line wrapping
    return " ".join(output.split())


def test_doctor_fails_when_the_vault_folder_is_missing(
    tmp_path, config, fake_client, monkeypatch
):
    config.vault.path.rmdir()
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "doctor")
    assert result.exit_code == 1
    output = _flat(result.output)
    assert "FAIL vault.path: there is no folder at" in output
    # later checks still run
    assert "fast_model 'fake-fast' is pulled" in output


def test_doctor_still_checks_sqlite_vec_when_ollama_is_down(tmp_path, config, monkeypatch):
    class Down:
        def list_models(self):
            raise OllamaUnavailableError("Cannot reach Ollama")

    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: Down())
    result = invoke_cli(tmp_path, config, "doctor")
    assert result.exit_code == 1
    output = _flat(result.output)
    assert "FAIL Cannot reach Ollama" in output
    assert "OK sqlite-vec extension loads" in output


def test_doctor_keeps_going_when_ollama_rejects_the_model_list(
    tmp_path, config, monkeypatch
):
    class Rejecting:
        def list_models(self):
            raise ModelRequestError("Ollama rejected the request: internal error")

    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: Rejecting())
    result = invoke_cli(tmp_path, config, "doctor")
    assert result.exit_code == 1
    output = _flat(result.output)
    assert "FAIL Ollama rejected the request" in output
    assert "OK sqlite-vec extension loads" in output


def test_commands_close_the_cache_when_they_finish(tmp_path, config, conn, monkeypatch):
    opened: list[sqlite3.Connection] = []
    real_connect = db.connect

    def tracking(cfg):
        opened.append(real_connect(cfg))
        return opened[-1]

    monkeypatch.setattr(db, "connect", tracking)
    add_entry(conn, date(2024, 1, 15))
    assert invoke_cli(tmp_path, config, "tags").exit_code == 0
    # exits early, from inside the open block
    assert invoke_cli(tmp_path, config, "show", "1999-01-01").exit_code == 1
    assert len(opened) == 2
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


def test_redirected_output_is_utf8_whatever_the_code_page(monkeypatch):
    raw = io.BytesIO()
    redirected = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", redirected)
    cli._utf8_output()
    print("Beach day 🌊")
    redirected.flush()
    assert raw.getvalue().decode("utf-8").strip() == "Beach day 🌊"


def test_a_range_that_ends_before_it_starts_is_refused(tmp_path, config, conn):
    result = invoke_cli(
        tmp_path, config, "tags", "--since", "2024-02-01", "--until", "2024-01-01"
    )
    assert result.exit_code == 1
    assert "--since 2024-02-01 is after --until 2024-01-01" in _flat(result.output)


def test_a_malformed_date_is_echoed_back_literally(tmp_path, config, conn):
    result = invoke_cli(tmp_path, config, "mood", "--since", "[bold]soon")
    assert result.exit_code == 1
    assert "got '[bold]soon'" in _flat(result.output)


def test_ctrl_c_during_a_chat_answer_drops_only_that_answer(
    tmp_path, config, conn, fake_client, monkeypatch
):
    asked: list[str] = []

    def plan(model, messages):
        asked.append(messages[-1]["content"])
        if len(asked) == 1:
            raise KeyboardInterrupt
        return QueryPlan(intent="factual")

    fake_client.structured["QueryPlan"] = plan
    fake_client.chat_response = "Nothing much."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "chat", input="first?\nsecond?\nexit\n")
    assert result.exit_code == 0, result.output
    assert "(cancelled)" in result.output
    assert asked == ["first?", "second?"]


def test_export_reports_a_path_it_cannot_write(tmp_path, config, conn):
    add_entry(conn, date(2024, 1, 15))
    target = tmp_path / "taken"
    target.mkdir()
    result = invoke_cli(tmp_path, config, "export", "--out", str(target))
    assert result.exit_code == 1
    assert "Could not write" in _flat(result.output)
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_stats_points_out_entries_dated_in_the_future(tmp_path, config, conn):
    add_entry(conn, date(2024, 1, 15), path="/vault/2024-01-15.md")
    add_entry(conn, date.today() + timedelta(days=400), path="/vault/typo.md")
    result = invoke_cli(tmp_path, config, "stats")
    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert "dated in the future" in output
    assert "typo.md" in output
    assert "2024-01-15.md" not in output


def test_chat_follow_ups_build_on_the_rewritten_questions(
    tmp_path, config, conn, fake_client, monkeypatch
):
    rewrites = {"and April?": "How was April 2024?", "and May?": "How was May 2024?"}
    transcripts: list[str] = []

    def chat(model, messages):
        assert messages[0]["content"] == query._CONTEXTUALIZE_SYSTEM
        content = messages[-1]["content"]
        transcripts.append(content)
        latest = content.split("Latest message: ", 1)[1].split("\n", 1)[0]
        return rewrites[latest]

    fake_client.chat_response = chat
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(
        tmp_path, config, "chat", input="How was March 2024?\nand April?\nand May?\nexit\n"
    )
    assert result.exit_code == 0, result.output
    assert len(transcripts) == 2
    assert "User: How was April 2024?" in transcripts[-1]
    assert "User: and April?" not in transcripts[-1]
