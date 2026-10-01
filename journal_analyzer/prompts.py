"""`journal prompt`. Works from stored summaries and tags, not the entry text."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

from . import context, profile, store
from .config import Config
from .ollama_client import SYNTHESIS_TEMPERATURE, OllamaClient

_SYSTEM = (
    "You are a thoughtful journaling coach. From the recent entries below, "
    "suggest {count} specific, open-ended prompts for the writer's next entry. "
    "The entry summaries are in the writer's own first-person voice; address "
    'each prompt to them as "you". Follow up on concrete threads that '
    "appear, such as named projects, people, decisions, or feelings that were left "
    "open or unresolved. Prefer specificity over generic self-reflection; a "
    "good prompt names something real from the entries. Return ONLY a "
    "numbered list, one prompt per line, with no preamble or commentary."
)

_NUMBER_PREFIX = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+")


# room for the notes the recent entries link to
LINKED_NOTES_CHARS = 3000
LINKED_NOTE_CHARS = 400
# extra room for long entries' longer summaries
DETAILS_CHARS = 4000


def _recent_context(
    conn: sqlite3.Connection, date_from: date, date_to: date, config: Config | None = None
) -> str:
    rows = store.entries_in_range(conn, date_from, date_to)
    ids = [r["id"] for r in rows]
    tags_by_entry = store.tags_for_entries(conn, ids)
    entities_by_entry = store.entities_for_entries(conn, ids)
    longer = context.longer_summaries(conn, rows, DETAILS_CHARS)
    lines: list[str] = []
    for row in rows:
        tags = tags_by_entry.get(row["id"], [])
        ents = [r["name"] for r in entities_by_entry.get(row["id"], [])]
        mood = f"{row['mood_score']:g}" if row["mood_score"] is not None else "-"
        parts = [f"- {row['entry_date']} (mood {mood})"]
        if tags:
            parts.append(f"[{', '.join(tags)}]")
        if ents:
            parts.append(f"who/what: {', '.join(ents)}")
        parts.append(f"- {longer.get(row['id']) or row['summary'] or '(no summary)'}")
        lines.append(" ".join(parts))
    if config is not None:
        notes = context.linked_notes(
            conn, ids, config.vault.path, LINKED_NOTES_CHARS, LINKED_NOTE_CHARS,
        )
        if notes:
            lines.append("\nWho and what these entries link to:")
            lines.extend(notes)
    return "\n".join(lines)


def generate_prompts(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    today: date | None = None,
    days: int = 14,
    count: int = 4,
) -> list[str]:
    today = today or date.today()
    date_from = today - timedelta(days=days)
    recent = _recent_context(conn, date_from, today, config)
    if not recent:
        return []
    text = client.chat(
        model=config.ollama.precise_model,
        messages=[
            {"role": "system", "content": _SYSTEM.format(count=count) + profile.context(conn, config)},
            {
                "role": "user",
                "content": (
                    f"Recent entries from {date_from} to {today} "
                    f"(oldest first):\n{recent}"
                ),
            },
        ],
        options={"temperature": SYNTHESIS_TEMPERATURE},
    )
    return _parse_list(text)[:count]


def _parse_list(text: str) -> list[str]:
    # Models open with "Here are four prompts:" even when told not to, so if
    # any lines are numbered or bulleted, only those count.
    listed: list[str] = []
    loose: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        item = _NUMBER_PREFIX.sub("", stripped).strip()
        if not item:
            continue
        loose.append(item)
        if _NUMBER_PREFIX.match(stripped):
            listed.append(item)
    return listed or loose
