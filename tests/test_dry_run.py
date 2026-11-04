from datetime import date, timedelta

from conftest import copy_fixtures, copy_note_fixtures, invoke_cli, make_config

from journal_analyzer import db as db_mod
from journal_analyzer import ingester


def _flat(output: str) -> str:
    return " ".join(output.split())


def _indexed(tmp_path, fake_client, **overrides):
    cfg = make_config(tmp_path, notes_paths=["Links"], **overrides)
    copy_fixtures(cfg.vault.path)
    copy_note_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    return cfg, conn


def _status(plan):
    return {f.path.name: f.status for f in plan.files}


def test_without_a_cache_everything_is_new(tmp_path):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    copy_fixtures(cfg.vault.path)
    copy_note_fixtures(cfg.vault.path)
    plan = ingester.plan_backfill(cfg, None)
    assert set(_status(plan).values()) == {"new"}
    assert len(plan.files) == 5
    extractions, summaries, embeddings = plan.model_calls()
    assert extractions == 2
    # John's note is past summarize_over_chars, the others aren't; an empty note still embeds
    assert summaries == 1
    assert embeddings == 5


def test_after_indexing_only_changes_count(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)
    plan = ingester.plan_backfill(cfg, conn)
    assert set(_status(plan).values()) == {"unchanged"}
    assert plan.model_calls() == (0, 0, 0)

    (cfg.vault.path / "2024-01-16.md").write_text("Edited.", encoding="utf-8")
    (cfg.vault.path / "2024-02-01.md").write_text("New one.", encoding="utf-8")
    (cfg.vault.path / "2024-01-15.md").unlink()
    plan = ingester.plan_backfill(cfg, conn)
    statuses = _status(plan)
    assert statuses["2024-01-16.md"] == "changed"
    assert statuses["2024-02-01.md"] == "new"
    assert [p.split("\\")[-1].split("/")[-1] for p in plan.dropped] == ["2024-01-15.md"]
    assert plan.model_calls() == (2, 0, 2)
    # nothing was written
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2


def test_force_counts_every_file(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)
    plan = ingester.plan_backfill(cfg, conn, force=True)
    assert set(_status(plan).values()) == {"forced"}


def test_problems_split_files_and_future_dates_are_found(tmp_path):
    cfg = make_config(tmp_path, split_heading_regex=r"^## \d{4}-\d{2}-\d{2}")
    vault = cfg.vault.path
    (vault / "random-notes.md").write_text("No date anywhere.", encoding="utf-8")
    (vault / "2024-02-01.md").write_text(
        "## 2024-02-01\nOne.\n## 2024-02-02\nTwo.\n", encoding="utf-8"
    )
    later = date.today() + timedelta(days=400)
    (vault / f"{later.isoformat()}.md").write_text("Typo year.", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "workspace.md").write_text("x", encoding="utf-8")
    cfg.vault.ignore_patterns = [".obsidian"]

    plan = ingester.plan_backfill(cfg, None)
    assert plan.ignored == 1
    by_name = {f.path.name: f for f in plan.files}
    assert "date" in (by_name["random-notes.md"].problem or "").lower()
    assert len(by_name["2024-02-01.md"].entries) == 2
    assert by_name[f"{later.isoformat()}.md"].entries[0].entry_date == later


def test_cli_dry_run_never_creates_a_cache(tmp_path, config):
    copy_fixtures(config.vault.path)
    result = invoke_cli(tmp_path, config, "index", "--dry-run")
    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert "Would index : 2 new, 0 changed, 0 errored last time" in output
    assert "Model calls : at least 4 (2 extractions" in output
    assert not config.db.path.exists()


def test_cli_dry_run_reads_an_existing_cache_without_writing(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)
    conn.close()
    before = cfg.db.path.read_bytes()
    (cfg.vault.path / "oops.md").write_text("No date here.", encoding="utf-8")
    result = invoke_cli(tmp_path, cfg, "index", "--dry-run")
    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert "1 new" in output and "Files that would fail" in output and "oops.md" in output
    assert cfg.db.path.read_bytes() == before
