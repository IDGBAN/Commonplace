from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import re
import threading
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
# how often a streamed call checks whether the model has loaded and updates
# its reading estimate
LOAD_POLL_SECONDS = 1.0
# Reading speed is learned from what Ollama reports after each call. A call
# that read fewer tokens than this was mostly served from Ollama's cache.
MIN_TOKENS_TO_LEARN = 64
# what a prompt's characters per token is taken to be until it's measured
DEFAULT_CHARS_PER_TOKEN = 3.5

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
    """Drops <think>...</think> from text that arrives a piece at a time,
    keeping what it dropped for take_hidden()."""

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False
        self._hidden: list[str] = []

    @property
    def inside(self) -> bool:
        return self._inside

    def take_hidden(self) -> str:
        hidden = "".join(self._hidden)
        self._hidden = []
        return hidden

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
                    self._hidden.append(self._buffer[: len(self._buffer) - keep])
                    self._buffer = self._buffer[len(self._buffer) - keep:] if keep else ""
                    break
                self._hidden.append(self._buffer[:end])
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


def _nothing() -> None:
    pass


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
        # model -> (chars per token, tokens read per second), learned as it goes
        self._speeds: dict[str, tuple[float, float] | None] = {}
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

    def planned_window(self, model: str, tokens: int) -> int:
        """The window a call needing this many tokens would get, without sending one."""
        window = max(fixed_window(self._config, model), self._windows.get(model, 0))
        window = window or DEFAULT_NUM_CTX
        while window < tokens:
            window *= 2
        return window

    def think_room(self, model: str) -> int:
        """The tokens a call to this model keeps for thinking, 0 if it won't think."""
        return self._config.ollama.think_tokens if self._thinking(model, self._think(model)) else 0

    def _room(self, reply_tokens: int, thinking: bool) -> int:
        return reply_tokens + (self._config.ollama.think_tokens if thinking else 0)

    def _speed(self, model: str) -> tuple[float, float] | None:
        name = _canonical_model(model)
        if name not in self._speeds:
            self._speeds[name] = self._memo.get_speed(name) if self._memo is not None else None
        return self._speeds[name]

    def _learn(self, model: str, messages: list[dict[str, str]], response: Any) -> None:
        """Keeps how fast the model read this prompt, from what Ollama reported."""
        get = getattr(response, "get", None)
        if not callable(get):
            return
        count, nanos = get("prompt_eval_count"), get("prompt_eval_duration")
        if not isinstance(count, int) or not isinstance(nanos, int):
            return
        if count < MIN_TOKENS_TO_LEARN or nanos <= 0:
            return
        rate = count / (nanos / 1e9)
        ratio = sum(len(m.get("content", "")) for m in messages) / count
        old = self._speed(model)
        # a ratio outside this means much of the prompt came from Ollama's cache
        per_token = ratio if 2 <= ratio <= 6 else None
        if old is not None:
            rate = (old[1] + rate) / 2
            per_token = (old[0] + per_token) / 2 if per_token else old[0]
        speed = (per_token or DEFAULT_CHARS_PER_TOKEN, rate)
        name = _canonical_model(model)
        self._speeds[name] = speed
        if self._memo is not None:
            self._memo.put_speed(name, *speed)

    def reading_seconds(self, model: str, messages: list[dict[str, str]]) -> float | None:
        """How long the model took to read this much text last time, if it's been timed."""
        speed = self._speed(model)
        if speed is None:
            return None
        chars = sum(len(m.get("content", "")) for m in messages)
        return chars / speed[0] / speed[1]

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
            model, messages, options, self._room(REPLY_TOKENS, self._thinking(model, think))
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
        text = self._chat_once(model, messages, opts, think)
        if not text and self._thinking(model, think):
            # it thought until the window was full and never answered; the
            # prompt is still in Ollama's cache, so this only costs the answer
            text = self._chat_once(model, messages, opts, False)
        if key is not None and self._memo is not None and text:
            self._memo.put_text(key, "chat", model, text)
        return text

    def _chat_once(
        self, model: str, messages: list[dict[str, str]], opts: dict[str, Any], think: bool | None
    ) -> str:
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
        self._learn(model, messages, response)
        return strip_thinking(response["message"]["content"] or "")

    def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        on_text: Callable[[str], None],
        options: dict[str, Any] | None = None,
        on_phase: Callable[[str], None] | None = None,
        on_thinking: Callable[[str], None] | None = None,
        on_reading: Callable[[float, float], None] | None = None,
    ) -> str:
        """Like chat, handing each piece of the reply to on_text as it arrives.

        on_phase hears "loading" or "reading" before the reply starts (Ollama
        loading the model, then reading the prompt) and "thinking" when the
        model starts reasoning. on_thinking gets that reasoning, which the
        reply itself leaves out. A model still thinking after [ollama]
        think_tokens is stopped and asked again without thinking, and on_phase
        hears "answering" when that happens. While the model reads, on_reading
        hears every second how long it has been reading and how long the same
        amount took before, when this model has been timed.
        """
        think = self._think(model)
        thinking = self._thinking(model, think)
        opts = self._sized(model, messages, options, self._room(REPLY_TOKENS, thinking))
        phase = on_phase or (lambda _phase: None)
        text, answered = self._stream_once(
            model, messages, opts, think, on_text, phase, on_thinking,
            self._config.ollama.think_tokens if thinking else None, on_reading,
        )
        if answered or not thinking:
            return text
        # Thinking used up its room, or ended without an answer. The model is
        # still loaded at this window with the prompt read, so asking again
        # without thinking only costs the answer.
        phase("answering")
        text, _ = self._stream_once(
            model, messages, opts, False, on_text,
            lambda p: None if p == "reading" else phase(p), on_thinking, None, None,
        )
        return text

    def _stream_once(
        self,
        model: str,
        messages: list[dict[str, str]],
        opts: dict[str, Any],
        think: bool | None,
        on_text: Callable[[str], None],
        phase: Callable[[str], None],
        on_thinking: Callable[[str], None] | None,
        think_budget: int | None,
        on_reading: Callable[[float, float], None] | None = None,
    ) -> tuple[str, bool]:
        """The reply, and False if it was stopped for thinking past think_budget
        or thought without answering."""
        parts: list[str] = []
        visible = ThinkFilter()
        thought = 0
        shown_any = False
        stop: Callable[[], None] = _nothing
        last: Any = None
        try:
            with self._until_it_starts(model, messages, int(opts["num_ctx"]), phase, on_reading):
                stream, stop = self._first_chunk(model, messages, opts, think)
            for chunk in stream:
                last = chunk
                message = chunk["message"]
                content = message["content"] or ""
                parts.append(content)
                shown = visible.feed(content)
                reasoning = (message.get("thinking") or "") + visible.take_hidden()
                if (reasoning or visible.inside) and not shown_any:
                    if not thought:
                        phase("thinking")
                    # a chunk is about a token
                    thought += 1
                    if think_budget is not None and thought > think_budget:
                        stop()
                        return "", False
                if reasoning and on_thinking:
                    on_thinking(reasoning)
                if shown:
                    shown_any = shown_any or bool(shown.strip())
                    on_text(shown)
        except (*_CONNECT_ERRORS, httpx.HTTPError) as exc:
            raise self._unavailable(exc) from exc
        except ollama.ResponseError as exc:
            raise self._rejected(model, exc) from exc
        finally:
            stop()
        # the last chunk carries what Ollama counted
        self._learn(model, messages, last)
        tail = visible.flush()
        if tail:
            shown_any = shown_any or bool(tail.strip())
            on_text(tail)
        return strip_thinking("".join(parts)), shown_any or not thought

    @contextlib.contextmanager
    def _until_it_starts(
        self,
        model: str,
        messages: list[dict[str, str]],
        window: int,
        on_phase: Callable[[str], None],
        on_reading: Callable[[float, float], None] | None = None,
    ) -> Iterator[None]:
        """Says "loading" until Ollama has the model in memory at this window,
        then "reading" until the reply starts, timing the reading for on_reading."""
        estimate = self.reading_seconds(model, messages) if on_reading else None
        loaded = self._loaded(model, window)
        reading_since = None if loaded is False else time.monotonic()
        on_phase("loading" if reading_since is None else "reading")
        if reading_since is not None and estimate is None:
            yield
            return
        started = threading.Event()

        def watch() -> None:
            since = reading_since
            while not started.wait(LOAD_POLL_SECONDS):
                if since is None:
                    # None means Ollama can't say, so it's taken as loaded
                    if self._loaded(model, window) is False:
                        continue
                    since = time.monotonic()
                    on_phase("reading")
                if estimate is None or on_reading is None:
                    return
                on_reading(time.monotonic() - since, estimate)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            yield
        finally:
            started.set()
            # so its last word can't land after the reply has started
            watcher.join()

    def _loaded(self, model: str, window: int) -> bool | None:
        """Whether Ollama has the model loaded at this window, None if it can't say."""
        try:
            running = self._client.ps()
        except (*_CONNECT_ERRORS, httpx.HTTPError, ollama.ResponseError):
            return None
        models = getattr(running, "models", None)
        if not isinstance(models, list):
            return None
        for m in models:
            if same_model(str(getattr(m, "model", "") or getattr(m, "name", "")), model):
                size = getattr(m, "context_length", None)
                return not isinstance(size, int) or size >= window
        return False

    def _first_chunk(
        self,
        model: str,
        messages: list[dict[str, str]],
        opts: dict[str, Any],
        think: bool | None,
    ) -> tuple[Iterator[Any], Callable[[], None]]:
        """The chunks, and a function that ends the request early, which makes
        Ollama stop writing."""
        # The request only goes out when the first chunk is read, so that's
        # where a connection refused during a restart shows up and gets retried.
        for attempt in range(MAX_CONNECT_ATTEMPTS):
            stream = self._client.chat(
                model=model, messages=messages, options=opts, think=think, stream=True
            )
            close = getattr(stream, "close", None)
            stop: Callable[[], None] = close if callable(close) else _nothing
            try:
                first = next(iter(stream))
            except StopIteration:
                return iter(()), stop
            except _CONNECT_ERRORS:
                if attempt == MAX_CONNECT_ATTEMPTS - 1:
                    raise
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            return itertools.chain([first], stream), stop
        raise AssertionError("unreachable")

    def chat_structured(
        self,
        model: str,
        messages: list[dict[str, str]],
        schema: type[T],
        options: dict[str, Any] | None = None,
        memo_key: object = None,
        reply_tokens: int | None = None,
        think: bool | None = None,
    ) -> T:
        """`memo_key` stands in for the messages when the memo is checked, for
        callers whose prompt carries something that shouldn't count.
        `reply_tokens` replaces STRUCTURED_MAX_TOKENS for a schema whose
        answer runs long. `think`, when given, overrides the model's
        *_model_think setting for this call."""
        if think is None:
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
            room = self._room(reply_tokens, thinking)
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
            self._learn(model, convo, response)
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
            model, messages, None, self._room(REPLY_TOKENS, self._thinking(model, think))
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
