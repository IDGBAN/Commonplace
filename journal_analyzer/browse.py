"""`journal browse`: a local web view of everything in the cache.

The server binds to 127.0.0.1 and opens the cache with db.connect_readonly, so
it can run next to `journal watch` without touching it. Every value comes from
store/query, the same functions the CLI and models use.

Reading is GET. Anything that does something is POST: asking a question, which
sets the models going and may be kept in the history, forgetting kept
questions, and starting upkeep like writing digests. A POST has to come from
this server's own page and carry the token that page was given, so no other
site can start one.

Asking streams server-sent events, since an answer can take minutes and the
page shows each step as it happens. The answer itself is written in the
background (see work.py), so a page that reloads or goes away doesn't stop
it, and can follow it again at /api/ask/<id>/events.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import (
    compare,
    db,
    fields,
    history,
    insights,
    links,
    parser,
    profile,
    query,
    review,
    serialize,
    store,
    themes,
    unlinked,
    upkeep,
    work,
)
from .config import Config
from .models import JournalError
from .ollama_client import OllamaClient
from .parser import relative_path

DEFAULT_PORT = 8765
HOST = "127.0.0.1"
MAX_PAGE_SIZE = 200
MAX_QUESTION_CHARS = 2000
SIMILAR_K = 6
NOTE_ENTRIES_LIMIT = 500
TAG_ENTRIES_LIMIT = 1000
UNRESOLVED_LINKS_LIMIT = 60
# the "gone quiet" list on the notes page, the same as `journal lapsed` defaults
LAPSED_MIN_LINKS = 5
LAPSED_QUIET_MONTHS = 6
COOCCURRING_LIMIT = 12
LINKED_WITH_LIMIT = 12
GROUP_VALUES_LIMIT = 30
PROFILE_FACTS_LIMIT = 300
# highest and lowest days shown on a metric's page
METRIC_DAYS_SHOWN = 5
# the note map: how many notes by default and at most, and how many entries
# two of them have to share to be joined
MAP_NOTES = 40
MAP_NOTES_MAX = 150
MAP_MIN_SHARED = 2
_MAX_ROWID = 2**63 - 1
_OTHER_SITES = frozenset({"cross-site", "same-site"})
# the page sends this with every POST; it gets the value from /api/session
TOKEN_HEADER = "X-Journal-Token"
# a question, the turns before it and what it's kept to fit many times over
MAX_BODY_BYTES = 256 * 1024
_NO_ENTRY = "There is no indexed entry with that id."
_NO_NOTE = "There is no indexed note with that id."

Json = dict[str, Any]
ClientFactory = Callable[[Config], OllamaClient]

_STATIC: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

# Journal text is rendered with textContent, but the policy is a second line
# of defence if a note ever contains markup.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Request:
    config: Config
    conn: sqlite3.Connection
    params: dict[str, str]
    make_client: ClientFactory

    def day(self, key: str) -> date | None:
        value = self.params.get(key, "").strip()
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, f"'{key}' must be a date like 2024-01-15."
            ) from None

    def number(self, key: str, default: int, lo: int, hi: int) -> int:
        value = self.params.get(key, "").strip()
        if not value:
            return default
        try:
            return min(max(int(value), lo), hi)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"'{key}' must be a number.") from None


# Serializers


def _row_id(digits: str, missing: str) -> int:
    # the route only lets digits through, but they can still be past SQLite's range
    if len(digits) > len(str(_MAX_ROWID)) or int(digits) > _MAX_ROWID:
        raise ApiError(HTTPStatus.NOT_FOUND, missing)
    return int(digits)


def _vault_path(req: Request, path: str) -> str:
    return relative_path(path, req.config.vault.path)


def _focus(req: Request, text: str) -> Json | None:
    """The lines a link's #heading or #^block points at, for ?at= on entry and note pages."""
    anchor = req.params.get("at", "").strip()
    if not anchor:
        return None
    span = parser.section_span(text, anchor)
    return {"anchor": anchor, "label": parser.anchor_label(anchor), "lines": list(span) if span else None}


def _has_vector(conn: sqlite3.Connection, table: str, id_column: str, row_id: int) -> bool:
    try:
        return conn.execute(
            f"SELECT 1 FROM {table} WHERE {id_column} = ?", (row_id,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False  # the vec tables are created by the first index run


# Routes


def overview(req: Request) -> Json:
    conn, cfg = req.conn, req.config
    today = date.today()
    baseline, scored = store.overall_mood(conn, None, None)
    on_this_day = [
        r for r in store.entries_on_month_day(conn, today.month, today.day)
        if r["entry_date"] != today.isoformat()
    ]
    db_bytes = cfg.db.path.stat().st_size if cfg.db.path.exists() else 0
    return {
        "today": today.isoformat(),
        "counts": asdict(store.stats(conn)),
        "writing": asdict(store.writing_stats(conn)),
        "note_kinds": [dict(r) for r in store.notes_by_kind(conn)],
        "monthly_mood": [dict(r) for r in store.monthly_mood(conn, None, None)],
        "baseline_mood": baseline,
        "scored_entries": scored,
        "mood_scale": cfg.indexing.mood_scale_max,
        "errored_files": [
            {"path": r["path"], "vault_path": _vault_path(req, r["path"]),
             "error": r["error_message"]}
            for r in store.errored_files(conn)
        ],
        "future_entries": [
            {"date": r["entry_date"], "vault_path": _vault_path(req, r["file_path"])}
            for r in store.entries_dated_after(conn, today)
        ],
        "on_this_day": serialize.entry_briefs(conn, on_this_day),
        "setup": {
            "vault": str(cfg.vault.path),
            "notes_paths": cfg.notes.paths,
            "other_files": cfg.notes.other_files,
            "db_path": str(cfg.db.path),
            "db_bytes": db_bytes,
            "schema_version": db.SCHEMA_VERSION,
            "fast_model": cfg.ollama.fast_model,
            "precise_model": cfg.ollama.precise_model,
            "embed_model": db.get_meta(conn, "embed_model") or cfg.ollama.embed_model,
            "embed_dim": db.get_meta(conn, "embed_dim"),
            "reranking": cfg.reranking.model if cfg.reranking.enabled else None,
        },
    }


def catalog(req: Request) -> Json:
    """Everything the jump box and calendar need, in one small payload."""
    conn = req.conn
    notes = store.all_notes(conn)
    learned = serialize.learned_names(conn, [r["id"] for r in notes])
    return {
        "mood_scale": req.config.indexing.mood_scale_max,
        "entries": [
            {"id": r["id"], "date": r["entry_date"], "mood": r["mood_score"],
             "words": r["word_count"], "summary": r["summary"]}
            for r in store.entry_days(conn)
        ],
        "notes": [
            {**serialize.note_ref(r), "aliases": json.loads(r["aliases"]),
             "learned": [n["name"] for n in learned.get(r["id"], [])]}
            for r in notes
        ],
        "tags": [{"name": r["name"], "n": r["n"]} for r in store.tag_stats(conn)],
    }


def _group_param(req: Request) -> tuple[tuple[str, str], list[int]] | None:
    """?group=relationship=family as the group and its notes."""
    raw = req.params.get("group", "").strip()
    if not raw:
        return None
    key, sep, value = raw.partition("=")
    if not sep or not key.strip() or not value.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, "'group' must be a property and a value, like relationship=family.")
    ids = store.group_note_ids(req.conn, key, value)
    if not ids:
        raise ApiError(HTTPStatus.NOT_FOUND, f"No note's {key.strip()} is '{value.strip()}'.")
    return (key.strip(), value.strip()), ids


def entries(req: Request) -> Json:
    note_id = req.number("note", -1, -1, sys.maxsize)
    limit = req.number("limit", 50, 1, MAX_PAGE_SIZE)
    offset = req.number("offset", 0, 0, sys.maxsize)
    date_from, date_to = req.day("from"), req.day("to")
    group = _group_param(req)
    rows, total = store.browse_entries(
        req.conn,
        date_from=date_from,
        date_to=date_to,
        tag=req.params.get("tag") or None,
        note_id=note_id if note_id >= 0 else None,
        text=req.params.get("q") or None,
        group=group[0] if group else None,
        group_ids=group[1] if group else (),
        newest_first=req.params.get("order", "newest") != "oldest",
        limit=limit,
        offset=offset,
    )
    note = store.notes_by_ids(req.conn, [note_id]) if note_id >= 0 else []
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "note": serialize.note_ref(note[0]) if note else None,
        "group": {"key": group[0][0], "value": group[0][1], "notes": len(group[1])} if group else None,
        "items": serialize.entry_briefs(req.conn, rows),
    }


def _groups(req: Request) -> list[Json]:
    """Properties that put two or more notes together, with their values."""
    wanted = set(req.config.notes.group_keys)
    out = []
    for key in store.property_keys(req.conn):
        if wanted and key["key"] not in wanted:
            continue
        values = [
            {"value": r["value"], "notes": r["notes"], "entries": r["entries"]}
            for r in store.property_groups(req.conn, key["key"])
        ]
        if any(v["notes"] >= 2 for v in values):
            out.append({"key": key["key"], "name": key["name"], "values": values[:GROUP_VALUES_LIMIT]})
    return out


def entry(req: Request, entry_id: str) -> Json:
    conn = req.conn
    row = store.entry_by_id(conn, _row_id(entry_id, _NO_ENTRY))
    if row is None:
        raise ApiError(HTTPStatus.NOT_FOUND, _NO_ENTRY)
    before, after = store.adjacent_entries(conn, row)
    file = store.get_file_record(conn, row["file_path"])
    similar_ids = store.similar_entry_ids(conn, row["id"], SIMILAR_K)
    order = {eid: i for i, eid in enumerate(similar_ids)}
    similar = sorted(store.entries_by_ids(conn, similar_ids), key=lambda r: order[r["id"]])
    digests = []
    day = date.fromisoformat(row["entry_date"])
    for period_type, key in store.period_keys_for_date(day).items():
        rollup = store.get_rollup(conn, period_type, key)
        digests.append(
            {"type": period_type, "key": key, "exists": rollup is not None,
             "stale": bool(rollup["stale"]) if rollup else False,
             "summary": rollup["summary"] if rollup else None}
        )
    # spans of days only exist once someone asks for one
    digests.extend(
        {"type": "range", "key": r["period_key"], "exists": True, "stale": bool(r["stale"]),
         "summary": r["summary"]}
        for r in store.range_rollups_overlapping(conn, day, day)
    )
    frontmatter = store.file_frontmatter(conn, row["file_path"])
    reading = store.entry_reading(conn, row["id"])
    own_mood = fields.frontmatter_mood(frontmatter, req.config)
    parts = serialize.note_parts(conn, row["id"])
    parts_found = store.sections_for_entries(conn, [row["id"]]).get(row["id"], [])
    embedded_parts = {
        p["id"] for p in parts_found if _has_vector(conn, "sections_vec", "section_id", p["id"])
    }
    return {
        "id": row["id"],
        "date": row["entry_date"],
        "mood": row["mood_score"],
        "mood_from_frontmatter": own_mood is not None and own_mood == row["mood_score"],
        "frontmatter_tags": parser.frontmatter_tags(frontmatter)
        if req.config.tags.from_frontmatter else [],
        "model_reading": {"mood": reading.mood_score, "tags": reading.tags} if reading else None,
        "mood_scale": req.config.indexing.mood_scale_max,
        "words": row["word_count"],
        "summary": row["summary"],
        "details": store.details_for_entries(conn, [row["id"]]).get(row["id"]),
        "model_used": row["model_used"],
        "extracted_at": row["extracted_at"],
        "raw_text": row["raw_text"],
        "path": row["file_path"],
        "vault_path": _vault_path(req, row["file_path"]),
        "obsidian_url": parser.obsidian_url(row["file_path"]),
        "file": {
            "status": file["status"], "last_indexed": file["last_indexed"],
            "error": file["error_message"],
        } if file else None,
        "tags": store.tags_for_entry(conn, row["id"]),
        "metrics": store.metrics_for_entries(conn, [row["id"]]).get(row["id"], {}),
        "notes": [
            {"id": r["id"], "title": r["name"], "kind": r["type"], "sections": parts.get(r["id"], [])}
            for r in store.entities_for_entry(conn, row["id"])
        ],
        "unresolved_links": store.unresolved_links_for_entry(conn, row["id"]),
        "links_to_days": serialize.days_linked_from(conn, row["id"]),
        "linked_from_days": serialize.days_linking_to(conn, row),
        "linked_from_notes": [serialize.note_ref(r) for r in store.notes_linking_day(conn, row)],
        "focus": _focus(req, row["raw_text"]),
        "parts": [
            {"heading": p["heading"], "lines": [p["start_line"], p["end_line"]],
             "embedded": p["id"] in embedded_parts}
            for p in parts_found
        ],
        "photos": [
            {"name": p["name"], "path": p["path"], "description": p["description"]}
            for p in store.photos_for_entries(conn, [row["id"]]).get(row["id"], [])
        ],
        "embedded": _has_vector(conn, "entries_vec", "entry_id", row["id"]),
        "similar": serialize.entry_briefs(conn, similar),
        "digests": digests,
        "prev": {"id": before["id"], "date": before["entry_date"]} if before else None,
        "next": {"id": after["id"], "date": after["entry_date"]} if after else None,
    }


def notes(req: Request) -> Json:
    conn = req.conn
    broken = links.diagnose(req.config, conn)
    _, latest = store.date_bounds(conn, None, None)
    quiet = (
        store.lapsed_notes(
            conn, LAPSED_MIN_LINKS, store.months_before(date.fromisoformat(latest), LAPSED_QUIET_MONTHS)
        )
        if latest else []
    )
    return {
        "kinds": [dict(r) for r in store.notes_by_kind(conn)],
        "notes": serialize.note_briefs(conn, store.all_notes(conn)),
        "groups": _groups(req),
        "gone_quiet": [
            {"id": r.note_id, "title": r.title, "kind": r.kind, "n": r.count, "last": r.last}
            for r in quiet
        ],
        "gone_quiet_rule": {"min": LAPSED_MIN_LINKS, "months": LAPSED_QUIET_MONTHS},
        # without the vault walk, a file outside the note folders reads as missing
        "unresolved_links": [asdict(b) for b in broken[:UNRESOLVED_LINKS_LIMIT]],
        "unresolved_total": len(broken),
    }


def note(req: Request, note_id: str) -> Json:
    conn = req.conn
    rows = store.notes_by_ids(conn, [_row_id(note_id, _NO_NOTE)])
    if not rows:
        raise ApiError(HTTPStatus.NOT_FOUND, _NO_NOTE)
    row = rows[0]
    parsed = store.note_from_row(row, req.config.vault.path)
    linking = serialize.entry_briefs(conn, store.entries_mentioning(conn, row["id"], NOTE_ENTRIES_LIMIT))
    mentions = store.mention_stats(conn, [row["id"]]).get(row["id"])
    if row["model_used"]:
        summary_source = "model"
    elif row["summary"]:
        summary_source = "own_text"
    else:
        summary_source = None
    return {
        **serialize.note_ref(row),
        "aliases": parsed.aliases,
        "learned_names": serialize.learned_names(conn, [row["id"]]).get(row["id"], []),
        "learned_min": req.config.notes.learned_alias_min,
        "properties": parsed.properties,
        "property_groups": [dict(r) for r in store.property_values_for(conn, row["id"])],
        "summary": row["summary"],
        "summary_source": summary_source,
        "model_used": row["model_used"],
        "extracted_at": row["extracted_at"],
        "raw_text": parsed.raw_text,
        "focus": _focus(req, parsed.raw_text),
        "words": row["word_count"],
        "path": row["file_path"],
        "vault_path": parsed.rel_path,
        "obsidian_url": parser.obsidian_url(row["file_path"]),
        "embedded": _has_vector(conn, "notes_vec", "note_id", row["id"]),
        "mentions": asdict(mentions) if mentions else None,
        "by_year": [dict(r) for r in store.mentions_by_year(conn, row["id"])],
        "avg_mood": store.browse_mood(conn, note_id=row["id"]),
        "baseline_mood": store.overall_mood(conn, None, None)[0],
        "mood_scale": req.config.indexing.mood_scale_max,
        "digest": serialize.note_digest_json(store.get_note_digest(conn, row["link_key"])),
        "digest_min_mentions": req.config.rollups.note_min_mentions,
        "linked_with": [dict(r) for r in store.notes_linked_with(conn, row["id"], LINKED_WITH_LIMIT)],
        "linked_days": serialize.entry_briefs(conn, store.days_linked_from_note(conn, row["id"])),
        "day_links_count": req.config.notes.day_links_count,
        "outlinks": [serialize.note_ref(r) for r in store.note_outlinks(conn, row["id"])],
        "backlinks": [serialize.note_ref(r) for r in store.note_backlinks(conn, row["id"])],
        "entries": linking,
        "entries_capped": bool(mentions and mentions.count > len(linking)),
        "unlinked": [
            {"id": m.entry_id, "date": m.entry_date, "name": m.name, "snippet": m.snippet}
            for m in unlinked.for_note(conn, row["id"])
        ],
    }


def tags(req: Request) -> Json:
    return {
        "baseline_mood": store.overall_mood(req.conn, None, None)[0],
        "mood_scale": req.config.indexing.mood_scale_max,
        "tags": [dict(r) for r in store.tag_stats(req.conn)],
    }


def tag(req: Request, name: str) -> Json:
    conn = req.conn
    rows, total = store.browse_entries(conn, tag=name, limit=TAG_ENTRIES_LIMIT)
    if not total:
        raise ApiError(HTTPStatus.NOT_FOUND, f"No indexed entry is tagged '{name}'.")
    return {
        "name": name,
        "total": total,
        "avg_mood": store.browse_mood(conn, tag=name),
        "baseline_mood": store.overall_mood(conn, None, None)[0],
        "mood_scale": req.config.indexing.mood_scale_max,
        "cooccurring": [
            dict(r) for r in store.tags_cooccurring_with(conn, name, COOCCURRING_LIMIT)
        ],
        "entries": serialize.entry_briefs(conn, rows),
        "entries_capped": total > len(rows),
    }


def digests(req: Request) -> Json:
    grouped: dict[str, list[Json]] = {"year": [], "month": [], "week": [], "range": []}
    for row in store.all_rollups(req.conn):
        grouped.setdefault(row["period_type"], []).append(serialize.rollup_json(row))
    notes = [
        {**serialize.note_ref({"id": r["note_id"], "title": r["title"], "kind": r["kind"]}),
         **(serialize.note_digest_json(r) or {})}
        for r in store.all_note_digests(req.conn)
    ]
    return {
        "mood_scale": req.config.indexing.mood_scale_max,
        "digests": grouped,
        "notes": notes,
        "note_min_mentions": req.config.rollups.note_min_mentions,
    }


def digest(req: Request, period_type: str, period_key: str) -> Json:
    conn = req.conn
    try:
        start, end = store.period_bounds(period_type, period_key)
    except ValueError:
        raise ApiError(
            HTTPStatus.BAD_REQUEST, f"'{period_type} {period_key}' isn't a digest period."
        ) from None
    row = store.get_rollup(conn, period_type, period_key)
    members = store.entries_in_range(conn, start, end)
    if row is None and not members:
        raise ApiError(HTTPStatus.NOT_FOUND, "There is no digest or entry for that period.")
    return {
        "type": period_type,
        "key": period_key,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "mood_scale": req.config.indexing.mood_scale_max,
        "digest": serialize.rollup_json(row) if row else None,
        "entries": serialize.entry_briefs(conn, members),
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def metrics(req: Request) -> Json:
    rows = store.metric_keys(req.conn)
    return {
        "metrics": [{**dict(r), "yes_no": store.metric_is_yes_no(req.conn, r["key"])} for r in rows],
    }


def metric(req: Request, key: str) -> Json:
    conn, cfg = req.conn, req.config
    name = key.strip().casefold()
    found = next((r for r in store.metric_keys(conn) if r["key"] == name), None)
    if found is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"No entry records '{name}'.")
    split = insights.mood_split(store.metric_mood_pairs(conn, name, None, None))
    least = cfg.insights.min_sample_size
    highest = store.entries_by_metric(conn, name, METRIC_DAYS_SHOWN, highest=True)
    lowest = store.entries_by_metric(conn, name, METRIC_DAYS_SHOWN, highest=False)
    return {
        **dict(found),
        "yes_no": store.metric_is_yes_no(conn, name),
        "mood_scale": cfg.indexing.mood_scale_max,
        "baseline_mood": store.overall_mood(conn, None, None)[0],
        "months": [dict(r) for r in store.monthly_metric(conn, name, None, None)],
        "mood_months": [dict(r) for r in store.monthly_mood_with_metric(conn, name)],
        # too few days on a side and the averages say little, as in journal insights
        "split": {
            "yes_no": split.yes_no, "median": split.median,
            "low": {"mood": _mean(split.low), "days": len(split.low)},
            "high": {"mood": _mean(split.high), "days": len(split.high)},
            "enough": len(split.low) >= least and len(split.high) >= least,
        } if split else None,
        "highest": [
            {**b, "value": r["value"]} for b, r in zip(serialize.entry_briefs(conn, highest), highest, strict=True)
        ],
        "lowest": [
            {**b, "value": r["value"]} for b, r in zip(serialize.entry_briefs(conn, lowest), lowest, strict=True)
        ],
        "days": [{"date": r["date"], "id": r["id"], "value": r["value"]} for r in store.metric_days(conn, name)],
    }


def _period_param(req: Request, key: str) -> tuple[str, str, date, date]:
    raw = req.params.get(key, "").strip()
    try:
        return store.parse_period(raw)
    except ValueError:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"'{raw}' isn't a period. Use a year (2024), a month (2024-03) or an ISO week (2024-W10).",
        ) from None


def comparison(req: Request) -> Json:
    """?first= and, optionally, ?second=; without it, the period just before the first."""
    kind, key, start, end = _period_param(req, "first")
    if req.params.get("second", "").strip():
        _, other, other_start, other_end = _period_param(req, "second")
        periods = ((key, start, end), (other, other_start, other_end))
    else:
        periods = (compare.previous(kind, start), (key, start, end))
    result = compare.compare(req.conn, *periods)
    if not result.a.writing.entries and not result.b.writing.entries:
        raise ApiError(HTTPStatus.NOT_FOUND, f"No entries in {result.a.label} or {result.b.label}.")
    titles = {}
    for label in (result.a.label, result.b.label):
        k, normal, _, _ = store.parse_period(label)
        titles[label] = {"kind": k, "key": normal, "title": store.period_title(k, normal)}
    return {
        **asdict(result),
        "periods": titles,
        "mood_scale": req.config.indexing.mood_scale_max,
        "min_count": compare.MIN_COUNT,
    }


def review_page(req: Request, period: str) -> Json:
    try:
        kind, key, _, _ = store.parse_period(period)
    except ValueError:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"'{period}' isn't a period. Use a year (2024), a month (2024-03) or an ISO week (2024-W10).",
        ) from None
    built = review.build(req.config, req.conn, key)
    if built is None:
        raise ApiError(HTTPStatus.NOT_FOUND, f"No entries in {store.period_title(kind, key)}.")
    return {**review.to_json(req.conn, built), "mood_scale": req.config.indexing.mood_scale_max}


def graph(req: Request) -> Json:
    """The notes linked most, and which of them turn up in the same entries."""
    date_from, date_to = req.day("from"), req.day("to")
    kind = req.params.get("kind", "").strip() or None
    nodes, edges = store.note_graph(
        req.conn,
        req.number("limit", MAP_NOTES, 1, MAP_NOTES_MAX),
        req.number("min", MAP_MIN_SHARED, 1, 1000),
        date_from, date_to, kind,
    )
    return {
        "nodes": [dict(r) for r in nodes],
        "edges": [dict(r) for r in edges],
        "kinds": [dict(r) for r in store.notes_by_kind(req.conn)],
        "min_shared": req.number("min", MAP_MIN_SHARED, 1, 1000),
    }


def themes_page(req: Request) -> Json:
    placed = themes.membership(req.conn)
    listed = themes.overview(req.conn, placed)
    # entries each month, so a theme's month can be shown as its share of it
    totals: dict[str, int] = {}
    for r in store.entry_days(req.conn):
        totals[r["entry_date"][:7]] = totals.get(r["entry_date"][:7], 0) + 1
    return {
        "status": themes.status_json(themes.status(req.conn, req.config)),
        "themes": [themes.theme_json(t) for t in listed],
        "month_totals": dict(sorted(totals.items())),
        "mood_scale": req.config.indexing.mood_scale_max,
    }


def theme_page(req: Request, theme_id: str) -> Json:
    missing = "There's no theme with that number. Themes are numbered again at each refresh."
    shown = themes.detail(req.conn, _row_id(theme_id, missing))
    if shown is None:
        raise ApiError(HTTPStatus.NOT_FOUND, missing)
    return {
        **themes.theme_json(shown.theme),
        "central": serialize.entry_briefs(req.conn, shown.central),
        "newest": serialize.entry_briefs(req.conn, shown.newest),
        "tags": [{"name": n, "entries": c} for n, c in shown.tags],
        "notes": [{"id": i, "title": t, "kind": k, "entries": c} for i, t, k, c in shown.notes],
        "baseline_mood": store.overall_mood(req.conn, None, None)[0],
        "mood_scale": req.config.indexing.mood_scale_max,
    }


HISTORY_LISTED = 30
_NO_QUESTION = "There's no question with that number in your history."


def history_list(req: Request) -> Json:
    limit = req.number("limit", HISTORY_LISTED, 1, max(req.config.history.keep, HISTORY_LISTED))
    return {
        "enabled": req.config.history.enabled,
        "total": history.count(req.config),
        "items": [
            {k: v for k, v in kept.as_json().items() if k not in ("answer", "sources")}
            for kept in history.recent(req.config, limit)
        ],
    }


def _kept_json(req: Request, kept: history.Kept) -> Json:
    # ids change when files are indexed again, so they're looked up now
    dates = []
    for d in kept.sources.get("dates", []):
        day = date.fromisoformat(d["date"])
        rows = store.entries_in_range(req.conn, day, day)
        dates.append({**d, "id": rows[0]["id"] if rows else None})
    notes = []
    for title in kept.sources.get("notes", []):
        matches = store.find_notes(req.conn, title)
        notes.append({"title": title, "id": matches[0]["id"] if matches else None})
    return {**kept.as_json(), "sources": {"dates": dates, "notes": notes}}


def history_item(req: Request, kept_id: str) -> Json:
    row_id = _row_id(kept_id, _NO_QUESTION)
    found = history.get(req.config, row_id)
    if found is None:
        raise ApiError(HTTPStatus.NOT_FOUND, _NO_QUESTION)
    # the turns before it in the same conversation, to read it in context and
    # to follow it up the way `journal ask --after` does
    earlier = [k for k in history.conversation_until(req.config, row_id) if k.id != found.id]
    return {**_kept_json(req, found), "earlier": [_kept_json(req, k) for k in earlier]}


def profile_page(req: Request) -> Json:
    conn, cfg = req.conn, req.config
    found = profile.current(conn)
    facts = profile.facts_json(conn, PROFILE_FACTS_LIMIT)
    # facts keep the entry's date, which is enough to link to that day
    days: dict[str, int | None] = {}
    for f in facts:
        day = str(f["date"])
        if day not in days:
            rows = store.entries_in_range(conn, date.fromisoformat(day), date.fromisoformat(day))
            days[day] = rows[0]["id"] if rows else None
    return {
        "enabled": cfg.profile.enabled,
        "note": cfg.profile.note or None,
        "profile": None if found is None else {
            "text": found.text, "names": found.names, "updated_at": found.updated_at,
        },
        **profile.summary_counts(conn),
        "facts_count": store.profile_fact_count(conn),
        "facts": [{**f, "entry_id": days.get(str(f["date"]))} for f in facts],
    }


def files(req: Request) -> Json:
    return {
        "files": [
            {
                "path": r["path"],
                "vault_path": _vault_path(req, r["path"]),
                "status": r["status"],
                "last_indexed": r["last_indexed"],
                "error": r["error_message"],
                "entries": r["entries"],
                "entry_id": r["entry_id"],
                "note_id": r["note_id"],
            }
            for r in store.files_overview(req.conn)
        ]
    }


def search(req: Request) -> Json:
    text = req.params.get("q", "").strip()
    if not text:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Type something to search for.")
    limit = req.number("limit", 20, 1, MAX_PAGE_SIZE)
    date_from, date_to = req.day("from"), req.day("to")
    notice = None
    results: query.SearchResults | None = None
    if req.params.get("mode") == "meaning":
        try:
            results = query.search(
                text, req.config, req.conn, req.make_client(req.config),
                limit=limit, date_from=date_from, date_to=date_to,
            )
        except JournalError as exc:
            # Ollama down or a different embed model: keyword hits still help
            notice = str(exc)
    mode = "meaning" if results is not None else "keyword"
    if results is None:
        results = query.keyword_search(
            text, req.conn, limit=limit, date_from=date_from, date_to=date_to
        )
    return {
        "q": text,
        "mode": mode,
        "notice": notice,
        "entries": serialize.entry_briefs(req.conn, results.entries, results.parts),
        "notes": serialize.note_briefs(req.conn, results.notes),
    }


def _ask_params(body: Json) -> tuple[str, list[tuple[str, str]]]:
    asked = body.get("q")
    question = asked.strip() if isinstance(asked, str) else ""
    if not question:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Type a question to ask.")
    if len(question) > MAX_QUESTION_CHARS:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"That question is over {MAX_QUESTION_CHARS} characters. Try a shorter one.",
        )
    raw = body.get("history") or []
    if not isinstance(raw, list) or not all(
        isinstance(turn, list) and len(turn) == 2 and all(isinstance(x, str) for x in turn)
        for turn in raw
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "The conversation so far couldn't be read.")
    history = [(q, a) for q, a in raw][-query.CHAT_HISTORY_TURNS:]
    return question, history


def _ask_scope(req: Request) -> tuple[store.EntryFilter | None, date | None, date | None]:
    """?note=, ?tag=, ?group= and ?from=/?to= keep an answer to some entries,
    like --about, --tag, --group, --since and --until on `journal ask`."""
    note_ids: tuple[int, ...] = ()
    raw_note = req.params.get("note", "").strip()
    if raw_note:
        if not raw_note.isdigit():
            raise ApiError(HTTPStatus.BAD_REQUEST, "'note' must be a note's id.")
        rows = store.notes_by_ids(req.conn, [_row_id(raw_note, _NO_NOTE)])
        if not rows:
            raise ApiError(HTTPStatus.NOT_FOUND, _NO_NOTE)
        note_ids = (rows[0]["id"],)
    tag = req.params.get("tag", "").strip().lstrip("#").strip().lower() or None
    if tag and not store.tag_exists(req.conn, tag):
        raise ApiError(HTTPStatus.NOT_FOUND, f"No indexed entry is tagged '{tag}'.")
    group = _group_param(req)
    date_from, date_to = req.day("from"), req.day("to")
    if date_from and date_to and date_from > date_to:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"'from' ({date_from}) is after 'to' ({date_to}).")
    only = store.EntryFilter(tag, note_ids, group[0] if group else None, tuple(group[1]) if group else ())
    return only or None, date_from, date_to


# What a POST can do besides asking. Each gets the config and the request's
# JSON body, and opens whatever it writes to itself.


def forget_question(server: BrowseServer, body: Json, kept_id: str) -> Json:
    row_id = _row_id(kept_id, _NO_QUESTION)
    if not history.delete(server.config, row_id):
        raise ApiError(HTTPStatus.NOT_FOUND, _NO_QUESTION)
    return {"deleted": row_id}


def clear_history(server: BrowseServer, body: Json) -> Json:
    try:
        held = history.clear(server.config)
    except OSError as exc:
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"Couldn't delete {history.history_path(server.config)}: {exc}. If a question is "
            "being answered, try again once it's done.",
        ) from exc
    return {"cleared": held}


_NO_RUN = "That answer isn't being written any more. `journal browse` may have been restarted."


def cancel_answer(server: BrowseServer, body: Json, run_id: str) -> Json:
    run = server.answers.get(run_id)
    if run is None:
        raise ApiError(HTTPStatus.NOT_FOUND, _NO_RUN)
    run.cancel()
    return {"cancelled": run_id}


def start_upkeep(server: BrowseServer, body: Json) -> Json:
    kind = body.get("kind")
    params: Json = {}
    if body.get("rebuild") is True:
        params["rebuild"] = True
    try:
        if kind == "digest":
            # the key the way digests are stored, so asking twice is one job
            params = {"key": store.parse_period(str(body.get("key", "")), ranges=True)[1]}
        label = upkeep.describe(str(kind), params)
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "That isn't something upkeep can do.") from None
    other = upkeep.elsewhere(server.config)
    if other is not None:
        raise ApiError(
            HTTPStatus.CONFLICT,
            f"{other.get('what', 'Something else')} is already working on the cache, in another "
            "window. Start this once it has finished.",
        )
    return server.jobs.add(str(kind), label, params).as_json()


def stop_upkeep(server: BrowseServer, body: Json) -> Json:
    return {"stopping": server.jobs.stop()}


def remove_upkeep(server: BrowseServer, body: Json, job_id: str) -> Json:
    if not server.jobs.remove(_row_id(job_id, "That job isn't waiting any more.")):
        raise ApiError(HTTPStatus.NOT_FOUND, "That job isn't waiting any more.")
    return {"removed": int(job_id)}


Handler = Callable[..., Json]
Action = Callable[..., Json]

_ACTIONS: list[tuple[re.Pattern[str], Action]] = [
    (re.compile(r"/api/history/(\d+)/delete"), forget_question),
    (re.compile(r"/api/history/clear"), clear_history),
    (re.compile(r"/api/ask/([0-9a-f]{32})/cancel"), cancel_answer),
    (re.compile(r"/api/upkeep/start"), start_upkeep),
    (re.compile(r"/api/upkeep/stop"), stop_upkeep),
    (re.compile(r"/api/upkeep/(\d+)/remove"), remove_upkeep),
]
_ANSWER_EVENTS = re.compile(r"/api/ask/([0-9a-f]{32})/events")
# said to a question that has to wait for an upkeep step to let go of the model
_WAITING_FOR_UPKEEP = "Waiting for the upkeep step in hand to finish..."

_ROUTES: list[tuple[re.Pattern[str], Handler]] = [
    (re.compile(r"/api/overview"), overview),
    (re.compile(r"/api/catalog"), catalog),
    (re.compile(r"/api/entries"), entries),
    (re.compile(r"/api/entries/(\d+)"), entry),
    (re.compile(r"/api/notes"), notes),
    (re.compile(r"/api/notes/(\d+)"), note),
    (re.compile(r"/api/tags"), tags),
    (re.compile(r"/api/tags/(.+)"), tag),
    (re.compile(r"/api/digests"), digests),
    (re.compile(r"/api/digests/(week|month|year|range)/([^/]+)"), digest),
    (re.compile(r"/api/files"), files),
    (re.compile(r"/api/profile"), profile_page),
    (re.compile(r"/api/metrics"), metrics),
    (re.compile(r"/api/compare"), comparison),
    (re.compile(r"/api/graph"), graph),
    (re.compile(r"/api/themes"), themes_page),
    (re.compile(r"/api/themes/(\d+)"), theme_page),
    (re.compile(r"/api/history"), history_list),
    (re.compile(r"/api/history/(\d+)"), history_item),
    (re.compile(r"/api/review/([^/]+)"), review_page),
    (re.compile(r"/api/metrics/(.+)"), metric),
    (re.compile(r"/api/search"), search),
]


# Server


class _Handler(BaseHTTPRequestHandler):
    server: BrowseServer
    server_version = "journal-browse"
    sys_version = ""

    def do_GET(self) -> None:
        # A page on another site can point a DNS name at 127.0.0.1; its
        # requests still carry that name in Host, so they're refused.
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Unexpected Host header."})
            return
        url = urlsplit(self.path)
        # The Host check doesn't stop a page elsewhere from sending requests
        # to 127.0.0.1 itself. It can't read the replies, but /api/ask would
        # still set the models going. Browsers say where a request came from.
        if url.path.startswith("/api/") and self.headers.get("Sec-Fetch-Site") in _OTHER_SITES:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Requests from other sites are refused."})
            return
        if url.path == "/api/session":
            # only this server's own page can read the reply, so only it can POST
            self._send_json(HTTPStatus.OK, {"token": self.server.token})
        elif url.path == "/api/upkeep":
            self._upkeep()
        elif found := _ANSWER_EVENTS.fullmatch(url.path):
            self._follow_again(found.group(1), url.query)
        elif url.path == "/api/ask" or any(p.fullmatch(url.path) for p, _ in _ACTIONS):
            self._wrong_method("POST")
        elif url.path.startswith("/api/"):
            self._api(url.path, url.query)
        else:
            self._static(url.path)

    def do_POST(self) -> None:
        # Read what was sent before answering, even to refuse it. Closing a
        # connection with its body unread resets it on Windows, and the page
        # would get a dropped connection where it needed the status.
        sent = self._sent()
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Unexpected Host header."})
            return
        path = urlsplit(self.path).path
        action = next(
            ((handler, match) for pattern, handler in _ACTIONS if (match := pattern.fullmatch(path))),
            None,
        )
        if path != "/api/ask" and action is None:
            if any(pattern.fullmatch(path) for pattern, _ in _ROUTES):
                self._wrong_method("GET")
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "No such API route."})
            return
        try:
            self._from_this_page()
            body = self._body(sent)
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        if action is None:
            self._ask(body)
        else:
            self._act(action[0], body, [unquote(g) for g in action[1].groups()])

    def _wrong_method(self, allowed: str = "GET, POST") -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": f"This address only takes {allowed}."},
            {"Allow": allowed},
        )

    def _other_method(self) -> None:
        self._sent()
        self._wrong_method()

    do_PUT = do_PATCH = do_DELETE = _other_method

    def _from_this_page(self) -> None:
        """Refuses a POST that this server's own page didn't send.

        A page on another site can make a browser send a POST here. Browsers
        say where a request came from, and that page can't read /api/session
        to learn the token, so it fails on every count.
        """
        refused = ApiError(HTTPStatus.FORBIDDEN, "Requests from other sites are refused.")
        if self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            raise refused
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            raise refused
        sent = self.headers.get(TOKEN_HEADER, "")
        if not secrets.compare_digest(sent.encode(), self.server.token.encode()):
            # A page left open outlives a restart of `journal browse`. It asks
            # /api/session again when it sees this status.
            raise ApiError(HTTPStatus.UNAUTHORIZED, "This page's session has ended. Reload it and try again.")

    def _sent(self) -> bytes | ApiError:
        """The request's body, or why it wasn't read."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return ApiError(HTTPStatus.LENGTH_REQUIRED, "The request needs a Content-Length.")
        if length < 0 or length > MAX_BODY_BYTES:
            return ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "That request is too big.")
        return self.rfile.read(length)

    def _body(self, sent: bytes | ApiError) -> Json:
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Send the request as application/json.")
        if isinstance(sent, ApiError):
            raise sent
        try:
            body = json.loads(sent or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "The request couldn't be read as JSON.") from None
        if not isinstance(body, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "The request has to be a JSON object.")
        return body

    def _act(self, action: Action, body: Json, args: list[str]) -> None:
        try:
            payload = action(self.server, body, *args)
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except JournalError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except sqlite3.Error as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"That couldn't be saved: {exc}"})
        except Exception as exc:  # noqa: BLE001 - otherwise the page just sees a dropped connection
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"The journal browser hit a bug: {type(exc).__name__}: {exc}"},
            )
        else:
            self._send_json(HTTPStatus.OK, payload)

    def log_message(self, format: str, *args: Any) -> None:
        # Search text is in the URL. Keep it out of terminal scrollback.
        return

    def _api(self, path: str, query_string: str) -> None:
        for pattern, handler in _ROUTES:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            params = {k: v[-1] for k, v in parse_qs(query_string).items()}
            args = [unquote(g) for g in match.groups()]
            try:
                with closing(db.connect_readonly(self.server.config)) as conn:
                    request = Request(self.server.config, conn, params, self.server.make_client)
                    payload = handler(request, *args)
            except ApiError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            except JournalError as exc:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            except sqlite3.Error as exc:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"The cache couldn't be read: {exc}"},
                )
            except Exception as exc:  # noqa: BLE001 - otherwise the page just sees a dropped connection
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"The journal browser hit a bug: {type(exc).__name__}: {exc}"},
                )
            else:
                self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "No such API route."})

    def _ask(self, body: Json) -> None:
        # what an answer is kept to is named as the entries page's address names it
        params = {
            k: str(v) for k, v in body.items() if isinstance(v, str | int) and not isinstance(v, bool)
        }
        config = self.server.config
        try:
            question, turns = _ask_params(body)
            with closing(db.connect_readonly(config)) as conn:
                only, since, until = _ask_scope(Request(config, conn, params, self.server.make_client))
                scope = history.scope_text(conn, only, since, until)
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        except JournalError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        except sqlite3.Error as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"The cache couldn't be read: {exc}"})
            return

        def write(run: work.Run) -> None:
            send = run.send
            try:
                with closing(db.connect_readonly(config)) as conn:
                    client = self.server.client()
                    standalone = question
                    rewriting = 0.0
                    if turns:
                        send("stage", {"text": "Resolving your follow-up..."})
                        started = time.monotonic()
                        standalone = query.contextualize_question(
                            question, turns, config, client, profile.names(conn, config)
                        )
                        rewriting = time.monotonic() - started
                        if standalone != question:
                            send("rewritten", {"question": standalone})
                    result = query.answer(
                        standalone, config, conn, client,
                        on_stage=lambda stage: send("stage", {"text": stage}),
                        since=since, until=until, only=only,
                        on_text=lambda text: send("text", {"text": text}),
                        on_thinking=lambda text: send("thinking", {"text": text}),
                    )
                    if turns:
                        result = query.with_step(result, "reading your follow-up", rewriting)
                    asked = params.get("conversation", "")
                    kept = history.record(
                        config, question, result, "browse",
                        asked if history.valid_conversation(asked) else history.new_conversation(),
                        standalone, scope,
                    )
                    send("answer", {
                        "question": standalone, **serialize.answer_json(conn, result), "history_id": kept,
                    })
            except JournalError as exc:
                send("problem", {"error": str(exc)})
            except sqlite3.Error as exc:
                send("problem", {"error": f"The cache couldn't be read: {exc}"})

        self._follow(self.server.answers.start(write, _WAITING_FOR_UPKEEP), 0)

    def _follow(self, run: work.Run, start: int) -> None:
        """Streams an answer's events to this page for as long as it listens."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()

        def send(event: str, data: Json) -> None:
            body = json.dumps(data, default=serialize.json_default, ensure_ascii=False)
            self.wfile.write(f"event: {event}\ndata: {body}\n\n".encode())
            self.wfile.flush()

        try:
            send("run", {"id": run.id})
            for item in run.follow(start):
                if item is None:
                    self.wfile.write(b": still writing\n\n")
                    self.wfile.flush()
                else:
                    send(*item)
        except (ConnectionError, OSError):
            # the page reloaded or went away; the answer carries on without it
            return

    def _follow_again(self, run_id: str, query_string: str) -> None:
        run = self.server.answers.get(run_id)
        if run is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": _NO_RUN})
            return
        raw = parse_qs(query_string).get("from", ["0"])[-1]
        self._follow(run, int(raw) if raw.isdigit() else 0)

    def _upkeep(self) -> None:
        config = self.server.config
        try:
            payload = {
                **upkeep.state(config), **self.server.jobs.snapshot(),
                "elsewhere": upkeep.elsewhere(config),
            }
        except JournalError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except sqlite3.Error as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"The cache couldn't be read: {exc}"})
        else:
            self._send_json(HTTPStatus.OK, payload)

    def _static(self, path: str) -> None:
        asset = _STATIC.get(path)
        if asset is None:
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
            return
        name, content_type = asset
        body = resources.files(__package__).joinpath("browse_static", name).read_bytes()
        self._send(HTTPStatus.OK, body, content_type)

    def _send_json(
        self, status: HTTPStatus, payload: Json, extra: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, default=serialize.json_default, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in {**_SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class BrowseServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's in use
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, config: Config, port: int, make_client: ClientFactory):
        self.config = config
        self.make_client = make_client
        super().__init__((HOST, port), _Handler)
        self.port: int = self.socket.getsockname()[1]
        self.allowed_hosts = {f"{HOST}:{self.port}", f"localhost:{self.port}"}
        self.allowed_origins = {f"http://{host}" for host in self.allowed_hosts}
        # new on every start, and only ever handed to this server's own page
        self.token = secrets.token_urlsafe(32)
        # Answers and upkeep share one client, so a model keeps the window it
        # was loaded with instead of being loaded again for each of them.
        self._client: OllamaClient | None = None
        self._client_guard = threading.Lock()
        self.lane = work.Lane()
        self.answers = work.Answers(self.lane)
        self.jobs = work.Jobs(config, self.lane, upkeep.runners(config, self.client))

    def client(self) -> OllamaClient:
        with self._client_guard:
            if self._client is None:
                self._client = self.make_client(self.config)
            return self._client

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/"


def make_server(
    config: Config, port: int = DEFAULT_PORT, make_client: ClientFactory = OllamaClient
) -> BrowseServer:
    try:
        return BrowseServer(config, port, make_client)
    except OSError as exc:
        raise JournalError(
            f"Couldn't listen on {HOST}:{port} ({exc}). If 'journal browse' is "
            "already running, use that window, or pass --port to pick another port."
        ) from exc


def serve(
    config: Config,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    on_ready: Callable[[str], None] = print,
) -> None:
    # fail here, in the terminal, rather than as an error page
    db.connect_readonly(config).close()
    server = make_server(config, port)
    on_ready(server.url)
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
