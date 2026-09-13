from __future__ import annotations

import re
from typing import TypeVar

from .config import Config
from .models import Extraction, NoteSummary, ParsedEntry, ParsedNote
from .ollama_client import OllamaClient
from .parser import extract_literal_tags

SUMMARY_MAX_CHARS = 200
NOTE_SUMMARY_MAX_CHARS = 400
# keeps the prompt inside Ollama's default window so the model isn't reloaded
NOTE_INPUT_MAX_CHARS = 8_000

_SYSTEM_PROMPT = """You extract structured data from a personal journal entry.
Return only the requested fields.
The entry comes with the path of its file in the journal. Folder and file \
names can say what the entry is about (a trip, a project, a person), so let \
them inform the summary and tags.
Rules:
- mood_score: a number from 1 to {mood_max} based STRICTLY on the entry's \
emotional content (1 = very negative, {mood_max} = very positive). If the \
entry is purely factual with no emotional content, use null.
- summary: one sentence, at most {summary_max} characters, capturing the gist. \
Write it in the voice of the person who wrote the entry, the way they would \
jot it in their own diary: first person (I, me, my), and the "I" may be left \
out where the sentence reads fine without it. Never describe the writer from \
outside. Write "Felt drained after work but the evening walk helped", not \
"The author expresses feeling drained after work". Don't open with "The \
author", "The user", "The writer" or "This entry".
- tags: 3-6 lowercase theme tags. Keep them general and reusable \
(e.g. "work", "health", "family"), not hyper-specific."""

_NOTE_PROMPT = """You summarize one note from someone's personal knowledge base. \
Its notes describe the people, places, groups, events, games and topics that \
come up in their journal. The note's file path is part of what it says: folder \
and file names often tell what kind of thing it is and what it is called. \
Write 1-3 sentences, at most {summary_max} characters, saying who or what the \
note is about and the facts in it that matter most. Use only what the note \
says. Write from the point of view of the person who keeps these notes, in the \
first person where they come into it ("my cousin", "I met her at work"), and \
start with the subject itself, by name. Never open with "This note" or "This \
file", and never refer to "the author" or "the user"."""

# "The author ..." and so on, which small models write even when told not to
_DETACHED_OPENING_RE = re.compile(
    r"^\W*(?:in\s+)?(?:the|this)\s+(?:journal\s+)?"
    r"(?:author|writer|user|narrator|journaler|diarist|entry|note|file|document)\b",
    re.IGNORECASE,
)

_VOICE_CORRECTION = (
    "The summary describes the writer from outside. Give the same fields again "
    "with the summary rewritten in the writer's own first-person voice (I, me, "
    'my), opening with the subject itself rather than "the author", "the user", '
    '"this entry", "this note" or "this file".'
)

_Summarized = TypeVar("_Summarized", Extraction, NoteSummary)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


def _in_writers_voice(
    client: OllamaClient,
    model: str,
    messages: list[dict[str, str]],
    schema: type[_Summarized],
) -> _Summarized:
    # One retry. If that's still off it's kept, not worth failing the file over.
    result = client.chat_structured(model=model, messages=messages, schema=schema)
    if not _DETACHED_OPENING_RE.match(result.summary):
        return result
    retry = [
        *messages,
        {"role": "assistant", "content": result.model_dump_json()},
        {"role": "user", "content": _VOICE_CORRECTION},
    ]
    return client.chat_structured(model=model, messages=retry, schema=schema)


def extract(entry: ParsedEntry, config: Config, client: OllamaClient) -> Extraction:
    text = entry.raw_text.strip()
    literal_tags = extract_literal_tags(text)

    if not text:
        return Extraction(mood_score=None, summary="(empty entry)", tags=literal_tags)

    heading = f"Journal entry dated {entry.entry_date.isoformat()}"
    if entry.rel_path:
        heading += f"\nFile: {entry.rel_path}"
    result = _in_writers_voice(
        client,
        config.ollama.fast_model,
        [
            {
                "role": "system",
                "content": _SYSTEM_PROMPT.format(
                    mood_max=config.indexing.mood_scale_max,
                    summary_max=SUMMARY_MAX_CHARS,
                ),
            },
            {"role": "user", "content": f"{heading}\n\n{text}"},
        ],
        Extraction,
    )

    # small models go out of range sometimes
    if result.mood_score is not None:
        result.mood_score = min(
            max(result.mood_score, 1.0), float(config.indexing.mood_scale_max)
        )
    result.summary = _clip(result.summary, SUMMARY_MAX_CHARS)

    merged: dict[str, None] = {}
    for tag in [t.lower().strip() for t in result.tags] + literal_tags:
        if tag:
            merged.setdefault(tag, None)
    result.tags = list(merged)
    return result


def summarize_note(
    note: ParsedNote, config: Config, client: OllamaClient
) -> tuple[str | None, str | None]:
    """Returns (summary, model used). Short notes are their own summary."""
    text = note.raw_text.strip()
    if not text:
        return None, None
    if len(text) <= config.notes.summarize_over_chars:
        return " ".join(text.split()), None
    result = _in_writers_voice(
        client,
        config.ollama.fast_model,
        [
            {
                "role": "system",
                "content": _NOTE_PROMPT.format(summary_max=NOTE_SUMMARY_MAX_CHARS),
            },
            {
                "role": "user",
                "content": f"{note.header()}\n\n{text}"[:NOTE_INPUT_MAX_CHARS],
            },
        ],
        NoteSummary,
    )
    summary = _clip(result.summary, NOTE_SUMMARY_MAX_CHARS)
    return (summary or None), config.ollama.fast_model
