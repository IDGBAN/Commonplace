import shutil
from datetime import date

import pytest
from conftest import EMBED_DIM, FIXTURES, copy_fixtures, copy_note_fixtures, make_config
from watchdog.events import (
    DirCreatedEvent,
    DirDeletedEvent,
    DirMovedEvent,
    FileDeletedEvent,
)

from journal_analyzer import db as db_mod
from journal_analyzer import ingester, store
from journal_analyzer.models import (
    ConfigError,
    Extraction,
    ModelRequestError,
    NoteSummary,
    OllamaUnavailableError,
    StructuredOutputError,
)


def _snapshot(conn):
    files = conn.execute(
        "SELECT path, content_hash, status FROM files ORDER BY path"
    ).fetchall()
    entries = conn.execute(
        "SELECT id, file_path, entry_date, raw_text, extracted_at "
        "FROM entries ORDER BY id"
    ).fetchall()
    return [tuple(r) for r in files], [tuple(r) for r in entries]


def test_backfill_idempotent_and_incremental(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    paths = copy_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)

    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts == {"indexed": 2, "skipped": 0, "error": 0, "dropped": 0}
    files1, entries1 = _snapshot(conn)
    assert len(entries1) == 2

    # second run skips everything
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts == {"indexed": 0, "skipped": 2, "error": 0, "dropped": 0}
    assert _snapshot(conn) == (files1, entries1)

    # change one file, only its rows should change
    target = paths[0]
    target.write_text(
        target.read_text(encoding="utf-8") + "\nAn extra evening thought.\n",
        encoding="utf-8",
    )
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts == {"indexed": 1, "skipped": 1, "error": 0, "dropped": 0}
    files2, entries2 = _snapshot(conn)
    changed_files = [f for f in files2 if f not in files1]
    assert [f[0] for f in changed_files] == [str(target)]
    untouched = [e for e in entries1 if e[1] != str(target)]
    assert all(e in entries2 for e in untouched)
    assert any("extra evening thought" in e[3] for e in entries2 if e[1] == str(target))


def test_malformed_file_is_marked_error_without_aborting(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    bad = cfg.vault.path / "no-date-in-name.md"
    bad.write_text("There is no date here at all.", encoding="utf-8")
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)

    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts == {"indexed": 1, "skipped": 0, "error": 1, "dropped": 0}
    row = conn.execute(
        "SELECT status, error_message FROM files WHERE path = ?", (str(bad),)
    ).fetchone()
    assert row["status"] == "error"
    assert "date" in row["error_message"].lower()
    # errored files get retried even with the same hash
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts["error"] == 1 and counts["skipped"] == 1


def test_edit_marks_rollups_stale(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    paths = copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)

    # fresh rollups to go stale
    for ptype, pkey in (("week", "2024-W03"), ("month", "2024-01"), ("year", "2024")):
        store.upsert_rollup(conn, ptype, pkey, "s", "[]", None, 1)
    assert date(2024, 1, 15).isocalendar().week == 3

    paths[0].write_text("A completely rewritten entry.", encoding="utf-8")
    ingester.run_backfill(cfg, conn, client=fake_client)
    stale = {
        (r["period_type"], r["period_key"])
        for r in conn.execute("SELECT * FROM rollups WHERE stale = 1")
    }
    assert stale == {("week", "2024-W03"), ("month", "2024-01"), ("year", "2024")}


def test_matches_glob_root_and_nested(tmp_path):
    cfg = make_config(tmp_path)
    vault = cfg.vault.path
    (vault / "sub").mkdir()
    assert ingester._matches_glob(vault / "2024-01-01.md", cfg)
    assert ingester._matches_glob(vault / "sub" / "2024-01-02.md", cfg)
    assert not ingester._matches_glob(vault / "notes.txt", cfg)
    assert not ingester._matches_glob(tmp_path / "outside.md", cfg)


def test_ignore_patterns_skip_bare_name_anywhere(tmp_path):
    cfg = make_config(
        tmp_path,
        ignore_patterns=[".stversions", ".obsidian", "*.tmp.md"],
    )
    vault = cfg.vault.path
    assert ingester._is_ignored(vault / ".obsidian" / "workspace.md", cfg)
    assert ingester._is_ignored(
        vault / "sub" / ".stversions" / "2024-01-01.md", cfg
    )
    assert ingester._is_ignored(vault / "2024-01-01.tmp.md", cfg)
    assert not ingester._is_ignored(vault / "2024-01-01.md", cfg)
    # matches_glob and the backfill walk both honor it
    assert not ingester._matches_glob(vault / ".obsidian" / "workspace.md", cfg)


def test_backfill_skips_ignored_folders(tmp_path, fake_client):
    cfg = make_config(
        tmp_path,
        ignore_patterns=[".stversions", ".stfolder", ".smart-env", ".obsidian"],
    )
    vault = cfg.vault.path
    copy_fixtures(vault, ["2024-01-15.md"])
    for junk_dir in (".obsidian", ".stfolder", ".smart-env", ".stversions"):
        d = vault / junk_dir
        d.mkdir()
        (d / "2024-09-09.md").write_text("junk note", encoding="utf-8")
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts == {"indexed": 1, "skipped": 0, "error": 0, "dropped": 0}
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 1


def test_matches_glob_reaches_deeply_nested_notes(tmp_path):
    cfg = make_config(tmp_path)
    vault = cfg.vault.path
    assert ingester._matches_glob(vault / "a" / "b" / "c" / "2024-01-01.md", cfg)


def test_matches_glob_honours_a_scoped_glob(tmp_path):
    cfg = make_config(tmp_path, daily_note_glob="Journal/**/*.md")
    vault = cfg.vault.path
    assert ingester._matches_glob(vault / "Journal" / "2024-01-01.md", cfg)
    assert ingester._matches_glob(vault / "Journal" / "2024" / "01-01.md", cfg)
    assert not ingester._matches_glob(vault / "Archive" / "2024-01-01.md", cfg)


def test_watch_and_backfill_agree_on_which_files_count(tmp_path):
    # watch and backfill have to agree on which files count
    cfg = make_config(tmp_path, ignore_patterns=[".obsidian"])
    vault = cfg.vault.path
    for rel in ("2024-01-01.md", "a/2024-01-02.md", "a/b/2024-01-03.md"):
        path = vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("note", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "2024-01-04.md").write_text("junk", encoding="utf-8")
    (vault / "notes.txt").write_text("not markdown", encoding="utf-8")

    walked = set(ingester._vault_files(cfg))
    watched = {
        p for p in vault.rglob("*") if p.is_file() and ingester._matches_glob(p, cfg)
    }
    assert walked == watched
    assert len(walked) == 3


def test_deleted_file_removes_entries_and_marks_rollups_stale(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    paths = copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    for ptype, pkey in (("week", "2024-W03"), ("month", "2024-01"), ("year", "2024")):
        store.upsert_rollup(conn, ptype, pkey, "s", "[]", None, 1)

    paths[0].unlink()
    ingester._apply_event(str(paths[0]), "delete", cfg, conn, fake_client)

    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM rollups WHERE stale = 1").fetchone()["c"] == 3


def test_unreadable_file_is_recorded_not_raised(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    missing = cfg.vault.path / "gone.md"
    assert ingester.process_file(missing, cfg, conn, fake_client) == "error"
    row = conn.execute(
        "SELECT status, error_message FROM files WHERE path = ?", (str(missing),)
    ).fetchone()
    assert row["status"] == "error"
    assert "unreadable" in row["error_message"]


def _notes_vault(tmp_path):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    cfg.notes.summarize_over_chars = 100
    copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    copy_note_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    return cfg, conn


def test_reference_notes_are_indexed_and_become_the_entities(tmp_path, fake_client):
    cfg, conn = _notes_vault(tmp_path)
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts == {"indexed": 4, "skipped": 0, "error": 0, "dropped": 0}
    kinds = {r["title"]: r["kind"] for r in conn.execute("SELECT title, kind FROM notes")}
    assert kinds == {
        "Sara Miller": "people",
        "Bear Mountain": "locations",
        "garden-project": "topics-things",
    }
    entry_id = conn.execute("SELECT id FROM entries").fetchone()["id"]
    linked = {(r["name"], r["type"]) for r in store.entities_for_entry(conn, entry_id)}
    assert linked == set(kinds.items())
    assert conn.execute("SELECT COUNT(*) c FROM notes_vec").fetchone()["c"] == 3
    assert ingester.run_backfill(cfg, conn, client=fake_client)["skipped"] == 4


def test_short_notes_skip_the_model_and_long_ones_are_summarized(tmp_path, fake_client):
    cfg, conn = _notes_vault(tmp_path)
    ingester.run_backfill(cfg, conn, client=fake_client)
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM notes")}
    assert rows["Sara Miller"]["summary"] == "A canned note summary."
    assert rows["Sara Miller"]["model_used"] == "fake-fast"
    assert rows["Bear Mountain"]["summary"] == (
        "State park an hour north with a steep summit trail."
    )
    assert rows["Bear Mountain"]["model_used"] is None
    assert rows["garden-project"]["summary"] is None
    schemas = [schema for _, schema in fake_client.structured_calls]
    assert schemas.count("NoteSummary") == 1


def test_notes_are_indexed_before_entries(tmp_path, fake_client):
    cfg, conn = _notes_vault(tmp_path)
    ingester.run_backfill(cfg, conn, client=fake_client)
    order = [schema for _, schema in fake_client.structured_calls]
    assert order.index("NoteSummary") < order.index("Extraction")


def test_notes_outside_the_daily_glob_are_still_found(tmp_path):
    cfg = make_config(tmp_path, notes_paths=["Links"], daily_note_glob="Journal/**/*.md")
    vault = cfg.vault.path
    copy_note_fixtures(vault)
    assert "Sara Miller.md" in {p.name for p in ingester._vault_files(cfg)}
    assert ingester._matches_glob(vault / "Links" / "People" / "Sara Miller.md", cfg)
    assert not ingester._matches_glob(vault / "Links" / "People" / "photo.jpg", cfg)


def test_deleting_a_note_removes_it_from_the_entities(tmp_path, fake_client):
    cfg, conn = _notes_vault(tmp_path)
    ingester.run_backfill(cfg, conn, client=fake_client)
    sara = cfg.vault.path / "Links" / "People" / "Sara Miller.md"
    sara.unlink()
    ingester._apply_event(str(sara), "delete", cfg, conn, fake_client)
    names = {r["name"] for r in store.top_entities(conn, None, None)}
    assert "Sara Miller" not in names
    assert "Bear Mountain" in names


def test_a_file_that_moves_into_the_notes_paths_becomes_a_note(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    vault = cfg.vault.path
    (vault / "Links").mkdir()
    (vault / "Links" / "2024-03-01.md").write_text("A dated file in Links.", encoding="utf-8")
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 1

    as_notes = make_config(tmp_path, notes_paths=["Links"])
    counts = ingester.run_backfill(as_notes, conn, client=fake_client)
    assert counts["indexed"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0
    assert tuple(conn.execute("SELECT title, kind FROM notes").fetchone()) == (
        "2024-03-01",
        "note",
    )


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


def _connect(cfg):
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    return conn


def test_backfill_forgets_files_deleted_while_nothing_watched(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    kept, gone = copy_fixtures(cfg.vault.path)
    conn = _connect(cfg)
    ingester.run_backfill(cfg, conn, client=fake_client)
    store.upsert_rollup(conn, "month", "2024-01", "s", "[]", None, 2)

    gone.unlink()
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts == {"indexed": 0, "skipped": 1, "error": 0, "dropped": 1}
    assert store.tracked_paths(conn) == [str(kept)]
    assert [r["entry_date"] for r in conn.execute("SELECT entry_date FROM entries")] == [
        "2024-01-15"
    ]
    assert conn.execute("SELECT stale FROM rollups").fetchone()["stale"] == 1


def test_backfill_forgets_files_that_are_ignored_now(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    vault = cfg.vault.path
    copy_fixtures(vault, ["2024-01-15.md"])
    (vault / "Drafts").mkdir()
    (vault / "Drafts" / "2024-01-20.md").write_text("Half a thought.", encoding="utf-8")
    conn = _connect(cfg)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert _count(conn, "entries") == 2

    ignoring = make_config(tmp_path, ignore_patterns=["Drafts"])
    assert ingester.run_backfill(ignoring, conn, client=fake_client)["dropped"] == 1
    assert _count(conn, "entries") == 1


def test_a_walk_that_finds_nothing_forgets_nothing(tmp_path, fake_client):
    # more likely an unmounted drive than a deleted journal
    cfg = make_config(tmp_path)
    [path] = copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    conn = _connect(cfg)
    ingester.run_backfill(cfg, conn, client=fake_client)
    path.unlink()
    assert ingester.run_backfill(cfg, conn, client=fake_client)["dropped"] == 0
    assert _count(conn, "entries") == 1


def test_files_indexed_from_another_vault_stay_while_they_exist(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    other = tmp_path / "other"
    other.mkdir()
    shutil.copy(FIXTURES / "2024-01-16.md", other / "2024-01-16.md")
    conn = _connect(cfg)
    elsewhere = cfg.model_copy(update={"vault": cfg.vault.model_copy(update={"path": other})})
    ingester.run_backfill(elsewhere, conn, client=fake_client)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert len(store.tracked_paths(conn)) == 2


def test_a_missing_vault_is_reported_before_any_model_call(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    cfg.vault.path.rmdir()
    conn = _connect(cfg)
    with pytest.raises(ConfigError, match="no folder at"):
        ingester.run_backfill(cfg, conn, client=fake_client)
    assert fake_client.embed_calls == []


def test_a_failed_extraction_keeps_the_previous_entries(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    [path] = copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    conn = _connect(cfg)
    ingester.run_backfill(cfg, conn, client=fake_client)

    def broken(model, messages):
        raise StructuredOutputError("gave up")

    fake_client.structured["Extraction"] = broken
    path.write_text("An edited entry.", encoding="utf-8")
    assert ingester.run_backfill(cfg, conn, client=fake_client)["error"] == 1
    [row] = conn.execute("SELECT raw_text FROM entries").fetchall()
    assert row["raw_text"].startswith("Went hiking")


def test_a_failed_embedding_keeps_the_previous_entries_and_vectors(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    [path] = copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    conn = _connect(cfg)
    ingester.run_backfill(cfg, conn, client=fake_client)
    real_embed = fake_client.embed

    def embed(model, text, options=None):
        if "edited" in text:
            raise ModelRequestError("embedding blew up")
        return real_embed(model, text, options)

    fake_client.embed = embed
    path.write_text("An edited entry.", encoding="utf-8")
    assert ingester.run_backfill(cfg, conn, client=fake_client)["error"] == 1
    [row] = conn.execute("SELECT id, raw_text FROM entries").fetchall()
    assert row["raw_text"].startswith("Went hiking")
    assert _count(conn, "entries_vec") == 1


def test_the_models_are_told_where_each_file_sits(tmp_path, fake_client):
    cfg, conn = _notes_vault(tmp_path)
    trip = cfg.vault.path / "Trips" / "2024-01-20.md"
    trip.parent.mkdir()
    trip.write_text("Packed for the coast.", encoding="utf-8")
    prompts: dict[str, list[str]] = {"Extraction": [], "NoteSummary": []}

    def extraction(model, messages):
        prompts["Extraction"].append(messages[-1]["content"])
        return Extraction(summary="ok")

    def note_summary(model, messages):
        prompts["NoteSummary"].append(messages[-1]["content"])
        return NoteSummary(summary="ok")

    fake_client.structured.update(Extraction=extraction, NoteSummary=note_summary)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert (
        "Journal entry dated 2024-01-20\nFile: Trips/2024-01-20.md\n\nPacked for the coast."
        in prompts["Extraction"]
    )
    [sara] = prompts["NoteSummary"]
    assert sara.startswith("Sara Miller (people)\nFile: Links/People/Sara Miller.md\n")
    assert any(
        text.startswith("Bear Mountain (locations)\nFile: Links/Locations/Bear Mountain.md")
        for text in fake_client.embed_calls
    )


def _trip_folder(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    trip = cfg.vault.path / "Trips" / "Lisbon"
    trip.mkdir(parents=True)
    copy_fixtures(trip)
    home = cfg.vault.path / "2024-03-01.md"
    home.write_text("Back home.", encoding="utf-8")
    conn = _connect(cfg)
    ingester.run_backfill(cfg, conn, client=fake_client)
    return cfg, conn, trip, home


def _drain(handler, cfg, conn, fake_client):
    handler.pending = {path: (0.0, kind) for path, (_, kind) in handler.pending.items()}
    assert ingester._apply_due(handler, cfg, conn, fake_client) is None


@pytest.mark.parametrize("event", [FileDeletedEvent, DirDeletedEvent])
def test_watch_forgets_every_file_in_a_folder_that_left(tmp_path, fake_client, event):
    # Windows (e.g. the Recycle Bin) sends one FileDeletedEvent for the folder
    # and nothing for the files in it
    cfg, conn, trip, home = _trip_folder(tmp_path, fake_client)
    assert len(store.tracked_paths(conn)) == 3
    shutil.move(trip.parent, tmp_path / "Recycle")
    handler = ingester._DebounceHandler(cfg)
    handler.on_deleted(event(str(trip.parent)))
    _drain(handler, cfg, conn, fake_client)
    assert store.tracked_paths(conn) == [str(home)]
    assert _count(conn, "entries") == 1


def test_ignored_paths_that_vanish_queue_nothing(tmp_path, fake_client):
    cfg = make_config(tmp_path, ignore_patterns=[".obsidian"])
    handler = ingester._DebounceHandler(cfg)
    handler.on_deleted(FileDeletedEvent(str(cfg.vault.path / ".obsidian" / "workspace.json")))
    assert handler.pending == {}


def test_watch_follows_a_folder_renamed_inside_the_vault(tmp_path, fake_client):
    cfg, conn, trip, home = _trip_folder(tmp_path, fake_client)
    renamed = trip.with_name("Porto")
    trip.rename(renamed)
    handler = ingester._DebounceHandler(cfg)
    handler.on_moved(DirMovedEvent(str(trip), str(renamed)))
    _drain(handler, cfg, conn, fake_client)
    assert sorted(store.tracked_paths(conn)) == sorted(
        [str(renamed / "2024-01-15.md"), str(renamed / "2024-01-16.md"), str(home)]
    )
    assert _count(conn, "entries") == 3


def test_a_folder_moved_into_the_vault_is_indexed(tmp_path, fake_client):
    cfg, conn, _, _ = _trip_folder(tmp_path, fake_client)
    arrived = cfg.vault.path / "Inbox"
    arrived.mkdir()
    (arrived / "2024-04-01.md").write_text("Found in a drawer.", encoding="utf-8")
    (arrived / "scan.jpg").write_bytes(b"\xff\xd8")
    handler = ingester._DebounceHandler(cfg)
    handler.on_created(DirCreatedEvent(str(arrived)))
    assert set(handler.pending) == {str(arrived / "2024-04-01.md")}
    _drain(handler, cfg, conn, fake_client)
    assert _count(conn, "entries") == 4


def test_an_unplugged_vault_forgets_nothing_while_watching(tmp_path, fake_client):
    cfg, conn, trip, _ = _trip_folder(tmp_path, fake_client)
    handler = ingester._DebounceHandler(cfg)
    handler.on_deleted(FileDeletedEvent(str(cfg.vault.path)))
    assert handler.pending == {}

    handler.on_deleted(FileDeletedEvent(str(trip)))
    unplugged = cfg.vault.path.with_name("elsewhere")
    cfg.vault.path.rename(unplugged)
    _drain(handler, cfg, conn, fake_client)
    assert len(store.tracked_paths(conn)) == 3


def test_folders_outside_the_vault_queue_nothing(tmp_path, fake_client):
    cfg, _, _, _ = _trip_folder(tmp_path, fake_client)
    handler = ingester._DebounceHandler(cfg)
    handler.on_deleted(DirDeletedEvent(str(tmp_path / "elsewhere")))
    outside = tmp_path / "incoming"
    outside.mkdir()
    (outside / "2024-02-01.md").write_text("Not in the vault.", encoding="utf-8")
    handler.on_created(DirCreatedEvent(str(outside)))
    assert handler.pending == {}


def test_watch_keeps_changes_queued_while_ollama_is_down(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    first, second = copy_fixtures(cfg.vault.path)
    conn = _connect(cfg)
    db_mod.ensure_vec_table(conn, EMBED_DIM, cfg.ollama.embed_model)
    handler = ingester._DebounceHandler(cfg)
    handler.pending = {str(first): (0.0, "change"), str(second): (0.0, "change")}

    def down(model, messages):
        raise OllamaUnavailableError("Ollama is down")

    fake_client.structured["Extraction"] = down
    outage = ingester._apply_due(handler, cfg, conn, fake_client)
    assert isinstance(outage, OllamaUnavailableError)
    assert set(handler.pending) == {str(first), str(second)}

    fake_client.structured["Extraction"] = Extraction(summary="back")
    handler.pending = {path: (0.0, kind) for path, (_, kind) in handler.pending.items()}
    assert ingester._apply_due(handler, cfg, conn, fake_client) is None
    assert handler.pending == {}
    assert _count(conn, "entries") == 2
