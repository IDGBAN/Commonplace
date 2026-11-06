"""Model results kept apart from the cache, so a rebuild doesn't redo them.

The cache is thrown away when its format changes, and `journal index --force`
re-reads every file. Both used to mean hours of model calls for answers that
come out the same, since extraction, summaries, digests and embeddings all
run at temperature 0. Each result here is keyed by everything that went into
it (the model and its weights, the prompt, the options), so anything that
would change the answer misses and goes to the model.
"""

from __future__ import annotations

import contextlib
import sqlite3
from array import array
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from .config import Config

console = Console(stderr=True)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memo (
  key        TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  model      TEXT NOT NULL,
  value      TEXT,
  vector     BLOB,
  created_at TEXT NOT NULL
);
-- how fast each model reads a prompt on this machine, for progress estimates
CREATE TABLE IF NOT EXISTS speeds (
  model             TEXT PRIMARY KEY,
  chars_per_token   REAL NOT NULL,
  tokens_per_second REAL NOT NULL,
  updated_at        TEXT NOT NULL
);
"""


def memo_path(config: Config) -> Path:
    if config.db.memo_path:
        return config.db.memo_path
    cache = config.db.path
    return cache.with_name(f"{cache.stem}-memo{cache.suffix or '.db'}")


class Memo:
    def __init__(self, path: Path):
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._broken = False

    def _db(self) -> sqlite3.Connection | None:
        if self._conn is not None or self._broken:
            return self._conn
        try:
            # same privacy as the cache: it holds summaries of the journal
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            is_new = not self.path.exists()
            conn = sqlite3.connect(self.path, check_same_thread=False)
            if is_new:
                with contextlib.suppress(OSError):
                    self.path.chmod(0o600)
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            # the memo only saves time, so carry on without it
            self._broken = True
            console.print(
                f"[yellow]Can't use the model memo at {self.path} ({exc}); every result "
                "comes from the model this time.[/yellow]"
            )
            return None
        self._conn = conn
        return conn

    def get_text(self, key: str) -> str | None:
        conn = self._db()
        if conn is None:
            return None
        row = conn.execute("SELECT value FROM memo WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put_text(self, key: str, kind: str, model: str, value: str) -> None:
        self._put(key, kind, model, value, None)

    def get_vector(self, key: str) -> list[float] | None:
        conn = self._db()
        if conn is None:
            return None
        row = conn.execute("SELECT vector FROM memo WHERE key = ?", (key,)).fetchone()
        if not row or row[0] is None:
            return None
        values = array("d")
        values.frombytes(row[0])
        return list(values)

    def get_speed(self, model: str) -> tuple[float, float] | None:
        """(chars per token, tokens read per second) last measured for a model."""
        conn = self._db()
        if conn is None:
            return None
        row = conn.execute(
            "SELECT chars_per_token, tokens_per_second FROM speeds WHERE model = ?", (model,)
        ).fetchone()
        return (float(row[0]), float(row[1])) if row else None

    def put_speed(self, model: str, chars_per_token: float, tokens_per_second: float) -> None:
        conn = self._db()
        if conn is None:
            return
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO speeds(model, chars_per_token, tokens_per_second, "
                "updated_at) VALUES (?, ?, ?, ?)",
                (model, chars_per_token, tokens_per_second, datetime.now(UTC).isoformat()),
            )

    def put_vector(self, key: str, model: str, vector: list[float]) -> None:
        self._put(key, "embed", model, None, array("d", vector).tobytes())

    def _put(
        self, key: str, kind: str, model: str, value: str | None, vector: bytes | None
    ) -> None:
        conn = self._db()
        if conn is None:
            return
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO memo(key, kind, model, value, vector, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, kind, model, value, vector, datetime.now(UTC).isoformat()),
            )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def open_for(config: Config) -> Memo | None:
    return Memo(memo_path(config)) if config.db.memo else None


def stats(config: Config) -> tuple[int, int] | None:
    """(results, bytes) for the memo file, or None when there isn't one."""
    path = memo_path(config)
    if not config.db.memo or not path.is_file():
        return None
    try:
        with contextlib.closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM memo").fetchone()[0]
    except sqlite3.Error:
        return None
    return count, path.stat().st_size
