from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from . import store

ExportEntry = dict[str, Any]


def collect_entries(
    conn: sqlite3.Connection,
    date_from: date | None = None,
    date_to: date | None = None,
    include_raw: bool = False,
) -> list[ExportEntry]:
    rows = store.entries_in_optional_range(conn, date_from, date_to)
    ids = [r["id"] for r in rows]
    # batched, since an export is usually the whole journal
    tags_by_entry = store.tags_for_entries(conn, ids)
    entities_by_entry = store.entities_for_entries(conn, ids)
    out: list[ExportEntry] = []
    for r in rows:
        item: ExportEntry = {
            "date": r["entry_date"],
            "source_path": r["file_path"],
            "mood_score": r["mood_score"],
            "summary": r["summary"],
            "word_count": r["word_count"],
            "tags": tags_by_entry.get(r["id"], []),
            "entities": [
                {"name": e["name"], "type": e["type"]}
                for e in entities_by_entry.get(r["id"], [])
            ],
        }
        if include_raw:
            item["raw_text"] = r["raw_text"]
        out.append(item)
    return out


def to_json(entries: list[ExportEntry]) -> str:
    return json.dumps({"entries": entries}, indent=2, ensure_ascii=False)


def to_markdown(entries: list[ExportEntry]) -> str:
    lines = ["# Journal export", ""]
    for e in entries:
        lines.append(f"## [[{e['date']}]]")
        mood = e["mood_score"]
        meta = [f"mood {mood:g}" if mood is not None else "mood -"]
        if e["tags"]:
            meta.append("tags: " + ", ".join(e["tags"]))
        ents = e["entities"]
        if ents:
            meta.append("mentions: " + ", ".join(x["name"] for x in ents))
        lines.append(f"*{' · '.join(meta)}*")
        lines.append("")
        if e["summary"]:
            lines.append(str(e["summary"]))
            lines.append("")
        if "raw_text" in e:
            lines.append(str(e["raw_text"]))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
