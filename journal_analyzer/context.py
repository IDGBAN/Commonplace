"""The notes and days an entry links to, written out as background for a model.

Extraction, digests, writing prompts and answers all see the people, places
and topics behind the links, not just their names. Each caller has its own
budget and keeps whole blocks only, most important first.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

from . import parser, store
from .models import ParsedEntry


def _clip(text: str, chars: int) -> str:
    body = " ".join(text.split())
    if len(body) > chars:
        body = body[: max(chars - 1, 0)].rsplit(" ", 1)[0] + "…"
    return body


def _part_phrase(anchor: str) -> str:
    if anchor.startswith("^"):
        return "a passage"
    return f'the "{parser.anchor_label(anchor)}" part'


def note_block(
    row: sqlite3.Row,
    vault: Path | None,
    chars: int,
    labels: list[str] | None = None,
    anchors: list[str] | None = None,
) -> str:
    """One line about a note: what it is, what it's called, and the start of what it says.

    `anchors` are the headings or blocks a link points at (parser.link_anchors).
    The text of those parts goes in place of the summary, after it when the
    note is also linked as a whole.
    """
    note = store.note_from_row(row, vault)
    facts = [f"{note.title} ({note.kind})"]
    parts = [
        (anchor, text)
        for anchor in anchors or []
        if anchor and (text := parser.find_section(note.raw_text, anchor))
    ]
    if parts:
        facts.append("linked to " + " and ".join(_part_phrase(a) for a, _ in parts))
    if labels:
        facts.append("written as " + ", ".join(f'"{label}"' for label in labels))
    if note.aliases:
        facts.append("also called " + ", ".join(note.aliases))
    facts.extend(f"{key}: {value}" for key, value in note.properties.items())
    pieces = [text for _, text in parts]
    if not parts or "" in (anchors or []):
        pieces.insert(0, row["summary"] or note.raw_text or "")
    body = _clip(" ".join(p for p in pieces if p), chars)
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
    """Blocks for what one entry links to, in the order it links them.

    A link to a heading or block of another day, like [[2024-01-14#Work]],
    brings that part of the day's text instead of the day's summary.
    """
    if chars <= 0 or not entry.links:
        return []
    notes = store.notes_for_targets(conn, entry.links)
    blocks: list[str] = []
    seen: set[int] = set()
    day = entry.entry_date.isoformat()
    for key in entry.links:
        anchors = entry.link_anchors.get(key, [])
        row = notes.get(key)
        if row is not None:
            if row["id"] not in seen:
                seen.add(row["id"])
                blocks.append(note_block(row, vault, chars, entry.link_labels.get(key), anchors))
            continue
        for other in store.entries_for_day_link(conn, key):
            # this entry's own earlier version, still stored while it's re-read
            if other["file_path"] == entry.source_path and other["entry_date"] == day:
                continue
            blocks.extend(_day_blocks(other, anchors, chars))
    return blocks


def _day_blocks(other: sqlite3.Row, anchors: list[str], chars: int) -> list[str]:
    blocks: list[str] = []
    whole = not anchors or "" in anchors
    for anchor in (a for a in anchors if a):
        text = parser.find_section(other["raw_text"], anchor)
        if text is None:
            whole = True  # renamed or gone since, so the summary will have to do
            continue
        blocks.append(
            f"- [[{other['entry_date']}#{anchor}]], {_part_phrase(anchor)} of another day "
            f"this entry links to: {_clip(text, chars)}"
        )
    if whole and other["summary"]:
        blocks.append(
            f"- [[{other['entry_date']}]], another day this entry links to: {other['summary']}"
        )
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
