from datetime import date

import pytest
from conftest import invoke_cli, make_config

from journal_analyzer import db as db_mod
from journal_analyzer import fields, ingester, parser, store
from journal_analyzer.models import DateParseError
from journal_analyzer.profile import ScannedFacts


def _flat(output: str) -> str:
    return " ".join(output.split())


def _write(cfg, name, text):
    path = cfg.vault.path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Before %%hidden%% after", "Before  after"),
        ("One\n%%\nmulti\nline\n%%\nTwo", "One\n\nTwo"),
        ("Visible %%never closed\nstill hidden", "Visible "),
        ("No comments here", "No comments here"),
    ],
)
def test_strip_comments(text, kept):
    assert parser.strip_comments(text) == kept


def test_comments_are_left_out_unless_switched_off(tmp_path):
    cfg = make_config(tmp_path)
    path = _write(cfg, "2024-01-01.md", "Walked. %%note to self: call John%% Slept.")
    [entry] = parser.parse_file(path, cfg)
    assert "call John" not in entry.raw_text
    cfg.vault.strip_comments = False
    [entry] = parser.parse_file(path, cfg)
    assert "call John" in entry.raw_text


@pytest.mark.parametrize(
    "text",
    [
        "---\nprivate: true\n---\nSecret.",
        "---\ntags: [private]\n---\nSecret.",
        "Secret. #private",
        "---\nprivate: yes\n---\nNo date in the name needed.",
    ],
)
def test_private_files_have_no_entries(tmp_path, text):
    cfg = make_config(tmp_path)
    assert parser.parse_file(_write(cfg, "2024-01-01.md", text), cfg) == []


def test_an_undated_private_file_is_not_an_error(tmp_path):
    cfg = make_config(tmp_path)
    assert parser.parse_file(_write(cfg, "draft.md", "Secret. #private"), cfg) == []
    with pytest.raises(DateParseError):
        parser.parse_file(_write(cfg, "draft2.md", "Not secret."), cfg)


def test_private_tag_can_be_changed_or_switched_off(tmp_path):
    cfg = make_config(tmp_path, private_tag="#Secret")
    assert cfg.vault.private_tag == "secret"
    path = _write(cfg, "2024-01-01.md", "Hmm. #private-ish #secret")
    assert parser.parse_file(path, cfg) == []
    cfg.vault.private_tag = ""
    assert len(parser.parse_file(path, cfg)) == 1
    assert parser.parse_file(_write(cfg, "2024-01-02.md", "---\nprivate: true\n---\nx"), cfg) == []


def test_split_files_drop_only_their_private_sections(tmp_path):
    cfg = make_config(tmp_path, split_heading_regex=r"^## \d{4}-\d{2}-\d{2}")
    path = _write(cfg, "log.md", "## 2024-02-01\nOpen.\n## 2024-02-02\nClosed. #private\n## bad\nAlso #private\n")
    assert [e.entry_date for e in parser.parse_file(path, cfg)] == [date(2024, 2, 1)]
    only_private = _write(cfg, "log2.md", "## 2024-02-03\nA #private\n## 2024-02-04\nB #private\n")
    assert parser.parse_file(only_private, cfg) == []


def test_private_notes(tmp_path):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    path = _write(cfg, "Links/People/Ex.md", "---\nprivate: true\n---\nLong story.")
    note = parser.parse_note(path, "people", cfg)
    assert note.private and "private" not in note.properties


def test_private_key_is_not_a_metric(config):
    assert fields.entry_metrics({"private": "false", "sleep": "7"}, config) == {"sleep": 7.0}


def _indexed(cfg, fake_client):
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    return conn, counts


def _status(conn, path):
    return store.get_file_record(conn, str(path))["status"]


def test_private_files_never_reach_a_model(tmp_path, fake_client):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    secret = _write(cfg, "2024-01-01.md", "Something only I should know. #private")
    _write(cfg, "Links/People/Ex.md", "---\nprivate: true\n---\n" + "Long story. " * 50)
    _write(cfg, "2024-01-02.md", "An ordinary day.")
    scanned = []

    def scan(model, messages):
        scanned.append(messages[-1]["content"])
        return ScannedFacts()

    fake_client.structured["ScannedFacts"] = scan
    conn, counts = _indexed(cfg, fake_client)
    assert counts["private"] == 2 and counts["indexed"] == 1
    assert [name for _, name in fake_client.structured_calls].count("Extraction") == 1
    assert scanned and all("only I should know" not in text for text in scanned)
    assert all("only I should know" not in t for t in fake_client.embed_calls)
    assert _status(conn, secret) == "private"
    assert store.stats(conn).private_files == 2
    assert ingester.run_backfill(cfg, conn, client=fake_client)["skipped"] == 3


def test_marking_an_entry_private_removes_it(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    path = _write(cfg, "2024-01-01.md", "A day.")
    conn, _ = _indexed(cfg, fake_client)
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    path.write_text("A day. #private", encoding="utf-8")
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entries_vec").fetchone()[0] == 0
    path.write_text("A day.", encoding="utf-8")
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert _status(conn, path) == "indexed"


def test_a_new_private_tag_hides_unchanged_files_without_models(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    secret = _write(cfg, "2024-01-01.md", "Diary. #secret")
    conn, _ = _indexed(cfg, fake_client)
    assert _status(conn, secret) == "indexed"
    calls = len(fake_client.structured_calls)
    cfg.vault.private_tag = "secret"
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert _status(conn, secret) == "private"
    assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    assert len(fake_client.structured_calls) == calls and counts["skipped"] == 1
    cfg.vault.private_tag = "private"
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts["indexed"] == 1 and _status(conn, secret) == "indexed"


def test_a_new_private_tag_survives_a_file_that_lost_its_date(tmp_path, fake_client):
    cfg = make_config(tmp_path, date_source="frontmatter")
    path = _write(cfg, "diary.md", "---\ndate: 2024-01-01\n---\nDiary.")
    conn, _ = _indexed(cfg, fake_client)
    assert _status(conn, path) == "indexed"
    path.write_text("Diary, date gone.", encoding="utf-8")
    cfg.vault.private_tag = "secret"
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts["error"] == 1 and _status(conn, path) == "error"


def test_a_cache_from_before_private_tags_gets_checked_once(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    secret = _write(cfg, "2024-01-01.md", "Diary. #private")
    cfg.vault.private_tag = ""
    conn, _ = _indexed(cfg, fake_client)
    conn.execute("DELETE FROM meta WHERE key = 'private_settings'")
    conn.commit()
    cfg.vault.private_tag = "private"
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert _status(conn, secret) == "private"


def test_switching_comments_reindexes_only_files_that_have_them(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    with_comment = _write(cfg, "2024-01-01.md", "Day. %%aside%%")
    _write(cfg, "2024-01-02.md", "Plain day.")
    conn, _ = _indexed(cfg, fake_client)
    cfg.vault.strip_comments = False
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert (counts["indexed"], counts["skipped"]) == (1, 1)
    text = conn.execute(
        "SELECT raw_text FROM entries WHERE file_path = ?", (str(with_comment),)
    ).fetchone()[0]
    assert "aside" in text


def test_dry_run_and_stats_count_private_files(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    _write(cfg, "2024-01-01.md", "Diary. #private")
    _write(cfg, "2024-01-02.md", "Plain day.")
    plan = ingester.plan_backfill(cfg, None)
    assert sum(1 for f in plan.files if f.private) == 1
    assert plan.model_calls() == (1, 0, 1)
    output = _flat(invoke_cli(tmp_path, cfg, "index", "--dry-run").output)
    assert "Private : 1 file left out, nothing read by a model" in output
    conn, _ = _indexed(cfg, fake_client)
    conn.close()
    assert "(0 errored, 1 private)" in _flat(invoke_cli(tmp_path, cfg, "stats").output)
