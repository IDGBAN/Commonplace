"""JSON shapes for entries, notes, digests and answers.

`journal browse` serves these and the CLI prints them with --json, so both
describe the cache the same way.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from . import store
from .models import Answer

Json = dict[str, Any]


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
