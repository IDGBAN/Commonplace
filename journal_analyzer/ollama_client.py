from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import re
import time
from collections.abc import Callable, Iterator
from functools import partial
from typing import Any, TypeVar

import httpx
import ollama
from pydantic import BaseModel, ValidationError

from . import memo
from .config import Config
from .models import (
    JournalError,
    ModelRequestError,
    OllamaUnavailableError,
    StructuredOutputError,
)

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
# Reasoning counts against num_predict and the window just like the reply.
# This is the room a thinking model gets for it. A JSON reply that's still
# thinking when it runs out is asked for again without thinking, so this caps
# how long a structured call can take.
THINK_TOKENS = 2048

# The window a prompt gets unless it needs more. It's always sent: Ollama's
# own default is whatever its app's context length setting says, up to 256k,
# and at that size a 27B model's cache alone is 16 GB. A longer prompt gets
# cut from the middle without any error, so those ask for more room.
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


def num_ctx_for(
    messages: list[dict[str, str]], fixed: int = 0, reply_tokens: int = REPLY_TOKENS
) -> int:
    """The fixed window, or DEFAULT_NUM_CTX without one, doubled until the
    prompt and a reply fit."""
    needed = prompt_tokens(messages) + reply_tokens
    window = fixed or DEFAULT_NUM_CTX
    while window < needed:
        window *= 2
    return window


def _options(
    messages: list[dict[str, str]],
    options: dict[str, Any] | None,
    fixed: int = 0,
    reply_tokens: int = REPLY_TOKENS,
) -> dict[str, Any]:
    opts = {"temperature": 0, **(options or {})}
    opts.setdefault("num_ctx", num_ctx_for(messages, fixed, reply_tokens))
    return opts


def _reply_room(reply_tokens: int, thinking: bool) -> int:
    return reply_tokens + (THINK_TOKENS if thinking else 0)


def _memo_options(opts: dict[str, Any]) -> dict[str, Any]:
    # the window only changes the reply when a prompt wouldn't fit, and then
    # it's raised to fit, so a different window setting still gives the same one
    return {k: v for k, v in opts.items() if k != "num_ctx"}


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
        self._shown: dict[str, Any] = {}
        self._can_think: dict[str, bool] = {}
        # the biggest window each model has been sent by this client
        self._windows: dict[str, int] = {}
        # (model, schema) pairs that ran out of room while thinking
        self._overthought: set[tuple[str, str]] = set()
        self._memo = memo.open_for(config)
        self._weights: dict[str, str] | None = None

    def _model_weights(self, model: str) -> str:
        # A re-pulled model keeps its name but gets new weights, and results
        # from the old ones shouldn't come back.
        if self._weights is None:
            self._weights = {}
            with contextlib.suppress(JournalError):
                listed = self._call("(list)", self._client.list)
                for m in getattr(listed, "models", None) or []:
                    name, digest = getattr(m, "model", None), getattr(m, "digest", None)
                    if isinstance(name, str) and isinstance(digest, str):
                        self._weights[_canonical_model(name)] = digest
        return self._weights.get(_canonical_model(model), "")

    def _memo_key(self, kind: str, model: str, **parts: Any) -> str:
        blob = json.dumps(
            {"kind": kind, "model": _canonical_model(model),
             "weights": self._model_weights(model), **parts},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

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

    def _sized(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any] | None,
        reply_tokens: int,
    ) -> dict[str, Any]:
        # Ollama reloads a model whenever its window changes, which on a CPU
        # takes from half a minute to several, and a reload also throws away
        # the prompt it has already read. So a window only grows in a run.
        smallest = max(fixed_window(self._config, model), self._windows.get(model, 0))
        opts = _options(messages, options, smallest, reply_tokens)
        self._windows[model] = max(smallest, int(opts["num_ctx"]))
        return opts

    def window(self, model: str) -> int:
        return context_window(self._config, model)

    def _think(self, model: str) -> bool | None:
        roles = self._config.ollama
        if model == roles.fast_model:
            return roles.fast_model_think
        if model == roles.precise_model:
            return roles.precise_model_think
        return None

    def _thinking(self, model: str, think: bool | None) -> bool:
        """Whether the reply will have reasoning in it, which needs room."""
        if think is not None:
            return think
        if model not in self._can_think:
            # left to the model, and the ones that can think do unless told
            # not to; when Ollama can't say, room is kept in case
            capabilities = None
            with contextlib.suppress(JournalError):
                capabilities = getattr(self._show(model), "capabilities", None)
            self._can_think[model] = (
                not isinstance(capabilities, list) or "thinking" in capabilities
            )
        return self._can_think[model]

    def _show(self, model: str) -> Any:
        if model not in self._shown:
            self._shown[model] = self._call(model, partial(self._client.show, model))
        return self._shown[model]

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any] | None = None,
    ) -> str:
        think = self._think(model)
        opts = self._sized(
            model, messages, options, _reply_room(REPLY_TOKENS, self._thinking(model, think))
        )
        key = None
        # only replies that come out the same every time are worth keeping
        if self._memo is not None and opts.get("temperature") == 0:
            key = self._memo_key(
                "chat", model, messages=messages, options=_memo_options(opts), think=think
            )
            kept = self._memo.get_text(key)
            if kept is not None:
                return kept
        response = self._call(
            model,
            partial(
                self._client.chat,
                model=model,
                messages=messages,
                options=opts,
                think=think,
            ),
        )
        text = strip_thinking(response["message"]["content"])
        if key is not None and self._memo is not None:
            self._memo.put_text(key, "chat", model, text)
        return text

    def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        on_text: Callable[[str], None],
        options: dict[str, Any] | None = None,
    ) -> str:
        """Like chat, handing each piece of the reply to on_text as it arrives."""
        think = self._think(model)
        opts = self._sized(
            model, messages, options, _reply_room(REPLY_TOKENS, self._thinking(model, think))
        )
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
        memo_key: object = None,
        reply_tokens: int | None = None,
    ) -> T:
        """`memo_key` stands in for the messages when the memo is checked, for
        callers whose prompt carries something that shouldn't count.
        `reply_tokens` replaces STRUCTURED_MAX_TOKENS for a schema whose
        answer runs long."""
        think = self._think(model)
        json_schema = _require_every_field(schema)
        key = None
        if self._memo is not None:
            base = {"temperature": 0, **(options or {})}
            if base.get("temperature") == 0:
                key = self._memo_key(
                    "structured", model,
                    messages=memo_key if memo_key is not None else messages,
                    schema=json_schema, options=_memo_options(base), think=think,
                )
                kept = self._memo.get_text(key)
                if kept is not None:
                    with contextlib.suppress(ValidationError):
                        return schema.model_validate_json(kept)
        result = self._chat_structured(
            model, messages, schema, options, think, json_schema,
            reply_tokens or STRUCTURED_MAX_TOKENS,
        )
        if key is not None and self._memo is not None:
            self._memo.put_text(key, "structured", model, result.model_dump_json())
        return result

    def _chat_structured(
        self,
        model: str,
        messages: list[dict[str, str]],
        schema: type[T],
        options: dict[str, Any] | None,
        think: bool | None,
        json_schema: dict[str, Any],
        reply_tokens: int,
    ) -> T:
        convo = list(messages)
        thinking = self._thinking(model, think)
        # calls for one schema in a run are the same task (a profile rebuild
        # makes one per chunk), and a model that thought past its room on one
        # will on the next, so those go straight to answering
        if thinking and (model, schema.__name__) in self._overthought:
            thinking, think = False, False
        retried = False
        while True:
            room = _reply_room(reply_tokens, thinking)
            # per attempt, since the retry also carries the bad reply
            opts = self._sized(model, convo, options, max(REPLY_TOKENS, room))
            opts.setdefault("num_predict", room)
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
            content = strip_thinking(response["message"]["content"] or "")
            try:
                return schema.model_validate_json(content)
            except ValidationError as exc:
                last_error = exc
            if thinking and (not content or response.get("done_reason") == "length"):
                # it ran out of room while thinking, so there's no JSON to
                # correct, and the same request would only do it again
                self._overthought.add((model, schema.__name__))
                thinking, think = False, False
                continue
            if retried:
                break
            retried = True
            convo = [
                *convo,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "Your previous response failed schema validation: "
                        f"{last_error}. Respond again with ONLY valid JSON matching "
                        "the schema."
                    ),
                },
            ]
        raise StructuredOutputError(
            f"Model '{model}' failed to produce valid {schema.__name__} JSON "
            f"after a retry: {last_error}"
        )

    def describe_image(self, model: str, prompt: str, image: bytes) -> str:
        """What a vision model says about one image. Kept in the memo by the image's bytes."""
        messages = [{"role": "user", "content": prompt}]
        think = self._think(model)
        opts = self._sized(
            model, messages, None, _reply_room(REPLY_TOKENS, self._thinking(model, think))
        )
        key = None
        if self._memo is not None:
            key = self._memo_key(
                "vision", model, prompt=prompt, image=hashlib.sha256(image).hexdigest(),
                options=_memo_options(opts), think=think,
            )
            kept = self._memo.get_text(key)
            if kept is not None:
                return kept
        response = self._call(
            model,
            partial(
                self._client.chat,
                model=model,
                messages=[{**messages[0], "images": [image]}],
                options=opts,
                think=think,
            ),
        )
        text = strip_thinking(response["message"]["content"])
        if key is not None and self._memo is not None:
            self._memo.put_text(key, "vision", model, text)
        return text

    def embed(
        self, model: str, text: str, options: dict[str, Any] | None = None
    ) -> list[float]:
        key = None
        if self._memo is not None:
            key = self._memo_key("embed", model, text=text, options=options or {})
            kept = self._memo.get_vector(key)
            if kept is not None:
                return kept
        response = self._call(
            model,
            partial(self._client.embed, model=model, input=text, options=options),
        )
        embeddings = response["embeddings"]
        if not embeddings:
            raise ModelRequestError(
                f"Embedding model '{model}' returned no vector for the input."
            )
        vector = list(embeddings[0])
        if key is not None and self._memo is not None:
            self._memo.put_vector(key, model, vector)
        return vector

    def list_models(self) -> list[str]:
        data = self._call("(list)", self._client.list)
        return [m["model"] for m in data["models"]]

    def context_length(self, model: str) -> int | None:
        info = self._show(model).modelinfo or {}
        return next((int(v) for k, v in info.items() if k.endswith(".context_length")), None)
