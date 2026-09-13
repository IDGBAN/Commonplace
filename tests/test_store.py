from datetime import date, timedelta

import sqlite_vec
from conftest import add_entry, add_note

from journal_analyzer import db, store


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]


def _named(rows) -> list[tuple[str, str]]:
    return [(r["name"], r["type"]) for r in rows]


def test_tags_are_shared_rather_than_duplicated(conn):
    e1 = add_entry(conn, date(2024, 1, 1), tags=["work", "health"])
    e2 = add_entry(conn, date(2024, 1, 2), tags=["work", "health"])
    assert e1 != e2
    assert _count(conn, "tags") == 2
    assert _count(conn, "entry_tags") == 4


def test_links_resolve_whichever_side_was_indexed_first(conn):
    # entry indexed before the note it links to
    entry = add_entry(conn, date(2024, 1, 1), links=["Sara Miller"])
    assert store.entities_for_entry(conn, entry) == []
    add_note(conn, "Sara Miller")
    assert _named(store.entities_for_entry(conn, entry)) == [("Sara Miller", "people")]


def test_links_ignore_case_like_obsidian(conn):
    entry = add_entry(conn, date(2024, 1, 1), links=["sara MILLER"])
    add_note(conn, "Sara Miller")
    assert _named(store.entities_for_entry(conn, entry)) == [("Sara Miller", "people")]


def test_links_to_missing_notes_are_not_entities(conn):
    entry = add_entry(conn, date(2024, 1, 1), links=["Nobody We Know"])
    assert store.entities_for_entry(conn, entry) == []
    assert store.stats(conn).links == 0


def test_cascade_delete_removes_joins_links_fts_and_vec(conn):
    add_note(conn, "Sara")
    eid = add_entry(
        conn, date(2024, 1, 1), text="walking in the rain", tags=["walk"],
        links=["Sara"], path="/vault/x.md",
    )
    conn.execute(
        "INSERT INTO entries_vec(entry_id, embedding) VALUES (?, ?)",
        (eid, sqlite_vec.serialize_float32([0.1] * 8)),
    )
    conn.commit()
    hits = conn.execute(
        "SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'rain'"
    ).fetchall()
    assert [r["rowid"] for r in hits] == [eid]

    assert store.delete_entries_for_file(conn, "/vault/x.md") == [date(2024, 1, 1)]
    for table in ("entries", "entry_tags", "entry_links", "entry_entities", "entries_vec"):
        assert _count(conn, table) == 0, table
    assert not conn.execute(
        "SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'rain'"
    ).fetchall()


def test_deleting_a_note_clears_its_rows_but_not_the_entry_links(conn):
    path = "/vault/Links/people/Sara.md"
    nid = add_note(conn, "Sara", aliases=["S"], links=["Bear Mountain"], text="climbs a lot")
    eid = add_entry(conn, date(2024, 1, 1), links=["Sara"])
    conn.execute(
        "INSERT INTO notes_vec(note_id, embedding) VALUES (?, ?)",
        (nid, sqlite_vec.serialize_float32([0.1] * 8)),
    )
    conn.commit()
    assert store.note_exists(conn, path)

    store.delete_note_for_file(conn, path)
    for table in ("notes", "note_names", "note_links", "notes_vec"):
        assert _count(conn, table) == 0, table
    assert not conn.execute(
        "SELECT rowid FROM notes_fts WHERE notes_fts MATCH 'climbs'"
    ).fetchall()
    assert store.entities_for_entry(conn, eid) == []
    assert not store.note_exists(conn, path)
    # the link stays in case the note comes back
    assert _count(conn, "entry_links") == 1


def test_mark_rollups_stale_hits_week_month_year(conn):
    for ptype, pkey in (("week", "2024-W01"), ("month", "2024-01"), ("year", "2024")):
        store.upsert_rollup(conn, ptype, pkey, "s", "[]", None, 1)
    store.mark_rollups_stale(conn, date(2024, 1, 3))  # ISO week 2024-W01
    stale = {
        (r["period_type"], r["period_key"])
        for r in conn.execute("SELECT * FROM rollups WHERE stale = 1")
    }
    assert stale == {("week", "2024-W01"), ("month", "2024-01"), ("year", "2024")}


def test_period_keys_iso_week():
    # 2023-01-01 is a Sunday, ISO week 52 of 2022.
    keys = store.period_keys_for_date(date(2023, 1, 1))
    assert keys == {"week": "2022-W52", "month": "2023-01", "year": "2023"}


def test_filtered_entry_ids_by_note_title_or_alias(conn):
    add_note(conn, "Sara Miller", aliases=["Sunny"])
    linked = add_entry(conn, date(2024, 1, 1), links=["Sara Miller"])
    other = add_entry(conn, date(2024, 1, 2))
    assert store.filtered_entry_ids(conn, entity_names=["Sara"]) == [linked]
    assert store.filtered_entry_ids(conn, entity_names=["sunny"]) == [linked]
    # unknown names drop the filter instead of matching nothing
    assert store.filtered_entry_ids(conn, entity_names=["Nobody"]) == [linked, other]
    assert store.filtered_entry_ids(conn, date_from=date(2025, 1, 1)) == []


def test_a_named_note_nothing_links_to_drops_the_filter_too(conn):
    add_note(conn, "Sara")
    eid = add_entry(conn, date(2024, 1, 1))
    assert store.filtered_entry_ids(conn, entity_names=["Sara"]) == [eid]


def test_fts_search_survives_a_junk_keyword(conn):
    eid = add_entry(conn, date(2024, 1, 1), text="walking in the rain")
    # a term that's only a quote mustn't break the MATCH for "rain"
    assert store.fts_search(conn, ['"', "rain"], None, 5) == [eid]
    assert store.fts_search(conn, ['"', "  "], None, 5) == []


def test_fts_search_respects_the_allowed_set(conn):
    add_entry(conn, date(2024, 1, 1), text="rain today")
    keep = add_entry(conn, date(2024, 1, 2), text="rain again")
    assert store.fts_search(conn, ["rain"], [keep], 5) == [keep]


def test_fts_search_finds_allowed_entries_however_far_down_they_rank(conn):
    strong = [add_entry(conn, date(2024, 1, 1), text="rain " * 20) for _ in range(60)]
    weak = add_entry(
        conn, date(2024, 1, 2), text="one brief shower of rain " + "filler " * 200
    )
    assert weak not in store.fts_search(conn, ["rain"], None, 5)
    assert store.fts_search(conn, ["rain"], [weak, strong[0]], 5) == [strong[0], weak]


def test_vec_search_finds_allowed_entries_however_far_down_they_rank(conn):
    ids = [add_entry(conn, date(2024, 1, 1) + timedelta(days=i)) for i in range(60)]
    for distance, eid in enumerate(ids):
        conn.execute(
            "INSERT INTO entries_vec(entry_id, embedding) VALUES (?, ?)",
            (eid, sqlite_vec.serialize_float32([float(distance)] + [0.0] * 7)),
        )
    conn.commit()
    origin = [0.0] * 8
    assert store.vec_search(conn, origin, None, 2) == ids[:2]
    assert store.vec_search(conn, origin, [ids[59], ids[50]], 5) == [ids[50], ids[59]]


def test_notes_fts_search_covers_titles_aliases_and_text(conn):
    sara = add_note(conn, "Sara Miller", aliases=["Sunny"], text="climbs on weekends")
    add_note(conn, "Bear Mountain", kind="locations", text="a state park")
    assert store.notes_fts_search(conn, ["miller"], 5) == [sara]
    assert store.notes_fts_search(conn, ["sunny"], 5) == [sara]
    assert store.notes_fts_search(conn, ["climbs"], 5) == [sara]
    assert store.notes_fts_search(conn, ['"'], 5) == []


def test_notes_vec_search_is_empty_before_the_vector_table_exists(config):
    conn = db.connect(config)
    db.init_schema(conn)
    assert store.notes_vec_search(conn, [0.1] * 8, 3) == []
    conn.close()


def test_batch_tag_and_link_lookups_match_the_per_entry_ones(conn):
    add_note(conn, "Sara")
    add_note(conn, "Park", kind="locations")
    ids = [
        add_entry(conn, date(2024, 1, d), tags=["work"], links=["Sara", "Park"])
        for d in (1, 2)
    ]
    tags = store.tags_for_entries(conn, ids)
    assert tags == {i: store.tags_for_entry(conn, i) for i in ids}
    entities = store.entities_for_entries(conn, ids)
    assert {i: _named(rows) for i, rows in entities.items()} == {
        i: _named(store.entities_for_entry(conn, i)) for i in ids
    }


def test_batch_lookups_on_an_empty_id_list(conn):
    assert store.tags_for_entries(conn, []) == {}
    assert store.entities_for_entries(conn, []) == {}


def test_top_entities_counts_links_and_filters_by_kind(conn):
    add_note(conn, "Sara")
    add_note(conn, "Park", kind="locations")
    add_entry(conn, date(2024, 1, 1), links=["Sara", "Park"])
    add_entry(conn, date(2024, 1, 2), links=["Sara"])
    rows = store.top_entities(conn, None, None)
    assert [(r["name"], r["type"], r["n"]) for r in rows] == [
        ("Sara", "people", 2),
        ("Park", "locations", 1),
    ]
    by_kind = store.top_entities(conn, None, None, entity_type="Locations")
    assert [r["name"] for r in by_kind] == ["Park"]


def test_find_notes_prefers_exact_title_then_alias_then_substring(conn):
    sam = add_note(conn, "Sam")
    add_note(conn, "Samuel Park", aliases=["Sam", "Sammy"])
    add_note(conn, "Samantha")
    assert [r["id"] for r in store.find_notes(conn, "SAM")] == [sam]
    assert [r["title"] for r in store.find_notes(conn, "sammy")] == ["Samuel Park"]
    assert [r["title"] for r in store.find_notes(conn, "saman")] == ["Samantha"]
    assert store.find_notes(conn, "   ") == []


def test_note_name_matching_treats_like_wildcards_literally(conn):
    add_note(conn, "100% Juice", kind="topics")
    add_note(conn, "Plain", kind="topics")
    assert len(store.note_ids_matching(conn, ["%"])) == 1
    assert store.note_ids_matching(conn, ["_"]) == []


def test_mention_stats_and_recent_mentions(conn):
    sara = add_note(conn, "Sara")
    lonely = add_note(conn, "Lonely Topic", kind="topics")
    for day in (3, 1, 2):
        add_entry(conn, date(2024, 1, day), links=["Sara"])
    stats = store.mention_stats(conn, [sara, lonely])
    assert stats[sara] == store.Mentions(3, "2024-01-01", "2024-01-03")
    assert lonely not in stats
    assert store.mention_stats(conn, [sara], date_from=date(2024, 1, 2))[sara].count == 2
    recent = store.entries_mentioning(conn, sara, 2)
    assert [r["entry_date"] for r in recent] == ["2024-01-03", "2024-01-02"]
    assert [tuple(r) for r in store.mentions_by_year(conn, sara)] == [("2024", 3)]
    assert len(store.entry_ids_mentioning(conn, [sara])) == 3


def test_notes_linked_from_entries_ranks_by_frequency(conn):
    sara = add_note(conn, "Sara")
    park = add_note(conn, "Park", kind="locations")
    e1 = add_entry(conn, date(2024, 1, 1), links=["Sara", "Park"])
    e2 = add_entry(conn, date(2024, 1, 2), links=["Sara"])
    rows = store.notes_linked_from_entries(conn, [e1, e2], limit=5)
    assert [(r["note_id"], r["n"]) for r in rows] == [(sara, 2), (park, 1)]


def test_links_between_notes_read_both_ways(conn):
    sara = add_note(conn, "Sara", links=["Garden", "Sara", "Nowhere"])
    garden = add_note(conn, "Garden", kind="topics")
    assert [r["title"] for r in store.note_outlinks(conn, sara)] == ["Garden"]
    assert [r["title"] for r in store.note_backlinks(conn, garden)] == ["Sara"]
    assert store.note_backlinks(conn, sara) == []


def test_writing_stats_streaks(conn):
    # 3 days, a gap, then 2 days
    for day in (1, 2, 3, 10, 11):
        add_entry(conn, date(2024, 1, day), text="a b c")
    s = store.writing_stats(conn)
    assert s.entries == 5
    assert s.days_written == 5
    assert s.words == 15
    assert s.avg_words == 3.0
    assert s.first_day == date(2024, 1, 1)
    assert s.last_day == date(2024, 1, 11)
    assert s.longest_streak == 3
    assert s.longest_streak_end == date(2024, 1, 3)
    assert s.current_streak == 2


def test_writing_stats_on_an_empty_cache(conn):
    s = store.writing_stats(conn)
    assert (s.entries, s.words, s.current_streak, s.longest_streak) == (0, 0, 0, 0)
    assert s.first_day is None and s.longest_streak_end is None


def test_same_day_entries_do_not_inflate_a_streak(conn):
    for _ in range(3):
        add_entry(conn, date(2024, 1, 1))
    s = store.writing_stats(conn)
    assert s.entries == 3
    assert s.days_written == 1
    assert s.longest_streak == 1


def test_stats_counts_notes_links_and_errored_files(conn):
    add_note(conn, "Sara")
    add_entry(conn, date(2024, 1, 1), links=["Sara", "Ghost"])
    store.upsert_file(conn, "/vault/bad.md", "h2", "error", "no date")
    s = store.stats(conn)
    assert (s.entries, s.notes, s.links, s.files) == (1, 1, 1, 3)
    assert s.errored_files == 1
    assert [r["path"] for r in store.errored_files(conn)] == ["/vault/bad.md"]
    assert [tuple(r) for r in store.notes_by_kind(conn)] == [("people", 1)]


def test_entries_in_optional_range_is_unbounded_by_default(conn):
    add_entry(conn, date(2024, 1, 1))
    add_entry(conn, date(2025, 1, 1))
    assert len(store.entries_in_optional_range(conn, None, None)) == 2
    assert len(store.entries_in_optional_range(conn, date(2024, 6, 1), None)) == 1
