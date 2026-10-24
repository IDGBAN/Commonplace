import json
from datetime import date

from conftest import add_entry, add_note, invoke_cli, make_config

from journal_analyzer import db, export, ingester, insights, parser, query, store


def _journal(conn):
    add_note(conn, "John Doe", properties={"Relationship": "Friend, School"})
    add_note(conn, "Jane Doe", properties={"relationship": "Family"})
    add_note(conn, "Sam Doe", properties={"Relationship": "Family"})
    add_note(conn, "Bear Mountain", kind="locations", properties={"Region": "North"})
    add_entry(conn, date(2024, 1, 1), mood=8, links=["Jane Doe", "Sam Doe"], path="/vault/1.md")
    add_entry(conn, date(2024, 1, 2), mood=8, links=["Jane Doe"], path="/vault/2.md")
    add_entry(conn, date(2024, 1, 3), mood=4, links=["John Doe"], path="/vault/3.md")
    add_entry(conn, date(2024, 1, 4), mood=4, links=["Bear Mountain"], path="/vault/4.md")


def test_list_properties_become_separate_values(tmp_path):
    cfg = make_config(tmp_path)
    path = tmp_path / "John Doe.md"
    path.write_text('---\nRelationship: [Friend, "[[School]]"]\nBorn: 1990\n---\nHi.', encoding="utf-8")
    note = parser.parse_note(path, "people", cfg)
    assert note.property_values == {"Relationship": ["Friend", "School"], "Born": ["1990"]}
    assert note.properties == {"Relationship": "Friend, School", "Born": "1990"}


def test_groups_count_notes_and_entries_once(conn):
    _journal(conn)
    groups = {r["value"]: r for r in store.property_groups(conn, "RELATIONSHIP")}
    assert (groups["Family"]["notes"], groups["Family"]["entries"]) == (2, 2)
    assert groups["Family"]["avg_mood"] == 8.0
    assert (groups["School"]["notes"], groups["School"]["entries"]) == (1, 1)
    assert [r["value"] for r in store.property_groups(conn, "relationship", date(2024, 1, 3))] == [
        "Friend", "School", "Family",
    ]
    assert store.property_groups(conn, "nothing") == []
    assert {r["name"] for r in store.property_keys(conn)} == {"Relationship", "Region"}


def test_a_group_filters_entries(conn):
    _journal(conn)
    ids = tuple(store.group_note_ids(conn, "relationship", " family "))
    only = store.EntryFilter(group=("relationship", "Family"), group_ids=ids)
    assert [r["entry_date"] for r in store.entries_in_optional_range(conn, None, None, only)] == [
        "2024-01-01", "2024-01-02",
    ]
    empty = store.EntryFilter(group=("relationship", "Gone"), group_ids=())
    assert store.entries_in_optional_range(conn, None, None, empty) == []
    assert query._filter_facts(conn, only) == ["Only entries that link a note whose relationship is Family are counted."]
    assert [e["date"] for e in export.collect_entries(conn, None, None, only=only)] == ["2024-01-01", "2024-01-02"]


def test_insights_find_groups_away_from_the_usual(conn, config):
    _journal(conn)
    add_entry(conn, date(2024, 1, 5), mood=8, links=["Jane Doe"], path="/vault/5.md")
    config.insights.min_sample_size = 2
    found = insights.compute_insights(config, conn)
    groups = [f.text for f in found.findings if f.kind == "group_mood"]
    assert groups == [
        "Days linking a note whose Relationship is Family: average mood 8 "
        "(higher than the 6.4 baseline) across 3 entries"
    ]
    config.notes.group_keys = ["region"]
    assert not [f for f in insights.compute_insights(config, conn).findings if f.kind == "group_mood"]


def test_entities_by_and_the_group_filter(tmp_path, config, conn):
    _journal(conn)
    conn.close()
    by = invoke_cli(tmp_path, config, "entities", "--by", "relationship", "--json")
    assert by.exit_code == 0, by.output
    data = json.loads(by.output)
    assert [(g["value"], g["notes"], g["entries"]) for g in data["groups"]] == [
        ("Family", 2, 2), ("Friend", 1, 1), ("School", 1, 1),
    ]
    table = invoke_cli(tmp_path, config, "entities", "--by", "Relationship")
    assert "Family" in table.output and "--group Relationship=VALUE" in table.output

    mood = invoke_cli(tmp_path, config, "mood", "--group", "relationship=family", "--json")
    assert mood.exit_code == 0, mood.output
    data = json.loads(mood.output)
    assert data["group"]["value"] == "Family"
    assert {n["title"] for n in data["group"]["notes"]} == {"Jane Doe", "Sam Doe"}
    assert data["months"] == [{"month": "2024-01", "avg_mood": 8.0, "entry_count": 2}]

    labeled = invoke_cli(tmp_path, config, "mood", "--group", "relationship:Family")
    assert "linking a note whose relationship is Family" in " ".join(labeled.output.split())

    for bad, message in [
        ("relationship", "takes a property and a value"),
        ("hair=red", "No note has a 'hair' property"),
        ("relationship=enemy", "Its values: Family, Friend, School"),
    ]:
        result = invoke_cli(tmp_path, config, "mood", "--group", bad)
        assert result.exit_code == 1 and message in result.output, result.output
    unknown = invoke_cli(tmp_path, config, "entities", "--by", "hair")
    assert unknown.exit_code == 1 and "Properties: Relationship, Region" in unknown.output


def test_notes_indexed_before_get_their_values_without_a_model(tmp_path, fake_client):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    people = cfg.vault.path / "Links" / "People"
    people.mkdir(parents=True)
    (people / "Jane Doe.md").write_text("---\nRelationship: [Family]\n---\nSister.", encoding="utf-8")
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    conn.execute("DELETE FROM note_property_values")
    conn.execute("DELETE FROM meta WHERE key = 'note_properties'")
    conn.commit()
    def model_calls():
        # the vector size probe runs on every index
        return len(fake_client.structured_calls) + sum(t != "dimension probe" for t in fake_client.embed_calls)

    calls = model_calls()
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert [r["value"] for r in store.property_groups(conn, "relationship")] == ["Family"]
    assert model_calls() == calls
    conn.close()
