import tomllib
from datetime import date

import pytest
from conftest import add_entry, copy_fixtures, invoke_cli, make_config

from journal_analyzer import db as db_mod
from journal_analyzer import extractor, fields, ingester, store
from journal_analyzer.config import TagsConfig
from journal_analyzer.models import Extraction, ParsedEntry


def _tags(config, **values):
    return config.model_copy(update={"tags": TagsConfig.model_validate(values)})


def test_merge_targets_are_normalized_and_chains_resolved():
    tags = TagsConfig.model_validate({"merge": {"#Friendship": "Friends", "pals": "friendship"}})
    assert tags.merge == {"friendship": "friends", "pals": "friends"}


@pytest.mark.parametrize("merge", [{"a": "b", "b": "a"}, {"a": "a"}, {"a": "b", "b": "c", "c": "a"}])
def test_merges_that_go_round_in_a_circle_are_refused(merge):
    with pytest.raises(ValueError, match="circle"):
        TagsConfig.model_validate({"merge": merge})


def test_merged_tags_count_as_their_target(config):
    cfg = _tags(config, merge={"friendship": "friends"}, ignore=["daily"])
    assert fields.final_tags(["friendship", "friends", "daily", "work"], {}, cfg) == ["friends", "work"]


def test_changing_the_merge_table_rewrites_stored_tags(conn, config):
    add_entry(conn, date(2024, 1, 1), tags=["friendship"], path="/vault/a.md")
    add_entry(conn, date(2024, 1, 2), tags=["friends"], path="/vault/b.md")
    fields.refresh(config, conn)
    merged = _tags(config, merge={"friendship": "friends"})
    assert fields.refresh(merged, conn) == 1
    assert [(r["name"], r["n"]) for r in store.top_tags(conn, None, None)] == [("friends", 2)]
    assert fields.refresh(config, conn) == 1
    assert {r["name"] for r in store.top_tags(conn, None, None)} == {"friends", "friendship"}


def test_the_model_is_shown_the_tags_already_in_use(config, fake_client):
    seen = {}

    def respond(model, messages):
        seen["system"] = messages[0]["content"]
        return Extraction(summary="ok", tags=["work"])

    fake_client.structured["Extraction"] = respond
    entry = ParsedEntry(date(2024, 3, 5), "Long shift.", "x")
    extractor.extract(entry, config, fake_client, known_tags=["work", "friends"])
    assert "Tags already used in this journal, most used first: work, friends." in seen["system"]
    extractor.extract(entry, config, fake_client)
    assert "already used" not in seen["system"]


def test_indexing_passes_the_most_used_tags(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    cfg.tags.known_in_prompt = 2
    vault = cfg.vault.path
    (vault / "2023-01-01.md").write_text("Odd one. #rare", encoding="utf-8")
    for day in (2, 3, 4):
        (vault / f"2023-01-0{day}.md").write_text("Shift, then a walk. #work #walks", encoding="utf-8")
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    systems = []

    def respond(model, messages):
        systems.append(messages[0]["content"])
        return Extraction(summary="ok")

    fake_client.structured["Extraction"] = respond
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert "already used" not in systems[0]  # nothing indexed yet

    copy_fixtures(vault, ["2024-01-15.md"])
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert "most used first: walks, work." in systems[-1]

    cfg.tags.known_in_prompt = 0
    (vault / "2024-01-15.md").write_text("Changed.", encoding="utf-8")
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert "already used" not in systems[-1]


def _rows(**counts):
    return [{"name": name, "n": n} for name, n in counts.items()]


def test_similar_tags_pairs_plurals_prefixes_and_near_spellings():
    pairs = fields.similar_tags(_rows(friend=2, friends=9, friendship=1, walk=3, walks=1,
                                      excercise=1, exercise=6, work=8, wok=1, city=2, cities=1))
    found = {(rare, common) for rare, _, common, _ in pairs}
    assert ("friend", "friends") in found
    assert ("friendship", "friends") in found
    assert ("walks", "walk") in found
    assert ("excercise", "exercise") in found
    assert ("cities", "city") in found
    assert not any("wok" in pair for pair in found)


def test_tags_similar_prints_a_merge_table_to_paste(tmp_path, config, conn):
    add_entry(conn, date(2024, 1, 1), tags=["friends", "walk"], path="/vault/a.md")
    add_entry(conn, date(2024, 1, 2), tags=["friends", "walk"], path="/vault/b.md")
    add_entry(conn, date(2024, 1, 3), tags=["friendship", "walks", "road trip"], path="/vault/c.md")
    add_entry(conn, date(2024, 1, 4), tags=["road trips"], path="/vault/d.md")
    result = invoke_cli(tmp_path, config, "tags", "--similar")
    assert result.exit_code == 0, result.output
    snippet = result.output[result.output.index("[tags]"):]
    merge = tomllib.loads(snippet)["tags"]["merge"]
    assert merge["friendship"] == "friends" and merge["walks"] == "walk"
    assert merge["road trips"] == "road trip"
