from datetime import date

from conftest import add_entry, add_note, invoke_cli, make_config

from journal_analyzer import db, ingester, nicknames, query, serialize, store, unlinked
from journal_analyzer.models import Extraction, ParsedEntry


def _linked(conn, day: int, labels: dict[str, list[str]], text: str = "x") -> int:
    path = f"/vault/2024-01-{day:02d}.md"
    store.upsert_file(conn, path, "h", "indexed")
    entry = ParsedEntry(date(2024, 1, day), text, path, links=list(labels), link_labels=labels)
    return store.save_entry(conn, entry, Extraction(summary=text), "m")


def _learned(config, conn):
    nicknames.refresh(config, conn)
    return [(name, n) for _, name, _, n in nicknames.learn(config, conn)]


def test_link_text_used_twice_becomes_a_name(conn, config):
    add_note(conn, "John Doe", aliases=["John"])
    _linked(conn, 1, {"john doe": ["Johnny"]})
    assert _learned(config, conn) == []
    _linked(conn, 2, {"john doe": ["johnny"]})
    assert _learned(config, conn) == [("Johnny", 2)]
    config.notes.learned_alias_min = 0
    assert _learned(config, conn) == []


def test_what_never_counts_as_a_name(conn, config):
    add_note(conn, "John Doe", aliases=["John"])
    add_note(conn, "Jane Doe")
    for day in (1, 2):
        _linked(conn, day, {
            "john doe": ["him", "John", "Jo", "his place", "Johnny's", "Boss"],
            "jane doe": ["Boss", "home"],
        })
    config.notes.learned_alias_ignore = ["HOME"]
    # Boss names two people, so neither; Johnny's is Johnny
    assert _learned(config, conn) == [("his place", 2), ("Johnny", 2)]


def test_possessive_of_a_known_name_is_not_new(conn, config):
    add_note(conn, "Jane Doe")
    for day in (1, 2):
        _linked(conn, day, {"jane doe": ["Jane Doe's", "Mom's"]})
    assert _learned(config, conn) == [("Mom", 2)]


def test_learned_names_find_the_note(conn, config):
    add_note(conn, "John Doe", aliases=["John"])
    add_note(conn, "Johnson", kind="places")
    for day in (1, 2):
        _linked(conn, day, {"john doe": ["Johnny"]})
    nicknames.refresh(config, conn)
    john = store.find_notes(conn, "John Doe")[0]["id"]
    assert [r["id"] for r in store.find_notes(conn, "johnny")] == [john]
    assert store.note_ids_matching(conn, ["johnn"]) == [john]
    assert query.notes_named_in("How is Johnny doing?", conn) == [john]
    assert serialize.learned_names(conn, [john]) == {john: [{"name": "Johnny", "entries": 2}]}


def test_links_still_resolve_only_by_name_and_alias(conn, config):
    add_note(conn, "John Doe")
    for day in (1, 2):
        _linked(conn, day, {"john doe": ["Johnny"]})
    nicknames.refresh(config, conn)
    later = add_entry(conn, date(2024, 1, 3), links=["Johnny"], path="/vault/3.md")
    assert store.entities_for_entry(conn, later) == []
    assert store.unresolved_links_for_entry(conn, later) == ["johnny"]


def test_unlinked_mentions_catch_the_learned_name(conn, config):
    add_note(conn, "John Doe")
    for day in (1, 2):
        _linked(conn, day, {"john doe": ["Johnny"]})
    add_entry(conn, date(2024, 1, 5), text="Coffee with Johnny today.", links=[], path="/vault/5.md")
    nicknames.refresh(config, conn)
    john = store.find_notes(conn, "John Doe")[0]["id"]
    [mention] = unlinked.for_note(conn, john)
    assert (mention.entry_date, mention.name) == ("2024-01-05", "Johnny")


def test_answers_are_told_the_names_the_journal_uses(conn, config):
    add_note(conn, "John Doe", text="Friend.")
    for day in (1, 2):
        _linked(conn, day, {"john doe": ["Johnny"]})
    nicknames.refresh(config, conn)
    notes = store.find_notes(conn, "John Doe")
    context = query._summary_context([], notes, query.QueryPlan(intent="factual"), config, conn)
    assert "the journal also writes it as Johnny" in context
    assert query._notes_used("You saw Johnny twice.", notes, [], query._learned_names(conn, notes)) == ["John Doe"]


def test_a_reindexed_note_gets_its_names_back(tmp_path, fake_client):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    people = cfg.vault.path / "Links" / "People"
    people.mkdir(parents=True)
    (people / "John Doe.md").write_text("Friend.", encoding="utf-8")
    for day in (1, 2):
        (cfg.vault.path / f"2024-01-0{day}.md").write_text("Saw [[John Doe|Johnny]].", encoding="utf-8")
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert [r["name"] for r in conn.execute("SELECT name FROM note_learned_names")] == ["Johnny"]

    (people / "John Doe.md").write_text("Old friend.", encoding="utf-8")
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert [r["name"] for r in conn.execute("SELECT name FROM note_learned_names")] == ["Johnny"]

    conn.execute("DELETE FROM entry_link_labels")
    conn.execute("DELETE FROM meta WHERE key = 'link_details'")
    conn.commit()
    calls = len(fake_client.structured_calls)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert conn.execute("SELECT COUNT(*) FROM entry_link_labels").fetchone()[0] == 2
    assert len(fake_client.structured_calls) == calls
    conn.close()

    result = invoke_cli(tmp_path, cfg, "note", "Johnny")
    assert result.exit_code == 0, result.output
    assert "you also write: Johnny (2 entries)" in result.output
