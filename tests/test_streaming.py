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


def _thinking_client(tmp_path, monkeypatch, inner):
    monkeypatch.setattr(ollama, "Client", lambda **kwargs: inner)
    cfg = make_config(tmp_path)
    cfg.ollama.precise_model_think = True
    return ollama_client.OllamaClient(cfg)


def test_thinking_past_its_room_is_stopped_and_answered_without(tmp_path, monkeypatch):
    # the model would think until the window filled and never answer
    closed = []

    def overthinks():
        try:
            while True:
                yield {"message": {"content": "", "thinking": "and yet "}}
        finally:
            closed.append(True)

    inner = MagicMock()
    inner.chat.side_effect = [overthinks(), _chunks("Short ", "answer.")]
    client = _thinking_client(tmp_path, monkeypatch, inner)
    client._config.ollama.think_tokens = 256
    phases, shown = [], []
    reply = client.chat_stream("fake-precise", [], shown.append, on_phase=phases.append)
    assert reply == "Short answer." and "".join(shown) == "Short answer."
    assert closed == [True]  # the request was ended, so Ollama stops writing
    first, second = inner.chat.call_args_list
    assert (first.kwargs["think"], second.kwargs["think"]) == (True, False)
    # same window, so Ollama keeps the model loaded and the prompt it already read
    assert first.kwargs["options"]["num_ctx"] == second.kwargs["options"]["num_ctx"]
    assert phases == ["reading", "thinking", "answering"]


def test_thinking_that_ends_without_an_answer_is_asked_again_without_it(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = [
        iter([{"message": {"content": "", "thinking": "hmm"}}, {"message": {"content": "  "}}]),
        _chunks("An answer."),
    ]
    client = _thinking_client(tmp_path, monkeypatch, inner)
    assert client.chat_stream("fake-precise", [], lambda _t: None) == "An answer."
    assert inner.chat.call_args.kwargs["think"] is False


def test_thinking_within_its_room_is_left_alone(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = iter([
        {"message": {"content": "", "thinking": "brief"}}, {"message": {"content": "Done."}},
    ])
    client = _thinking_client(tmp_path, monkeypatch, inner)
    assert client.chat_stream("fake-precise", [], lambda _t: None) == "Done."
    assert inner.chat.call_count == 1


def test_an_empty_reply_from_a_model_that_did_not_think_is_not_asked_again(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _chunks("")
    client = _client(tmp_path, monkeypatch, inner)
    assert client.chat_stream("fake-fast", [], lambda _t: None) == ""
    assert inner.chat.call_count == 1


def test_a_whole_reply_that_thought_and_said_nothing_is_asked_again(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = [
        {"message": {"content": "", "thinking": "on and on"}},
        {"message": {"content": "The digest."}},
    ]
    client = _thinking_client(tmp_path, monkeypatch, inner)
    assert client.chat("fake-precise", [{"role": "user", "content": "q"}]) == "The digest."
    assert [c.kwargs["think"] for c in inner.chat.call_args_list] == [True, False]


def _done(prompt_tokens, seconds):
    return {"message": {"content": ""}, "done": True,
            "prompt_eval_count": prompt_tokens, "prompt_eval_duration": int(seconds * 1e9)}


def test_reading_speed_is_learned_from_what_ollama_reports_and_kept(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.show.return_value.capabilities = ["completion"]
    messages = [{"role": "user", "content": "x" * 4000}]
    inner.chat.return_value = iter([{"message": {"content": "Hi"}}, _done(1000, 125)])
    client = _client(tmp_path, monkeypatch, inner)
    assert client.reading_seconds("m", messages) is None  # never timed
    client.chat_stream("m", messages, lambda _t: None)
    # 4 characters a token, 8 tokens a second
    assert client.reading_seconds("m", messages) == pytest.approx(125)
    # kept with the memo, so the next run starts out knowing
    later = _client(tmp_path, monkeypatch, inner)
    assert later.reading_seconds("m:latest", messages) == pytest.approx(125)


def test_a_prompt_mostly_read_from_ollamas_cache_teaches_nothing_wrong(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.show.return_value.capabilities = ["completion"]
    long = [{"role": "user", "content": "x" * 4000}]
    inner.chat.return_value = iter([{"message": {"content": "Hi"}}, _done(1000, 125)])
    client = _client(tmp_path, monkeypatch, inner)
    client.chat_stream("m", long, lambda _t: None)
    # a follow-up that only read its last few tokens
    inner.chat.return_value = iter([{"message": {"content": "Hi"}}, _done(20, 1)])
    client.chat_stream("m", [*long, {"role": "user", "content": "and?"}], lambda _t: None)
    # 200 of 4004 characters a token would be the cache talking
    inner.chat.return_value = iter([{"message": {"content": "Hi"}}, _done(200, 25)])
    client.chat_stream("m", [*long, {"role": "user", "content": "more"}], lambda _t: None)
    assert client._speed("m")[0] == pytest.approx(4.0)
    assert client._speed("m")[1] == pytest.approx(8.0)


def test_reading_is_timed_against_how_long_it_took_before(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_client, "LOAD_POLL_SECONDS", 0.01)
    inner = MagicMock()
    inner.show.return_value.capabilities = ["completion"]
    inner.ps.return_value = _running(("m", 4096))
    client = _client(tmp_path, monkeypatch, inner)
    client._speeds["m"] = (4.0, 8.0)
    ticks = threading.Event()
    seen = []

    def reading(elapsed, estimate):
        seen.append((elapsed, estimate))
        if len(seen) >= 3:
            ticks.set()

    def reply():
        ticks.wait(5)
        yield {"message": {"content": "Hi"}}

    inner.chat.return_value = reply()
    client.chat_stream("m", [{"role": "user", "content": "x" * 800}], lambda _t: None,
                       on_reading=reading)
    assert len(seen) >= 3
    assert all(estimate == pytest.approx(25) for _, estimate in seen)
    assert seen[0][0] < seen[-1][0]


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


def test_an_empty_answer_says_so_instead_of_printing_nothing(
    tmp_path, config, conn, fake_client, monkeypatch
):
    add_entry(conn, date(2024, 1, 15), summary="Hiked", path="/vault/2024-01-15.md")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = ""
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "ask", "When?")
    assert result.exit_code == 0, result.output
    assert query.NO_ANSWER.split()[0] in result.output
    assert result.output.count("Sources:") == 1
    # an empty fast answer is no answer, so the precise model got a go too
    assert fake_client.streamed_calls == [config.ollama.fast_model, config.ollama.precise_model]


def test_the_reading_bar_says_how_far_and_how_long():
    stages = []
    show = query._reading_bar(stages.append, "Re-reading 2 entries in full, about 900 words...")
    show(150, 600)
    show(30, 60)
    show(700, 600)
    assert stages[0] == (
        "Re-reading 2 entries in full, about 900 words... \u2588\u2588\u2588"
        + "\u2591" * 9 + " 25%, about 8 minutes left"
    )
    assert stages[1].endswith(" 50%, under a minute left")
    # it never claims to be done before the answer starts
    assert stages[2].endswith("99%, taking longer than last time")


def test_each_step_of_an_answer_is_timed_in_order(conn, config, fake_client):
    add_entry(conn, date(2024, 1, 15), summary="Hiked")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = lambda model, messages: (
        "INSUFFICIENT" if model == config.ollama.fast_model else "From the full text."
    )
    streamed = query.answer("q", config, conn, fake_client, today=date(2024, 6, 1),
                            on_text=lambda _t: None)
    assert [t.step for t in streamed.timings] == [
        "working out the question", "searching", "fast model reading",
        "fast model writing", "precise model reading", "precise model writing",
    ]
    assert all(t.seconds >= 0 for t in streamed.timings)
    assert streamed.model == config.ollama.precise_model
    # without streaming each call is one step
    whole = query.answer("q", config, conn, fake_client, today=date(2024, 6, 1))
    assert [t.step for t in whole.timings][2:] == ["fast model", "precise model"]
    fake_client.chat_response = "Straight from the summaries."
    fast = query.answer("q", config, conn, fake_client, today=date(2024, 6, 1))
    assert fast.model == config.ollama.fast_model


def test_timings_read_as_one_line():
    from journal_analyzer.models import Timing

    line = query.timing_line([
        Timing(step="working out the question", seconds=4.2),
        Timing(step="searching", seconds=0.1),
        Timing(step="precise model reading", seconds=1142),
        Timing(step="precise model writing", seconds=95),
    ])
    # the tenth of a second of searching isn't worth a mention, but it counts
    assert line == (
        "Took 20m 41s: working out the question 4.2s, precise model reading 19m 02s, "
        "precise model writing 1m 35s."
    )
    assert query.timing_line([]) == ""
    assert query.duration(3725) == "1h 02m"
    follow_up = query.with_step(
        query.Answer(text="a", stage_used="fast_summary"), "reading your follow-up", 3
    )
    assert follow_up.timings[0].step == "reading your follow-up"


def test_ask_says_how_long_it_took(tmp_path, config, conn, fake_client, monkeypatch):
    add_entry(conn, date(2024, 1, 15), summary="Hiked", path="/vault/2024-01-15.md")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "ask", "When?")
    assert result.exit_code == 0, result.output
    assert "Took " in result.output
    assert f"by {config.ollama.fast_model}" in result.output


def test_ask_prints_the_streamed_answer_once(tmp_path, config, conn, fake_client, monkeypatch):
    add_entry(conn, date(2024, 1, 15), summary="Hiked", path="/vault/2024-01-15.md")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked on the fifteenth."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "ask", "When?")
    assert result.exit_code == 0, result.output
    assert result.output.count("You hiked on the fifteenth.") == 1
    assert "Sources: [[2024-01-15]]" in result.output
