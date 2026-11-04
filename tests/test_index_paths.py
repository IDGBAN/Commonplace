import os
from contextlib import closing

import pytest
from conftest import copy_fixtures, copy_note_fixtures, invoke_cli, make_config

from journal_analyzer import db as db_mod
from journal_analyzer import ingester, store
from journal_analyzer.models import ConfigError


def _flat(output: str) -> str:
    return " ".join(output.split())


@pytest.fixture
def indexed(tmp_path, fake_client):
    cfg = make_config(tmp_path, notes_paths=["Links"], ignore_patterns=["Drafts"])
    copy_fixtures(cfg.vault.path)
    copy_note_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    fake_client.structured_calls.clear()
    yield cfg, conn
    conn.close()


def _summary(conn, day):
    return conn.execute("SELECT summary FROM entries WHERE entry_date = ?", (day,)).fetchone()[0]


def test_only_the_named_file_is_read_again(indexed, fake_client):
    cfg, conn = indexed
    day = cfg.vault.path / "2024-01-15.md"
    day.write_text(day.read_text(encoding="utf-8") + "\nOne more line.\n", encoding="utf-8")
    other = cfg.vault.path / "2024-01-16.md"
    other.write_text(other.read_text(encoding="utf-8") + "\nAlso edited.\n", encoding="utf-8")

    targets = ingester.resolve_targets(cfg, conn, [day.relative_to(cfg.vault.path)])
    counts = ingester.run_backfill(cfg, conn, client=fake_client, targets=targets)

    assert counts == {"indexed": 1, "private": 0, "skipped": 0, "error": 0, "dropped": 0}
    texts = dict(conn.execute("SELECT entry_date, raw_text FROM entries").fetchall())
    assert "One more line." in texts["2024-01-15"]
    assert "Also edited." not in texts["2024-01-16"]


def test_an_unchanged_file_is_skipped_unless_forced(indexed, fake_client):
    cfg, conn = indexed
    targets = ingester.resolve_targets(cfg, conn, [cfg.vault.path / "2024-01-15.md"])
    assert ingester.run_backfill(cfg, conn, client=fake_client, targets=targets)["skipped"] == 1
    assert fake_client.structured_calls == []

    counts = ingester.run_backfill(cfg, conn, force=True, client=fake_client, targets=targets)
    assert counts["indexed"] == 1
    assert ("fake-fast", "Extraction") in fake_client.structured_calls


def test_a_folder_names_the_files_in_it(indexed):
    cfg, conn = indexed
    targets = ingester.resolve_targets(cfg, conn, [cfg.vault.path / "Links" / "People"])
    assert [p.name for p in targets.files] == ["John Doe.md"]


def test_a_path_from_the_current_folder_works(indexed, monkeypatch):
    cfg, conn = indexed
    monkeypatch.chdir(cfg.vault.path / "Links")
    targets = ingester.resolve_targets(cfg, conn, [ingester.Path("People/John Doe.md")])
    assert [p.name for p in targets.files] == ["John Doe.md"]


def test_a_deleted_file_is_dropped_from_the_cache(indexed, fake_client):
    cfg, conn = indexed
    day = cfg.vault.path / "2024-01-16.md"
    day.unlink()
    targets = ingester.resolve_targets(cfg, conn, [day])
    assert targets.files == [] and targets.gone == [str(day)]

    counts = ingester.run_backfill(cfg, conn, client=fake_client, targets=targets)
    assert counts["dropped"] == 1
    assert str(day) not in store.tracked_paths(conn)
    assert len(store.tracked_paths(conn)) == 4


def test_a_deleted_folder_drops_what_was_in_it(indexed):
    cfg, conn = indexed
    folder = cfg.vault.path / "Links" / "Locations"
    for f in folder.iterdir():
        f.unlink()
    folder.rmdir()
    targets = ingester.resolve_targets(cfg, conn, [folder])
    assert [os.path.basename(k) for k in targets.gone] == ["Bear Mountain.md"]


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda vault: vault.parent / "elsewhere.md", "isn't in the vault"),
        (lambda vault: vault / "Drafts" / "2024-02-01.md", "ignore_patterns"),
        (lambda vault: vault / "nothing-here.md", "There's no file or folder"),
        (lambda vault: vault / "picture.png", "daily_note_glob"),
        (lambda vault: vault / "Drafts", "Nothing in Drafts"),
    ],
)
def test_paths_a_full_run_wouldnt_index_are_refused(indexed, make, message):
    cfg, conn = indexed
    vault = cfg.vault.path
    (vault.parent / "elsewhere.md").write_text("x", encoding="utf-8")
    (vault / "Drafts").mkdir()
    (vault / "Drafts" / "2024-02-01.md").write_text("x", encoding="utf-8")
    (vault / "picture.png").write_bytes(b"\x89PNG")
    with pytest.raises(ConfigError, match=message):
        ingester.resolve_targets(cfg, conn, [make(vault)])


def test_the_dry_run_only_plans_the_named_files(indexed, tmp_path):
    cfg, conn = indexed
    (cfg.vault.path / "2024-01-16.md").write_text("Edited.", encoding="utf-8")
    (cfg.vault.path / "2024-02-01.md").write_text("New.", encoding="utf-8")
    targets = ingester.resolve_targets(cfg, conn, [cfg.vault.path / "2024-02-01.md"])
    plan = ingester.plan_backfill(cfg, conn, targets=targets)
    assert [(f.path.name, f.status) for f in plan.files] == [("2024-02-01.md", "new")]
    assert plan.dropped == [] and plan.targeted


def test_cli_indexes_the_named_file(indexed, tmp_path, fake_client, monkeypatch):
    cfg, conn = indexed
    conn.close()
    monkeypatch.setattr(ingester, "OllamaClient", lambda c: fake_client)
    (cfg.vault.path / "2024-02-01.md").write_text("A new day.", encoding="utf-8")
    (cfg.vault.path / "2024-02-02.md").write_text("Another.", encoding="utf-8")

    result = invoke_cli(tmp_path, cfg, "index", "--dry-run", "2024-02-01.md")
    assert result.exit_code == 0, result.output
    assert "Files named : 1 (1 journal, 0 notes)" in _flat(result.output)

    result = invoke_cli(tmp_path, cfg, "index", "2024-02-01.md")
    assert result.exit_code == 0, result.output
    assert "Done: 1 indexed, 0 skipped, 0 errored." in _flat(result.output)
    with closing(db_mod.connect(cfg)) as check:
        days = [r[0] for r in check.execute("SELECT entry_date FROM entries ORDER BY entry_date")]
    assert days == ["2024-01-15", "2024-01-16", "2024-02-01"]


def test_cli_refuses_a_path_outside_the_vault(indexed, tmp_path):
    cfg, conn = indexed
    conn.close()
    result = invoke_cli(tmp_path, cfg, "index", str(tmp_path / "config.toml"))
    assert result.exit_code == 1
    assert "isn't in the vault" in _flat(result.output)
