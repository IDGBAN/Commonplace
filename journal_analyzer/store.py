from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import sqlite_vec

from .models import Extraction, ParsedEntry, ParsedNote
from .parser import relative_path

# allowed_ids can be the whole journal, too many for one placeholder each
_JSON_IDS = "SELECT value FROM json_each(?)"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _placeholders(values: Sequence[object]) -> str:
    return ",".join("?" for _ in values)


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


def get_file_record(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()


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
        conn.execute("DELETE FROM entries WHERE file_path = ?", (path,))
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
    return entry_id


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
            f"SELECT id FROM tags WHERE name IN ({_placeholders(tags)})", tags
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
    return note_id


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
        for r in conn.execute(f"SELECT * FROM notes WHERE id IN ({_placeholders(ids)})", ids)
    }
    return [by_id[i] for i in ids if i in by_id]


def note_ids_matching(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    """Substring match on titles and aliases."""
    terms = [n.strip().casefold() for n in names if n.strip()]
    if not terms:
        return []
    clause = " OR ".join("key LIKE ? ESCAPE '\\'" for _ in terms)
    return [
        r["note_id"]
        for r in conn.execute(
            f"SELECT DISTINCT note_id FROM note_names WHERE {clause} ORDER BY note_id",
            [_like_contains(t) for t in terms],
        )
    ]


def find_notes(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    """Exact title, else exact alias, else substring matches."""
    key = name.strip().casefold()
    if not key:
        return []
    ids = [r["id"] for r in conn.execute("SELECT id FROM notes WHERE link_key = ?", (key,))]
    if not ids:
        ids = [
            r["note_id"]
            for r in conn.execute(
                "SELECT DISTINCT note_id FROM note_names WHERE key = ?", (key,)
            )
        ]
    rows = notes_by_ids(conn, ids or note_ids_matching(conn, [name]))
    return sorted(rows, key=lambda r: r["title"].casefold())


def note_exists(conn: sqlite3.Connection, path: str) -> bool:
    return conn.execute("SELECT 1 FROM notes WHERE file_path = ?", (path,)).fetchone() is not None


def note_name_index(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT note_id, key FROM note_names").fetchall())


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
    where = " AND ".join([f"ee.entity_id IN ({_placeholders(note_ids)})", *clauses])
    rows = conn.execute(
        f"""SELECT ee.entity_id AS note_id, COUNT(*) AS n,
                   MIN(e.entry_date) AS first, MAX(e.entry_date) AS last
            FROM entry_entities ee
            JOIN entries e ON e.id = ee.entry_id
            WHERE {where}
            GROUP BY ee.entity_id""",
        [*note_ids, *params],
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
                WHERE ee.entity_id IN ({_placeholders(note_ids)})
                ORDER BY e.entry_date DESC, e.id DESC""",
            note_ids,
        )
    ]


def entries_mentioning(
    conn: sqlite3.Connection, note_id: int, limit: int
) -> list[sqlite3.Row]:
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
                WHERE entry_id IN ({_placeholders(entry_ids)})
                GROUP BY entity_id
                ORDER BY n DESC, note_id
                LIMIT ?""",
            [*entry_ids, limit],
        ).fetchall()
    )


def note_outlinks(conn: sqlite3.Connection, note_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT DISTINCT n.* FROM note_links nl
               JOIN notes n ON n.link_key = nl.target
               WHERE nl.note_id = ? AND n.id != nl.note_id
               ORDER BY n.title""",
            (note_id,),
        ).fetchall()
    )


def note_backlinks(conn: sqlite3.Connection, note_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """SELECT DISTINCT src.* FROM notes dst
               JOIN note_links nl ON nl.target = dst.link_key
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


def filtered_entry_ids(
    conn: sqlite3.Connection,
    date_from: date | None = None,
    date_to: date | None = None,
    entity_names: list[str] | None = None,
) -> list[int]:
    """Oldest first, so callers can slice off the end for the most recent.

    If the name filter matches nothing it's dropped instead of returning no
    candidates, since the router's entity names aren't always right.
    """
    note_ids = note_ids_matching(conn, entity_names or [])
    clauses, params = _range_clauses(date_from, date_to, alias="e.")
    if note_ids:
        clauses.append(
            "e.id IN (SELECT entry_id FROM entry_entities "
            f"WHERE entity_id IN ({_placeholders(note_ids)}))"
        )
        params.extend(note_ids)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    ids = [
        r["id"]
        for r in conn.execute(
            f"SELECT e.id FROM entries e {where} ORDER BY e.entry_date, e.id", params
        )
    ]
    if note_ids and not ids:
        return filtered_entry_ids(conn, date_from, date_to, None)
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
        sql += f" AND rowid IN ({_JSON_IDS})"
        params.append(json.dumps(allowed_ids))
    try:
        rows = conn.execute(f"{sql} ORDER BY rank LIMIT ?", [*params, k]).fetchall()
    except sqlite3.OperationalError:
        return []  # bad MATCH syntax from model output
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
        sql += f" AND entry_id IN ({_JSON_IDS})"
        params.append(json.dumps(allowed_ids))
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
        f"SELECT * FROM entries WHERE id IN ({_placeholders(ids)}) ORDER BY entry_date", ids
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
    conn: sqlite3.Connection, date_from: date | None, date_to: date | None
) -> list[sqlite3.Row]:
    where, params = _range_where(date_from, date_to)
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
            "SELECT en.name, en.type FROM entities en "
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
        f"SELECT et.entry_id AS eid, t.name FROM tags t "
        f"JOIN entry_tags et ON et.tag_id = t.id "
        f"WHERE et.entry_id IN ({_placeholders(entry_ids)}) ORDER BY et.entry_id, t.name",
        entry_ids,
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
        f"SELECT ee.entry_id AS eid, en.name, en.type FROM entities en "
        f"JOIN entry_entities ee ON ee.entity_id = en.id "
        f"WHERE ee.entry_id IN ({_placeholders(entry_ids)}) "
        f"ORDER BY ee.entry_id, en.type, en.name",
        entry_ids,
    ):
        out[r["eid"]].append(r)
    return dict(out)


def monthly_mood(
    conn: sqlite3.Connection, date_from: date | None, date_to: date | None
) -> list[sqlite3.Row]:
    where, params = _range_where(date_from, date_to)
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
) -> list[sqlite3.Row]:
    where, params = _range_where(date_from, date_to, alias="e.")
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
            f"""SELECT en.name, en.type, COUNT(*) AS n
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


def writing_stats(conn: sqlite3.Connection) -> WritingStats:
    row = conn.execute(
        """SELECT COUNT(*) AS entries,
                  COALESCE(SUM(word_count), 0) AS words,
                  COUNT(DISTINCT entry_date) AS days
           FROM entries"""
    ).fetchone()
    days = [
        date.fromisoformat(r["entry_date"])
        for r in conn.execute(
            "SELECT DISTINCT entry_date FROM entries ORDER BY entry_date"
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
    conn: sqlite3.Connection, date_from: date | None, date_to: date | None
) -> tuple[str | None, str | None]:
    where, params = _range_where(date_from, date_to)
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
    words: int
    tags: int
    links: int  # only links that resolve to an indexed note
    rollups: int


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
        words=_count("SELECT COALESCE(SUM(word_count), 0) AS n FROM entries"),
        tags=_count("SELECT COUNT(*) AS n FROM tags"),
        links=_count("SELECT COUNT(*) AS n FROM entry_entities"),
        rollups=_count("SELECT COUNT(*) AS n FROM rollups"),
    )


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
