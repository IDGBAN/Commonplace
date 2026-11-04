from datetime import date

import pytest
from conftest import add_entry, make_config

from journal_analyzer import db as db_mod
from journal_analyzer import fields, ingester, parser, store
from journal_analyzer.models import Extraction


def _with(config, **vault):
    return config.model_copy(update={"vault": config.vault.model_copy(update=vault)})


def _tags_config(config, **tags):
    return config.model_copy(update={"tags": config.tags.model_copy(update=tags)})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("7", 7.0),
        ("7.5", 7.5),
        ("7,5", 7.5),
        (["4"], 4.0),
        ("20", 10.0),  # clamped to the top of the scale
        ("0", 1.0),
    ],
)
def test_frontmatter_mood_on_the_same_scale(config, raw, expected):
    cfg = _with(config, frontmatter_mood_key="mood")
    assert fields.frontmatter_mood({"mood": raw}, cfg) == expected


def test_frontmatter_mood_is_rescaled(config):
    cfg = _with(config, frontmatter_mood_key="Mood", frontmatter_mood_min=1, frontmatter_mood_max=5)
    assert fields.frontmatter_mood({"mood": "1"}, cfg) == 1.0
    assert fields.frontmatter_mood({"mood": "5"}, cfg) == 10.0
    assert fields.frontmatter_mood({"mood": "3"}, cfg) == 5.5


def test_a_fraction_carries_its_own_scale(config):
    cfg = _with(config, frontmatter_mood_key="mood", frontmatter_mood_min=0)
    assert fields.frontmatter_mood({"mood": "3/5"}, cfg) == pytest.approx(6.4)


@pytest.mark.parametrize("raw", ["tired", "", "7-8", []])
def test_words_and_ranges_are_not_a_mood(config, raw):
    cfg = _with(config, frontmatter_mood_key="mood")
    assert fields.frontmatter_mood({"mood": raw}, cfg) is None


def test_frontmatter_mood_is_off_without_a_key(config):
    assert fields.frontmatter_mood({"mood": "7"}, config) is None


def test_frontmatter_tags_join_the_reading_and_ignored_ones_go(config):
    cfg = _tags_config(config, ignore=["daily"])
    tags = fields.final_tags(["work", "Daily"], {"tags": ["#Travel", "daily", "work"]}, cfg)
    assert tags == ["work", "travel"]


def test_frontmatter_tags_can_be_switched_off(config):
    cfg = _tags_config(config, from_frontmatter=False)
    assert fields.final_tags(["work"], {"tags": "travel"}, cfg) == ["work"]


def test_final_reading_keeps_the_summary(config):
    cfg = _with(config, frontmatter_mood_key="mood")
    reading = Extraction(mood_score=4, summary="A walk.", tags=["walks"])
    final = fields.final_reading(reading, {"mood": "9", "tags": "outdoors"}, cfg)
    assert (final.mood_score, final.summary, final.tags) == (9.0, "A walk.", ["walks", "outdoors"])
    assert reading.mood_score == 4  # not changed in place


def _index(cfg, text: str, fake_client, name: str = "2024-03-01.md"):
    (cfg.vault.path / name).write_text(text, encoding="utf-8")
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    return conn


def test_indexing_stores_your_mood_and_keeps_the_models(tmp_path, fake_client):
    cfg = make_config(tmp_path, frontmatter_mood_key="mood")
    conn = _index(cfg, "---\nmood: 3\ntags: [travel]\n---\nTrain to the coast.", fake_client)
    row = conn.execute("SELECT id, mood_score FROM entries").fetchone()
    assert row["mood_score"] == 3.0
    assert set(store.tags_for_entry(conn, row["id"])) >= {"travel", "friends"}
    reading = store.entry_reading(conn, row["id"])
    assert reading is not None and reading.mood_score == 7.0
    assert "travel" not in reading.tags


def test_changing_the_settings_needs_no_model_calls(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    conn = _index(cfg, "---\nmood: 3\ntags: [travel]\n---\nTrain to the coast.", fake_client)
    entry_id = conn.execute("SELECT id FROM entries").fetchone()["id"]
    assert conn.execute("SELECT mood_score FROM entries").fetchone()[0] == 7.0
    calls = len(fake_client.structured_calls)

    store.upsert_rollup(conn, "month", "2024-03", "s", "[]", 7.0, 1)
    on = _tags_config(_with(cfg, frontmatter_mood_key="mood"), ignore=["travel"])
    assert fields.refresh(on, conn) == 1
    assert conn.execute("SELECT mood_score FROM entries").fetchone()[0] == 3.0
    assert "travel" not in store.tags_for_entry(conn, entry_id)
    assert conn.execute("SELECT stale FROM rollups").fetchone()[0] == 1
    # the ignored tag has no entries left, so it's gone from the tag list
    assert conn.execute("SELECT 1 FROM tags WHERE name = 'travel'").fetchone() is None

    assert fields.refresh(on, conn) == 0
    assert fields.refresh(cfg, conn) == 1
    assert conn.execute("SELECT mood_score FROM entries").fetchone()[0] == 7.0
    assert "travel" in store.tags_for_entry(conn, entry_id)
    assert len(fake_client.structured_calls) == calls


def test_entries_from_before_readings_were_kept_get_one(tmp_path, fake_client):
    cfg = make_config(tmp_path, frontmatter_mood_key="mood")
    path = cfg.vault.path / "2024-03-02.md"
    path.write_bytes(b"---\nmood: 2\n---\nOld entry.")
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    # what an older version left: the file's hash, its entry, no reading or frontmatter
    add_entry(conn, date(2024, 3, 2), mood=6.0, tags=["old"], path=str(path))
    store.upsert_file(conn, str(path), parser.compute_hash(path.read_bytes()), "indexed")

    assert fields.refresh(cfg, conn) == 1
    entry_id = conn.execute("SELECT id FROM entries").fetchone()["id"]
    assert conn.execute("SELECT mood_score FROM entries").fetchone()[0] == 2.0
    reading = store.entry_reading(conn, entry_id)
    assert reading is not None and (reading.mood_score, reading.tags) == (6.0, ["old"])


def test_a_file_changed_since_indexing_is_left_for_the_index_run(tmp_path, fake_client):
    cfg = make_config(tmp_path, frontmatter_mood_key="mood")
    path = cfg.vault.path / "2024-03-02.md"
    path.write_text("---\nmood: 2\n---\nOld entry.", encoding="utf-8")
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    add_entry(conn, date(2024, 3, 2), mood=6.0, path=str(path))
    store.upsert_file(conn, str(path), "an older hash", "indexed")
    fields.refresh(cfg, conn)
    assert conn.execute("SELECT mood_score FROM entries").fetchone()[0] == 6.0


def test_settings_fingerprint_ignores_order(config):
    a = _tags_config(config, ignore=["a", "b"])
    b = _tags_config(config, ignore=["b", "a"])
    assert fields.settings_fingerprint(a) == fields.settings_fingerprint(b)
