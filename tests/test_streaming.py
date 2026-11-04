from datetime import date
from unittest.mock import MagicMock

import httpx
import ollama
import pytest
from conftest import add_entry, invoke_cli, make_config

from journal_analyzer import cli, ollama_client, query
from journal_analyzer.models import OllamaUnavailableError, QueryPlan


def _feed(pieces):
    f = ollama_client.ThinkFilter()
    out = "".join(f.feed(p) for p in pieces)
    return out + f.flush()


@pytest.mark.parametrize(
    ("pieces", "shown"),
    [
        (["<thi", "nk>reason", "ing</th", "ink>\nHello", " world"], "Hello world"),
        (["<THINK>x</THINK>Hi"], "Hi"),
        (["a < b", " and <c>"], "a < b and <c>"),
        (["Hello <", "think>hidden</think> there"], "Hello there"),
        (["<think>never closed"], ""),
        (["ends with <th"], "ends with <th"),
    ],
)
def test_think_filter_across_pieces(pieces, shown):
    assert _feed(pieces) == shown


def _chunks(*texts):
    return iter([{"message": {"content": t}} for t in texts])


def _client(tmp_path, monkeypatch, inner):
    monkeypatch.setattr(ollama, "Client", lambda **kwargs: inner)
    monkeypatch.setattr(ollama_client.time, "sleep", lambda _s: None)
    return ollama_client.OllamaClient(make_config(tmp_path))


def test_chat_stream_hands_over_pieces_and_returns_the_reply(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _chunks("<think>hm</think>", "You ", "went ", "hiking.")
    client = _client(tmp_path, monkeypatch, inner)
    seen = []
    assert client.chat_stream("m", [{"role": "user", "content": "q"}], seen.append) == "You went hiking."
    assert "".join(seen) == "You went hiking."
    assert inner.chat.call_args.kwargs["stream"] is True


def test_chat_stream_retries_a_refused_connection(tmp_path, monkeypatch):
    def refused():
        raise httpx.ConnectError("restarting")
        yield  # pragma: no cover

    inner = MagicMock()
    inner.chat.side_effect = [refused(), _chunks("back")]
    client = _client(tmp_path, monkeypatch, inner)
    assert client.chat_stream("m", [], lambda _t: None) == "back"
    assert inner.chat.call_count == 2


def test_chat_stream_reports_a_dropped_connection(tmp_path, monkeypatch):
    def dropped():
        yield {"message": {"content": "Half"}}
        raise httpx.RemoteProtocolError("closed")

    inner = MagicMock()
    inner.chat.return_value = dropped()
    client = _client(tmp_path, monkeypatch, inner)
    with pytest.raises(OllamaUnavailableError):
        client.chat_stream("m", [], lambda _t: None)


def _held(pieces):
    shown = []
    hold = query._HoldUntilAnswer(shown.append)
    for p in pieces:
        hold(p)
    hold.finish("".join(pieces))
    return "".join(shown), hold.passing


def test_a_fast_reply_is_held_until_it_cannot_be_insufficient():
    assert _held(["**INSUF", "FICIENT", "**"]) == ("", False)
    assert _held(["In", " March", " you hiked."]) == ("In March you hiked.", True)
    assert _held(["Insufficient", "ly documented, but yes."]) == (
        "Insufficiently documented, but yes.", True)
    assert _held(["Insufficient"]) == ("", None)
    assert _held(["Yes."]) == ("Yes.", True)


def test_answers_stream_from_whichever_stage_answers(conn, config, fake_client):
    add_entry(conn, date(2024, 1, 15), summary="Hiked")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = lambda model, messages: (
        "INSUFFICIENT" if model == config.ollama.fast_model else "  From the full text."
    )
    shown = []
    result = query.answer("q", config, conn, fake_client, today=date(2024, 6, 1), on_text=shown.append)
    assert result.stage_used == "precise_raw"
    assert "".join(shown) == "From the full text."
    assert fake_client.streamed_calls == [config.ollama.fast_model, config.ollama.precise_model]


def test_ask_prints_the_streamed_answer_once(tmp_path, config, conn, fake_client, monkeypatch):
    add_entry(conn, date(2024, 1, 15), summary="Hiked", path="/vault/2024-01-15.md")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked on the fifteenth."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "ask", "When?")
    assert result.exit_code == 0, result.output
    assert result.output.count("You hiked on the fifteenth.") == 1
    assert "Sources: [[2024-01-15]]" in result.output
