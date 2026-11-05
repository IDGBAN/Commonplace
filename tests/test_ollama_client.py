from unittest.mock import MagicMock

import httpx
import ollama
import pytest
from conftest import make_config

from journal_analyzer import ollama_client
from journal_analyzer.models import (
    Extraction,
    ModelRequestError,
    OllamaUnavailableError,
    StructuredOutputError,
)


def _client(tmp_path, monkeypatch, inner):
    monkeypatch.setattr(ollama, "Client", lambda **kwargs: inner)
    monkeypatch.setattr(ollama_client.time, "sleep", lambda _s: None)
    return ollama_client.OllamaClient(make_config(tmp_path))


def _reply(content: str, done_reason: str = "stop") -> dict:
    return {"message": {"content": content}, "done_reason": done_reason}


def test_strip_thinking_removes_reasoning_block():
    out = ollama_client.strip_thinking(
        "<think>the user wants a summary\nlet me check</think>\nMarch was calm."
    )
    assert out == "March was calm."


def test_strip_thinking_keeps_plain_text_untouched():
    assert ollama_client.strip_thinking("March was calm.") == "March was calm."


def test_strip_thinking_handles_unterminated_block():
    assert ollama_client.strip_thinking("<think>still musing") == "still musing"


def test_chat_strips_thinking(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply("<think>hmm</think>You went hiking.")
    client = _client(tmp_path, monkeypatch, inner)
    assert client.chat("m", [{"role": "user", "content": "q"}]) == "You went hiking."


def test_connection_error_is_retried_then_reported(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = httpx.ConnectError("refused")
    client = _client(tmp_path, monkeypatch, inner)
    with pytest.raises(OllamaUnavailableError):
        client.chat("m", [])
    assert inner.chat.call_count == ollama_client.MAX_CONNECT_ATTEMPTS


def test_transient_connection_error_recovers(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = [httpx.ConnectError("restarting"), _reply("back up")]
    client = _client(tmp_path, monkeypatch, inner)
    assert client.chat("m", []) == "back up"


def test_timeout_is_not_retried(tmp_path, monkeypatch):
    # retrying would multiply request_timeout_seconds by the attempt count
    inner = MagicMock()
    inner.chat.side_effect = httpx.ReadTimeout("too slow")
    client = _client(tmp_path, monkeypatch, inner)
    with pytest.raises(OllamaUnavailableError, match="request_timeout_seconds") as exc:
        client.chat("m", [])
    assert "ollama serve" not in str(exc.value)
    assert inner.chat.call_count == 1


def test_missing_model_reports_pull_hint(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = ollama.ResponseError("model 'x' not found")
    client = _client(tmp_path, monkeypatch, inner)
    with pytest.raises(ModelRequestError, match="ollama pull fake-fast"):
        client.chat("fake-fast", [])


def test_structured_retries_once_on_invalid_json(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = [_reply("not json"), _reply('{"summary": "ok"}')]
    client = _client(tmp_path, monkeypatch, inner)
    result = client.chat_structured("m", [], Extraction)
    assert result.summary == "ok"
    assert inner.chat.call_count == 2


def test_structured_replies_lose_their_reasoning_block_before_parsing(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply('<think>tags first</think>\n{"summary": "ok"}')
    client = _client(tmp_path, monkeypatch, inner)
    assert client.chat_structured("fake-precise", [], Extraction).summary == "ok"
    assert inner.chat.call_count == 1


def test_structured_gives_up_after_the_retry(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply("still not json")
    client = _client(tmp_path, monkeypatch, inner)
    with pytest.raises(StructuredOutputError):
        client.chat_structured("m", [], Extraction)
    assert inner.chat.call_count == 2


def test_embed_rejects_an_empty_response(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.embed.return_value = {"embeddings": []}
    client = _client(tmp_path, monkeypatch, inner)
    with pytest.raises(ModelRequestError):
        client.embed("fake-embed", "text")


def test_embed_passes_options_through(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.embed.return_value = {"embeddings": [[0.1, 0.2]]}
    client = _client(tmp_path, monkeypatch, inner)
    assert client.embed("m", "text", options={"num_ctx": 4096}) == [0.1, 0.2]
    inner.embed.assert_called_once_with(model="m", input="text", options={"num_ctx": 4096})


def test_context_length_is_read_from_model_metadata_once(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.show.return_value.modelinfo = {
        "general.architecture": "qwen3",
        "qwen3.context_length": 32768,
    }
    client = _client(tmp_path, monkeypatch, inner)
    assert client.context_length("qwen3-embedding:0.6b") == 32768
    assert client.context_length("qwen3-embedding:0.6b") == 32768
    inner.show.assert_called_once_with("qwen3-embedding:0.6b")


def test_context_length_is_none_when_the_model_reports_none(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.show.return_value.modelinfo = None
    client = _client(tmp_path, monkeypatch, inner)
    assert client.context_length("m") is None


def _client_with(tmp_path, monkeypatch, inner, **ollama_changes):
    cfg = make_config(tmp_path)
    cfg = cfg.model_copy(update={"ollama": cfg.ollama.model_copy(update=ollama_changes)})
    monkeypatch.setattr(ollama, "Client", lambda **kwargs: inner)
    return ollama_client.OllamaClient(cfg)


def test_the_fast_model_answers_without_reasoning_by_default(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply("ok")
    _client(tmp_path, monkeypatch, inner).chat("fake-fast", [])
    assert inner.chat.call_args.kwargs["think"] is False


def test_the_precise_model_keeps_its_own_default_unless_configured(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply("ok")
    _client(tmp_path, monkeypatch, inner).chat("fake-precise", [])
    assert inner.chat.call_args.kwargs["think"] is None
    _client_with(tmp_path, monkeypatch, inner, precise_model_think=True).chat("fake-precise", [])
    assert inner.chat.call_args.kwargs["think"] is True


def test_models_outside_the_two_roles_are_left_alone(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply("ok")
    _client(tmp_path, monkeypatch, inner).chat("some-other-model", [])
    assert inner.chat.call_args.kwargs["think"] is None


def test_json_replies_are_capped_with_room_for_reasoning_when_the_model_thinks(
    tmp_path, monkeypatch
):
    inner = MagicMock()
    inner.chat.return_value = _reply('{"summary": "ok"}')
    client = _client(tmp_path, monkeypatch, inner)
    client.chat_structured("fake-fast", [], Extraction)
    options = inner.chat.call_args.kwargs["options"]
    assert options["num_predict"] == ollama_client.STRUCTURED_MAX_TOKENS
    client.chat_structured("fake-precise", [], Extraction)
    options = inner.chat.call_args.kwargs["options"]
    assert options["num_predict"] == (
        ollama_client.STRUCTURED_MAX_TOKENS + ollama_client.THINK_TOKENS
    )


def test_a_model_left_to_itself_that_cannot_think_gets_no_room_for_it(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.show.return_value.capabilities = ["completion"]
    inner.chat.return_value = _reply('{"summary": "ok"}')
    client = _client(tmp_path, monkeypatch, inner)
    client.chat_structured("fake-precise", [], Extraction)
    assert inner.chat.call_args.kwargs["options"]["num_predict"] == ollama_client.STRUCTURED_MAX_TOKENS
    assert inner.chat.call_args.kwargs["think"] is None
    inner.show.return_value.capabilities = ["completion", "thinking"]
    client = _client(tmp_path, monkeypatch, inner)
    client.chat_structured("fake-precise", [{"role": "user", "content": "new"}], Extraction)
    options = inner.chat.call_args.kwargs["options"]
    assert options["num_predict"] > ollama_client.STRUCTURED_MAX_TOKENS


def test_json_lost_to_reasoning_is_asked_for_again_without_it(tmp_path, monkeypatch):
    # thinking used up the reply, so there was no JSON to correct
    inner = MagicMock()
    inner.chat.side_effect = [_reply("", "length"), _reply('{"summary": "ok"}')]
    client = _client_with(tmp_path, monkeypatch, inner, precise_model_think=True)
    assert client.chat_structured("fake-precise", [], Extraction).summary == "ok"
    first, second = inner.chat.call_args_list
    assert (first.kwargs["think"], second.kwargs["think"]) == (True, False)
    assert second.kwargs["options"]["num_predict"] == ollama_client.STRUCTURED_MAX_TOKENS
    assert second.kwargs["messages"] == []


def test_an_empty_reply_after_reasoning_is_asked_for_again_without_it(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = [_reply(""), _reply('{"summary": "ok"}')]
    client = _client_with(tmp_path, monkeypatch, inner, precise_model_think=True)
    assert client.chat_structured("fake-precise", [], Extraction).summary == "ok"
    assert inner.chat.call_args.kwargs["think"] is False


def test_dropping_the_reasoning_still_leaves_the_usual_retry(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = [
        _reply("", "length"), _reply("not json"), _reply('{"summary": "ok"}'),
    ]
    client = _client_with(tmp_path, monkeypatch, inner, precise_model_think=True)
    assert client.chat_structured("fake-precise", [], Extraction).summary == "ok"
    assert inner.chat.call_count == 3
    inner.chat.side_effect = [_reply("", "length"), _reply("no"), _reply("still no")]
    with pytest.raises(StructuredOutputError):
        client.chat_structured("fake-precise", [{"role": "user", "content": "again"}], Extraction)
    assert inner.chat.call_count == 6


def test_a_reply_cut_off_without_reasoning_gets_the_usual_retry(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = [_reply('{"summ', "length"), _reply('{"summary": "ok"}')]
    client = _client(tmp_path, monkeypatch, inner)
    assert client.chat_structured("fake-fast", [], Extraction).summary == "ok"
    assert inner.chat.call_args.kwargs["think"] is False
    assert inner.chat.call_args.kwargs["messages"][-2]["content"] == '{"summ'


def test_a_thinking_model_gets_room_in_the_window_to_think(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply("ok")
    client = _client_with(tmp_path, monkeypatch, inner, precise_model_think=True)
    client.chat("fake-precise", _long(6_000))
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == 8192
    client.chat("fake-fast", _long(6_000))
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == 8192
    client = _client_with(tmp_path, monkeypatch, inner, precise_model_think=False)
    client.chat("fake-precise", _long(6_000))
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == ollama_client.DEFAULT_NUM_CTX


def _long(chars: int) -> list[dict[str, str]]:
    return [{"role": "user", "content": "x" * chars}]


def test_prompts_that_fit_still_say_which_window_they_want(tmp_path, monkeypatch):
    # Ollama's own default can be set as high as 256k tokens, which is a lot of
    # memory for a prompt this size
    inner = MagicMock()
    inner.show.return_value.capabilities = ["completion"]
    inner.chat.return_value = _reply("ok")
    _client(tmp_path, monkeypatch, inner).chat("m", _long(6_000))
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == ollama_client.DEFAULT_NUM_CTX


def test_a_long_prompt_gets_a_window_it_fits_in(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply("ok")
    client = _client(tmp_path, monkeypatch, inner)
    client.chat("m", _long(12_000))
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == 8192
    client.chat("m", _long(60_000))
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == 32768


def test_an_explicit_window_is_kept(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply("ok")
    _client(tmp_path, monkeypatch, inner).chat("m", _long(60_000), options={"num_ctx": 2048})
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == 2048


def test_a_structured_retry_is_sized_with_the_failed_reply_in_it(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.side_effect = [_reply("x" * 9_000), _reply('{"summary": "ok"}')]
    client = _client(tmp_path, monkeypatch, inner)
    client.chat_structured("m", _long(3_000), Extraction)
    first, retry = (c.kwargs["options"] for c in inner.chat.call_args_list)
    assert first["num_ctx"] == ollama_client.DEFAULT_NUM_CTX
    assert retry["num_ctx"] == 8192


def test_a_bigger_reply_cap_gets_a_window_with_room_for_it(tmp_path, monkeypatch):
    inner = MagicMock()
    inner.chat.return_value = _reply('{"summary": "ok"}')
    client = _client(tmp_path, monkeypatch, inner)
    client.chat_structured("fake-fast", _long(9_000), Extraction)
    options = inner.chat.call_args.kwargs["options"]
    assert (options["num_predict"], options["num_ctx"]) == (ollama_client.STRUCTURED_MAX_TOKENS, 8192)
    # a new prompt, since the same one would come back from the memo
    client.chat_structured("fake-fast", _long(9_003), Extraction, reply_tokens=6000)
    options = inner.chat.call_args.kwargs["options"]
    assert (options["num_predict"], options["num_ctx"]) == (6000, 16384)


def test_a_fixed_window_is_always_sent_and_grown_only_when_needed(tmp_path, monkeypatch):
    # one size for every call, so Ollama loads the model once
    inner = MagicMock()
    inner.chat.return_value = _reply("ok")
    client = _client(tmp_path, monkeypatch, inner)
    client.chat("fake-fast", _long(100))
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == 8192
    client.chat("fake-fast", _long(30_000))
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == 16384
    client.chat("fake-precise", _long(100))
    assert inner.chat.call_args.kwargs["options"]["num_ctx"] == ollama_client.DEFAULT_NUM_CTX
    assert client.window("fake-fast") == 8192
    assert client.window("fake-precise") == ollama_client.DEFAULT_NUM_CTX


def test_structured_calls_require_every_field_to_be_present(tmp_path, monkeypatch):
    # pydantic marks defaulted fields optional and models then leave them out
    inner = MagicMock()
    inner.chat.return_value = _reply('{"summary": "ok"}')
    _client(tmp_path, monkeypatch, inner).chat_structured("m", [], Extraction)
    sent = inner.chat.call_args.kwargs["format"]
    assert set(sent["required"]) == {"mood_score", "summary", "tags"}
    assert "required" not in Extraction.model_json_schema()


def test_model_names_match_across_case_and_the_latest_tag():
    assert ollama_client.same_model("nomic-embed-text", "nomic-embed-text:latest")
    assert ollama_client.same_model("Qwen3:14B", "qwen3:14b")
    # a bare name means llama3.2:latest, which isn't the :3b
    assert not ollama_client.same_model("llama3.2", "llama3.2:3b")
