"""JSON shapes for entries, notes, digests and answers.

`journal browse` serves these and the CLI prints them with --json, so both
describe the cache the same way.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date
from typing import Any

from . import parser, store
from .models import Answer

Json = dict[str, Any]


def section_ref(anchor: str) -> Json:
    return {"anchor": anchor, "label": parser.anchor_label(anchor)}


def _add_sections(sections: list[Json], anchors: list[str]) -> None:
    for anchor in anchors:
        if all(s["anchor"] != anchor for s in sections):
            sections.append(section_ref(anchor))


def note_parts(conn: sqlite3.Connection, entry_id: int) -> dict[int, list[Json]]:
    """The headings and blocks an entry links to in each note, by note id."""
    anchors = store.link_anchors_for_entries(conn, [entry_id]).get(entry_id, {})
    parts: dict[int, list[Json]] = {}
    for target, note in store.notes_for_targets(conn, list(anchors)).items():
        _add_sections(parts.setdefault(note["id"], []), anchors[target])
    return parts


def _days(rows: list[sqlite3.Row], anchors: Callable[[sqlite3.Row], list[str]]) -> list[Json]:
    days: dict[int, Json] = {}
    for r in rows:
        day = days.setdefault(r["id"], {"id": r["id"], "date": r["entry_date"], "sections": []})
        _add_sections(day["sections"], anchors(r))
    return list(days.values())


def days_linked_from(conn: sqlite3.Connection, entry_id: int) -> list[Json]:
    """The other days an entry links to, each with the parts of it the links point at."""
    anchors = store.link_anchors_for_entries(conn, [entry_id]).get(entry_id, {})
    return _days(
        store.days_linked_from_entry(conn, entry_id), lambda r: anchors.get(r["target"], [])
    )


def days_linking_to(conn: sqlite3.Connection, entry: sqlite3.Row) -> list[Json]:
    """The entries that link to this one's day, with the parts of it they point at."""
    rows = store.entries_linking_to_day(conn, entry)
    anchors = store.link_anchors_for_entries(conn, [r["id"] for r in rows])
    return _days(rows, lambda r: anchors.get(r["id"], {}).get(r["target"], []))


def entry_briefs(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Json]:
    tags = store.tags_for_entries(conn, [r["id"] for r in rows])
    return [
        {
            "id": r["id"],
            "date": r["entry_date"],
            "mood": r["mood_score"],
            "words": r["word_count"],
            "summary": r["summary"],
            "tags": tags.get(r["id"], []),
        }
        for r in rows
    ]


def note_ref(row: sqlite3.Row | dict[str, Any]) -> Json:
    return {"id": row["id"], "title": row["title"], "kind": row["kind"]}


def note_briefs(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[Json]:
    mentions = store.mention_stats(conn, [r["id"] for r in rows])
    out = []
    for r in rows:
        m = mentions.get(r["id"])
        out.append(
            {
                **note_ref(r),
                "aliases": json.loads(r["aliases"]),
                "summary": r["summary"],
                "mentions": m.count if m else 0,
                "first": m.first if m else None,
                "last": m.last if m else None,
            }
        )
    return out


def rollup_json(row: sqlite3.Row) -> Json:
    try:
        tags = json.loads(row["dominant_tags"] or "[]")
    except json.JSONDecodeError:
        tags = []
    try:
        start, end = store.period_bounds(row["period_type"], row["period_key"])
        bounds: list[str | None] = [start.isoformat(), end.isoformat()]
    except ValueError:
        bounds = [None, None]
    return {
        "type": row["period_type"],
        "key": row["period_key"],
        "summary": row["summary"],
        "tags": tags,
        "mood": row["avg_mood"],
        "entries": row["entry_count"],
        "stale": bool(row["stale"]),
        "generated_at": row["generated_at"],
        "start": bounds[0],
        "end": bounds[1],
    }


def note_digest_json(row: sqlite3.Row | None) -> Json | None:
    if row is None:
        return None
    return {
        "summary": row["summary"],
        "stale": bool(row["stale"]),
        "generated_at": row["generated_at"],
        "entries": row["entry_count"],
        "mood": row["avg_mood"],
        "first": row["first_date"],
        "last": row["last_date"],
    }


def answer_json(conn: sqlite3.Connection, answer: Answer) -> Json:
    dates = []
    for day in answer.cited_dates:
        rows = store.entries_in_range(conn, day, day)
        dates.append({"date": day.isoformat(), "id": rows[0]["id"] if rows else None})
    notes = []
    for title in answer.cited_notes:
        found = store.find_notes(conn, title)
        notes.append({"title": title, "id": found[0]["id"] if found else None})
    read = store.entries_by_ids(conn, answer.read_entry_ids)
    return {
        "text": answer.text,
        "body": answer.body,
        "stage": answer.stage_used,
        "sources": {"dates": dates, "notes": notes},
        "entries": entry_briefs(conn, read),
        "notes": note_briefs(conn, store.notes_by_ids(conn, answer.read_note_ids)),
    }


def json_default(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")
