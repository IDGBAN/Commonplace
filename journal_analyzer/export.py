from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import date
from typing import Any

from . import store

ExportEntry = dict[str, Any]

# Spreadsheets run a cell that starts with one of these as a formula, so
# "- [x] pack lunch" shows up as #NAME?. Text cells get a ' in front.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_CSV_LIST_SEPARATOR = "; "


def collect_entries(
    conn: sqlite3.Connection,
    date_from: date | None = None,
    date_to: date | None = None,
    include_raw: bool = False,
    only: store.EntryFilter | None = None,
) -> list[ExportEntry]:
    rows = store.entries_in_optional_range(conn, date_from, date_to, only)
    ids = [r["id"] for r in rows]
    # batched, since an export is usually the whole journal
    tags_by_entry = store.tags_for_entries(conn, ids)
    entities_by_entry = store.entities_for_entries(conn, ids)
    metrics_by_entry = store.metrics_for_entries(conn, ids)
    details_by_entry = store.details_for_entries(conn, ids)
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
            "metrics": metrics_by_entry.get(r["id"], {}),
            "details": details_by_entry.get(r["id"]),
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
        meta.extend(f"{key} {value:g}" for key, value in e["metrics"].items())
        lines.append(f"*{' · '.join(meta)}*")
        lines.append("")
        if e["summary"]:
            lines.append(str(e["summary"]))
            lines.append("")
        if e["details"]:
            lines.append(str(e["details"]))
            lines.append("")
        if "raw_text" in e:
            lines.append(str(e["raw_text"]))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _text_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def _number_cell(value: float | int | None) -> str:
    return "" if value is None else f"{value:g}"


def to_csv(entries: list[ExportEntry]) -> str:
    """One row per entry, with a column for each metric any entry has."""
    keys = sorted({key for e in entries for key in e["metrics"]})
    fixed = ["date", "mood", "words", "tags", "notes"]
    raw = any("raw_text" in e for e in entries)
    tail = ["summary", "details", "source_path", *(["raw_text"] if raw else [])]
    # a metric called words or tags would otherwise share a header with them
    taken = set(fixed + tail)
    header = [*fixed, *(f"{k} (metric)" if k in taken else k for k in keys), *tail]
    buf = io.StringIO()
    # the file is written in text mode, which turns these into the platform's line ends
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for e in entries:
        writer.writerow([
            e["date"],
            _number_cell(e["mood_score"]),
            e["word_count"],
            _text_cell(_CSV_LIST_SEPARATOR.join(e["tags"])),
            _text_cell(_CSV_LIST_SEPARATOR.join(x["name"] for x in e["entities"])),
            *(_number_cell(e["metrics"].get(k)) for k in keys),
            _text_cell(e["summary"]),
            _text_cell(e["details"]),
            _text_cell(e["source_path"]),
            *([_text_cell(e.get("raw_text"))] if raw else []),
        ])
    return buf.getvalue()
