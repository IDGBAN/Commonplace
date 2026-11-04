from datetime import date

from conftest import add_entry, add_note, invoke_cli, make_config

from journal_analyzer import db, ingester, rollups, store


def test_notes_and_the_days_they_link(conn):
    trip = add_note(conn, "Lisbon Trip", kind="events", links=["2024-03-05", "2024-03-06 flight home"])
    add_entry(conn, date(2024, 3, 5), summary="Tram up the hill", path="/vault/2024-03-05.md")
    flight = add_entry(conn, date(2024, 3, 6), summary="Flew back", path="/vault/2024-03-06 flight home.md")
    add_entry(conn, date(2024, 3, 7), path="/vault/2024-03-07.md")
    assert [r["entry_date"] for r in store.days_linked_from_note(conn, trip)] == ["2024-03-05", "2024-03-06"]
    assert [r["title"] for r in store.notes_linking_day(conn, store.entry_by_id(conn, flight))] == ["Lisbon Trip"]
    assert store.unresolved_note_links(conn) == []


def test_they_count_as_mentions_only_when_asked(conn, config):
    trip = add_note(conn, "Lisbon Trip", kind="events", links=["2024-03-05"])
    day = add_entry(conn, date(2024, 3, 5), path="/vault/2024-03-05.md")
    store.upsert_rollup(conn, "month", "2024-03", "s", "[]", 6.0, 1)
    ingester._refresh_day_mentions(config, conn)
    assert store.entities_for_entry(conn, day) == []

    config.notes.day_links_count = True
    ingester._refresh_day_mentions(config, conn)
    assert [r["name"] for r in store.entities_for_entry(conn, day)] == ["Lisbon Trip"]
    assert store.mention_stats(conn, [trip])[trip].count == 1
    assert store.get_rollup(conn, "month", "2024-03")["stale"] == 1

    config.notes.day_links_count = False
    ingester._refresh_day_mentions(config, conn)
    assert store.entities_for_entry(conn, day) == []


def test_a_day_linked_both_ways_counts_once(conn, config):
    add_note(conn, "Lisbon Trip", kind="events", links=["2024-03-05"])
    day = add_entry(conn, date(2024, 3, 5), links=["Lisbon Trip"], path="/vault/2024-03-05.md")
    config.notes.day_links_count = True
    ingester._refresh_day_mentions(config, conn)
    assert len(store.entities_for_entry(conn, day)) == 1
    assert store.stats(conn).links == 1


def test_relationship_digests_read_the_days_a_note_links(conn, config):
    trip = add_note(conn, "Lisbon Trip", kind="events", links=["2024-03-05"])
    add_entry(conn, date(2024, 3, 5), summary="Tram up the hill", tags=["travel"], path="/vault/2024-03-05.md")
    add_entry(conn, date(2024, 3, 9), links=["Lisbon Trip"], summary="Showed the photos", path="/vault/9.md")
    text, _, _ = rollups.note_digest_input(config, conn, store.notes_by_ids(conn, [trip])[0])
    assert "Days the note itself links to:\n- 2024-03-05 (mood 6) [travel]: Tram up the hill" in text


def test_changing_a_linked_day_marks_the_note_digest_stale(tmp_path, fake_client):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    events = cfg.vault.path / "Links" / "Events"
    events.mkdir(parents=True)
    (events / "Lisbon Trip.md").write_text("Planned on [[2024-03-05]].", encoding="utf-8")
    day = cfg.vault.path / "2024-03-05.md"
    day.write_text("Tram up the hill.", encoding="utf-8")
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    store.upsert_note_digest(conn, "lisbon trip", "s", store.Mentions(1, "2024-03-05", "2024-03-05"), None)
    day.write_text("Tram up the hill, then custard tarts.", encoding="utf-8")
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert store.get_note_digest(conn, "lisbon trip")["stale"] == 1
    conn.close()

    note = invoke_cli(tmp_path, cfg, "note", "Lisbon Trip")
    assert "links to days: 2024-03-05" in note.output
    show = invoke_cli(tmp_path, cfg, "show", "2024-03-05")
    assert "linked from notes: Lisbon Trip" in show.output
