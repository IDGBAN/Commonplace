import os
import sqlite3
import stat

import pytest
from conftest import make_config

from journal_analyzer import db
from journal_analyzer.models import DatabaseError


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_a_new_cache_is_readable_by_its_owner_only(tmp_path):
    cfg = make_config(tmp_path)
    private = tmp_path / "private" / "journal.db"
    cfg = cfg.model_copy(update={"db": cfg.db.model_copy(update={"path": private})})
    db.connect(cfg).close()
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(private.stat().st_mode) == 0o600


def test_a_fresh_cache_is_stamped_with_the_current_format(config):
    conn = db.connect(config)
    db.init_schema(conn)
    db.init_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    conn.close()


def test_a_cache_from_before_formats_were_versioned_is_refused(config):
    legacy = sqlite3.connect(config.db.path)
    legacy.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY)")
    legacy.commit()
    legacy.close()
    conn = db.connect(config)
    with pytest.raises(DatabaseError, match="older version") as exc:
        db.init_schema(conn)
    assert "journal init && journal index" in str(exc.value)
    conn.close()


def test_a_cache_from_a_newer_version_is_refused(config):
    conn = db.connect(config)
    db.init_schema(conn)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(DatabaseError, match="newer version"):
        db.init_schema(conn)
    conn.close()


def test_the_schema_has_note_and_vector_tables(conn):
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"entries_vec", "notes_vec", "notes_fts", "entry_links", "note_names"} <= names
