"""What `journal mcp` keeps from its client, by [mcp] in config.toml.

With exclude_tags or exclude_kinds set, the tools read a copy of the cache in
memory with those entries and notes taken out, along with what was written
from them: the digests of the periods they're in or that link them, the
relationship digests of the notes they link or that link them, and the
profile when facts from them went into it. The copy is made again whenever
the cache file changes, so it keeps up with `journal watch`.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import db, nicknames, parser, profile, store
from .config import Config
from .models import FrontmatterValue

# one JSON array instead of a placeholder per id, as in store
_VALUES = "SELECT value FROM json_each(?)"
_VECTOR_TABLES = (
    ("entries_vec", "entry_id", "entries"),
    ("sections_vec", "section_id", "entry_sections"),
    ("notes_vec", "note_id", "notes"),
)


def active(config: Config) -> bool:
    return bool(config.mcp.exclude_tags or config.mcp.exclude_kinds)


@dataclass
class Withheld:
    entries: int = 0
    notes: int = 0
    digests: int = 0
    note_digests: int = 0
    profile: bool = False
    themes: int = 0


def _tags_of_entries(conn: sqlite3.Connection, wanted: set[str]) -> set[int]:
    found = {
        r["entry_id"]
        for r in conn.execute(
            "SELECT et.entry_id FROM entry_tags et JOIN tags t ON t.id = et.tag_id "
            f"WHERE t.name IN ({_VALUES})",
            (json.dumps(sorted(wanted)),),
        )
    }
    # the model's own tags, kept even when [tags] ignore drops them
    for r in conn.execute("SELECT entry_id, tags FROM entry_readings"):
        with suppress(json.JSONDecodeError, TypeError):
            if {str(t).lower() for t in json.loads(r["tags"])} & wanted:
                found.add(r["entry_id"])
    for r in conn.execute(
        "SELECT e.id, e.raw_text, ff.data FROM entries e "
        "LEFT JOIN file_frontmatter ff ON ff.path = e.file_path"
    ):
        frontmatter: dict[str, FrontmatterValue] = {}
        with suppress(json.JSONDecodeError, TypeError):
            frontmatter = json.loads(r["data"] or "{}")
        written = set(parser.extract_literal_tags(r["raw_text"])) | set(parser.frontmatter_tags(frontmatter))
        if written & wanted:
            found.add(r["id"])
    return found


def _note_file_tags(path: str) -> set[str]:
    # notes don't keep their frontmatter in the cache
    try:
        return set(parser.frontmatter_tags(parser.read_frontmatter(parser.read_bytes(Path(path)))))
    except OSError:
        return set()


def _excluded_notes(conn: sqlite3.Connection, config: Config) -> set[int]:
    kinds = set(config.mcp.exclude_kinds)
    tags = set(config.mcp.exclude_tags)
    found = set()
    for r in conn.execute("SELECT id, kind, raw_text, file_path FROM notes"):
        if r["kind"].casefold() in kinds or (
            tags and (set(parser.extract_literal_tags(r["raw_text"])) | _note_file_tags(r["file_path"])) & tags
        ):
            found.add(r["id"])
    return found


def _ids(values: set[int]) -> tuple[str]:
    return (json.dumps(sorted(values)),)


def _covers(bounds: tuple[date, date], days: list[date]) -> bool:
    i = bisect.bisect_left(days, bounds[0])
    return i < len(days) and days[i] <= bounds[1]


def _withhold(config: Config, conn: sqlite3.Connection) -> Withheld:
    entries = _tags_of_entries(conn, set(config.mcp.exclude_tags)) if config.mcp.exclude_tags else set()
    notes = _excluded_notes(conn, config)
    values = _VALUES
    note_targets = [
        r["target"] for r in conn.execute(
            f"SELECT target FROM link_targets WHERE note_id IN ({values})", _ids(notes)
        )
    ]
    # what was written with them in its prompt
    linking_notes = {
        r["entry_id"] for r in conn.execute(
            f"SELECT entry_id FROM entry_entities WHERE entity_id IN ({values})", _ids(notes)
        )
    }
    days = sorted({
        date.fromisoformat(r["entry_date"]) for r in conn.execute(
            f"SELECT entry_date FROM entries WHERE id IN ({values})", _ids(entries | linking_notes)
        )
    })
    digests = []
    for r in conn.execute("SELECT id, period_type, period_key FROM rollups"):
        try:
            bounds = store.period_bounds(r["period_type"], r["period_key"])
        except ValueError:
            digests.append(r["id"])  # can't tell what it covers
            continue
        if _covers(bounds, days):
            digests.append(r["id"])
    # a note's digest reads the days it links, like an event note's
    day_rows = conn.execute(
        f"""SELECT entry_date AS key,
                   entry_date IN (SELECT entry_date FROM entries WHERE id NOT IN ({values})) AS kept
            FROM entries WHERE id IN ({values})
            UNION
            SELECT k.key, k.path IN (SELECT file_path FROM entries WHERE id NOT IN ({values}))
            FROM entry_file_keys k JOIN entries e ON e.file_path = k.path WHERE e.id IN ({values})""",
        (*_ids(entries), *_ids(entries), *_ids(entries), *_ids(entries)),
    ).fetchall()
    day_keys = [r["key"] for r in day_rows]
    # days with nothing left on them, which links to would show up as broken
    gone_days = [r["key"] for r in day_rows if not r["kept"]]
    keys = {
        r["link_key"] for r in conn.execute(
            f"""SELECT n.link_key FROM notes n
                WHERE n.id IN ({values})
                   OR n.id IN (SELECT entity_id FROM entry_entities WHERE entry_id IN ({values}))
                   OR n.id IN (SELECT note_id FROM note_links WHERE target IN ({values}))""",
            (*_ids(notes), *_ids(entries), json.dumps(note_targets + day_keys)),
        )
    }
    hashes = {
        profile.text_hash(r["raw_text"]) for r in conn.execute(
            f"SELECT raw_text FROM entries WHERE id IN ({values})", _ids(entries)
        )
    }
    facts = [
        r for r in conn.execute("SELECT id, text_hash, folded FROM profile_facts")
        if r["text_hash"] in hashes
    ]
    me = config.profile.note
    me_withheld = bool(me) and any(
        parser.relative_path(r["file_path"], config.vault.path) == me
        for r in conn.execute(f"SELECT file_path FROM notes WHERE id IN ({values})", _ids(notes))
    )
    drop_profile = me_withheld or any(r["folded"] for r in facts)
    present = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    # a theme's name was written from its central entries
    named_from = entries | linking_notes
    themes = [
        r["id"] for r in conn.execute("SELECT id, named_from FROM themes")
        if set(json.loads(r["named_from"])) & named_from
    ] if "themes" in present else []
    files = [
        r["file_path"] for r in conn.execute(
            f"SELECT DISTINCT file_path FROM entries WHERE id IN ({values}) "
            f"UNION SELECT file_path FROM notes WHERE id IN ({values})",
            (*_ids(entries), *_ids(notes)),
        )
    ]

    with conn:
        conn.execute(f"DELETE FROM entries WHERE id IN ({values})", _ids(entries))
        # links to what's gone would show up as broken, naming it
        for table in ("entry_links", "entry_link_anchors", "entry_link_labels", "note_links"):
            conn.execute(f"DELETE FROM {table} WHERE target IN ({values})", (json.dumps(note_targets + gone_days),))
        conn.execute(f"DELETE FROM notes WHERE id IN ({values})", _ids(notes))
        conn.execute(
            f"DELETE FROM files WHERE path IN ({values}) "
            "AND path NOT IN (SELECT file_path FROM entries) AND path NOT IN (SELECT file_path FROM notes)",
            (json.dumps(files),),
        )
        conn.execute(f"DELETE FROM rollups WHERE id IN ({values})", (json.dumps(digests),))
        note_digests = conn.execute(
            f"DELETE FROM note_digests WHERE link_key IN ({values})", (json.dumps(sorted(keys)),)
        ).rowcount
        conn.execute(f"DELETE FROM profile_facts WHERE id IN ({values})", (json.dumps([r["id"] for r in facts]),))
        if drop_profile:
            conn.execute(f"DELETE FROM meta WHERE key IN ({values})", (json.dumps(profile.TEXT_KEYS),))
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM entry_tags)")
        if themes:
            # the grouping stays; only what the model wrote from those entries goes
            conn.execute(
                f"UPDATE themes SET name = NULL, about = NULL WHERE id IN ({values})", (json.dumps(themes),)
            )
        for table, column, owner in _VECTOR_TABLES:
            if table in present:
                conn.execute(f"DELETE FROM {table} WHERE {column} NOT IN (SELECT id FROM {owner})")
    # names learned only from what's gone shouldn't name anyone
    nicknames.refresh(config, conn)
    return Withheld(len(entries), len(notes), len(digests), note_digests, drop_profile, len(themes))


def _fingerprint(path: Path) -> tuple[tuple[int, int] | None, ...]:
    out: list[tuple[int, int] | None] = []
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


class FilteredCache:
    """The cache as the MCP client may see it, kept in memory."""

    def __init__(self, config: Config):
        self.config = config
        self._copy: sqlite3.Connection | None = None
        self._seen: tuple[tuple[int, int] | None, ...] | None = None
        self.withheld = Withheld()

    def connection(self) -> sqlite3.Connection:
        """Up to date with the cache file. Don't close it; it's reused."""
        if self._copy is not None and _fingerprint(self.config.db.path) == self._seen:
            return self._copy
        source = db.connect_readonly(self.config)
        # after opening, which can create the -wal file, and before copying,
        # so a write during the copy makes the next call copy again
        now = _fingerprint(self.config.db.path)
        copy = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            copy.row_factory = sqlite3.Row
            db.load_vec(copy)
            source.backup(copy)
            copy.execute("PRAGMA foreign_keys = ON")
            self.withheld = _withhold(self.config, copy)
            copy.execute("PRAGMA query_only = ON")
        except BaseException:
            copy.close()
            raise
        finally:
            source.close()
        self.close()
        self._copy, self._seen = copy, now
        return copy

    def close(self) -> None:
        if self._copy is not None:
            self._copy.close()
            self._copy = None
