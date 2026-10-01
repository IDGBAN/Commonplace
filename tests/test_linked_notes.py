from datetime import date

from conftest import add_entry, add_note, copy_fixtures, copy_note_fixtures, make_config

from journal_analyzer import context, extractor, ingester, parser, prompts, rollups, store
from journal_analyzer import db as db_mod
from journal_analyzer.models import DetailedExtraction, Extraction, ParsedEntry


def _note_ids(conn, entry_id):
    return {r["name"] for r in store.entities_for_entry(conn, entry_id)}


def test_link_labels_keep_how_a_link_was_shown():
    labels = parser.link_labels("[[John Doe|John]] and [[Bear Mountain]] and [[John Doe|S.]] [[img.png]]")
    assert labels == {"john doe": ["John", "S."]}


def test_entries_carry_their_links_labels(tmp_path):
    cfg = make_config(tmp_path)
    [entry] = parser.parse_file(copy_fixtures(cfg.vault.path, ["2024-01-15.md"])[0], cfg)
    assert entry.link_labels == {"john doe": ["John"]}


def test_a_link_to_an_alias_finds_its_note(conn):
    add_note(conn, "John Doe", aliases=["John"])
    entry_id = add_entry(conn, date(2024, 1, 1), links=["John"])
    assert _note_ids(conn, entry_id) == {"John Doe"}
    assert store.unresolved_links_for_entry(conn, entry_id) == []


def test_a_note_named_like_the_alias_wins(conn):
    add_note(conn, "John Doe", aliases=["John"])
    add_note(conn, "John", kind="pets")
    entry_id = add_entry(conn, date(2024, 1, 1), links=["John"])
    assert _note_ids(conn, entry_id) == {"John"}


def test_an_alias_two_notes_share_resolves_to_neither(conn):
    add_note(conn, "John Doe", aliases=["John"])
    add_note(conn, "John Lee", aliases=["John"])
    entry_id = add_entry(conn, date(2024, 1, 1), links=["John"])
    assert _note_ids(conn, entry_id) == set()
    assert store.unresolved_links_for_entry(conn, entry_id) == ["john"]


def test_note_links_resolve_aliases_too(conn):
    john = add_note(conn, "John Doe", aliases=["John"])
    garden = add_note(conn, "Garden", links=["John"])
    assert [r["title"] for r in store.note_outlinks(conn, garden)] == ["John Doe"]
    assert [r["title"] for r in store.note_backlinks(conn, john)] == ["Garden"]


def test_an_alias_link_marks_the_notes_digest_stale(conn):
    add_note(conn, "John Doe", aliases=["John"])
    store.upsert_note_digest(conn, "john doe", "s", store.Mentions(1, "2024-01-01", "2024-01-01"), None)
    store.mark_note_digests_stale(conn, ["john"])
    assert conn.execute("SELECT stale FROM note_digests").fetchone()[0] == 1


def test_note_blocks_say_what_the_note_is_and_how_it_was_written(conn, config):
    add_note(conn, "John Doe", aliases=["John"], properties={"Relationship": "Friend"},
             text="We met at school. " * 10)
    row = store.find_notes(conn, "John Doe")[0]
    block = context.note_block(row, None, 40, ["John"])
    assert block.startswith('- John Doe (people); written as "John"; also called John; Relationship: Friend: We met')
    assert block.endswith("…") and len(block.split(": ", 2)[-1]) <= 41


def test_fit_keeps_whole_blocks_in_order():
    assert context.fit(["aaaa", "bbbbbbbbbb", "cc"], 9) == ["aaaa", "cc"]


def test_entry_links_cover_notes_and_other_days(conn, config):
    add_note(conn, "John Doe", aliases=["John"], text="Friend.")
    add_entry(conn, date(2024, 1, 14), summary="Bought boots", path="/vault/14.md")
    entry = ParsedEntry(date(2024, 1, 15), "x", "/vault/15.md",
                        links=["john", "2024-01-14", "nobody"], link_labels={"john": ["John"]})
    blocks = context.entry_links(conn, entry, config.vault.path, 600)
    assert blocks == [
        '- John Doe (people); written as "John"; also called John: Friend.',
        "- [[2024-01-14]], another day this entry links to: Bought boots",
    ]
    assert context.entry_links(conn, entry, config.vault.path, 0) == []


def _extract_user_message(config, fake_client, entry, linked):
    seen = {}

    def respond(model, messages):
        seen["user"] = messages[-1]["content"]
        return Extraction(summary="ok")

    fake_client.structured["Extraction"] = respond
    fake_client.structured["DetailedExtraction"] = lambda model, messages: (
        respond(model, messages) and DetailedExtraction(summary="ok")
    )
    extractor.extract(entry, config, fake_client, linked=linked)
    return seen["user"]


def test_extraction_gets_the_linked_notes_that_fit(config, fake_client):
    entry = ParsedEntry(date(2024, 3, 5), "Climbed with John.", "x")
    user = _extract_user_message(config, fake_client, entry, ["- John Doe (people): Friend."])
    assert user.endswith("not these:\n- John Doe (people): Friend.")

    huge = ParsedEntry(date(2024, 3, 5), "word " * 6_000, "x")
    user = _extract_user_message(config, fake_client, huge, ["- John Doe (people): Friend."])
    assert "John Doe" not in user


def test_a_bigger_window_fits_more_notes(config, fake_client):
    entry = ParsedEntry(date(2024, 3, 5), "Busy day.", "x")
    blocks = [f"- Person {i} (people): " + "detail " * 80 for i in range(40)]
    small = _extract_user_message(config, fake_client, entry, blocks).count("- Person")
    config.ollama.fast_num_ctx = 16384
    large = _extract_user_message(config, fake_client, entry, blocks).count("- Person")
    assert 0 < small < large


def test_indexing_hands_extraction_the_linked_notes(tmp_path, fake_client):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    copy_note_fixtures(cfg.vault.path)
    seen = []

    def respond(model, messages):
        seen.append(messages[-1]["content"])
        return Extraction(summary="ok")

    fake_client.structured["Extraction"] = respond
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    [entry_prompt] = [s for s in seen if s.startswith("Journal entry dated")]
    assert '- John Doe (people); written as "John"; also called John; Relationship: Friend' in entry_prompt
    assert "- Bear Mountain (locations)" in entry_prompt


def test_digests_get_the_notes_their_entries_link(conn, config, fake_client):
    add_note(conn, "John Doe", text="Friend who keeps bees.")
    add_note(conn, "Office", kind="places", text="Where Meridian happens.")
    for day in (1, 2):
        add_entry(conn, date(2024, 1, day), links=["John Doe"], path=f"/vault/{day}.md")
    add_entry(conn, date(2024, 1, 3), links=["Office"], path="/vault/3.md")
    seen = []
    fake_client.chat_response = lambda model, messages: seen.append(messages[-1]["content"]) or "ok"
    rollups.generate_rollups(config, conn, fake_client, ("month", "year"))
    month, year = seen
    assert "Notes about the people, places and topics these entries link to" in month
    assert month.index("John Doe (people)") < month.index("Office (places)")
    assert "Friend who keeps bees." in year

    seen.clear()
    config.rollups.linked_notes_chars = 0
    rollups.generate_rollups(config, conn, fake_client, ("month",), only_stale=False)
    assert "Notes about" not in seen[0]


def test_writing_prompts_see_who_the_recent_entries_link(conn, config):
    add_note(conn, "John Doe", text="Friend who keeps bees.")
    add_entry(conn, date(2024, 1, 10), links=["John Doe"], summary="Bees")
    text = prompts._recent_context(conn, date(2024, 1, 1), date(2024, 1, 31), config)
    assert "Who and what these entries link to:\n- John Doe (people): Friend who keeps bees." in text
