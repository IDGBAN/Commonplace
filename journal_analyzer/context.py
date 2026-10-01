"""The notes and days an entry links to, written out as background for a model.

Extraction, digests, writing prompts and answers all see the people, places
and topics behind the links, not just their names. Each caller has its own
budget and keeps whole blocks only, most important first.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import date
from pathlib import Path

from . import store
from .models import ParsedEntry

_ISO_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def note_block(
    row: sqlite3.Row, vault: Path | None, chars: int, labels: list[str] | None = None
) -> str:
    """One line about a note: what it is, what it's called, and the start of what it says."""
    note = store.note_from_row(row, vault)
    facts = [f"{note.title} ({note.kind})"]
    if labels:
        facts.append("written as " + ", ".join(f'"{label}"' for label in labels))
    if note.aliases:
        facts.append("also called " + ", ".join(note.aliases))
    facts.extend(f"{key}: {value}" for key, value in note.properties.items())
    body = " ".join((row["summary"] or note.raw_text or "").split())
    if len(body) > chars:
        body = body[: max(chars - 1, 0)].rsplit(" ", 1)[0] + "…"
    return "- " + "; ".join(facts) + (f": {body}" if body else "")


def fit(blocks: list[str], budget: int) -> list[str]:
    """As many blocks as fit in `budget` characters, in order, skipping any that don't."""
    kept: list[str] = []
    used = 0
    for block in blocks:
        cost = len(block) + 1
        if used + cost > budget:
            continue
        kept.append(block)
        used += cost
    return kept


def longer_summaries(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], budget: int
) -> dict[int, str]:
    """The longer summaries that fit in `budget` extra characters, by entry id.

    The longest entries go first, since a one-liner loses the most for them.
    Anything left over is described by its one-line summary as before.
    """
    if budget <= 0 or not rows:
        return {}
    found = store.details_for_entries(conn, [r["id"] for r in rows])
    chosen: dict[int, str] = {}
    used = 0
    for row in sorted(rows, key=lambda r: -r["word_count"]):
        text = found.get(row["id"])
        if not text:
            continue
        extra = len(text) - len(row["summary"] or "")
        if used + extra > budget:
            continue
        chosen[row["id"]] = text
        used += extra
    return chosen


def entry_links(
    conn: sqlite3.Connection, entry: ParsedEntry, vault: Path, chars: int
) -> list[str]:
    """Blocks for what one entry links to, in the order it links them."""
    if chars <= 0 or not entry.links:
        return []
    notes = store.notes_for_targets(conn, entry.links)
    blocks: list[str] = []
    seen: set[int] = set()
    for key in entry.links:
        row = notes.get(key)
        if row is not None:
            if row["id"] not in seen:
                seen.add(row["id"])
                blocks.append(note_block(row, vault, chars, entry.link_labels.get(key)))
        elif _ISO_DAY_RE.fullmatch(key):
            try:
                day = date.fromisoformat(key)
            except ValueError:
                continue
            for other in store.entries_in_range(conn, day, day):
                if other["summary"]:
                    blocks.append(f"- [[{key}]], another day this entry links to: {other['summary']}")
    return blocks


def linked_notes(
    conn: sqlite3.Connection,
    entry_ids: list[int],
    vault: Path,
    budget: int,
    chars: int,
) -> list[str]:
    """Blocks for the notes a set of entries links to, the most linked first."""
    if budget <= 0 or not entry_ids:
        return []
    counts: Counter[int] = Counter()
    for names in store.entities_for_entries(conn, entry_ids).values():
        counts.update(n["id"] for n in names)
    ordered = [note_id for note_id, _ in counts.most_common()]
    rows = store.notes_by_ids(conn, ordered)
    return fit([note_block(r, vault, chars) for r in rows], budget)
