from __future__ import annotations

import itertools
import re
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any, TypeVar

import httpx
import ollama
from pydantic import BaseModel, ValidationError

from .config import Config
from .models import ModelRequestError, OllamaUnavailableError, StructuredOutputError

T = TypeVar("T", bound=BaseModel)

# answers only; extraction, routing and rollups run at 0
SYNTHESIS_TEMPERATURE = 0.3

# Ollama refuses connections for a moment while it restarts or loads a model,
# so those are retried. Timeouts aren't, since waiting the full timeout again
# just makes it look like this tool hung.
MAX_CONNECT_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 0.5

# Real JSON replies are a few hundred tokens. One this long is a model stuck
# in a loop.
STRUCTURED_MAX_TOKENS = 1024

# Ollama's default window. A longer prompt gets cut from the middle without
# any error, so those ask for more room. Prompts that fit leave num_ctx unset
# because changing it makes Ollama reload the model.
DEFAULT_NUM_CTX = 4096
REPLY_TOKENS = 1024
# low on purpose so "fits" really means fits
CHARS_PER_TOKEN_ESTIMATE = 3
# role markers etc.
MESSAGE_OVERHEAD_TOKENS = 8

_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError)
# qwen3 and similar models leave their <think> block in the content
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    cleaned = _THINK_BLOCK_RE.sub("", text)
    # unclosed <think>, the reply was cut off mid-reasoning
    if "<think>" in cleaned.lower():
        cleaned = cleaned[cleaned.lower().index("<think>") + len("<think>"):]
    return cleaned.strip()


class ThinkFilter:
    """Drops <think>...</think> from text that arrives a piece at a time."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    @staticmethod
    def _partial_tag(text: str, tag: str) -> int:
        # how many trailing chars could be the start of tag
        lowered = text.lower()
        for k in range(min(len(tag) - 1, len(text)), 0, -1):
            if lowered.endswith(tag[:k]):
                return k
        return 0

    def feed(self, text: str) -> str:
        self._buffer += text
        shown: list[str] = []
        while True:
            lowered = self._buffer.lower()
            if self._inside:
                end = lowered.find(self._CLOSE)
                if end == -1:
                    keep = self._partial_tag(self._buffer, self._CLOSE)
                    self._buffer = self._buffer[len(self._buffer) - keep:] if keep else ""
                    break
                self._buffer = self._buffer[end + len(self._CLOSE):].lstrip()
                self._inside = False
                continue
            start = lowered.find(self._OPEN)
            if start == -1:
                keep = self._partial_tag(self._buffer, self._OPEN)
                shown.append(self._buffer[: len(self._buffer) - keep])
                self._buffer = self._buffer[len(self._buffer) - keep:]
                break
            shown.append(self._buffer[:start])
            self._buffer = self._buffer[start + len(self._OPEN):]
            self._inside = True
        return "".join(shown)

    def flush(self) -> str:
        rest = "" if self._inside else self._buffer
        self._buffer = ""
        return rest


def same_model(a: str, b: str) -> bool:
    # Ollama ignores case and reads a bare name as name:latest
    return _canonical_model(a) == _canonical_model(b)


def _canonical_model(name: str) -> str:
    return name.strip().lower().removesuffix(":latest")


def fixed_window(config: Config, model: str) -> int:
    roles = config.ollama
    if model == roles.fast_model:
        return roles.fast_num_ctx
    if model == roles.precise_model:
        return roles.precise_num_ctx
    return 0


def context_window(config: Config, model: str) -> int:
    """The window a prompt for this model gets without asking for more."""
    return fixed_window(config, model) or DEFAULT_NUM_CTX


def prompt_tokens(messages: list[dict[str, str]]) -> int:
    return sum(
        len(m.get("content", "")) // CHARS_PER_TOKEN_ESTIMATE + MESSAGE_OVERHEAD_TOKENS
        for m in messages
    )


def num_ctx_for(messages: list[dict[str, str]], fixed: int = 0) -> int | None:
    """None if the default window is big enough. With a fixed window, that one
    unless the prompt needs more."""
    needed = prompt_tokens(messages) + REPLY_TOKENS
    base = fixed or DEFAULT_NUM_CTX
    if needed <= base:
        return fixed or None
    window = base
    while window < needed:
        window *= 2
    return window


def _options(
    messages: list[dict[str, str]], options: dict[str, Any] | None, fixed: int = 0
) -> dict[str, Any]:
    opts = {"temperature": 0, **(options or {})}
    if "num_ctx" not in opts and (window := num_ctx_for(messages, fixed)) is not None:
        opts["num_ctx"] = window
    return opts


def _require_every_field(schema: type[BaseModel]) -> dict[str, Any]:
    # Pydantic makes fields with defaults optional, and models then skip them
    # (entities went missing this way). Nullable fields can still be null.
    json_schema = schema.model_json_schema()
    return {**json_schema, "required": list(json_schema.get("properties", {}))}


class OllamaClient:
    def __init__(self, config: Config):
        self._config = config
        self._client = ollama.Client(
            host=config.ollama.host,
            timeout=config.ollama.request_timeout_seconds,
        )
        self._context_lengths: dict[str, int | None] = {}

    def _unavailable(self, exc: Exception) -> OllamaUnavailableError:
        settings = self._config.ollama
        # a read timeout means Ollama is up but slow, so don't say to start it
        if isinstance(exc, httpx.TimeoutException) and not isinstance(
            exc, httpx.ConnectTimeout
        ):
            return OllamaUnavailableError(
                f"Ollama at {settings.host} did not answer within "
                f"{settings.request_timeout_seconds}s ({exc}). Loading a large "
                "model or reading a long input can take longer than that; raise "
                "ollama.request_timeout_seconds in config.toml if it keeps happening."
            )
        return OllamaUnavailableError(
            f"Cannot reach Ollama at {settings.host} ({exc}). "
            "Start it with 'ollama serve' and pull the configured models: "
            f"ollama pull {settings.fast_model} && "
            f"ollama pull {settings.precise_model} && "
            f"ollama pull {settings.embed_model}"
        )

    def _rejected(self, model: str, exc: ollama.ResponseError) -> ModelRequestError:
        hint = ""
        if "not found" in str(exc).lower():
            hint = f" Pull it with: ollama pull {model}"
        return ModelRequestError(
            f"Ollama rejected the request for model '{model}': {exc}.{hint}"
        )

    def _call(self, model: str, fn: Callable[[], Any]) -> Any:
        for attempt in range(MAX_CONNECT_ATTEMPTS):
            try:
                return fn()
            except _CONNECT_ERRORS as exc:
                if attempt == MAX_CONNECT_ATTEMPTS - 1:
                    raise self._unavailable(exc) from exc
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
            except httpx.HTTPError as exc:
                raise self._unavailable(exc) from exc
            except ollama.ResponseError as exc:
                raise self._rejected(model, exc) from exc
        raise AssertionError("unreachable")

    def _fixed_ctx(self, model: str) -> int:
        return fixed_window(self._config, model)

    def window(self, model: str) -> int:
        return context_window(self._config, model)

    def _think(self, model: str) -> bool | None:
        roles = self._config.ollama
        if model == roles.fast_model:
            return roles.fast_model_think
        if model == roles.precise_model:
            return roles.precise_model_think
        return None

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any] | None = None,
    ) -> str:
        opts = _options(messages, options, self._fixed_ctx(model))
        response = self._call(
            model,
            partial(
                self._client.chat,
                model=model,
                messages=messages,
                options=opts,
                think=self._think(model),
            ),
        )
        return strip_thinking(response["message"]["content"])

    def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        on_text: Callable[[str], None],
        options: dict[str, Any] | None = None,
    ) -> str:
        """Like chat, handing each piece of the reply to on_text as it arrives."""
        opts = _options(messages, options, self._fixed_ctx(model))
        think = self._think(model)
        parts: list[str] = []
        visible = ThinkFilter()
        try:
            stream = iter(self._first_chunk(model, messages, opts, think))
            for chunk in stream:
                content = chunk["message"]["content"] or ""
                parts.append(content)
                shown = visible.feed(content)
                if shown:
                    on_text(shown)
        except (*_CONNECT_ERRORS, httpx.HTTPError) as exc:
            raise self._unavailable(exc) from exc
        except ollama.ResponseError as exc:
            raise self._rejected(model, exc) from exc
        tail = visible.flush()
        if tail:
            on_text(tail)
        return strip_thinking("".join(parts))

    def _first_chunk(
        self,
        model: str,
        messages: list[dict[str, str]],
        opts: dict[str, Any],
        think: bool | None,
    ) -> Iterator[Any]:
        # The request only goes out when the first chunk is read, so that's
        # where a connection refused during a restart shows up and gets retried.
        for attempt in range(MAX_CONNECT_ATTEMPTS):
            stream = self._client.chat(
                model=model, messages=messages, options=opts, think=think, stream=True
            )
            try:
                first = next(iter(stream))
            except StopIteration:
                return iter(())
            except _CONNECT_ERRORS:
                if attempt == MAX_CONNECT_ATTEMPTS - 1:
                    raise
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            return itertools.chain([first], stream)
        raise AssertionError("unreachable")

    def chat_structured(
        self,
        model: str,
        messages: list[dict[str, str]],
        schema: type[T],
        options: dict[str, Any] | None = None,
    ) -> T:
        think = self._think(model)
        json_schema = _require_every_field(schema)
        convo = list(messages)
        last_error: Exception | None = None
        for _ in range(2):
            # per attempt, since the retry also carries the bad reply
            opts = _options(convo, options, self._fixed_ctx(model))
            if think is False:
                opts.setdefault("num_predict", STRUCTURED_MAX_TOKENS)
            response = self._call(
                model,
                partial(
                    self._client.chat,
                    model=model,
                    messages=convo,
                    format=json_schema,
                    options=opts,
                    think=think,
                ),
            )
            # some models put a think block before the JSON anyway
            content = strip_thinking(response["message"]["content"])
            try:
                return schema.model_validate_json(content)
            except ValidationError as exc:
                last_error = exc
                convo = [
                    *convo,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response failed schema validation: "
                            f"{exc}. Respond again with ONLY valid JSON matching "
                            "the schema."
                        ),
                    },
                ]
        raise StructuredOutputError(
            f"Model '{model}' failed to produce valid {schema.__name__} JSON "
            f"after a retry: {last_error}"
        )

    def embed(
        self, model: str, text: str, options: dict[str, Any] | None = None
    ) -> list[float]:
        response = self._call(
            model,
            partial(self._client.embed, model=model, input=text, options=options),
        )
        embeddings = response["embeddings"]
        if not embeddings:
            raise ModelRequestError(
                f"Embedding model '{model}' returned no vector for the input."
            )
        return list(embeddings[0])

    def list_models(self) -> list[str]:
        data = self._call("(list)", self._client.list)
        return [m["model"] for m in data["models"]]

    def context_length(self, model: str) -> int | None:
        if model not in self._context_lengths:
            info = self._call(model, partial(self._client.show, model)).modelinfo or {}
            self._context_lengths[model] = next(
                (int(v) for k, v in info.items() if k.endswith(".context_length")),
                None,
            )
        return self._context_lengths[model]
