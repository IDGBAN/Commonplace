from datetime import date

from conftest import add_entry, add_note, invoke_cli, make_config

from journal_analyzer import context, db, ingester, parser, serialize, store
from journal_analyzer.models import Extraction, ParsedEntry

DAY = """# Morning
Coffee on the balcony.

# Work
Long meeting about the move.
## Lunch
Ate with the team.

# Evening
Read until late.
"""


def test_anchors_keep_what_a_link_points_at():
    text = "[[2024-01-14#Work]] [[2024-01-14]] [[John Doe#Kids|his kids]] ![[Notes#^ab12]] [[img.png#x]]"
    assert parser.link_anchors(text) == {
        "2024-01-14": ["Work", ""],
        "john doe": ["Kids"],
        "notes": ["^ab12"],
    }
    assert parser.link_anchors("[[2024-01-14]] [[John Doe|John]]") == {}
    assert parser.link_anchor("Note#A#B|shown") == "A#B"


def test_heading_links_read_the_way_obsidian_shows_them():
    text = "See [[2024-01-14#Work#Lunch]], [[Notes#^ab12]] and [[#Top]]."
    assert parser.clean_text(text) == "See 2024-01-14 > Work > Lunch, Notes and Top."
    assert parser.link_labels("[[John Doe#Kids]] [[John Doe#Kids|his kids]]") == {"john doe": ["his kids"]}


def test_anchor_labels_read_like_the_heading():
    assert parser.anchor_label("Work") == "Work"
    assert parser.anchor_label("Work#Lunch") == "Lunch"
    assert parser.anchor_label("^ab12") == "passage"


def test_a_heading_section_runs_to_the_next_heading_at_its_level():
    assert parser.find_section(DAY, "Work") == "Long meeting about the move.\n## Lunch\nAte with the team."
    assert parser.find_section(DAY, "Evening") == "Read until late."
    assert parser.section_span(DAY, "Work") == (3, 7)


def test_headings_match_the_way_obsidian_writes_them():
    text = "## Work: the **big** move ##\nPacked boxes.\n"
    assert parser.find_section(text, "work the big move") == "Packed boxes."
    assert parser.find_section(text, "Gone") is None
    assert parser.find_section("```\n# Work\nnot a heading\n```\n", "Work") is None


def test_nested_anchors_look_inside_the_outer_heading():
    text = "# Monday\n## Notes\nfirst\n# Tuesday\n## Notes\nsecond\n"
    assert parser.find_section(text, "Tuesday#Notes") == "second"
    assert parser.find_section(text, "Renamed#Notes") == "first"


def test_block_ids_find_their_paragraph_or_list_item():
    text = "Intro.\n\nA long\nparagraph here ^para1\n\n- one\n- two ^item2\n\n> quoted\n\n^quote\n"
    assert parser.find_section(text, "^para1") == "A long\nparagraph here"
    assert parser.find_section(text, "^item2") == "- two"
    assert parser.find_section(text, "^quote") == "> quoted"
    assert parser.find_section(text, "^missing") is None


def test_carriage_returns_dont_hide_headings():
    assert parser.find_section("# Work\r\nPacked.\r\n# Home\r\nRest.", "Work") == "Packed."


def test_parsed_entries_carry_their_anchors(tmp_path):
    cfg = make_config(tmp_path)
    path = cfg.vault.path / "2024-01-15.md"
    path.write_text("See [[2024-01-14#Work]] and [[John Doe#Kids]].", encoding="utf-8")
    [entry] = parser.parse_file(path, cfg)
    assert entry.link_anchors == {"2024-01-14": ["Work"], "john doe": ["Kids"]}


def _entry(links, anchors, day=date(2024, 1, 15), path="/vault/15.md"):
    return ParsedEntry(day, "x", path, links=links, link_anchors=anchors)


def test_a_link_to_part_of_a_day_brings_that_part(conn, config):
    add_entry(conn, date(2024, 1, 14), text=DAY, summary="Busy day", path="/vault/14.md")
    blocks = context.entry_links(conn, _entry(["2024-01-14"], {"2024-01-14": ["Work"]}), config.vault.path, 600)
    assert blocks == [
        '- [[2024-01-14#Work]], the "Work" part of another day this entry links to: '
        "Long meeting about the move. ## Lunch Ate with the team."
    ]


def test_a_part_that_is_gone_falls_back_to_the_summary(conn, config):
    add_entry(conn, date(2024, 1, 14), text=DAY, summary="Busy day", path="/vault/14.md")
    gone = _entry(["2024-01-14"], {"2024-01-14": ["Holiday"]})
    assert context.entry_links(conn, gone, config.vault.path, 600) == [
        "- [[2024-01-14]], another day this entry links to: Busy day"
    ]
    both = _entry(["2024-01-14"], {"2024-01-14": ["Evening", ""]})
    assert context.entry_links(conn, both, config.vault.path, 600) == [
        '- [[2024-01-14#Evening]], the "Evening" part of another day this entry links to: Read until late.',
        "- [[2024-01-14]], another day this entry links to: Busy day",
    ]


def test_a_link_to_part_of_a_note_brings_that_part(conn, config):
    add_note(conn, "John Doe", text="# Work\nAccountant.\n# Kids\nTwo, Sam and Alex.", summary="My friend.")
    only_part = _entry(["john doe"], {"john doe": ["Kids"]})
    assert context.entry_links(conn, only_part, config.vault.path, 600) == [
        '- John Doe (people); linked to the "Kids" part: Two, Sam and Alex.'
    ]
    with_whole = _entry(["john doe"], {"john doe": ["", "Kids"]})
    assert context.entry_links(conn, with_whole, config.vault.path, 600) == [
        '- John Doe (people); linked to the "Kids" part: My friend. Two, Sam and Alex.'
    ]
    missing = _entry(["john doe"], {"john doe": ["Pets"]})
    assert context.entry_links(conn, missing, config.vault.path, 600) == ["- John Doe (people): My friend."]


def test_a_day_file_with_words_after_the_date_is_that_day(conn, config):
    add_entry(conn, date(2024, 1, 14), summary="Car trouble", path="/vault/2024-01-14 needs links.md")
    entry_id = add_entry(conn, date(2024, 1, 15), links=["2024-01-14 needs links"], path="/vault/15.md")
    assert store.unresolved_links_for_entry(conn, entry_id) == []
    assert [r["entry_date"] for r in store.days_linked_from_entry(conn, entry_id)] == ["2024-01-14"]
    earlier = store.entry_by_id(conn, entry_id - 1)
    assert [r["id"] for r in store.entries_linking_to_day(conn, earlier)] == [entry_id]
    linked = _entry(["2024-01-14 needs links"], {})
    assert context.entry_links(conn, linked, config.vault.path, 600) == [
        "- [[2024-01-14]], another day this entry links to: Car trouble"
    ]


def test_an_entry_never_reads_its_own_old_version(conn, config):
    add_entry(conn, date(2024, 1, 15), text=DAY, summary="Old me", path="/vault/15.md")
    own = _entry(["2024-01-15"], {"2024-01-15": ["Work"]})
    assert context.entry_links(conn, own, config.vault.path, 600) == []


def test_the_cache_keeps_anchors_and_file_names(conn):
    entry = ParsedEntry(date(2024, 1, 15), "x", "/vault/2024-01-15 trip.md", links=["2024-01-14"],
                        link_anchors={"2024-01-14": ["Work", "", "^ab"]})
    store.upsert_file(conn, entry.source_path, "h", "indexed")
    entry_id = store.save_entry(conn, entry, Extraction(summary="s"), "m")
    assert store.link_anchors_for_entries(conn, [entry_id]) == {entry_id: {"2024-01-14": ["Work", "^ab"]}}
    key = conn.execute("SELECT key FROM entry_file_keys WHERE path = ?", (entry.source_path,)).fetchone()
    assert key["key"] == "2024-01-15 trip"
    store.delete_entries_for_file(conn, entry.source_path)
    assert conn.execute("SELECT COUNT(*) FROM entry_file_keys").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM entry_link_anchors").fetchone()[0] == 0


def test_older_caches_learn_their_file_names_without_reindexing(conn):
    add_entry(conn, date(2024, 1, 14), path="/vault/2024-01-14 trip.md")
    conn.execute("DELETE FROM entry_file_keys")
    db.init_schema(conn)
    assert [r["key"] for r in conn.execute("SELECT key FROM entry_file_keys")] == ["2024-01-14 trip"]


def test_index_fills_in_anchors_for_entries_indexed_before(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    (cfg.vault.path / "2024-01-14.md").write_text(DAY, encoding="utf-8")
    (cfg.vault.path / "2024-01-15.md").write_text("Back to [[2024-01-14#Work]].", encoding="utf-8")
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    later = conn.execute("SELECT id FROM entries WHERE entry_date = '2024-01-15'").fetchone()["id"]
    assert store.link_anchors_for_entries(conn, [later]) == {later: {"2024-01-14": ["Work"]}}

    conn.execute("DELETE FROM entry_link_anchors")
    conn.execute("DELETE FROM meta WHERE key = 'link_details'")
    conn.commit()
    calls = len(fake_client.structured_calls)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert store.link_anchors_for_entries(conn, [later]) == {later: {"2024-01-14": ["Work"]}}
    assert len(fake_client.structured_calls) == calls


def test_day_and_note_parts_come_out_in_json_shapes(conn):
    add_note(conn, "John Doe", text="# Kids\nTwo.")
    earlier = add_entry(conn, date(2024, 1, 14), text=DAY, path="/vault/14.md")
    entry = ParsedEntry(date(2024, 1, 15), "x", "/vault/15.md", links=["2024-01-14", "john doe"],
                        link_anchors={"2024-01-14": ["Work", "Evening"], "john doe": ["Kids"]})
    store.upsert_file(conn, entry.source_path, "h", "indexed")
    later = store.save_entry(conn, entry, Extraction(summary="s"), "m")
    john = store.find_notes(conn, "John Doe")[0]["id"]
    assert serialize.note_parts(conn, later) == {john: [{"anchor": "Kids", "label": "Kids"}]}
    assert serialize.days_linked_from(conn, later) == [{
        "id": earlier, "date": "2024-01-14",
        "sections": [{"anchor": "Work", "label": "Work"}, {"anchor": "Evening", "label": "Evening"}],
    }]
    [linking] = serialize.days_linking_to(conn, store.entry_by_id(conn, earlier))
    assert linking["id"] == later and [s["label"] for s in linking["sections"]] == ["Work", "Evening"]


def test_show_lists_the_parts_of_other_days(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    (cfg.vault.path / "2024-01-14.md").write_text(DAY, encoding="utf-8")
    (cfg.vault.path / "2024-01-15.md").write_text("Back to [[2024-01-14#Work]].", encoding="utf-8")
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    conn.close()
    result = invoke_cli(tmp_path, cfg, "show", "2024-01-15")
    assert result.exit_code == 0, result.output
    assert "other days: 2024-01-14 (Work)" in result.output
