from __future__ import annotations

import calendar
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import sqlite_vec

from .models import Extraction, FrontmatterValue, ParsedEntry, ParsedNote
from .parser import Section, file_link_key, relative_path

if TYPE_CHECKING:
    from .photos import Photo

# Lists go in as one JSON array rather than a placeholder each. An id list can
# be the whole journal, and SQLite before 3.32 allows only 999 variables.
_JSON_VALUES = "SELECT value FROM json_each(?)"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_list(values: Sequence[object]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _like_contains(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def week_key(d: date) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def year_key(d: date) -> str:
    return d.strftime("%Y")


def period_keys_for_date(d: date) -> dict[str, str]:
    return {"week": week_key(d), "month": month_key(d), "year": year_key(d)}


# `journal digest 2024-03-02..2024-03-11`; longer spans have month and year digests
RANGE_MAX_DAYS = 92
_RANGE_SEP = ".."


def range_key(start: date, end: date) -> str:
    return f"{start.isoformat()}{_RANGE_SEP}{end.isoformat()}"


def period_bounds(period_type: str, period_key: str) -> tuple[date, date]:
    """First and last day of a week, month, year or range key. ValueError if malformed."""
    if period_type == "range":
        first, sep, last = period_key.partition(_RANGE_SEP)
        if not sep:
            raise ValueError(f"'{period_key}' isn't a span of days")
        return date.fromisoformat(first), date.fromisoformat(last)
    if period_type == "week":
        year, week = period_key.split("-W")
        start = date.fromisocalendar(int(year), int(week), 1)
        return start, date.fromisocalendar(int(year), int(week), 7)
    if period_type == "month":
        start = datetime.strptime(period_key, "%Y-%m").date()
        next_month = date(start.year + start.month // 12, start.month % 12 + 1, 1)
        return start, date.fromordinal(next_month.toordinal() - 1)
    if period_type == "year":
        start = datetime.strptime(period_key, "%Y").date()
        return start, date(start.year, 12, 31)
    raise ValueError(f"unknown period type '{period_type}'")


_PERIOD_PATTERNS = (
    ("year", re.compile(r"\d{4}")),
    ("month", re.compile(r"\d{4}-\d{1,2}")),
    ("week", re.compile(r"\d{4}-[Ww]\d{1,2}")),
)


_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")


def parse_period(text: str, ranges: bool = False) -> tuple[str, str, date, date]:
    """A year, month or ISO week as typed, like 2024, 2024-03 or 2024-W10, and
    with `ranges` a span of days like 2024-03-02..2024-03-11.

    Returns (kind, key, first day, last day), the key written the way rollups
    are stored, so 2024-w3 is 2024-W03. ValueError if it's none of those, or
    a span that ends before it starts or is longer than RANGE_MAX_DAYS.
    """
    value = text.strip()
    found = _RANGE_RE.fullmatch(value) if ranges else None
    if found:
        try:
            start, end = date.fromisoformat(found[1]), date.fromisoformat(found[2])
        except ValueError:
            raise ValueError(f"'{text}' has a day that isn't a date") from None
        if end < start:
            raise ValueError(f"{value} ends before it starts")
        days = (end - start).days + 1
        if days > RANGE_MAX_DAYS:
            raise ValueError(
                f"{value} is {days} days, and a span can be up to {RANGE_MAX_DAYS}. "
                "For longer ones, read the month or year digests"
            )
        return "range", range_key(start, end), start, end
    for kind, pattern in _PERIOD_PATTERNS:
        if pattern.fullmatch(value):
            start, end = period_bounds(kind, value.upper())
            return kind, _PERIOD_KEY_FN[kind](start), start, end
    raise ValueError(f"'{text}' isn't a year, a month or an ISO week")


def _day_title(d: date, month: bool = True, year: bool = True) -> str:
    return " ".join([str(d.day), *([d.strftime("%B")] if month else []), *([str(d.year)] if year else [])])


def period_title(kind: str, key: str) -> str:
    """2024, March 2024, Week 10 of 2024 or 2 to 11 March 2024, for a key the
    way rollups store it."""
    if kind == "range":
        start, end = period_bounds(kind, key)
        if start == end:
            return _day_title(start)
        if start.year != end.year:
            return f"{_day_title(start)} to {_day_title(end)}"
        if start.month != end.month:
            return f"{_day_title(start, year=False)} to {_day_title(end)}"
        return f"{_day_title(start, month=False, year=False)} to {_day_title(end)}"
    if kind == "month":
        return datetime.strptime(key, "%Y-%m").strftime("%B %Y")
    if kind == "week":
        year, week = key.split("-W")
        return f"Week {int(week)} of {year}"
    return key


def get_file_record(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()


def file_states(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT f.path, f.content_hash, f.status,
                      (SELECT COUNT(*) FROM entries e WHERE e.file_path = f.path) AS entries
               FROM files f ORDER BY f.path"""
        ).fetchall()
    )


def tracked_paths(conn: sqlite3.Connection) -> list[str]:
    return [r["path"] for r in conn.execute("SELECT path FROM files ORDER BY path")]


def upsert_file(
    conn: sqlite3.Connection,
    path: str,
    content_hash: str,
    status: str,
    error_message: str | None = None,
) -> None:
    with conn:
        conn.execute(
            """INSERT INTO files(path, content_hash, last_indexed, status, error_message)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 content_hash = excluded.content_hash,
                 last_indexed = excluded.last_indexed,
                 status = excluded.status,
                 error_message = excluded.error_message""",
            (path, content_hash, _now(), status, error_message),
        )


def delete_entries_for_file(conn: sqlite3.Connection, path: str) -> list[date]:
    rows = conn.execute(
        "SELECT id, entry_date FROM entries WHERE file_path = ?", (path,)
    ).fetchall()
    dates = [date.fromisoformat(r["entry_date"]) for r in rows]
    with conn:
        # vec0 tables don't cascade
        for r in rows:
            conn.execute("DELETE FROM entries_vec WHERE entry_id = ?", (r["id"],))
            _delete_section_vectors(conn, r["id"])
        conn.execute("DELETE FROM entries WHERE file_path = ?", (path,))
        conn.execute("DELETE FROM entry_file_keys WHERE path = ?", (path,))
    return dates


def delete_note_for_file(conn: sqlite3.Connection, path: str) -> None:
    row = conn.execute("SELECT id FROM notes WHERE file_path = ?", (path,)).fetchone()
    if row is None:
        return
    with conn:
        conn.execute("DELETE FROM notes_vec WHERE note_id = ?", (row["id"],))
        conn.execute("DELETE FROM notes WHERE id = ?", (row["id"],))


def delete_file(conn: sqlite3.Connection, path: str) -> list[date]:
    dates = delete_entries_for_file(conn, path)
    delete_note_for_file(conn, path)
    with conn:
        conn.execute("DELETE FROM files WHERE path = ?", (path,))
    return dates


def save_entry(
    conn: sqlite3.Connection,
    entry: ParsedEntry,
    extraction: Extraction,
    model_used: str,
) -> int:
    with conn:
        cur = conn.execute(
            """INSERT INTO entries
               (file_path, entry_date, raw_text, word_count, mood_score,
                summary, model_used, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.source_path,
                entry.entry_date.isoformat(),
                entry.raw_text,
                len(entry.raw_text.split()),
                extraction.mood_score,
                extraction.summary,
                model_used,
                _now(),
            ),
        )
        entry_id = cur.lastrowid
        assert entry_id is not None
        _link_tags(conn, entry_id, extraction.tags)
        conn.executemany(
            "INSERT OR IGNORE INTO entry_links(entry_id, target) VALUES (?, ?)",
            [(entry_id, target) for target in entry.links],
        )
        _save_link_details(conn, entry_id, entry)
        conn.execute(
            "INSERT OR REPLACE INTO entry_file_keys(path, key) VALUES (?, ?)",
            (entry.source_path, file_link_key(entry.source_path)),
        )
    return entry_id


def save_link_details(conn: sqlite3.Connection, entry_id: int, entry: ParsedEntry) -> None:
    """Replaces what's kept about how an entry's links are written."""
    with conn:
        _save_link_details(conn, entry_id, entry)


def _save_link_details(conn: sqlite3.Connection, entry_id: int, entry: ParsedEntry) -> None:
    conn.execute("DELETE FROM entry_link_anchors WHERE entry_id = ?", (entry_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO entry_link_anchors(entry_id, target, anchor) VALUES (?, ?, ?)",
        [
            (entry_id, target, anchor)
            for target, anchors in entry.link_anchors.items()
            for anchor in anchors
            if anchor
        ],
    )
    conn.execute("DELETE FROM entry_link_labels WHERE entry_id = ?", (entry_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO entry_link_labels(entry_id, target, label) VALUES (?, ?, ?)",
        [
            (entry_id, target, label)
            for target, labels in entry.link_labels.items()
            for label in labels
        ],
    )


def link_label_uses(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(note_id, label, entry_id) for each link shown with its own text that finds a note."""
    return list(
        conn.execute(
            """SELECT lt.note_id, ell.label, ell.entry_id
               FROM entry_link_labels ell JOIN link_targets lt ON lt.target = ell.target
               ORDER BY lt.note_id, ell.entry_id"""
        ).fetchall()
    )


def note_name_keys(conn: sqlite3.Connection) -> set[str]:
    """Every title and alias, as keys."""
    return {r["key"] for r in conn.execute("SELECT key FROM note_names")}


def replace_learned_names(
    conn: sqlite3.Connection, names: Sequence[tuple[int, str, str, int]]
) -> None:
    """(note_id, name, key, entries) rows, replacing the ones there were."""
    with conn:
        conn.execute("DELETE FROM note_learned_names")
        conn.executemany(
            "INSERT OR REPLACE INTO note_learned_names(note_id, name, key, entries) VALUES (?, ?, ?, ?)",
            names,
        )


def learned_names_for(conn: sqlite3.Connection, note_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    """The names learned from link text for these notes, most used first."""
    if not note_ids:
        return {}
    try:
        rows = conn.execute(
            f"""SELECT note_id, name, entries FROM note_learned_names
                WHERE note_id IN ({_JSON_VALUES})
                  AND key NOT IN (SELECT key FROM note_names)
                ORDER BY note_id, entries DESC, name""",
            (_json_list(note_ids),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # a read-only connection to a cache from before the table
    out: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        out[r["note_id"]].append(r)
    return dict(out)


def link_anchors_for_entries(
    conn: sqlite3.Connection, entry_ids: list[int]
) -> dict[int, dict[str, list[str]]]:
    """The headings and blocks each entry's links point at, by entry and link key."""
    if not entry_ids:
        return {}
    try:
        rows = conn.execute(
            f"""SELECT entry_id, target, anchor FROM entry_link_anchors
                WHERE entry_id IN ({_JSON_VALUES}) ORDER BY entry_id, target, rowid""",
            (_json_list(entry_ids),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # a read-only connection to a cache from before the table
    out: dict[int, dict[str, list[str]]] = defaultdict(dict)
    for r in rows:
        out[r["entry_id"]].setdefault(r["target"], []).append(r["anchor"])
    return dict(out)


def entry_files(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT f.path, f.content_hash FROM files f
               WHERE f.status = 'indexed'
                 AND EXISTS (SELECT 1 FROM entries e WHERE e.file_path = f.path)
               ORDER BY f.path"""
        ).fetchall()
    )


def entries_for_file(conn: sqlite3.Connection, path: str) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM entries WHERE file_path = ? ORDER BY id", (path,)).fetchall()
    )


def save_entry_reading(conn: sqlite3.Connection, entry_id: int, reading: Extraction) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO entry_readings(entry_id, mood_score, tags) VALUES (?, ?, ?)",
            (entry_id, reading.mood_score, _json_list(reading.tags)),
        )


def save_entry_details(conn: sqlite3.Connection, entry_id: int, details: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO entry_details(entry_id, details) VALUES (?, ?)",
            (entry_id, details),
        )


def details_for_entries(conn: sqlite3.Connection, entry_ids: list[int]) -> dict[int, str]:
    if not entry_ids:
        return {}
    try:
        rows = conn.execute(
            f"SELECT entry_id, details FROM entry_details WHERE entry_id IN ({_JSON_VALUES})",
            (_json_list(entry_ids),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # a read-only connection to a cache from before the table
    return {r["entry_id"]: r["details"] for r in rows}


def save_file_frontmatter(
    conn: sqlite3.Connection, path: str, fields: dict[str, FrontmatterValue]
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO file_frontmatter(path, data) VALUES (?, ?)",
            (path, json.dumps(fields, ensure_ascii=False)),
        )


def delete_file_frontmatter(conn: sqlite3.Connection, path: str) -> None:
    with conn:
        conn.execute("DELETE FROM file_frontmatter WHERE path = ?", (path,))


def file_frontmatter(conn: sqlite3.Connection, path: str) -> dict[str, FrontmatterValue]:
    try:
        row = conn.execute(
            "SELECT data FROM file_frontmatter WHERE path = ?", (path,)
        ).fetchone()
    except sqlite3.OperationalError:
        return {}  # a read-only connection to a cache from before the table
    return json.loads(row["data"]) if row else {}


def entry_reading(conn: sqlite3.Connection, entry_id: int) -> Extraction | None:
    try:
        row = conn.execute(
            "SELECT mood_score, tags FROM entry_readings WHERE entry_id = ?", (entry_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return Extraction(mood_score=row["mood_score"], tags=json.loads(row["tags"]))


def backfill_entry_readings(conn: sqlite3.Connection) -> int:
    """Entries indexed before readings were kept: what's stored is what the model said."""
    missing = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM entries WHERE id NOT IN (SELECT entry_id FROM entry_readings)"
        )
    ]
    if not missing:
        return 0
    tags = tags_for_entries(conn, missing)
    moods = {
        r["id"]: r["mood_score"]
        for r in conn.execute(
            f"SELECT id, mood_score FROM entries WHERE id IN ({_JSON_VALUES})",
            (_json_list(missing),),
        )
    }
    with conn:
        conn.executemany(
            "INSERT INTO entry_readings(entry_id, mood_score, tags) VALUES (?, ?, ?)",
            [(i, moods.get(i), _json_list(tags.get(i, []))) for i in missing],
        )
    return len(missing)


def entry_files_without_frontmatter(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT f.path, f.content_hash FROM files f
               WHERE EXISTS (SELECT 1 FROM entries e WHERE e.file_path = f.path)
                 AND NOT EXISTS (SELECT 1 FROM file_frontmatter fm WHERE fm.path = f.path)
               ORDER BY f.path"""
        ).fetchall()
    )


@dataclass(frozen=True)
class StoredReading:
    entry_id: int
    entry_date: date
    mood_score: float | None
    tags: list[str]
    reading: Extraction
    frontmatter: dict[str, FrontmatterValue]


def stored_readings(conn: sqlite3.Connection) -> list[StoredReading]:
    rows = conn.execute(
        """SELECT e.id, e.entry_date, e.mood_score,
                  r.mood_score AS reading_mood, r.tags AS reading_tags,
                  COALESCE(fm.data, '{}') AS frontmatter
           FROM entries e
           JOIN entry_readings r ON r.entry_id = e.id
           LEFT JOIN file_frontmatter fm ON fm.path = e.file_path
           ORDER BY e.id"""
    ).fetchall()
    tags = tags_for_entries(conn, [r["id"] for r in rows])
    return [
        StoredReading(
            entry_id=r["id"],
            entry_date=date.fromisoformat(r["entry_date"]),
            mood_score=r["mood_score"],
            tags=tags.get(r["id"], []),
            reading=Extraction(
                mood_score=r["reading_mood"], tags=json.loads(r["reading_tags"])
            ),
            frontmatter=json.loads(r["frontmatter"]),
        )
        for r in rows
    ]


def update_entry_mood_and_tags(
    conn: sqlite3.Connection, entry_id: int, mood: float | None, tags: list[str]
) -> None:
    with conn:
        conn.execute("UPDATE entries SET mood_score = ? WHERE id = ?", (mood, entry_id))
        conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
        _link_tags(conn, entry_id, tags)


def replace_entry_metrics(
    conn: sqlite3.Connection, entry_id: int, metrics: dict[str, float]
) -> None:
    with conn:
        conn.execute("DELETE FROM entry_metrics WHERE entry_id = ?", (entry_id,))
        conn.executemany(
            "INSERT INTO entry_metrics(entry_id, key, value) VALUES (?, ?, ?)",
            [(entry_id, key, value) for key, value in metrics.items()],
        )


def metrics_for_entries(
    conn: sqlite3.Connection, entry_ids: list[int]
) -> dict[int, dict[str, float]]:
    if not entry_ids:
        return {}
    out: dict[int, dict[str, float]] = defaultdict(dict)
    try:
        rows = conn.execute(
            f"""SELECT entry_id, key, value FROM entry_metrics
                WHERE entry_id IN ({_JSON_VALUES}) ORDER BY entry_id, key""",
            (_json_list(entry_ids),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # a read-only connection to a cache from before the table
    for r in rows:
        out[r["entry_id"]][r["key"]] = r["value"]
    return dict(out)


def metric_keys(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """SELECT m.key, COUNT(*) AS n, MIN(m.value) AS lo, MAX(m.value) AS hi,
                          ROUND(AVG(m.value), 2) AS avg,
                          MIN(e.entry_date) AS first, MAX(e.entry_date) AS last
                   FROM entry_metrics m JOIN entries e ON e.id = m.entry_id
                   GROUP BY m.key ORDER BY n DESC, m.key"""
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def metric_averages(
    conn: sqlite3.Connection, date_from: date | None, date_to: date | None
) -> dict[str, tuple[float, int]]:
    """Each metric's average and how many entries record it, in a range."""
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        rows = conn.execute(
            f"""SELECT m.key, AVG(m.value) AS avg, COUNT(*) AS n
                FROM entry_metrics m JOIN entries e ON e.id = m.entry_id
                {where} GROUP BY m.key ORDER BY m.key""",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["key"]: (round(r["avg"], 2), r["n"]) for r in rows}


def monthly_metric(
    conn: sqlite3.Connection,
    key: str,
    date_from: date | None,
    date_to: date | None,
    only: EntryFilter | None = None,
) -> list[sqlite3.Row]:
    clauses, params = _filter_clauses(date_from, date_to, only, alias="e.")
    where = " AND ".join(["m.key = ?", *clauses])
    return list(
        conn.execute(
            f"""SELECT substr(e.entry_date, 1, 7) AS month,
                       ROUND(AVG(m.value), 2) AS avg, COUNT(*) AS n
                FROM entry_metrics m JOIN entries e ON e.id = m.entry_id
                WHERE {where}
                GROUP BY month ORDER BY month""",
            [key, *params],
        ).fetchall()
    )


def metric_is_yes_no(conn: sqlite3.Connection, key: str) -> bool:
    """Whether every value is 0 or 1, as a true/false property is stored."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(value NOT IN (0, 1)) AS other FROM entry_metrics WHERE key = ?", (key,)
    ).fetchone()
    return bool(row["n"]) and not row["other"]


def metric_days(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    """Each day's value, averaged over its entries when it has several."""
    return list(
        conn.execute(
            """SELECT e.entry_date AS date, AVG(m.value) AS value, MIN(e.id) AS id
               FROM entry_metrics m JOIN entries e ON e.id = m.entry_id
               WHERE m.key = ? GROUP BY e.entry_date ORDER BY e.entry_date""",
            (key,),
        ).fetchall()
    )


def entries_by_metric(conn: sqlite3.Connection, key: str, limit: int, highest: bool) -> list[sqlite3.Row]:
    """Entries with the highest or lowest values, with the value as `value`."""
    direction = "DESC" if highest else "ASC"
    return list(
        conn.execute(
            f"""SELECT e.*, m.value AS value
                FROM entry_metrics m JOIN entries e ON e.id = m.entry_id
                WHERE m.key = ? ORDER BY m.value {direction}, e.entry_date LIMIT ?""",
            (key, limit),
        ).fetchall()
    )


def monthly_mood_with_metric(conn: sqlite3.Connection, key: str) -> list[sqlite3.Row]:
    """Mood by month over just the entries that record the metric, shaped like monthly_mood."""
    return list(
        conn.execute(
            """SELECT substr(e.entry_date, 1, 7) AS month,
                      ROUND(AVG(e.mood_score), 2) AS avg_mood, COUNT(*) AS entry_count
               FROM entry_metrics m JOIN entries e ON e.id = m.entry_id
               WHERE m.key = ? GROUP BY month ORDER BY month""",
            (key,),
        ).fetchall()
    )


def metric_mood_pairs(
    conn: sqlite3.Connection, key: str, date_from: date | None, date_to: date | None
) -> list[tuple[float, float]]:
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    where = " AND ".join(["m.key = ?", "e.mood_score IS NOT NULL", *clauses])
    return [
        (r["value"], r["mood_score"])
        for r in conn.execute(
            f"""SELECT m.value, e.mood_score
                FROM entry_metrics m JOIN entries e ON e.id = m.entry_id
                WHERE {where}""",
            [key, *params],
        )
    ]


def prune_unused_tags(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            "DELETE FROM tags WHERE id NOT IN (SELECT DISTINCT tag_id FROM entry_tags)"
        )


def _delete_section_vectors(conn: sqlite3.Connection, entry_id: int) -> None:
    if not _has_table(conn, "sections_vec"):
        return
    conn.execute(
        "DELETE FROM sections_vec WHERE section_id IN "
        "(SELECT id FROM entry_sections WHERE entry_id = ?)",
        (entry_id,),
    )


def replace_entry_sections(
    conn: sqlite3.Connection,
    entry_id: int,
    sections: Sequence[Section],
    vectors: Sequence[list[float] | None] = (),
) -> None:
    """`vectors` go with `sections` in order; a missing one leaves that part to keywords."""
    with conn:
        _delete_section_vectors(conn, entry_id)
        conn.execute("DELETE FROM entry_sections WHERE entry_id = ?", (entry_id,))
        for i, section in enumerate(sections):
            cur = conn.execute(
                """INSERT INTO entry_sections(entry_id, heading, start_line, end_line, text)
                   VALUES (?, ?, ?, ?, ?)""",
                (entry_id, section.heading, section.start, section.end, section.text),
            )
            vector = vectors[i] if i < len(vectors) else None
            if vector is not None:
                assert cur.lastrowid is not None
                conn.execute(
                    "INSERT INTO sections_vec(section_id, embedding) VALUES (?, ?)",
                    (cur.lastrowid, sqlite_vec.serialize_float32(vector)),
                )


def sections_for_entries(
    conn: sqlite3.Connection, entry_ids: list[int]
) -> dict[int, list[sqlite3.Row]]:
    if not entry_ids:
        return {}
    try:
        rows = conn.execute(
            f"""SELECT * FROM entry_sections WHERE entry_id IN ({_JSON_VALUES})
                ORDER BY entry_id, start_line""",
            (_json_list(entry_ids),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # a read-only connection to a cache from before the table
    out: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        out[r["entry_id"]].append(r)
    return dict(out)


def section_vec_search(
    conn: sqlite3.Connection, query_vector: list[float], allowed_ids: list[int] | None, k: int
) -> list[sqlite3.Row]:
    """The parts closest in meaning, best first, with the entry each belongs to."""
    try:
        sql = "SELECT section_id, distance FROM sections_vec WHERE embedding MATCH ?"
        params: list[object] = [sqlite_vec.serialize_float32(query_vector)]
        if allowed_ids is not None:
            ids = [
                r[0]
                for r in conn.execute(
                    f"SELECT id FROM entry_sections WHERE entry_id IN ({_JSON_VALUES})",
                    (_json_list(allowed_ids),),
                )
            ]
            if not ids:
                return []
            sql += f" AND section_id IN ({_JSON_VALUES})"
            params.append(_json_list(ids))
        hits = conn.execute(f"{sql} ORDER BY distance LIMIT ?", [*params, k]).fetchall()
    except sqlite3.OperationalError:
        return []  # sections_vec is created by the first index run
    return _sections_in_order(conn, [r["section_id"] for r in hits])


def sections_fts_search(
    conn: sqlite3.Connection, terms: list[str], allowed_ids: list[int] | None, k: int
) -> list[sqlite3.Row]:
    match = _match_expression(terms)
    if match is None:
        return []
    sql = """SELECT f.rowid AS id FROM sections_fts f
             JOIN entry_sections s ON s.id = f.rowid
             WHERE sections_fts MATCH ?"""
    params: list[object] = [match]
    if allowed_ids is not None:
        sql += f" AND s.entry_id IN ({_JSON_VALUES})"
        params.append(_json_list(allowed_ids))
    try:
        rows = conn.execute(f"{sql} ORDER BY rank LIMIT ?", [*params, k]).fetchall()
    except sqlite3.OperationalError:
        return []
    return _sections_in_order(conn, [r["id"] for r in rows])


def save_entry_photos(conn: sqlite3.Connection, entry_id: int, photos: Sequence[Photo]) -> None:
    with conn:
        conn.execute("DELETE FROM entry_photos WHERE entry_id = ?", (entry_id,))
        conn.executemany(
            "INSERT INTO entry_photos(entry_id, name, path, description) VALUES (?, ?, ?, ?)",
            [(entry_id, p.name, p.path, p.description) for p in photos],
        )


def photos_for_entries(conn: sqlite3.Connection, entry_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
    if not entry_ids:
        return {}
    try:
        rows = conn.execute(
            f"""SELECT * FROM entry_photos WHERE entry_id IN ({_JSON_VALUES})
                ORDER BY entry_id, id""",
            (_json_list(entry_ids),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # a read-only connection to a cache from before the table
    out: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        out[r["entry_id"]].append(r)
    return dict(out)


def photos_fts_search(
    conn: sqlite3.Connection, terms: list[str], allowed_ids: list[int] | None, k: int
) -> list[int]:
    """Entries whose photos' descriptions match, best first."""
    match = _match_expression(terms)
    if match is None:
        return []
    sql = """SELECT p.entry_id FROM photos_fts f JOIN entry_photos p ON p.id = f.rowid
             WHERE photos_fts MATCH ?"""
    params: list[object] = [match]
    if allowed_ids is not None:
        sql += f" AND p.entry_id IN ({_JSON_VALUES})"
        params.append(_json_list(allowed_ids))
    try:
        rows = conn.execute(f"{sql} ORDER BY rank LIMIT ?", [*params, k]).fetchall()
    except sqlite3.OperationalError:
        return []
    return list(dict.fromkeys(r["entry_id"] for r in rows))


def _sections_in_order(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    by_id = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT * FROM entry_sections WHERE id IN ({_JSON_VALUES})", (_json_list(ids),)
        )
    }
    return [by_id[i] for i in ids if i in by_id]


def save_entry_vector(conn: sqlite3.Connection, entry_id: int, vector: list[float]) -> None:
    _save_vector(conn, "entries_vec", "entry_id", entry_id, vector)


def save_note_vector(conn: sqlite3.Connection, note_id: int, vector: list[float]) -> None:
    _save_vector(conn, "notes_vec", "note_id", note_id, vector)


def _save_vector(
    conn: sqlite3.Connection, table: str, id_column: str, row_id: int, vector: list[float]
) -> None:
    # vec0 has no ON CONFLICT
    with conn:
        conn.execute(f"DELETE FROM {table} WHERE {id_column} = ?", (row_id,))
        conn.execute(
            f"INSERT INTO {table}({id_column}, embedding) VALUES (?, ?)",
            (row_id, sqlite_vec.serialize_float32(vector)),
        )


def _link_tags(conn: sqlite3.Connection, entry_id: int, tags: list[str]) -> None:
    if not tags:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO tags(name) VALUES (?)", [(t,) for t in tags]
    )
    ids = [
        r["id"]
        for r in conn.execute(
            f"SELECT id FROM tags WHERE name IN ({_JSON_VALUES})", (_json_list(tags),)
        )
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO entry_tags(entry_id, tag_id) VALUES (?, ?)",
        [(entry_id, tag_id) for tag_id in ids],
    )


def save_note(
    conn: sqlite3.Connection,
    note: ParsedNote,
    summary: str | None,
    model_used: str | None,
) -> int:
    with conn:
        cur = conn.execute(
            """INSERT INTO notes
               (file_path, title, link_key, kind, aliases, properties, raw_text,
                word_count, summary, model_used, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                note.source_path,
                note.title,
                note.title.casefold(),
                note.kind,
                json.dumps(note.aliases, ensure_ascii=False),
                json.dumps(note.properties, ensure_ascii=False),
                note.raw_text,
                len(note.raw_text.split()),
                summary,
                model_used,
                _now(),
            ),
        )
        note_id = cur.lastrowid
        assert note_id is not None
        names: dict[str, str] = {}
        for name in (note.title, *note.aliases):
            names.setdefault(name.casefold(), name)
        conn.executemany(
            "INSERT OR IGNORE INTO note_names(note_id, name, key) VALUES (?, ?, ?)",
            [(note_id, name, key) for key, name in names.items()],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO note_links(note_id, target) VALUES (?, ?)",
            [(note_id, target) for target in note.links],
        )
        _save_property_values(conn, note_id, note)
    return note_id


def save_note_property_values(conn: sqlite3.Connection, note_id: int, note: ParsedNote) -> None:
    with conn:
        _save_property_values(conn, note_id, note)


def _save_property_values(conn: sqlite3.Connection, note_id: int, note: ParsedNote) -> None:
    conn.execute("DELETE FROM note_property_values WHERE note_id = ?", (note_id,))
    conn.executemany(
        """INSERT OR IGNORE INTO note_property_values(note_id, key, name, value, value_key)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (note_id, name.strip().casefold(), name.strip(), value, " ".join(value.casefold().split()))
            for name, values in note.property_values.items()
            for value in values
        ],
    )


def property_keys(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Each property notes have, with how many notes and different values."""
    try:
        return list(
            conn.execute(
                """SELECT key, MIN(name) AS name, COUNT(DISTINCT note_id) AS notes,
                          COUNT(DISTINCT value_key) AS "values"
                   FROM note_property_values GROUP BY key ORDER BY notes DESC, key"""
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []  # a read-only connection to a cache from before the table


def property_values_for(conn: sqlite3.Connection, note_id: int) -> list[sqlite3.Row]:
    """A note's property values, each with how many notes share it."""
    try:
        return list(
            conn.execute(
                """SELECT v.key, v.name, v.value,
                          (SELECT COUNT(DISTINCT o.note_id) FROM note_property_values o
                           WHERE o.key = v.key AND o.value_key = v.value_key) AS notes
                   FROM note_property_values v WHERE v.note_id = ?
                   ORDER BY v.rowid""",
                (note_id,),
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def group_note_ids(conn: sqlite3.Connection, key: str, value: str) -> list[int]:
    """The notes whose property `key` has `value`, both compared ignoring case."""
    try:
        rows = conn.execute(
            """SELECT DISTINCT note_id FROM note_property_values
               WHERE key = ? AND value_key = ? ORDER BY note_id""",
            (key.strip().casefold(), " ".join(value.casefold().split())),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r["note_id"] for r in rows]


def property_groups(
    conn: sqlite3.Connection,
    key: str,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[sqlite3.Row]:
    """The values of one property, each with its notes and the entries that link them.

    An entry that links two notes in a group counts once for it.
    """
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    on = "".join(f" AND {c}" for c in clauses)
    key = key.strip().casefold()
    try:
        return list(
            conn.execute(
                f"""WITH members AS (
                      SELECT DISTINCT v.value_key, ee.entry_id
                      FROM note_property_values v
                      JOIN entry_entities ee ON ee.entity_id = v.note_id
                      WHERE v.key = ?)
                    SELECT g.value, g.notes, COUNT(e.id) AS entries,
                           ROUND(AVG(e.mood_score), 2) AS avg_mood,
                           MIN(e.entry_date) AS first, MAX(e.entry_date) AS last
                    FROM (SELECT value_key, MIN(value) AS value, COUNT(DISTINCT note_id) AS notes
                          FROM note_property_values WHERE key = ? GROUP BY value_key) g
                    LEFT JOIN members m ON m.value_key = g.value_key
                    LEFT JOIN entries e ON e.id = m.entry_id{on}
                    GROUP BY g.value_key
                    ORDER BY entries DESC, g.notes DESC, g.value COLLATE NOCASE""",
                [key, key, *params],
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def group_mood_stats(
    conn: sqlite3.Connection,
    date_from: date | None,
    date_to: date | None,
    min_count: int,
    keys: Sequence[str] = (),
) -> list[sqlite3.Row]:
    """Mood on the days that link any note of a group, for groups of two notes or more.

    `keys` limits it to those properties; empty means all of them.
    """
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    wanted = [k.strip().casefold() for k in keys if k.strip()]
    if wanted:
        clauses.append(f"v.key IN ({_JSON_VALUES})")
        params.append(_json_list(wanted))
    where = "".join(f" AND {c}" for c in clauses)
    return list(
        conn.execute(
            f"""WITH grouped AS (
                  SELECT key, value_key FROM note_property_values
                  GROUP BY key, value_key HAVING COUNT(DISTINCT note_id) >= 2),
                members AS (
                  SELECT DISTINCT v.key, v.value_key, ee.entry_id
                  FROM note_property_values v
                  JOIN grouped g ON g.key = v.key AND g.value_key = v.value_key
                  JOIN entry_entities ee ON ee.entity_id = v.note_id
                  JOIN entries e ON e.id = ee.entry_id
                  WHERE e.mood_score IS NOT NULL{where})
                SELECT m.key,
                       (SELECT MIN(name) FROM note_property_values n WHERE n.key = m.key) AS name,
                       (SELECT MIN(value) FROM note_property_values n
                        WHERE n.key = m.key AND n.value_key = m.value_key) AS value,
                       COUNT(*) AS n, ROUND(AVG(e.mood_score), 2) AS avg_mood
                FROM members m JOIN entries e ON e.id = m.entry_id
                GROUP BY m.key, m.value_key
                HAVING COUNT(*) >= ?
                ORDER BY avg_mood""",
            [*params, min_count],
        ).fetchall()
    )


def note_from_row(row: sqlite3.Row, vault: Path | None = None) -> ParsedNote:
    # without a vault, rel_path (and the File: header line) stays empty
    return ParsedNote(
        title=row["title"],
        kind=row["kind"],
        raw_text=row["raw_text"],
        source_path=row["file_path"],
        aliases=json.loads(row["aliases"]),
        properties=json.loads(row["properties"]),
        rel_path=relative_path(row["file_path"], vault) if vault is not None else "",
    )


def notes_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """Rows come back in the order of `ids`."""
    if not ids:
        return []
    by_id = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT * FROM notes WHERE id IN ({_JSON_VALUES})", (_json_list(ids),)
        )
    }
    return [by_id[i] for i in ids if i in by_id]


def _names_table(conn: sqlite3.Connection) -> str:
    # known_names adds the names learned from link text; a read-only connection
    # to a cache from before it only has titles and aliases
    return "known_names" if _has_table(conn, "note_learned_names") else "note_names"


def note_ids_matching(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    """Substring match on titles, aliases and names learned from link text."""
    terms = [n.strip().casefold() for n in names if n.strip()]
    if not terms:
        return []
    clause = " OR ".join("key LIKE ? ESCAPE '\\'" for _ in terms)
    return [
        r["note_id"]
        for r in conn.execute(
            f"SELECT DISTINCT note_id FROM {_names_table(conn)} WHERE {clause} ORDER BY note_id",
            [_like_contains(t) for t in terms],
        )
    ]


def find_notes(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """Exact title, else exact alias or learned name, else substring matches."""
    key = name.strip().casefold()
    if not key:
        return []
    ids = [r["id"] for r in conn.execute("SELECT id FROM notes WHERE link_key = ?", (key,))]
    if not ids:
        ids = [
            r["note_id"]
            for r in conn.execute(
                f"SELECT DISTINCT note_id FROM {_names_table(conn)} WHERE key = ?", (key,)
            )
        ]
    rows = notes_by_ids(conn, ids or note_ids_matching(conn, [name]))
    return sorted(rows, key=lambda r: r["title"].casefold())


def notes_for_targets(conn: sqlite3.Connection, targets: list[str]) -> dict[str, sqlite3.Row]:
    """The note each link key resolves to, by file name or a unique alias."""
    if not targets:
        return {}
    rows = conn.execute(
        f"""SELECT lt.target AS link_target, n.* FROM link_targets lt
            JOIN notes n ON n.id = lt.note_id
            WHERE lt.target IN ({_JSON_VALUES})
            ORDER BY n.id""",
        (_json_list(targets),),
    ).fetchall()
    found: dict[str, sqlite3.Row] = {}
    for r in rows:
        found.setdefault(r["link_target"], r)
    return found


def notes_by_link_key(conn: sqlite3.Connection, link_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM notes WHERE link_key = ? ORDER BY id LIMIT 1", (link_key,)
    ).fetchone()


def note_exists(conn: sqlite3.Connection, path: str) -> bool:
    return conn.execute("SELECT 1 FROM notes WHERE file_path = ?", (path,)).fetchone() is not None


def note_name_index(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(f"SELECT note_id, name, key FROM {_names_table(conn)}").fetchall())


def notes_by_kind(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT kind, COUNT(*) AS n FROM notes GROUP BY kind ORDER BY n DESC, kind"
        ).fetchall()
    )


@dataclass(frozen=True)
class Mentions:
    count: int
    first: str
    last: str


def mention_stats(
    conn: sqlite3.Connection,
    note_ids: list[int],
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict[int, Mentions]:
    if not note_ids:
        return {}
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    where = " AND ".join([f"ee.entity_id IN ({_JSON_VALUES})", *clauses])
    rows = conn.execute(
        f"""SELECT ee.entity_id AS note_id, COUNT(*) AS n,
                   MIN(e.entry_date) AS first, MAX(e.entry_date) AS last
            FROM entry_entities ee
            JOIN entries e ON e.id = ee.entry_id
            WHERE {where}
            GROUP BY ee.entity_id""",
        [_json_list(note_ids), *params],
    )
    return {r["note_id"]: Mentions(r["n"], r["first"], r["last"]) for r in rows}


def mentions_by_year(
    conn: sqlite3.Connection,
    note_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[sqlite3.Row]:
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    where = " AND ".join(["ee.entity_id = ?", *clauses])
    return list(
        conn.execute(
            f"""SELECT substr(e.entry_date, 1, 4) AS year, COUNT(*) AS n
                FROM entry_entities ee
                JOIN entries e ON e.id = ee.entry_id
                WHERE {where}
                GROUP BY year ORDER BY year""",
            [note_id, *params],
        ).fetchall()
    )


def entry_ids_mentioning(conn: sqlite3.Connection, note_ids: list[int]) -> list[int]:
    if not note_ids:
        return []
    return [
        r["id"]
        for r in conn.execute(
            f"""SELECT DISTINCT e.id, e.entry_date
                FROM entries e
                JOIN entry_entities ee ON ee.entry_id = e.id
                WHERE ee.entity_id IN ({_JSON_VALUES})
                ORDER BY e.entry_date DESC, e.id DESC""",
            (_json_list(note_ids),),
        )
    ]


def entries_mentioning(
    conn: sqlite3.Connection, note_id: int, limit: int
) -> list[sqlite3.Row]:
    """Newest first. A negative limit means all of them."""
    return list(
        conn.execute(
            """SELECT e.* FROM entries e
               JOIN entry_entities ee ON ee.entry_id = e.id
               WHERE ee.entity_id = ?
               ORDER BY e.entry_date DESC, e.id DESC
               LIMIT ?""",
            (note_id, limit),
        ).fetchall()
    )


def notes_linked_from_entries(
    conn: sqlite3.Connection, entry_ids: list[int], limit: int
) -> list[sqlite3.Row]:
    if not entry_ids:
        return []
    return list(
        conn.execute(
            f"""SELECT entity_id AS note_id, COUNT(*) AS n
                FROM entry_entities
                WHERE entry_id IN ({_JSON_VALUES})
                GROUP BY entity_id
                ORDER BY n DESC, note_id
                LIMIT ?""",
            (_json_list(entry_ids), limit),
        ).fetchall()
    )


def months_before(day: date, months: int) -> date:
    """The same day `months` months earlier, or the month's last day if it's shorter."""
    year, month_index = divmod(day.year * 12 + day.month - 1 - months, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def months_between(start: date, end: date) -> int:
    """Whole months from start to end."""
    return (end.year - start.year) * 12 + end.month - start.month - (end.day < start.day)


@dataclass(frozen=True)
class Lapsed:
    note_id: int
    title: str
    kind: str
    count: int
    first: str
    last: str
    busiest_year: str
    busiest_count: int


def lapsed_notes(
    conn: sqlite3.Connection, min_links: int, quiet_since: date, kind: str | None = None
) -> list[Lapsed]:
    """Notes linked at least min_links times, but not on or after quiet_since."""
    clauses = ["1 = 1"]
    params: list[object] = []
    if kind:
        clauses.append("n.kind = ?")
        params.append(kind.casefold())
    rows = conn.execute(
        f"""SELECT n.id, n.title, n.kind, COUNT(*) AS n_links,
                   MIN(e.entry_date) AS first, MAX(e.entry_date) AS last
            FROM notes n
            JOIN entry_entities ee ON ee.entity_id = n.id
            JOIN entries e ON e.id = ee.entry_id
            WHERE {" AND ".join(clauses)}
            GROUP BY n.id
            HAVING COUNT(*) >= ? AND MAX(e.entry_date) < ?
            ORDER BY n_links DESC, n.title""",
        [*params, min_links, quiet_since.isoformat()],
    ).fetchall()
    out = []
    for r in rows:
        years = mentions_by_year(conn, r["id"])
        busiest = max(years, key=lambda y: (y["n"], y["year"]))
        out.append(
            Lapsed(r["id"], r["title"], r["kind"], r["n_links"], r["first"], r["last"],
                   busiest["year"], busiest["n"])
        )
    return out


def notes_linked_with(
    conn: sqlite3.Connection,
    note_id: int,
    limit: int,
    date_from: date | None = None,
    date_to: date | None = None,
    kind: str | None = None,
) -> list[sqlite3.Row]:
    """Notes linked from the same entries as this one, and how many entries they share."""
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    if kind:
        clauses.append("n.kind = ?")
        params.append(kind.casefold())
    where = "".join(f" AND {c}" for c in clauses)
    return list(
        conn.execute(
            f"""SELECT n.id, n.title, n.kind, COUNT(*) AS n
                FROM entry_entities a
                JOIN entry_entities b ON b.entry_id = a.entry_id AND b.entity_id != a.entity_id
                JOIN notes n ON n.id = b.entity_id
                JOIN entries e ON e.id = a.entry_id
                WHERE a.entity_id = ?{where}
                GROUP BY n.id
                ORDER BY n DESC, n.title COLLATE NOCASE
                LIMIT ?""",
            [note_id, *params, limit],
        ).fetchall()
    )


def note_graph(
    conn: sqlite3.Connection,
    limit: int,
    min_shared: int,
    date_from: date | None = None,
    date_to: date | None = None,
    kind: str | None = None,
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """The `limit` notes linked from the most entries, and the pairs of them
    linked from at least `min_shared` of the same entries, as (nodes, edges).
    Nodes have id, title, kind and n; edges a, b (note ids, a < b) and n."""
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    if kind:
        clauses.append("n.kind = ?")
        params.append(kind.casefold())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    # one pass over the links in range, shared by both queries
    linked = f"""SELECT DISTINCT ee.entry_id, ee.entity_id FROM entry_entities ee
                 JOIN entries e ON e.id = ee.entry_id JOIN notes n ON n.id = ee.entity_id {where}"""
    nodes = conn.execute(
        f"""WITH linked AS ({linked})
            SELECT n.id, n.title, n.kind, COUNT(*) AS n
            FROM linked l JOIN notes n ON n.id = l.entity_id
            GROUP BY n.id ORDER BY n DESC, n.title COLLATE NOCASE LIMIT ?""",
        [*params, limit],
    ).fetchall()
    ids = _json_list([r["id"] for r in nodes])
    edges = conn.execute(
        f"""WITH linked AS ({linked}),
                 shown AS (SELECT * FROM linked WHERE entity_id IN ({_JSON_VALUES}))
            SELECT a.entity_id AS a, b.entity_id AS b, COUNT(*) AS n
            FROM shown a JOIN shown b ON b.entry_id = a.entry_id AND b.entity_id > a.entity_id
            GROUP BY a.entity_id, b.entity_id HAVING COUNT(*) >= ?
            ORDER BY n DESC, a, b""",
        [*params, ids, min_shared],
    ).fetchall()
    return list(nodes), list(edges)


def note_outlinks(conn: sqlite3.Connection, note_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT DISTINCT n.* FROM note_links nl
               JOIN link_targets lt ON lt.target = nl.target
               JOIN notes n ON n.id = lt.note_id
               WHERE nl.note_id = ? AND n.id != nl.note_id
               ORDER BY n.title""",
            (note_id,),
        ).fetchall()
    )


def note_backlinks(conn: sqlite3.Connection, note_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT DISTINCT src.* FROM notes dst
               JOIN link_targets lt ON lt.note_id = dst.id
               JOIN note_links nl ON nl.target = lt.target
               JOIN notes src ON src.id = nl.note_id
               WHERE dst.id = ? AND src.id != dst.id
               ORDER BY src.title""",
            (note_id,),
        ).fetchall()
    )


def mark_rollups_stale(conn: sqlite3.Connection, d: date) -> None:
    keys = period_keys_for_date(d)
    with conn:
        for period_type, period_key in keys.items():
            conn.execute(
                "UPDATE rollups SET stale = 1 "
                "WHERE period_type = ? AND period_key = ?",
                (period_type, period_key),
            )
        day = d.isoformat()
        conn.execute(
            "UPDATE rollups SET stale = 1 WHERE period_type = 'range' "
            "AND substr(period_key, 1, 10) <= ? AND substr(period_key, 13, 10) >= ?",
            (day, day),
        )


def range_rollups_overlapping(
    conn: sqlite3.Connection, date_from: date | None = None, date_to: date | None = None
) -> list[sqlite3.Row]:
    """Digests of spans of days that share at least one day with this range, by when they start."""
    # keys are ISO dates, so they compare as strings
    return list(
        conn.execute(
            "SELECT * FROM rollups WHERE period_type = 'range' "
            "AND (? IS NULL OR substr(period_key, 13, 10) >= ?) "
            "AND (? IS NULL OR substr(period_key, 1, 10) <= ?) ORDER BY period_key",
            (
                date_from and date_from.isoformat(), date_from and date_from.isoformat(),
                date_to and date_to.isoformat(), date_to and date_to.isoformat(),
            ),
        ).fetchall()
    )


def upsert_rollup(
    conn: sqlite3.Connection,
    period_type: str,
    period_key: str,
    summary: str,
    dominant_tags_json: str,
    avg_mood: float | None,
    entry_count: int,
) -> None:
    with conn:
        conn.execute(
            """INSERT INTO rollups
               (period_type, period_key, summary, dominant_tags, avg_mood,
                entry_count, stale, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(period_type, period_key) DO UPDATE SET
                 summary = excluded.summary,
                 dominant_tags = excluded.dominant_tags,
                 avg_mood = excluded.avg_mood,
                 entry_count = excluded.entry_count,
                 stale = 0,
                 generated_at = excluded.generated_at""",
            (period_type, period_key, summary, dominant_tags_json,
             avg_mood, entry_count, _now()),
        )


def link_targets_for_file(conn: sqlite3.Connection, path: str) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT el.target FROM entry_links el
           JOIN entries e ON e.id = el.entry_id
           WHERE e.file_path = ?""",
        (path,),
    )
    keys = [r["target"] for r in rows]
    note = conn.execute("SELECT link_key FROM notes WHERE file_path = ?", (path,)).fetchone()
    return keys + ([note["link_key"]] if note else []) + note_keys_linking_file_days(conn, path)


def note_keys_linking_file_days(conn: sqlite3.Connection, path: str) -> list[str]:
    """The notes that link the days of this file's entries, whose digests read those days."""
    by_file = (
        " OR nl.target IN (SELECT key FROM entry_file_keys WHERE path = :path)"
        if _has_table(conn, "entry_file_keys") else ""
    )
    return [
        r["link_key"]
        for r in conn.execute(
            f"""SELECT DISTINCT n.link_key FROM notes n JOIN note_links nl ON nl.note_id = n.id
                WHERE nl.target IN (SELECT entry_date FROM entries WHERE file_path = :path){by_file}""",
            {"path": path},
        )
    ]


def link_targets_for_entries(conn: sqlite3.Connection, entry_ids: list[int]) -> list[str]:
    if not entry_ids:
        return []
    return [
        r["target"]
        for r in conn.execute(
            f"SELECT DISTINCT target FROM entry_links WHERE entry_id IN ({_JSON_VALUES})",
            (_json_list(entry_ids),),
        )
    ]


def mark_note_digests_stale(conn: sqlite3.Connection, link_keys: Sequence[str]) -> None:
    """Link keys as written, so an alias marks its note's digest too."""
    if not link_keys:
        return
    keys = _json_list(list(dict.fromkeys(link_keys)))
    with conn:
        conn.execute(
            f"""UPDATE note_digests SET stale = 1
                WHERE link_key IN ({_JSON_VALUES})
                   OR link_key IN (SELECT n.link_key FROM link_targets lt
                                   JOIN notes n ON n.id = lt.note_id
                                   WHERE lt.target IN ({_JSON_VALUES}))""",
            (keys, keys),
        )


def notes_linked_at_least(conn: sqlite3.Connection, minimum: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT n.*, COUNT(*) AS n_entries
               FROM notes n
               JOIN entry_entities ee ON ee.entity_id = n.id
               GROUP BY n.id
               HAVING COUNT(*) >= ?
               ORDER BY n_entries DESC, n.title""",
            (minimum,),
        ).fetchall()
    )


def upsert_note_digest(
    conn: sqlite3.Connection,
    link_key: str,
    summary: str,
    mentions: Mentions,
    avg_mood: float | None,
) -> None:
    with conn:
        conn.execute(
            """INSERT INTO note_digests
               (link_key, summary, entry_count, avg_mood, first_date, last_date,
                stale, generated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(link_key) DO UPDATE SET
                 summary = excluded.summary,
                 entry_count = excluded.entry_count,
                 avg_mood = excluded.avg_mood,
                 first_date = excluded.first_date,
                 last_date = excluded.last_date,
                 stale = 0,
                 generated_at = excluded.generated_at""",
            (link_key, summary, mentions.count, avg_mood, mentions.first, mentions.last, _now()),
        )


def get_note_digest(conn: sqlite3.Connection, link_key: str) -> sqlite3.Row | None:
    try:
        return conn.execute(
            "SELECT * FROM note_digests WHERE link_key = ?", (link_key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # a read-only connection to a cache from before the table


def note_digests_for(conn: sqlite3.Connection, link_keys: list[str]) -> dict[str, sqlite3.Row]:
    if not link_keys:
        return {}
    try:
        rows = conn.execute(
            f"SELECT * FROM note_digests WHERE link_key IN ({_JSON_VALUES})",
            (_json_list(link_keys),),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["link_key"]: r for r in rows}


def all_note_digests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return list(
            conn.execute(
                """SELECT d.*, n.id AS note_id, n.title, n.kind
                   FROM note_digests d JOIN notes n ON n.link_key = d.link_key
                   ORDER BY d.entry_count DESC, n.title"""
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


def prune_note_digests(conn: sqlite3.Connection, keep: set[str]) -> int:
    doomed = [
        r["link_key"]
        for r in conn.execute("SELECT link_key FROM note_digests")
        if r["link_key"] not in keep
    ]
    if doomed:
        with conn:
            conn.execute(
                f"DELETE FROM note_digests WHERE link_key IN ({_JSON_VALUES})",
                (_json_list(doomed),),
            )
    return len(doomed)


def profile_scanned_hashes(conn: sqlite3.Connection) -> set[str]:
    try:
        return {r["text_hash"] for r in conn.execute("SELECT text_hash FROM profile_scans")}
    except sqlite3.OperationalError:
        return set()  # a read-only connection to a cache from before the table


def save_profile_scan(
    conn: sqlite3.Connection,
    hashes: list[str],
    facts: list[tuple[str, str, str, str]],
) -> None:
    """Entries read for the profile, and the (hash, date, category, fact) found in them."""
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO profile_scans(text_hash, scanned_at) VALUES (?, ?)",
            [(h, _now()) for h in hashes],
        )
        conn.executemany(
            "INSERT INTO profile_facts(text_hash, entry_date, category, fact) VALUES (?, ?, ?, ?)",
            facts,
        )


def unfolded_profile_facts(
    conn: sqlite3.Connection, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM profile_facts WHERE folded = 0 ORDER BY entry_date, id"
    try:
        if limit is not None:
            return list(conn.execute(sql + " LIMIT ?", (limit,)).fetchall())
        return list(conn.execute(sql).fetchall())
    except sqlite3.OperationalError:
        return []


def mark_profile_facts_folded(conn: sqlite3.Connection, ids: list[int]) -> None:
    if ids:
        with conn:
            conn.execute(
                f"UPDATE profile_facts SET folded = 1 WHERE id IN ({_JSON_VALUES})",
                (_json_list(ids),),
            )


def unfold_profile_facts(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("UPDATE profile_facts SET folded = 0")


def forget_profile_scans(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM profile_facts")
        conn.execute("DELETE FROM profile_scans")


def prune_profile(conn: sqlite3.Connection, live_hashes: set[str]) -> int:
    live = _json_list(sorted(live_hashes))
    with conn:
        gone = conn.execute(
            f"DELETE FROM profile_facts WHERE text_hash NOT IN ({_JSON_VALUES})", (live,)
        ).rowcount
        conn.execute(f"DELETE FROM profile_scans WHERE text_hash NOT IN ({_JSON_VALUES})", (live,))
    return gone


def profile_fact_count(conn: sqlite3.Connection) -> int:
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM profile_facts").fetchone()["n"]
    except sqlite3.OperationalError:
        return 0


def profile_facts(
    conn: sqlite3.Connection,
    limit: int,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[sqlite3.Row]:
    """Newest first."""
    clauses, params = _range_clauses(date_from, date_to)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        return list(
            conn.execute(
                f"SELECT * FROM profile_facts {where} ORDER BY entry_date DESC, id DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        )
    except sqlite3.OperationalError:
        return []


_PERIOD_KEY_FN = {"week": week_key, "month": month_key, "year": year_key}


def get_rollups_in_range(
    conn: sqlite3.Connection,
    period_type: str,
    date_from: date | None,
    date_to: date | None,
) -> list[sqlite3.Row]:
    # keys are zero-padded, so plain string comparison orders them correctly
    key_fn = _PERIOD_KEY_FN[period_type]
    sql = "SELECT * FROM rollups WHERE period_type = ?"
    params: list[object] = [period_type]
    if date_from is not None:
        sql += " AND period_key >= ?"
        params.append(key_fn(date_from))
    if date_to is not None:
        sql += " AND period_key <= ?"
        params.append(key_fn(date_to))
    return list(conn.execute(sql + " ORDER BY period_key", params).fetchall())


@dataclass(frozen=True)
class EntryFilter:
    """Only entries with this tag, linking at least one of these notes, and
    linking at least one note of this group (a property and its value)."""

    tag: str | None = None
    note_ids: tuple[int, ...] = ()
    group: tuple[str, str] | None = None
    group_ids: tuple[int, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.tag or self.note_ids or self.group)


def _filter_clauses(
    date_from: date | None,
    date_to: date | None,
    only: EntryFilter | None,
    alias: str = "",
) -> tuple[list[str], list[object]]:
    clauses, params = _range_clauses(date_from, date_to, alias)
    if only and only.tag:
        clauses.append(
            f"{alias}id IN (SELECT et.entry_id FROM entry_tags et "
            "JOIN tags t ON t.id = et.tag_id WHERE t.name = ?)"
        )
        params.append(only.tag)
    if only and only.note_ids:
        clauses.append(
            f"{alias}id IN (SELECT entry_id FROM entry_entities "
            f"WHERE entity_id IN ({_JSON_VALUES}))"
        )
        params.append(_json_list(list(only.note_ids)))
    if only and only.group:
        # a group with no notes left matches nothing, rather than everything
        clauses.append(
            f"{alias}id IN (SELECT entry_id FROM entry_entities "
            f"WHERE entity_id IN ({_JSON_VALUES}))"
        )
        params.append(_json_list(list(only.group_ids)))
    return clauses, params


def _filter_where(
    date_from: date | None, date_to: date | None, only: EntryFilter | None, alias: str = ""
) -> tuple[str, list[object]]:
    clauses, params = _filter_clauses(date_from, date_to, only, alias)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def tag_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM tags t JOIN entry_tags et ON et.tag_id = t.id
           WHERE t.name = ? LIMIT 1""",
        (name,),
    ).fetchone() is not None


def filtered_entry_ids(
    conn: sqlite3.Connection,
    date_from: date | None = None,
    date_to: date | None = None,
    entity_names: list[str] | None = None,
    only: EntryFilter | None = None,
) -> list[int]:
    """Oldest first, so callers can slice off the end for the most recent.

    If the name filter matches nothing it's dropped instead of returning no
    candidates, since the router's entity names aren't always right. Notes
    named in `only` are what the person asked for, so they always apply, and
    the router's names are left out.
    """
    note_ids = (
        [] if only and (only.note_ids or only.group) else note_ids_matching(conn, entity_names or [])
    )
    clauses, params = _filter_clauses(date_from, date_to, only, alias="e.")
    if note_ids:
        clauses.append(
            "e.id IN (SELECT entry_id FROM entry_entities "
            f"WHERE entity_id IN ({_JSON_VALUES}))"
        )
        params.append(_json_list(note_ids))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    ids = [
        r["id"]
        for r in conn.execute(
            f"SELECT e.id FROM entries e {where} ORDER BY e.entry_date, e.id", params
        )
    ]
    if note_ids and not ids:
        return filtered_entry_ids(conn, date_from, date_to, None, only)
    return ids


def _match_expression(terms: list[str]) -> str | None:
    # A term that's only quote chars would become "" and break the whole MATCH.
    cleaned = [stripped for t in terms if (stripped := t.replace('"', "").strip())]
    return " OR ".join(f'"{t}"' for t in cleaned) if cleaned else None


def fts_search(
    conn: sqlite3.Connection,
    terms: list[str],
    allowed_ids: list[int] | None,
    k: int,
) -> list[int]:
    match = _match_expression(terms)
    if match is None:
        return []
    sql = "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?"
    params: list[object] = [match]
    if allowed_ids is not None:
        sql += f" AND rowid IN ({_JSON_VALUES})"
        params.append(_json_list(allowed_ids))
    try:
        rows = conn.execute(f"{sql} ORDER BY rank LIMIT ?", [*params, k]).fetchall()
    except sqlite3.OperationalError:
        return []  # bad MATCH syntax from model output
    return [r["rowid"] for r in rows]


def fts_phrase_ids(conn: sqlite3.Connection, phrase: str) -> list[int]:
    """Every entry whose text has these words in a row."""
    match = _match_expression([phrase])
    if match is None:
        return []
    try:
        rows = conn.execute(
            "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?", (match,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r["rowid"] for r in rows]


def notes_fts_search(conn: sqlite3.Connection, terms: list[str], k: int) -> list[int]:
    match = _match_expression(terms)
    if match is None:
        return []
    try:
        rows = conn.execute(
            "SELECT rowid FROM notes_fts WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, k),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r["rowid"] for r in rows]


def vec_search(
    conn: sqlite3.Connection,
    query_vector: list[float],
    allowed_ids: list[int] | None,
    k: int,
) -> list[int]:
    sql = "SELECT entry_id FROM entries_vec WHERE embedding MATCH ?"
    params: list[object] = [sqlite_vec.serialize_float32(query_vector)]
    if allowed_ids is not None:
        sql += f" AND entry_id IN ({_JSON_VALUES})"
        params.append(_json_list(allowed_ids))
    rows = conn.execute(f"{sql} ORDER BY distance LIMIT ?", [*params, k]).fetchall()
    return [r["entry_id"] for r in rows]


def notes_vec_search(
    conn: sqlite3.Connection, query_vector: list[float], k: int
) -> list[int]:
    try:
        rows = conn.execute(
            "SELECT note_id FROM notes_vec WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (sqlite_vec.serialize_float32(query_vector), k),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # notes_vec is created by the first index run
    return [r["note_id"] for r in rows]


def entries_by_ids(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    rows = conn.execute(
        f"SELECT * FROM entries WHERE id IN ({_JSON_VALUES}) ORDER BY entry_date, id",
        (_json_list(ids),),
    ).fetchall()
    return list(rows)


def entries_in_range(
    conn: sqlite3.Connection, date_from: date, date_to: date
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM entries WHERE entry_date >= ? AND entry_date <= ? "
            "ORDER BY entry_date",
            (date_from.isoformat(), date_to.isoformat()),
        ).fetchall()
    )


def entries_in_optional_range(
    conn: sqlite3.Connection,
    date_from: date | None,
    date_to: date | None,
    only: EntryFilter | None = None,
) -> list[sqlite3.Row]:
    where, params = _filter_where(date_from, date_to, only)
    return list(
        conn.execute(
            f"SELECT * FROM entries {where} ORDER BY entry_date, id", params
        ).fetchall()
    )


def tags_for_entry(conn: sqlite3.Connection, entry_id: int) -> list[str]:
    return [
        r["name"]
        for r in conn.execute(
            "SELECT t.name FROM tags t JOIN entry_tags et ON et.tag_id = t.id "
            "WHERE et.entry_id = ? ORDER BY t.name",
            (entry_id,),
        ).fetchall()
    ]


def entities_for_entry(conn: sqlite3.Connection, entry_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT en.id, en.name, en.type FROM entities en "
            "JOIN entry_entities ee ON ee.entity_id = en.id "
            "WHERE ee.entry_id = ? ORDER BY en.type, en.name",
            (entry_id,),
        ).fetchall()
    )


def tags_for_entries(
    conn: sqlite3.Connection, entry_ids: list[int]
) -> dict[int, list[str]]:
    if not entry_ids:
        return {}
    out: dict[int, list[str]] = defaultdict(list)
    for r in conn.execute(
        "SELECT et.entry_id AS eid, t.name FROM tags t "
        "JOIN entry_tags et ON et.tag_id = t.id "
        f"WHERE et.entry_id IN ({_JSON_VALUES}) ORDER BY et.entry_id, t.name",
        (_json_list(entry_ids),),
    ):
        out[r["eid"]].append(r["name"])
    return dict(out)


def entities_for_entries(
    conn: sqlite3.Connection, entry_ids: list[int]
) -> dict[int, list[sqlite3.Row]]:
    if not entry_ids:
        return {}
    out: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute(
        "SELECT ee.entry_id AS eid, en.id, en.name, en.type FROM entities en "
        "JOIN entry_entities ee ON ee.entity_id = en.id "
        f"WHERE ee.entry_id IN ({_JSON_VALUES}) "
        "ORDER BY ee.entry_id, en.type, en.name",
        (_json_list(entry_ids),),
    ):
        out[r["eid"]].append(r)
    return dict(out)


def monthly_mood(
    conn: sqlite3.Connection,
    date_from: date | None,
    date_to: date | None,
    only: EntryFilter | None = None,
) -> list[sqlite3.Row]:
    where, params = _filter_where(date_from, date_to, only)
    return list(
        conn.execute(
            f"""SELECT substr(entry_date, 1, 7) AS month,
                       ROUND(AVG(mood_score), 2) AS avg_mood,
                       COUNT(*) AS entry_count
                FROM entries {where}
                GROUP BY month ORDER BY month""",
            params,
        ).fetchall()
    )


def top_tags(
    conn: sqlite3.Connection,
    date_from: date | None,
    date_to: date | None,
    limit: int = 10,
    only: EntryFilter | None = None,
) -> list[sqlite3.Row]:
    where, params = _filter_where(date_from, date_to, only, alias="e.")
    return list(
        conn.execute(
            f"""SELECT t.name, COUNT(*) AS n
                FROM tags t
                JOIN entry_tags et ON et.tag_id = t.id
                JOIN entries e ON e.id = et.entry_id
                {where}
                GROUP BY t.name ORDER BY n DESC, t.name LIMIT ?""",
            [*params, limit],
        ).fetchall()
    )


def top_entities(
    conn: sqlite3.Connection,
    date_from: date | None,
    date_to: date | None,
    limit: int = 10,
    entity_type: str | None = None,
) -> list[sqlite3.Row]:
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    if entity_type:
        clauses.append("en.type = ?")
        params.append(entity_type.casefold())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return list(
        conn.execute(
            f"""SELECT en.name, en.type, COUNT(*) AS n, MIN(en.id) AS id
                FROM entities en
                JOIN entry_entities ee ON ee.entity_id = en.id
                JOIN entries e ON e.id = ee.entry_id
                {where}
                GROUP BY en.name, en.type
                ORDER BY n DESC, en.name LIMIT ?""",
            [*params, limit],
        ).fetchall()
    )


def entries_on_month_day(
    conn: sqlite3.Connection, month: int, day: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM entries WHERE substr(entry_date, 6, 5) = ? "
            "ORDER BY entry_date",
            (f"{month:02d}-{day:02d}",),
        ).fetchall()
    )


@dataclass(frozen=True)
class WritingStats:
    entries: int
    words: int
    days_written: int
    avg_words: float
    first_day: date | None
    last_day: date | None
    current_streak: int
    longest_streak: int
    longest_streak_end: date | None


def writing_stats(
    conn: sqlite3.Connection, date_from: date | None = None, date_to: date | None = None
) -> WritingStats:
    where, params = _range_where(date_from, date_to)
    row = conn.execute(
        f"""SELECT COUNT(*) AS entries,
                  COALESCE(SUM(word_count), 0) AS words,
                  COUNT(DISTINCT entry_date) AS days
           FROM entries {where}""",
        params,
    ).fetchone()
    days = [
        date.fromisoformat(r["entry_date"])
        for r in conn.execute(
            f"SELECT DISTINCT entry_date FROM entries {where} ORDER BY entry_date", params
        )
    ]
    longest = run = 0
    longest_end: date | None = None
    prev: date | None = None
    for d in days:
        run = run + 1 if prev is not None and (d - prev).days == 1 else 1
        if run > longest:
            longest, longest_end = run, d
        prev = d
    return WritingStats(
        entries=row["entries"],
        words=row["words"],
        days_written=row["days"],
        avg_words=round(row["words"] / row["entries"], 1) if row["entries"] else 0.0,
        first_day=days[0] if days else None,
        last_day=days[-1] if days else None,
        # ends at the last entry rather than today, since people often write a day late
        current_streak=run,
        longest_streak=longest,
        longest_streak_end=longest_end,
    )


def entries_by_mood(
    conn: sqlite3.Connection, date_from: date, date_to: date, limit: int, highest: bool
) -> list[sqlite3.Row]:
    direction = "DESC" if highest else "ASC"
    return list(
        conn.execute(
            f"""SELECT * FROM entries
                WHERE mood_score IS NOT NULL AND entry_date >= ? AND entry_date <= ?
                ORDER BY mood_score {direction}, entry_date
                LIMIT ?""",
            (date_from.isoformat(), date_to.isoformat(), limit),
        ).fetchall()
    )


def notes_first_linked_in(
    conn: sqlite3.Connection, date_from: date, date_to: date, limit: int
) -> list[sqlite3.Row]:
    """Notes no entry linked before this range, and how often they're linked in it."""
    return list(
        conn.execute(
            """SELECT n.id, n.title, n.kind, MIN(e.entry_date) AS first,
                      SUM(e.entry_date <= ?) AS n
               FROM notes n
               JOIN entry_entities ee ON ee.entity_id = n.id
               JOIN entries e ON e.id = ee.entry_id
               GROUP BY n.id
               HAVING MIN(e.entry_date) >= ? AND MIN(e.entry_date) <= ?
               ORDER BY n DESC, n.title
               LIMIT ?""",
            (date_to.isoformat(), date_from.isoformat(), date_to.isoformat(), limit),
        ).fetchall()
    )


def similar_to_day(conn: sqlite3.Connection, day: date, limit: int) -> list[sqlite3.Row] | None:
    """The entries closest in meaning to that day's, best first, or None if
    nothing was written that day. The day's own entries are left out."""
    day_rows = entries_in_range(conn, day, day)
    if not day_rows:
        return None
    day_ids = {r["id"] for r in day_rows}
    seen: dict[int, None] = {}
    for r in day_rows:
        for eid in similar_entry_ids(conn, r["id"], limit + len(day_rows)):
            if eid not in day_ids:
                seen.setdefault(eid, None)
    ids = list(seen)[:limit]
    order = {eid: i for i, eid in enumerate(ids)}
    return sorted(entries_by_ids(conn, ids), key=lambda r: order[r["id"]])


def similar_entry_ids(conn: sqlite3.Connection, entry_id: int, k: int) -> list[int]:
    try:
        row = conn.execute(
            "SELECT embedding FROM entries_vec WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return []
        rows = conn.execute(
            "SELECT entry_id FROM entries_vec WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (row["embedding"], k + 1),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r["entry_id"] for r in rows if r["entry_id"] != entry_id][:k]


def date_bounds(
    conn: sqlite3.Connection,
    date_from: date | None,
    date_to: date | None,
    only: EntryFilter | None = None,
) -> tuple[str | None, str | None]:
    where, params = _filter_where(date_from, date_to, only)
    row = conn.execute(
        f"SELECT MIN(entry_date) AS lo, MAX(entry_date) AS hi FROM entries {where}",
        params,
    ).fetchone()
    return row["lo"], row["hi"]


def _range_clauses(
    date_from: date | None, date_to: date | None, alias: str = ""
) -> tuple[list[str], list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if date_from:
        clauses.append(f"{alias}entry_date >= ?")
        params.append(date_from.isoformat())
    if date_to:
        clauses.append(f"{alias}entry_date <= ?")
        params.append(date_to.isoformat())
    return clauses, params


def _range_where(
    date_from: date | None, date_to: date | None, alias: str = ""
) -> tuple[str, list[object]]:
    clauses, params = _range_clauses(date_from, date_to, alias)
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def overall_mood(
    conn: sqlite3.Connection, date_from: date | None, date_to: date | None
) -> tuple[float | None, int]:
    clauses, params = _range_clauses(date_from, date_to)
    where = "WHERE " + " AND ".join(["mood_score IS NOT NULL", *clauses])
    row = conn.execute(
        f"SELECT AVG(mood_score) AS avg, COUNT(mood_score) AS n FROM entries {where}",
        params,
    ).fetchone()
    avg = round(row["avg"], 2) if row["avg"] is not None else None
    return avg, row["n"]


def tag_mood_stats(
    conn: sqlite3.Connection,
    date_from: date | None,
    date_to: date | None,
    min_count: int,
) -> list[sqlite3.Row]:
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    where = "WHERE " + " AND ".join(["e.mood_score IS NOT NULL", *clauses])
    return list(
        conn.execute(
            f"""SELECT t.name,
                       COUNT(*) AS n,
                       ROUND(AVG(e.mood_score), 2) AS avg_mood
                FROM tags t
                JOIN entry_tags et ON et.tag_id = t.id
                JOIN entries e ON e.id = et.entry_id
                {where}
                GROUP BY t.name
                HAVING COUNT(*) >= ?
                ORDER BY avg_mood""",
            [*params, min_count],
        ).fetchall()
    )


def entity_mood_stats(
    conn: sqlite3.Connection,
    date_from: date | None,
    date_to: date | None,
    min_count: int,
) -> list[sqlite3.Row]:
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    where = "WHERE " + " AND ".join(["e.mood_score IS NOT NULL", *clauses])
    return list(
        conn.execute(
            f"""SELECT en.name, en.type,
                       COUNT(*) AS n,
                       ROUND(AVG(e.mood_score), 2) AS avg_mood
                FROM entities en
                JOIN entry_entities ee ON ee.entity_id = en.id
                JOIN entries e ON e.id = ee.entry_id
                {where}
                GROUP BY en.name, en.type
                HAVING COUNT(*) >= ?
                ORDER BY avg_mood""",
            [*params, min_count],
        ).fetchall()
    )


def weekday_mood(
    conn: sqlite3.Connection, date_from: date | None, date_to: date | None
) -> list[sqlite3.Row]:
    # %w counts from Sunday = 0
    clauses, params = _range_clauses(date_from, date_to)
    where = "WHERE " + " AND ".join(["mood_score IS NOT NULL", *clauses])
    return list(
        conn.execute(
            f"""SELECT CAST(strftime('%w', entry_date) AS INTEGER) AS dow,
                       COUNT(*) AS n,
                       ROUND(AVG(mood_score), 2) AS avg_mood
                FROM entries {where}
                GROUP BY dow ORDER BY dow""",
            params,
        ).fetchall()
    )


def month_of_year_mood(
    conn: sqlite3.Connection, date_from: date | None, date_to: date | None
) -> list[sqlite3.Row]:
    clauses, params = _range_clauses(date_from, date_to)
    where = "WHERE " + " AND ".join(["mood_score IS NOT NULL", *clauses])
    return list(
        conn.execute(
            f"""SELECT CAST(strftime('%m', entry_date) AS INTEGER) AS moy,
                       COUNT(*) AS n,
                       ROUND(AVG(mood_score), 2) AS avg_mood
                FROM entries {where}
                GROUP BY moy ORDER BY moy""",
            params,
        ).fetchall()
    )


def tag_cooccurrence(
    conn: sqlite3.Connection,
    date_from: date | None,
    date_to: date | None,
    min_count: int,
    limit: int = 8,
) -> list[sqlite3.Row]:
    # tag_id < tag_id so each pair only shows up once
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return list(
        conn.execute(
            f"""SELECT t1.name AS tag_a, t2.name AS tag_b, COUNT(*) AS n
                FROM entry_tags et1
                JOIN entry_tags et2
                  ON et1.entry_id = et2.entry_id AND et1.tag_id < et2.tag_id
                JOIN tags t1 ON t1.id = et1.tag_id
                JOIN tags t2 ON t2.id = et2.tag_id
                JOIN entries e ON e.id = et1.entry_id
                {where}
                GROUP BY t1.name, t2.name
                HAVING COUNT(*) >= ?
                ORDER BY n DESC, tag_a, tag_b
                LIMIT ?""",
            [*params, min_count, limit],
        ).fetchall()
    )


@dataclass(frozen=True)
class CacheStats:
    entries: int
    notes: int
    files: int
    date_from: str | None
    date_to: str | None
    errored_files: int
    private_files: int
    words: int
    tags: int
    links: int  # only links that resolve to an indexed note
    rollups: int
    note_digests: int


def stats(conn: sqlite3.Connection) -> CacheStats:
    def _count(sql: str) -> int:
        return conn.execute(sql).fetchone()["n"]

    lo, hi = date_bounds(conn, None, None)
    return CacheStats(
        entries=_count("SELECT COUNT(*) AS n FROM entries"),
        notes=_count("SELECT COUNT(*) AS n FROM notes"),
        files=_count("SELECT COUNT(*) AS n FROM files"),
        date_from=lo,
        date_to=hi,
        errored_files=_count("SELECT COUNT(*) AS n FROM files WHERE status = 'error'"),
        private_files=_count("SELECT COUNT(*) AS n FROM files WHERE status = 'private'"),
        words=_count("SELECT COALESCE(SUM(word_count), 0) AS n FROM entries"),
        tags=_count("SELECT COUNT(*) AS n FROM tags"),
        links=_count("SELECT COUNT(*) AS n FROM entry_entities"),
        rollups=_count("SELECT COUNT(*) AS n FROM rollups"),
        note_digests=_optional_count(conn, "note_digests"),
    )


def _optional_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    except sqlite3.OperationalError:
        return 0  # a read-only connection to a cache from before the table


def entries_dated_after(conn: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT entry_date, file_path FROM entries WHERE entry_date > ? "
            "ORDER BY entry_date, file_path",
            (day.isoformat(),),
        ).fetchall()
    )


def errored_files(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT path, error_message FROM files WHERE status = 'error' "
            "ORDER BY path"
        ).fetchall()
    )


# Browsing. Everything below only reads.


def entry_by_id(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()


def adjacent_entries(
    conn: sqlite3.Connection, entry: sqlite3.Row
) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
    """The entries just before and after, in (date, id) order."""
    day, entry_id = entry["entry_date"], entry["id"]
    before = conn.execute(
        """SELECT id, entry_date FROM entries
           WHERE entry_date < ? OR (entry_date = ? AND id < ?)
           ORDER BY entry_date DESC, id DESC LIMIT 1""",
        (day, day, entry_id),
    ).fetchone()
    after = conn.execute(
        """SELECT id, entry_date FROM entries
           WHERE entry_date > ? OR (entry_date = ? AND id > ?)
           ORDER BY entry_date, id LIMIT 1""",
        (day, day, entry_id),
    ).fetchone()
    return before, after


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _links_nothing(conn: sqlite3.Connection, target: str = "el.target") -> str:
    clause = f"""NOT EXISTS (SELECT 1 FROM link_targets lt WHERE lt.target = {target})
                 AND NOT EXISTS (SELECT 1 FROM entries d WHERE d.entry_date = {target})"""
    # a read-only connection to a cache from before entry_file_keys has dates only
    if _has_table(conn, "entry_file_keys"):
        clause += f" AND NOT EXISTS (SELECT 1 FROM entry_file_keys fk WHERE fk.key = {target})"
    return clause


def unresolved_links_for_entry(conn: sqlite3.Connection, entry_id: int) -> list[str]:
    """Link targets that are neither a note nor a day with an entry.

    They don't count as mentions until a note with that name is indexed.
    """
    return [
        r["target"]
        for r in conn.execute(
            f"""SELECT el.target FROM entry_links el
                WHERE el.entry_id = ? AND {_links_nothing(conn)}
                ORDER BY el.target""",
            (entry_id,),
        )
    ]


def days_linked_from_entry(conn: sqlite3.Connection, entry_id: int) -> list[sqlite3.Row]:
    """Entries on the days this entry links to, like [[2024-01-15]], with the link
    target that found each. A day linked two ways comes up once for each."""
    select = "SELECT d.id, d.entry_date, el.target FROM entry_links el"
    sql = f"{select} JOIN entries d ON d.entry_date = el.target WHERE el.entry_id = :id"
    if _has_table(conn, "entry_file_keys"):
        sql += f""" UNION {select}
                    JOIN entry_file_keys fk ON fk.key = el.target
                    JOIN entries d ON d.file_path = fk.path
                    WHERE el.entry_id = :id"""
    rows = conn.execute(sql + " ORDER BY 2, 1, 3", {"id": entry_id}).fetchall()
    return [r for r in rows if r["id"] != entry_id]


def entries_linking_to_day(conn: sqlite3.Connection, entry: sqlite3.Row) -> list[sqlite3.Row]:
    """Entries that link to this one's day, by its date or its file's name, with the target."""
    by_file = (
        " OR el.target IN (SELECT key FROM entry_file_keys WHERE path = ?)"
        if _has_table(conn, "entry_file_keys") else ""
    )
    params = [entry["entry_date"], *([entry["file_path"]] if by_file else []), entry["id"]]
    return list(
        conn.execute(
            f"""SELECT DISTINCT e.id, e.entry_date, el.target FROM entry_links el
                JOIN entries e ON e.id = el.entry_id
                WHERE (el.target = ?{by_file}) AND e.id != ?
                ORDER BY e.entry_date, e.id, el.target""",
            params,
        ).fetchall()
    )


def days_linked_from_note(conn: sqlite3.Connection, note_id: int) -> list[sqlite3.Row]:
    """Entries on the days a note links to, like an event note's [[2024-03-05]]."""
    select = "SELECT d.* FROM note_links nl"
    sql = f"{select} JOIN entries d ON d.entry_date = nl.target WHERE nl.note_id = :id"
    if _has_table(conn, "entry_file_keys"):
        sql += f""" UNION {select}
                    JOIN entry_file_keys fk ON fk.key = nl.target
                    JOIN entries d ON d.file_path = fk.path
                    WHERE nl.note_id = :id"""
    return list(conn.execute(sql + " ORDER BY entry_date, id", {"id": note_id}).fetchall())


def notes_linking_day(conn: sqlite3.Connection, entry: sqlite3.Row) -> list[sqlite3.Row]:
    """Notes that link this entry's day, by its date or its file's name."""
    by_file = (
        " OR nl.target IN (SELECT key FROM entry_file_keys WHERE path = :path)"
        if _has_table(conn, "entry_file_keys") else ""
    )
    return list(
        conn.execute(
            f"""SELECT DISTINCT n.* FROM notes n JOIN note_links nl ON nl.note_id = n.id
                WHERE nl.target = :day{by_file}
                ORDER BY n.title COLLATE NOCASE""",
            {"day": entry["entry_date"], "path": entry["file_path"]},
        ).fetchall()
    )


def note_day_links(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    """(entry_id, note_id) for each day some note links to."""
    sql = """SELECT d.id, nl.note_id FROM note_links nl
             JOIN entries d ON d.entry_date = nl.target"""
    if _has_table(conn, "entry_file_keys"):
        sql += """ UNION SELECT d.id, nl.note_id FROM note_links nl
                   JOIN entry_file_keys fk ON fk.key = nl.target
                   JOIN entries d ON d.file_path = fk.path"""
    return {(r[0], r[1]) for r in conn.execute(sql)}


def note_day_mentions(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    return {(r[0], r[1]) for r in conn.execute("SELECT entry_id, note_id FROM note_day_mentions")}


def replace_note_day_mentions(conn: sqlite3.Connection, pairs: set[tuple[int, int]]) -> None:
    with conn:
        conn.execute("DELETE FROM note_day_mentions")
        conn.executemany(
            "INSERT INTO note_day_mentions(entry_id, note_id) VALUES (?, ?)", sorted(pairs)
        )


def entries_for_day_link(conn: sqlite3.Connection, target: str) -> list[sqlite3.Row]:
    """The entries a link like [[2024-01-15]] or [[2024-01-15 trip]] points at."""
    by_file = (
        " OR file_path IN (SELECT path FROM entry_file_keys WHERE key = ?)"
        if _has_table(conn, "entry_file_keys") else ""
    )
    return list(
        conn.execute(
            f"SELECT * FROM entries WHERE entry_date = ?{by_file} ORDER BY entry_date, id",
            (target, target) if by_file else (target,),
        ).fetchall()
    )


def unresolved_links(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            f"""SELECT el.target, COUNT(*) AS n, MAX(e.entry_date) AS last
               FROM entry_links el
               JOIN entries e ON e.id = el.entry_id
               WHERE {_links_nothing(conn)}
               GROUP BY el.target
               ORDER BY n DESC, el.target
               LIMIT ?""",
            (limit,),
        ).fetchall()
    )


def unresolved_note_links(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Link targets in notes that find nothing, and how many notes write each."""
    return list(
        conn.execute(
            f"""SELECT nl.target, COUNT(DISTINCT nl.note_id) AS n
                FROM note_links nl
                WHERE {_links_nothing(conn, "nl.target")}
                GROUP BY nl.target
                ORDER BY n DESC, nl.target"""
        ).fetchall()
    )


def shared_aliases(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Aliases more than one note has, and no note is named, with those notes' titles.

    A link to one finds no note at all.
    """
    rows = conn.execute(
        """SELECT nn.key, n.title FROM note_names nn JOIN notes n ON n.id = nn.note_id
           WHERE nn.key IN (
             SELECT key FROM note_names GROUP BY key HAVING COUNT(DISTINCT note_id) > 1)
           AND nn.key NOT IN (SELECT link_key FROM notes)
           ORDER BY nn.key, n.title COLLATE NOCASE"""
    ).fetchall()
    out: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        out[r["key"]].append(r["title"])
    return dict(out)


def note_names_by_key(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Each title and alias key, with the note it names."""
    return {
        r["key"]: r
        for r in conn.execute(
            """SELECT nn.key, nn.name, n.id, n.title, n.kind
               FROM note_names nn JOIN notes n ON n.id = nn.note_id ORDER BY n.id"""
        )
    }


def files_with_status(conn: sqlite3.Connection, statuses: Sequence[str]) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            f"""SELECT path, status, error_message FROM files
                WHERE status IN ({_JSON_VALUES}) ORDER BY path""",
            (_json_list(list(statuses)),),
        ).fetchall()
    )


def entries_linking(conn: sqlite3.Connection, target: str) -> list[sqlite3.Row]:
    """Entries with a link to this key, newest first."""
    return list(
        conn.execute(
            """SELECT e.* FROM entries e JOIN entry_links el ON el.entry_id = e.id
               WHERE el.target = ? ORDER BY e.entry_date DESC, e.id DESC""",
            (target,),
        ).fetchall()
    )


def notes_linking(conn: sqlite3.Connection, target: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT n.* FROM notes n JOIN note_links nl ON nl.note_id = n.id
               WHERE nl.target = ? ORDER BY n.title COLLATE NOCASE""",
            (target,),
        ).fetchall()
    )


def _browse_where(
    date_from: date | None,
    date_to: date | None,
    tag: str | None,
    note_id: int | None,
    text: str | None,
    group: tuple[str, str] | None = None,
    group_ids: Sequence[int] = (),
) -> tuple[str, list[object]]:
    only = EntryFilter(
        tag or None, (note_id,) if note_id is not None else (), group, tuple(group_ids)
    )
    clauses, params = _filter_clauses(date_from, date_to, only, alias="e.")
    if text and text.strip():
        pattern = _like_contains(text.strip())
        clauses.append(
            "(e.summary LIKE ? ESCAPE '\\' OR e.raw_text LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern])
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def browse_entries(
    conn: sqlite3.Connection,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    tag: str | None = None,
    note_id: int | None = None,
    text: str | None = None,
    group: tuple[str, str] | None = None,
    group_ids: Sequence[int] = (),
    newest_first: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[sqlite3.Row], int]:
    """One page of entries and the total that match."""
    where, params = _browse_where(date_from, date_to, tag, note_id, text, group, group_ids)
    total = conn.execute(f"SELECT COUNT(*) AS n FROM entries e {where}", params).fetchone()["n"]
    direction = "DESC" if newest_first else "ASC"
    rows = conn.execute(
        f"""SELECT e.* FROM entries e {where}
            ORDER BY e.entry_date {direction}, e.id {direction}
            LIMIT ? OFFSET ?""",
        [*params, limit, offset],
    ).fetchall()
    return list(rows), total


def browse_mood(
    conn: sqlite3.Connection,
    *,
    tag: str | None = None,
    note_id: int | None = None,
) -> float | None:
    """Average mood over every entry browse_entries would match, not just one page."""
    where, params = _browse_where(None, None, tag, note_id, None)
    row = conn.execute(
        f"SELECT ROUND(AVG(e.mood_score), 2) AS avg FROM entries e {where}", params
    ).fetchone()
    return row["avg"]


def entry_days(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT id, entry_date, mood_score, word_count, summary FROM entries "
            "ORDER BY entry_date, id"
        ).fetchall()
    )


def all_entry_texts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT id, file_path, raw_text FROM entries ORDER BY id").fetchall()
    )


def all_notes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM notes ORDER BY kind, title COLLATE NOCASE"
        ).fetchall()
    )


def tag_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT t.name, COUNT(*) AS n,
                      ROUND(AVG(e.mood_score), 2) AS avg_mood,
                      COUNT(e.mood_score) AS scored,
                      MIN(e.entry_date) AS first, MAX(e.entry_date) AS last
               FROM tags t
               JOIN entry_tags et ON et.tag_id = t.id
               JOIN entries e ON e.id = et.entry_id
               GROUP BY t.name
               ORDER BY n DESC, t.name"""
        ).fetchall()
    )


def tags_cooccurring_with(
    conn: sqlite3.Connection, tag: str, limit: int
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT t2.name, COUNT(*) AS n
               FROM tags t1
               JOIN entry_tags et1 ON et1.tag_id = t1.id
               JOIN entry_tags et2 ON et2.entry_id = et1.entry_id AND et2.tag_id != t1.id
               JOIN tags t2 ON t2.id = et2.tag_id
               WHERE t1.name = ?
               GROUP BY t2.name
               ORDER BY n DESC, t2.name
               LIMIT ?""",
            (tag, limit),
        ).fetchall()
    )


def all_rollups(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM rollups ORDER BY period_type, period_key DESC"
        ).fetchall()
    )


def get_rollup(
    conn: sqlite3.Connection, period_type: str, period_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rollups WHERE period_type = ? AND period_key = ?",
        (period_type, period_key),
    ).fetchone()


def files_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT f.path, f.status, f.last_indexed, f.error_message,
                      (SELECT COUNT(*) FROM entries e WHERE e.file_path = f.path) AS entries,
                      (SELECT n.id FROM notes n WHERE n.file_path = f.path) AS note_id,
                      (SELECT MIN(e.id) FROM entries e WHERE e.file_path = f.path) AS entry_id
               FROM files f
               ORDER BY f.status = 'indexed', f.path"""
        ).fetchall()
    )
