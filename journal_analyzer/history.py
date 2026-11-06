"""Your questions and their answers, kept only with [history] enabled = true.

They live in a file of their own next to the cache (journal-history.db), so a
rebuild doesn't touch them and deleting the file deletes all of them. Only
what's needed to read an answer again is kept: the question, the answer with
its Sources line, the days and notes it cited and what it was kept to. Not the
entries it read, nor the sentences quoted from them.

Deleted rows are overwritten rather than left in free pages (secure_delete),
and the file keeps a rollback journal instead of a -wal file, which would
hold copies of what was deleted until the next checkpoint.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePath
from typing import Any

from pydantic import ValidationError
from rich.console import Console

from . import store
from .config import Config
from .models import Answer, ConfigError, Timing

console = Console(stderr=True)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS asked (
  id           INTEGER PRIMARY KEY,
  conversation TEXT NOT NULL,
  asked_at     TEXT NOT NULL,
  source       TEXT NOT NULL,
  question     TEXT NOT NULL,
  standalone   TEXT,
  scope        TEXT NOT NULL DEFAULT '',
  stage        TEXT NOT NULL,
  answer       TEXT NOT NULL,
  sources      TEXT NOT NULL DEFAULT '{}',
  -- [{"step": ..., "seconds": ...}], how long each part of the answer took
  timings      TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_asked_conversation ON asked(conversation);
"""
# where a question was asked
SOURCES = ("ask", "chat", "browse")
# files SQLite can leave next to the database
_SIDE_FILES = ("-journal", "-wal", "-shm")


def history_path(config: Config) -> Path:
    if config.history.path:
        return config.history.path
    cache = config.db.path
    return cache.with_name(f"{cache.stem}-history{cache.suffix or '.db'}")


def _checked_path(config: Config) -> Path:
    path = history_path(config)
    if PurePath(path).is_relative_to(config.vault.path):
        raise ConfigError(
            f"[history] path in config.toml is inside the vault ({path}). Nothing is ever "
            "written there, so pick a place outside it, or leave it empty to keep the "
            "history next to the cache."
        )
    return path


def new_conversation() -> str:
    return uuid.uuid4().hex


def valid_conversation(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


@contextlib.contextmanager
def _writable(config: Config) -> Iterator[sqlite3.Connection]:
    path = _checked_path(config)
    # the same privacy as the cache, since answers are made of the journal
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    is_new = not path.exists()
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        if is_new:
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(_SCHEMA)
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(asked)")}
        if "timings" not in columns:
            # files from before timings were kept
            conn.execute("ALTER TABLE asked ADD COLUMN timings TEXT NOT NULL DEFAULT '[]'")
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _readable(config: Config) -> Iterator[sqlite3.Connection | None]:
    """None when there's no history file, which reading never creates."""
    path = history_path(config)
    if not path.is_file():
        yield None
        return
    # rw rather than ro: after a crash SQLite has to roll back the journal
    # it left, and a read-only connection can't
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'asked'"
        ).fetchone()
        yield conn if has_table else None
    finally:
        conn.close()


def scope_text(
    conn: sqlite3.Connection,
    only: store.EntryFilter | None,
    since: date | None = None,
    until: date | None = None,
) -> str:
    """What an answer was kept to, like "entries linking John Doe from 2024-03-01", or empty."""
    parts = []
    if only and only.note_ids:
        parts.append("linking " + ", ".join(r["title"] for r in store.notes_by_ids(conn, list(only.note_ids))))
    if only and only.group:
        parts.append(f"linking a note whose {only.group[0]} is {only.group[1]}")
    if only and only.tag:
        parts.append(f"tagged {only.tag}")
    if since and until:
        parts.append(f"from {since} to {until}")
    elif since:
        parts.append(f"from {since} on")
    elif until:
        parts.append(f"up to {until}")
    return ("entries " + " and ".join(parts)) if parts else ""


def _sources(answer: Answer) -> str:
    return json.dumps({
        "dates": [
            {"date": d.isoformat(), "section": answer.cited_parts.get(d.isoformat())}
            for d in answer.cited_dates
        ],
        "notes": list(answer.cited_notes),
    }, ensure_ascii=False)


def _prune(config: Config, conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM asked WHERE id NOT IN (SELECT id FROM asked ORDER BY id DESC LIMIT ?)",
        (config.history.keep,),
    )
    days = config.history.forget_after_days
    if days:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        conn.execute("DELETE FROM asked WHERE asked_at < ?", (cutoff,))


def record(
    config: Config,
    question: str,
    answer: Answer,
    source: str,
    conversation: str,
    standalone: str | None = None,
    scope: str = "",
) -> int | None:
    """Keeps a question and its answer when history is on. Returns its id, or
    None when it's off or the file can't be written, which is said on stderr
    and doesn't stop the answer."""
    if not config.history.enabled:
        return None
    try:
        with _writable(config) as conn, conn:
            cur = conn.execute(
                "INSERT INTO asked(conversation, asked_at, source, question, standalone, scope, "
                "stage, answer, sources, timings) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    conversation, datetime.now(UTC).isoformat(), source, question,
                    standalone if standalone and standalone != question else None,
                    scope, answer.stage_used, answer.text, _sources(answer),
                    json.dumps([t.model_dump() for t in answer.timings]),
                ),
            )
            _prune(config, conn)
            return cur.lastrowid
    except (OSError, sqlite3.Error, ConfigError) as exc:
        console.print(f"[yellow]Couldn't keep this question in your history: {exc}[/yellow]")
        return None


def _forget_old(config: Config) -> None:
    # keep and forget_after_days hold even when nothing new is being asked
    if not config.history.forget_after_days or not history_path(config).is_file():
        return
    with contextlib.suppress(OSError, sqlite3.Error), _writable(config) as conn, conn:
        _prune(config, conn)


@dataclass(frozen=True)
class Kept:
    id: int
    conversation: str
    asked_at: str
    source: str
    question: str
    standalone: str | None
    scope: str
    stage: str
    answer: str
    sources: dict[str, Any]
    timings: list[Timing] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "conversation": self.conversation, "asked_at": self.asked_at,
            "source": self.source, "question": self.question, "standalone": self.standalone,
            "scope": self.scope, "stage": self.stage, "answer": self.answer, "sources": self.sources,
            "timings": [t.model_dump() for t in self.timings],
        }


def _kept(row: sqlite3.Row) -> Kept:
    try:
        sources = json.loads(row["sources"])
    except json.JSONDecodeError:
        sources = {"dates": [], "notes": []}
    timings: list[Timing] = []
    # a file from before timings were kept that hasn't been written to since
    if "timings" in row.keys():  # noqa: SIM118 - "in" on a Row looks at values
        with contextlib.suppress(json.JSONDecodeError, ValidationError, TypeError):
            timings = [Timing.model_validate(t) for t in json.loads(row["timings"])]
    return Kept(
        row["id"], row["conversation"], row["asked_at"], row["source"], row["question"],
        row["standalone"], row["scope"], row["stage"], row["answer"], sources, timings,
    )


def recent(config: Config, limit: int = 50) -> list[Kept]:
    """Newest first."""
    _forget_old(config)
    with _readable(config) as conn:
        if conn is None:
            return []
        rows = conn.execute("SELECT * FROM asked ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_kept(r) for r in rows]


def get(config: Config, kept_id: int) -> Kept | None:
    _forget_old(config)
    with _readable(config) as conn:
        if conn is None:
            return None
        row = conn.execute("SELECT * FROM asked WHERE id = ?", (kept_id,)).fetchone()
    return _kept(row) if row else None


def last_conversation(config: Config, source: str) -> list[Kept]:
    """The latest conversation from `source`, oldest turn first."""
    _forget_old(config)
    with _readable(config) as conn:
        if conn is None:
            return []
        latest = conn.execute(
            "SELECT conversation FROM asked WHERE source = ? ORDER BY id DESC LIMIT 1", (source,)
        ).fetchone()
        if latest is None:
            return []
        rows = conn.execute(
            "SELECT * FROM asked WHERE conversation = ? ORDER BY id", (latest["conversation"],)
        ).fetchall()
    return [_kept(r) for r in rows]


def count(config: Config) -> int:
    with _readable(config) as conn:
        return conn.execute("SELECT COUNT(*) FROM asked").fetchone()[0] if conn is not None else 0


def delete(config: Config, kept_id: int) -> bool:
    """Forgets one question. False if there's no such one."""
    if not history_path(config).is_file():
        return False
    with _writable(config) as conn, conn:
        return conn.execute("DELETE FROM asked WHERE id = ?", (kept_id,)).rowcount > 0


def clear(config: Config) -> int | None:
    """Deletes the history file and what SQLite keeps next to it. Returns how
    many questions it held, or None if it couldn't be read, which doesn't
    stop it going. OSError if a file can't be removed."""
    path = history_path(config)
    held: int | None = 0
    if path.is_file():
        try:
            held = count(config)
        except sqlite3.Error:
            held = None
    for p in (path, *(path.with_name(path.name + suffix) for suffix in _SIDE_FILES)):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
    return held


def stats(config: Config) -> tuple[int, int] | None:
    """(questions, bytes) for the history file, or None when there isn't one."""
    path = history_path(config)
    if not path.is_file():
        return None
    try:
        return count(config), path.stat().st_size
    except sqlite3.Error:
        return None
