from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import sqlite_vec

from .config import Config
from .models import DatabaseError, EmbeddingModelMismatchError
from .ollama_client import same_model

# Bump on any schema change. Old caches get rebuilt, not migrated.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path          TEXT PRIMARY KEY,
  content_hash  TEXT NOT NULL,
  last_indexed  TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  error_message TEXT
);

CREATE TABLE IF NOT EXISTS entries (
  id           INTEGER PRIMARY KEY,
  file_path    TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
  entry_date   TEXT NOT NULL,
  raw_text     TEXT NOT NULL,
  word_count   INTEGER NOT NULL,
  mood_score   REAL,
  summary      TEXT,
  model_used   TEXT,
  extracted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_date ON entries(entry_date);

CREATE TABLE IF NOT EXISTS tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS entry_tags (
  entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  PRIMARY KEY (entry_id, tag_id)
);

CREATE TABLE IF NOT EXISTS notes (
  id           INTEGER PRIMARY KEY,
  file_path    TEXT NOT NULL UNIQUE REFERENCES files(path) ON DELETE CASCADE,
  title        TEXT NOT NULL,
  link_key     TEXT NOT NULL,
  kind         TEXT NOT NULL,
  aliases      TEXT NOT NULL DEFAULT '[]',
  properties   TEXT NOT NULL DEFAULT '{}',
  raw_text     TEXT NOT NULL,
  word_count   INTEGER NOT NULL,
  summary      TEXT,
  model_used   TEXT,
  extracted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notes_link_key ON notes(link_key);

-- title and aliases
CREATE TABLE IF NOT EXISTS note_names (
  note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  name    TEXT NOT NULL,
  key     TEXT NOT NULL,
  PRIMARY KEY (note_id, key)
);
CREATE INDEX IF NOT EXISTS idx_note_names_key ON note_names(key);

-- Resolved against notes.link_key when read, so a link can come before its note.
CREATE TABLE IF NOT EXISTS entry_links (
  entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
  target   TEXT NOT NULL,
  PRIMARY KEY (entry_id, target)
);
CREATE INDEX IF NOT EXISTS idx_entry_links_target ON entry_links(target);

CREATE TABLE IF NOT EXISTS note_links (
  note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  target  TEXT NOT NULL,
  PRIMARY KEY (note_id, target)
);
CREATE INDEX IF NOT EXISTS idx_note_links_target ON note_links(target);

CREATE VIEW IF NOT EXISTS entities AS
  SELECT id, title AS name, kind AS type FROM notes;
CREATE VIEW IF NOT EXISTS entry_entities AS
  SELECT DISTINCT el.entry_id, n.id AS entity_id
  FROM entry_links el JOIN notes n ON n.link_key = el.target;

CREATE TABLE IF NOT EXISTS rollups (
  id            INTEGER PRIMARY KEY,
  period_type   TEXT NOT NULL,
  period_key    TEXT NOT NULL,
  summary       TEXT,
  dominant_tags TEXT,
  avg_mood      REAL,
  entry_count   INTEGER,
  stale         INTEGER NOT NULL DEFAULT 0,
  generated_at  TEXT,
  UNIQUE(period_type, period_key)
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
  raw_text,
  content='entries',
  content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS entries_fts_ai AFTER INSERT ON entries BEGIN
  INSERT INTO entries_fts(rowid, raw_text) VALUES (new.id, new.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS entries_fts_ad AFTER DELETE ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, raw_text)
  VALUES ('delete', old.id, old.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS entries_fts_au AFTER UPDATE OF raw_text ON entries BEGIN
  INSERT INTO entries_fts(entries_fts, rowid, raw_text)
  VALUES ('delete', old.id, old.raw_text);
  INSERT INTO entries_fts(rowid, raw_text) VALUES (new.id, new.raw_text);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title,
  aliases,
  raw_text,
  content='notes',
  content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS notes_fts_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, title, aliases, raw_text)
  VALUES (new.id, new.title, new.aliases, new.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, aliases, raw_text)
  VALUES ('delete', old.id, old.title, old.aliases, old.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS notes_fts_au AFTER UPDATE OF title, aliases, raw_text ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, title, aliases, raw_text)
  VALUES ('delete', old.id, old.title, old.aliases, old.raw_text);
  INSERT INTO notes_fts(rowid, title, aliases, raw_text)
  VALUES (new.id, new.title, new.aliases, new.raw_text);
END;
"""


def connect(config: Config) -> sqlite3.Connection:
    db_path: Path = config.db.path
    try:
        # The cache has the whole journal in it, so keep it private. The -wal
        # and -shm files copy the db file's mode.
        db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        is_new = not db_path.exists()
        # Access is sequential (watchdog threads only queue events), but the
        # watch loop may not be on the thread that connected.
        conn = sqlite3.connect(db_path, check_same_thread=False)
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseError(
            f"Could not open the cache database at {db_path}: {exc}. "
            "Check that db.path in your config points somewhere writable."
        ) from exc
    if is_new:
        # FAT and some network drives don't do chmod
        with contextlib.suppress(OSError):
            db_path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL so ask/stats can read while watch or index is writing
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        load_vec(conn)
    except DatabaseError:
        conn.close()
        raise
    return conn


def load_vec(conn: sqlite3.Connection) -> None:
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.OperationalError) as exc:
        raise DatabaseError(
            "Could not load the sqlite-vec extension. Your Python's sqlite3 "
            "must support loadable extensions and the sqlite-vec package must "
            f"be installed. Underlying error: {exc}"
        ) from exc


def _rebuild_hint(conn: sqlite3.Connection) -> str:
    path = conn.execute("PRAGMA database_list").fetchone()["file"] or "the database"
    return f"delete {path} and re-run 'journal init && journal index' to rebuild it"


def init_schema(conn: sqlite3.Connection) -> None:
    # the vec tables need the embedding size, so ensure_vec_table makes those
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        populated = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'entries'"
        ).fetchone()
        if populated:
            age = "an older" if version < SCHEMA_VERSION else "a newer"
            raise DatabaseError(
                f"This cache was built by {age} version of journal-analyzer "
                f"(format {version}, this version reads format {SCHEMA_VERSION}). "
                f"To use this version, {_rebuild_hint(conn)}."
            )
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def verify_embed_model(conn: sqlite3.Connection, dim: int, model: str) -> None:
    # Two models can have the same width, so the name is checked too. Older
    # caches only recorded the width.
    stored_dim = get_meta(conn, "embed_dim")
    stored_model = get_meta(conn, "embed_model")
    if stored_dim is not None and not stored_dim.isdigit():
        raise DatabaseError(
            f"The stored embedding dimension ('{stored_dim}') is not a number, "
            f"so the cache is corrupt. To fix it, {_rebuild_hint(conn)}."
        )
    if stored_dim is not None and int(stored_dim) != dim:
        built_by = f" by '{stored_model}'" if stored_model else ""
        raise EmbeddingModelMismatchError(
            f"The configured embed model '{model}' produces {dim}-dimensional "
            f"vectors, but the cache was built with {stored_dim}-dimensional "
            f"ones{built_by}. Either restore the original embed_model in "
            f"config.toml, or {_rebuild_hint(conn)}."
        )
    if stored_model is not None and not same_model(stored_model, model):
        raise EmbeddingModelMismatchError(
            f"The cache was embedded with '{stored_model}' but config.toml now "
            f"names '{model}', and vectors from different models can't be "
            f"compared. Either set embed_model back to '{stored_model}', or "
            f"{_rebuild_hint(conn)}."
        )


def ensure_vec_table(conn: sqlite3.Connection, dim: int, model: str) -> None:
    verify_embed_model(conn, dim, model)
    for table, id_column in (("entries_vec", "entry_id"), ("notes_vec", "note_id")):
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
            f"{id_column} INTEGER PRIMARY KEY, embedding FLOAT[{dim}])"
        )
    set_meta(conn, "embed_dim", str(dim))
    set_meta(conn, "embed_model", model)
    conn.commit()
