from conftest import make_config

from journal_analyzer import db, ingester, parser, store


def _vault(tmp_path, other_files=True):
    cfg = make_config(tmp_path, notes_paths=["Links"], daily_note_glob="[0-9][0-9][0-9][0-9]/**/*.md")
    cfg.notes.other_files = other_files
    root = cfg.vault.path
    files = {
        "2024/01/2024-01-15.md": "Climbed with [[Josh]] and talked about [[Garden]].",
        "Josh.md": "Josh from the climbing gym.",
        "Projects/Garden.md": "Raised beds.",
        "Links/People/John Doe.md": "Friend.",
        ".trash/Old.md": "Deleted note.",
        ".obsidian/templates/Daily.md": "{{date}}",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return cfg


def test_files_outside_the_glob_become_notes(tmp_path):
    cfg = _vault(tmp_path)
    root = cfg.vault.path
    assert parser.note_kind(root / "Josh.md", cfg) == "note"
    assert parser.note_kind(root / "Projects" / "Garden.md", cfg) == "projects"
    assert parser.note_kind(root / "Links" / "People" / "John Doe.md", cfg) == "people"
    assert parser.note_kind(root / "2024" / "01" / "2024-01-15.md", cfg) is None
    assert parser.note_kind(root / ".trash" / "Old.md", cfg) is None
    assert parser.note_kind(root / "picture.png", cfg) is None
    cfg.notes.other_files = False
    assert parser.note_kind(root / "Josh.md", cfg) is None


def test_index_reads_them_and_links_find_them(tmp_path, fake_client):
    cfg = _vault(tmp_path)
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    kinds = {r["title"]: r["kind"] for r in store.all_notes(conn)}
    assert kinds == {"Josh": "note", "Garden": "projects", "John Doe": "people"}
    entry = conn.execute("SELECT id FROM entries").fetchone()["id"]
    assert {r["name"] for r in store.entities_for_entry(conn, entry)} == {"Josh", "Garden"}

    cfg.notes.other_files = False
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts["dropped"] == 2
    assert {r["title"] for r in store.all_notes(conn)} == {"John Doe"}


def test_watch_picks_up_new_ones(tmp_path):
    cfg = _vault(tmp_path)
    root = cfg.vault.path
    assert ingester._matches_glob(root / "Ideas.md", cfg)
    assert not ingester._matches_glob(root / ".trash" / "Old.md", cfg)
    cfg.notes.other_files = False
    assert not ingester._matches_glob(root / "Ideas.md", cfg)


def test_dry_run_counts_them_as_notes(tmp_path):
    cfg = _vault(tmp_path)
    plan = ingester.plan_backfill(cfg, None)
    assert sum(1 for f in plan.files if f.kind is not None) == 3
    assert sum(1 for f in plan.files if f.kind is None) == 1
