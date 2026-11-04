from datetime import date

from conftest import invoke_cli, make_config
from typer.testing import CliRunner

from journal_analyzer import cli, db, ingester, parser, photos, query, store
from journal_analyzer.models import Extraction, ModelRequestError, ParsedEntry


def test_entries_know_the_images_they_link(tmp_path):
    cfg = make_config(tmp_path)
    path = cfg.vault.path / "2024-03-04.md"
    path.write_text(
        "Beach day ![[beach.jpg|300]] and [[Links/!Files/sunset.PNG]], ![a map](maps/route%20one.webp), "
        "![[diagram.svg]], ![remote](https://example.com/x.jpg) and [[John Doe]].",
        encoding="utf-8",
    )
    [entry] = parser.parse_file(path, cfg)
    assert entry.images == ["beach.jpg", "Links/!Files/sunset.PNG", "maps/route one.webp"]
    assert entry.links == ["john doe"]


def test_split_files_give_each_section_its_own_images(tmp_path):
    cfg = make_config(tmp_path, split_heading_regex=r"^## \d{4}-\d{2}-\d{2}")
    path = cfg.vault.path / "2024-03.md"
    path.write_text("## 2024-03-01\nSea ![[sea.jpg]]\n## 2024-03-02\nHills ![[hills.jpg]]", encoding="utf-8")
    first, second = parser.parse_file(path, cfg)
    assert (first.images, second.images) == (["sea.jpg"], ["hills.jpg"])


def _vault(tmp_path):
    cfg = make_config(tmp_path)
    cfg.ollama.vision_model = "fake-vision"
    root = cfg.vault.path
    for rel in ["Links/!Files/beach.jpg", "Archive/old/2020/beach.jpg", ".trash/hidden.jpg", "sunset.png"]:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(rel.encode())
    return cfg


def test_images_are_found_the_way_obsidian_finds_them(tmp_path):
    cfg = _vault(tmp_path)
    root = cfg.vault.path
    assert photos.find(cfg, "beach.jpg") == root / "Links" / "!Files" / "beach.jpg"
    assert photos.find(cfg, "Archive/old/2020/beach.jpg") == root / "Archive" / "old" / "2020" / "beach.jpg"
    assert photos.find(cfg, "hidden.jpg") is None
    assert photos.find(cfg, "SUNSET.png") == root / "sunset.png"


def test_images_outside_the_vault_are_never_read(tmp_path):
    cfg = _vault(tmp_path)
    outside = tmp_path / "outside" / "private.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"jpg")
    assert photos.find(cfg, "../outside/private.jpg") is None
    assert photos.find(cfg, outside.as_posix()) is None


def test_only_so_many_photos_and_only_with_a_vision_model(tmp_path):
    cfg = _vault(tmp_path)
    entry = ParsedEntry(date(2024, 3, 4), "x", "x", images=["beach.jpg", "sunset.png", "missing.jpg"])
    assert [name for name, _ in photos.to_describe(entry, cfg)] == ["beach.jpg", "sunset.png"]
    cfg.indexing.photos_per_entry = 1
    assert [name for name, _ in photos.to_describe(entry, cfg)] == ["beach.jpg"]
    cfg.ollama.vision_model = ""
    assert photos.to_describe(entry, cfg) == []


def test_indexing_describes_photos_and_reads_them_with_the_entry(tmp_path, fake_client):
    cfg = _vault(tmp_path)
    (cfg.vault.path / "2024-03-04.md").write_text("Lovely evening. ![[beach.jpg]]", encoding="utf-8")
    (cfg.vault.path / "2024-03-05.md").write_text("![[sunset.png]]", encoding="utf-8")
    seen = []

    def extract(model, messages):
        seen.append(messages[-1]["content"])
        return Extraction(summary="ok")

    fake_client.structured["Extraction"] = extract
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert [m for m, _ in fake_client.vision_calls] == ["fake-vision", "fake-vision"]
    evening = next(s for s in seen if "Lovely evening" in s)
    assert "Photos in this entry, as a vision model describes them:\n- beach.jpg: Two people" in evening
    # a day that's only a photo is still read
    assert any(s.startswith("Journal entry dated 2024-03-05") and "sunset.png" in s for s in seen)
    assert any("Two people on a sandy beach" in t for t in fake_client.embed_calls)
    day = conn.execute("SELECT id FROM entries WHERE entry_date = '2024-03-04'").fetchone()["id"]
    [photo] = store.photos_for_entries(conn, [day])[day]
    assert (photo["name"], photo["path"]) == ("beach.jpg", "Links/!Files/beach.jpg")
    assert query.keyword_search("sandy beach", conn).entries[0]["entry_date"] in ("2024-03-04", "2024-03-05")
    conn.close()

    show = invoke_cli(tmp_path, cfg, "show", "2024-03-04")
    assert "photo beach.jpg: Two people on a sandy beach at sunset." in " ".join(show.output.split())


def test_a_model_that_cant_read_images_is_skipped(tmp_path, fake_client):
    cfg = _vault(tmp_path)
    (cfg.vault.path / "2024-03-04.md").write_text("Out. ![[beach.jpg]] ![[sunset.png]]", encoding="utf-8")

    def refuse(model, image):
        raise ModelRequestError("this model doesn't take images")

    fake_client.vision_response = refuse
    conn = db.connect(cfg)
    db.init_schema(conn)
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts["indexed"] == 1 and counts["error"] == 0
    assert conn.execute("SELECT COUNT(*) FROM entry_photos").fetchone()[0] == 0
    conn.close()


def test_changing_the_vision_model_reindexes_only_files_with_photos(tmp_path, fake_client):
    cfg = _vault(tmp_path)
    cfg.ollama.vision_model = ""
    (cfg.vault.path / "2024-03-04.md").write_text("Out. ![[beach.jpg]]", encoding="utf-8")
    (cfg.vault.path / "2024-03-05.md").write_text("No photos today.", encoding="utf-8")
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert fake_client.vision_calls == []
    cfg.ollama.vision_model = "fake-vision"
    plan = ingester.plan_backfill(cfg, conn)
    assert plan.photo_calls() == 0  # nothing is queued until the next real run
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert (counts["indexed"], counts["skipped"]) == (1, 1)
    assert len(fake_client.vision_calls) == 1
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts["indexed"] == 0
    conn.close()


def test_dry_run_counts_photo_descriptions(tmp_path):
    cfg = _vault(tmp_path)
    (cfg.vault.path / "2024-03-04.md").write_text("![[beach.jpg]] ![[sunset.png]] ![[nope.jpg]]", encoding="utf-8")
    assert ingester.plan_backfill(cfg, None).photo_calls() == 2


def test_doctor_checks_the_vision_model(tmp_path, monkeypatch, fake_client):
    cfg = _vault(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text(
        f"[vault]\npath = '{cfg.vault.path.as_posix()}'\n[db]\npath = '{cfg.db.path.as_posix()}'\n"
        "[ollama]\nfast_model = 'fake-fast'\nprecise_model = 'fake-precise'\n"
        "embed_model = 'fake-embed'\nvision_model = 'fake-vision'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("journal_analyzer.cli.OllamaClient", lambda config: fake_client)
    result = CliRunner().invoke(cli.app, ["doctor", "--config", str(path)])
    assert result.exit_code == 1
    assert "MISSING vision_model 'fake-vision'" in result.output
