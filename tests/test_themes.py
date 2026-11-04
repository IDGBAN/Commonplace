import json
import random
from datetime import date, timedelta

import pytest
from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import cli, store, themes
from journal_analyzer.models import OllamaUnavailableError

# three topics far apart, and a little noise around each
CENTRES = [
    [1, 0, 0, 0, 0, 0, 0, 0],
    [0, 0, 0, 1, 0, 0, 0, 0],
    [0, 0, 0, 0, 0, 0, 1, 0],
]
PLACES = ["Park", "Office", "Garden"]


def _vector(topic, rng):
    return [x + rng.gauss(0, 0.05) for x in CENTRES[topic]]


def _journal(conn, per_topic=10):
    rng = random.Random(3)
    for place in PLACES:
        add_note(conn, place, kind="places")
    truth = {}
    day = date(2024, 1, 1)
    for i in range(per_topic * 3):
        topic = i % 3
        entry = add_entry(conn, day, mood=4.0 + topic, tags=[f"topic{topic}"], links=[PLACES[topic]],
                          summary=f"Day {i} about topic {topic}", path=f"/vault/{day}.md")
        store.save_entry_vector(conn, entry, _vector(topic, rng))
        truth[entry] = topic
        day += timedelta(days=1)
    return truth


def _named(model, messages):
    text = messages[-1]["content"]
    place = next((p for p in PLACES if p in text.split("Notes they link most:")[-1]), "nowhere")
    return themes.ThemeName(name=f"Days at the {place}.", about=f"Time   spent at the {place}.")


@pytest.fixture
def journal(conn, config):
    config.themes.min_entries = 5
    return _journal(conn)


def _groups(found, truth):
    by = {}
    for entry_id, (c, _) in found.assigned.items():
        by.setdefault(c, set()).add(truth[entry_id])
    return by


def test_entries_close_in_meaning_end_up_together(conn, config, journal):
    found = themes.group(conn, config, count=3)
    assert len(found.centroids) == 3 and len(found.assigned) == 30
    assert all(len(topics) == 1 for topics in _groups(found, journal).values())


def test_the_same_entries_give_the_same_themes(conn, config, journal):
    first = themes.group(conn, config, count=3).assigned
    assert {e: c for e, (c, _) in themes.group(conn, config, count=3).assigned.items()} == {
        e: c for e, (c, _) in first.items()
    }


def test_themes_too_small_are_folded_into_the_others(conn, config, journal):
    found = themes.group(conn, config, count=8)
    sizes = {}
    for c, _ in found.assigned.values():
        sizes[c] = sizes.get(c, 0) + 1
    assert all(n >= config.themes.min_entries for n in sizes.values())
    assert len(found.centroids) == len(sizes)


def test_how_many_themes_it_looks_for(config):
    assert themes.theme_count(config, 400) == 10
    assert themes.theme_count(config, 20) == 3
    assert themes.theme_count(config, 100_000) == themes.AUTO_MOST
    config.themes.count = 7
    assert themes.theme_count(config, 400) == 7
    # never more than leave room for min_entries each
    assert themes.theme_count(config, 21) == 4


def test_too_few_entries_to_group(conn, config):
    add_entry(conn, date(2024, 1, 1))
    with pytest.raises(themes.ThemesError, match="at least 20"):
        themes.group(conn, config)


def test_the_chunk_reader_matches_reading_row_by_row(conn, journal):
    fast = themes._from_chunks(conn)
    slow = dict(conn.execute("SELECT entry_id, embedding FROM entries_vec").fetchall())
    assert fast == slow


def test_it_still_works_when_the_chunks_cant_be_read(conn, config, journal, monkeypatch):
    expected = {e: c for e, (c, _) in themes.group(conn, config, count=3).assigned.items()}
    monkeypatch.setattr(themes, "_from_chunks", lambda conn: None)
    assert {e: c for e, (c, _) in themes.group(conn, config, count=3).assigned.items()} == expected


def test_refresh_names_and_keeps_them(conn, config, journal, fake_client):
    fake_client.structured["ThemeName"] = _named
    done = themes.refresh(config, conn, fake_client, count=3)
    assert (done.themes, done.entries, done.named, done.naming_error) == (3, 30, 3, None)
    listed = themes.overview(conn)
    assert sorted(t.label for t in listed) == ["Days at the Garden", "Days at the Office", "Days at the Park"]
    park = next(t for t in listed if t.label == "Days at the Park")
    assert park.about == "Time spent at the Park."
    assert (park.entries, park.first, park.mood) == (10, "2024-01-01", 4.0)
    assert sum(park.months.values()) == 10
    row = conn.execute("SELECT named_from FROM themes WHERE id = ?", (park.id,)).fetchone()
    assert len(json.loads(row["named_from"])) == themes.NAMED_FROM
    prompt = fake_client.structured_calls[-1]
    assert prompt == ("fake-fast", "ThemeName")


def test_a_model_outage_keeps_the_grouping_without_names(conn, config, journal, fake_client):
    def down(model, messages):
        raise OllamaUnavailableError("Ollama isn't running.")

    fake_client.structured["ThemeName"] = down
    done = themes.refresh(config, conn, fake_client, count=3)
    assert done.themes == 3 and done.named == 0 and "isn't running" in done.naming_error
    labels = sorted(t.label for t in themes.overview(conn))
    assert labels == ["Unnamed: Garden, topic2", "Unnamed: Office, topic1", "Unnamed: Park, topic0"]


def test_new_entries_go_with_the_nearest_theme_until_a_refresh(conn, config, journal, fake_client):
    fake_client.structured["ThemeName"] = _named
    themes.refresh(config, conn, fake_client, count=3)
    rng = random.Random(9)
    newer = add_entry(conn, date(2024, 6, 1), summary="Back at the park", path="/vault/new.md")
    store.save_entry_vector(conn, newer, _vector(0, rng))
    placed = themes.membership(conn)
    theme_id, _, was_placed = placed[newer]
    assert was_placed
    park = next(t for t in themes.overview(conn) if t.label == "Days at the Park")
    assert theme_id == park.id and park.entries == 11 and park.last == "2024-06-01"
    found = themes.status(conn, config)
    assert (found.grouped, found.placed, found.stale) == (30, 1, False)
    config.themes.stale_share = 0.01
    assert themes.status(conn, config).stale


def test_a_deleted_entry_leaves_its_theme(conn, config, journal, fake_client):
    fake_client.structured["ThemeName"] = _named
    themes.refresh(config, conn, fake_client, count=3)
    gone = next(iter(journal))
    store.delete_entries_for_file(conn, conn.execute("SELECT file_path FROM entries WHERE id = ?", (gone,)).fetchone()[0])
    assert gone not in themes.membership(conn)
    assert sum(t.entries for t in themes.overview(conn)) == 29


def test_a_theme_in_detail_and_in_a_range(conn, config, journal, fake_client):
    fake_client.structured["ThemeName"] = _named
    themes.refresh(config, conn, fake_client, count=3)
    park = next(t for t in themes.overview(conn) if t.label == "Days at the Park")
    shown = themes.detail(conn, park.id)
    assert len(shown.central) == themes.NAMED_FROM and len(shown.newest) == 10
    assert shown.tags == [("topic0", 10)]
    assert [(t, n) for _, t, _, n in shown.notes] == [("Park", 10)]
    assert themes.detail(conn, 99) is None
    found = themes.in_range(conn, date(2024, 1, 1), date(2024, 1, 9), limit=5)
    assert sorted((t.label, n) for t, n in found) == [
        ("Days at the Garden", 3), ("Days at the Office", 3), ("Days at the Park", 3),
    ]
    assert themes.in_range(conn, date(2024, 1, 1), date(2024, 1, 2), limit=5) == []


def test_no_themes_yet_reads_as_empty(conn, config):
    assert themes.overview(conn) == [] and themes.membership(conn) == {}
    assert themes.status(conn, config).themes == 0


def test_the_themes_command(tmp_path, config, conn, journal, fake_client, monkeypatch):
    fake_client.structured["ThemeName"] = _named
    monkeypatch.setattr(cli, "OllamaClient", lambda c: fake_client)
    result = invoke_cli(tmp_path, config, "themes")
    assert result.exit_code == 0 and "No themes yet" in result.output
    result = invoke_cli(tmp_path, config, "themes", "--count", "3")
    assert result.exit_code == 1 and "only goes with --refresh" in result.output
    result = invoke_cli(tmp_path, config, "themes", "--refresh", "--count", "3")
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    # the table is narrow in tests, so names wrap
    assert "Found 3 themes in 30 entries." in flat and "Days at" in flat
    data = json.loads(invoke_cli(tmp_path, config, "themes", "--json").stdout)
    assert len(data["themes"]) == 3 and data["status"]["grouped"] == 30
    park = next(t for t in data["themes"] if t["name"] == "Days at the Park")
    shown = invoke_cli(tmp_path, config, "themes", str(park["id"]))
    assert "Time spent at the Park." in shown.output and "Closest to its middle" in shown.output
    detail = json.loads(invoke_cli(tmp_path, config, "themes", str(park["id"]), "--json").stdout)
    assert detail["notes"][0]["title"] == "Park" and len(detail["central"]) == themes.NAMED_FROM
    missing = invoke_cli(tmp_path, config, "themes", "99")
    assert missing.exit_code == 1 and "There's no theme 99" in missing.output


def test_the_command_says_when_theres_too_little(tmp_path, config, conn, fake_client, monkeypatch):
    monkeypatch.setattr(cli, "OllamaClient", lambda c: fake_client)
    add_entry(conn, date(2024, 1, 1))
    result = invoke_cli(tmp_path, config, "themes", "--refresh", "--json")
    assert result.exit_code == 1 and "at least 20" in json.loads(result.stdout)["error"]


def test_narrative_answers_hear_about_the_themes_in_their_dates(conn, config, journal, fake_client):
    from conftest import plan_for

    from journal_analyzer import query

    fake_client.structured["ThemeName"] = _named
    themes.refresh(config, conn, fake_client, count=3)
    lines = query._theme_lines(plan_for("narrative", date_from=date(2024, 1, 1), date_to=date(2024, 1, 30)), conn)
    assert len(lines) == 3 and all(line.startswith("- [theme] Days at the ") for line in lines)
    assert "(10 of its 10 entries fall here)" in lines[0]
    # a factual question about a few days doesn't need them
    assert query._theme_lines(plan_for("factual", date_from=date(2024, 1, 1), date_to=date(2024, 1, 5)), conn) == []


def test_mcp_withholds_a_theme_named_from_a_left_out_entry(conn, config, journal, fake_client):
    from journal_analyzer import withhold

    fake_client.structured["ThemeName"] = _named
    themes.refresh(config, conn, fake_client, count=3)
    park = next(t for t in themes.overview(conn) if t.label == "Days at the Park")
    central = json.loads(conn.execute("SELECT named_from FROM themes WHERE id = ?", (park.id,)).fetchone()[0])
    conn.execute("UPDATE entries SET raw_text = 'Hard session. #therapy' WHERE id = ?", (central[0],))
    conn.commit()
    config.mcp.exclude_tags = ["therapy"]
    filtered = withhold.FilteredCache(config)
    copy = filtered.connection()
    assert filtered.withheld.themes == 1
    assert sorted(t.label for t in themes.overview(copy)) == [
        "Days at the Garden", "Days at the Office", "Unnamed: Park, topic0",
    ]
    assert all(t.name != "Days at the Park" for t, _ in themes.in_range(copy, None, None, 5))
    filtered.close()


def test_the_browser_lists_themes_by_month(conn, config, journal, fake_client):
    from journal_analyzer import browse

    req = browse.Request(config, conn, {}, lambda c: None)
    empty_page = browse.themes_page(req)
    assert empty_page["themes"] == [] and empty_page["status"]["made_at"] is None
    fake_client.structured["ThemeName"] = _named
    themes.refresh(config, conn, fake_client, count=3)
    page = browse.themes_page(req)
    assert len(page["themes"]) == 3 and page["status"]["grouped"] == 30
    assert page["month_totals"] == {"2024-01": 30}
    park = next(t for t in page["themes"] if t["name"] == "Days at the Park")
    assert park["months"] == {"2024-01": 10}
    detail = browse.theme_page(req, str(park["id"]))
    assert detail["label"] == "Days at the Park" and len(detail["central"]) == themes.NAMED_FROM
    assert detail["notes"][0]["title"] == "Park" and detail["tags"][0] == {"name": "topic0", "entries": 10}
    with pytest.raises(browse.ApiError) as missing:
        browse.theme_page(req, "99")
    assert missing.value.status == 404
