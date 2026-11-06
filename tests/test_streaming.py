import threading
from datetime import date
from types import SimpleNamespace
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


def test_think_filter_keeps_what_it_drops():
    f = ollama_client.ThinkFilter()
    f.feed("<think>weigh")
    assert f.inside and f.take_hidden() == "weigh"
    f.feed("ing it</th")
    assert f.take_hidden() == "ing it"
    assert f.feed("ink>Done") == "Done"
    assert not f.inside and f.take_hidden() == ""


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


def test_chat_stream_hands_reasoning_over_apart_from_the_reply(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = iter([
        {"message": {"content": "", "thinking": "Which day? "}},
        {"message": {"content": "", "thinking": "The fifteenth."}},
        {"message": {"content": "You hiked."}},
    ])
    client = _client(tmp_path, monkeypatch, inner)
    phases, thought, shown = [], [], []
    reply = client.chat_stream(
        "m", [], shown.append, on_phase=phases.append, on_thinking=thought.append
    )
    assert reply == "You hiked." and "".join(shown) == "You hiked."
    assert "".join(thought) == "Which day? The fifteenth."
    assert phases == ["reading", "thinking"]


def test_reasoning_left_in_the_reply_is_handed_over_too(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _chunks("<think>hm", "m</think>", "Yes.")
    client = _client(tmp_path, monkeypatch, inner)
    phases, thought, shown = [], [], []
    client.chat_stream("m", [], shown.append, on_phase=phases.append, on_thinking=thought.append)
    assert ("".join(thought), "".join(shown)) == ("hmm", "Yes.")
    assert phases == ["reading", "thinking"]


def _running(*models):
    return SimpleNamespace(models=[SimpleNamespace(model=m, context_length=n) for m, n in models])


def test_a_stream_says_when_the_model_is_loading_and_when_it_is_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_client, "LOAD_POLL_SECONDS", 0.01)
    loaded = threading.Event()
    answers = iter([_running(), _running(("other", 4096)), _running(("m", 4096))])

    def ps():
        found = next(answers, _running(("m", 4096)))
        if found.models and found.models[0].model == "m":
            loaded.set()
        return found

    def reply():
        loaded.wait(5)
        yield {"message": {"content": "Hi"}}

    inner = MagicMock()
    inner.show.return_value.capabilities = ["completion"]
    inner.ps.side_effect = ps
    inner.chat.return_value = reply()
    client = _client(tmp_path, monkeypatch, inner)
    phases = []
    assert client.chat_stream("m", [], lambda _t: None, on_phase=phases.append) == "Hi"
    assert phases == ["loading", "reading"]


def test_a_model_loaded_at_a_smaller_window_still_has_to_load(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.show.return_value.capabilities = ["completion"]
    inner.ps.return_value = _running(("m:latest", 2048))
    inner.chat.return_value = _chunks("Hi")
    client = _client(tmp_path, monkeypatch, inner)
    phases = []
    client.chat_stream("m", [], lambda _t: None, on_phase=phases.append)
    assert phases[0] == "loading"
    inner.ps.return_value = _running(("m:latest", 4096))
    inner.chat.return_value = _chunks("Hi again")
    phases.clear()
    client.chat_stream("m", [{"role": "user", "content": "2"}], lambda _t: None, on_phase=phases.append)
    assert phases == ["reading"]


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


def test_reasoning_reaches_the_answer_only_when_it_is_turned_on(conn, config, fake_client):
    add_entry(conn, date(2024, 1, 15), summary="Hiked")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked."
    fake_client.thinking_response = "Looking at January."
    thought, stages = [], []

    def ask():
        query.answer("q", config, conn, fake_client, today=date(2024, 6, 1), on_text=lambda _t: None,
                     on_thinking=thought.append, on_stage=stages.append)

    ask()
    assert thought == []
    assert stages[-1] == "Thinking before it answers..."
    config.ollama.show_thinking = True
    ask()
    assert thought == ["Looking at January."]


def test_ask_prints_reasoning_dimmed_before_the_answer():
    status = MagicMock()
    stream = cli._Streamed(status)
    with cli.console.capture() as captured:
        stream.stage("Answering from summaries...")
        stream.think("Hmm, ")
        stream.think("the fifteenth.")
        stream.stage("Re-reading 2 entries in full, about 300 words...")
        stream.think("Yes, hiking.")
        stream("You hiked.")
        stream.think("ignored once the answer has started")
    assert captured.get() == "Hmm, the fifteenth.\n\nYes, hiking.\n\nYou hiked."
    # the spinner comes back between a fast try that gave up and the precise one
    assert status.start.call_count == 1 and status.stop.call_count == 3


def test_ask_prints_the_streamed_answer_once(tmp_path, config, conn, fake_client, monkeypatch):
    add_entry(conn, date(2024, 1, 15), summary="Hiked", path="/vault/2024-01-15.md")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked on the fifteenth."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "ask", "When?")
    assert result.exit_code == 0, result.output
    assert result.output.count("You hiked on the fifteenth.") == 1
    assert "Sources: [[2024-01-15]]" in result.output
