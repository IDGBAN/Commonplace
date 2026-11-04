from datetime import date

from conftest import (
    add_entry,
    add_note,
    copy_fixtures,
    copy_note_fixtures,
    invoke_cli,
    make_config,
)

from journal_analyzer import db as db_mod
from journal_analyzer import ingester, query, rollups, store
from journal_analyzer.models import QueryPlan


def _john(conn, entries: int = 3, start_year: int = 2023) -> int:
    note_id = add_note(conn, "John Doe", text="Friend from school.", aliases=["John"],
                       properties={"Relationship": "Friend"})
    for i in range(entries):
        add_entry(conn, date(start_year + i % 2, 1 + i // 2, 10), mood=4.0 + i,
                  tags=["friends"], links=["John Doe"], summary=f"saw John {i}",
                  path=f"/vault/e{i}.md")
    return note_id


def _with_min(config, minimum: int):
    return config.model_copy(
        update={"rollups": config.rollups.model_copy(update={"note_min_mentions": minimum})}
    )


def _digest(conn, key: str = "john doe"):
    return conn.execute("SELECT * FROM note_digests WHERE link_key = ?", (key,)).fetchone()


def test_notes_linked_often_enough_get_a_digest(conn, config, fake_client):
    _john(conn)
    add_note(conn, "Rarely Seen")
    add_entry(conn, date(2023, 5, 1), links=["Rarely Seen"], path="/vault/r.md")
    fake_client.chat_response = "John has been my climbing friend for years."
    counts = rollups.generate_rollups(_with_min(config, 3), conn, fake_client)
    assert counts["note"] == 1
    row = _digest(conn)
    assert row["summary"] == "John has been my climbing friend for years."
    assert (row["entry_count"], row["first_date"], row["last_date"]) == (3, "2023-01-10", "2024-01-10")
    assert row["avg_mood"] == 5.0 and row["stale"] == 0
    assert _digest(conn, "rarely seen") is None


def test_the_digest_reads_the_note_and_each_years_entries(conn, config, fake_client):
    _john(conn)
    seen = []

    def chat(model, messages):
        seen.append((model, messages))
        return "ok"

    fake_client.chat_response = chat
    rollups.generate_rollups(_with_min(config, 3), conn, fake_client, period_types=("note",))
    [(model, messages)] = seen
    assert model == config.ollama.precise_model
    text = messages[-1]["content"]
    assert text.startswith("Note: John Doe (people)")
    assert "Relationship: Friend" in text and "What the note says: Friend from school." in text
    assert "Linked from 3 entries, 2023-01-10 to 2024-01-10" in text
    assert "2023 (2 entries, average mood 5.0):" in text
    assert "- 2024-01-10 (mood 5) [friends]: saw John 1" in text
    assert text.index("2023 (") < text.index("2024 (")


def test_busy_years_are_sampled_evenly(conn, config):
    note_id = add_note(conn, "John Doe")
    for day in range(1, 29):
        add_entry(conn, date(2024, 2, day), links=["John Doe"], summary=f"d{day}",
                  path=f"/vault/{day}.md")
    small = config.model_copy(
        update={"rollups": config.rollups.model_copy(update={"note_entries_per_year": 5})}
    )
    text, mentions, _ = rollups.note_digest_input(small, conn, store.notes_by_ids(conn, [note_id])[0])
    assert "2024 (28 entries, average mood 6.0, 5 shown):" in text
    shown = [line for line in text.splitlines() if line.startswith("- 2024")]
    assert len(shown) == 5
    assert shown[0].startswith("- 2024-02-01") and shown[-1].startswith("- 2024-02-28")
    assert mentions is not None and mentions.count == 28


def test_fresh_digests_are_kept_and_stale_ones_rewritten(conn, config, fake_client):
    _john(conn)
    cfg = _with_min(config, 3)
    rollups.generate_rollups(cfg, conn, fake_client, period_types=("note",))
    calls = len(fake_client.chat_calls)
    assert rollups.generate_rollups(cfg, conn, fake_client, period_types=("note",))["note"] == 0
    store.mark_note_digests_stale(conn, ["john doe"])
    assert rollups.generate_rollups(cfg, conn, fake_client, period_types=("note",))["note"] == 1
    assert len(fake_client.chat_calls) == calls + 1
    assert rollups.generate_rollups(
        cfg, conn, fake_client, period_types=("note",), only_stale=False
    )["note"] == 1


def test_digests_go_when_their_note_stops_qualifying(conn, config, fake_client):
    _john(conn)
    rollups.generate_rollups(_with_min(config, 3), conn, fake_client, period_types=("note",))
    rollups.generate_rollups(_with_min(config, 4), conn, fake_client, period_types=("note",))
    assert _digest(conn) is None


def test_zero_turns_them_off(conn, config, fake_client):
    _john(conn)
    assert rollups.generate_rollups(_with_min(config, 0), conn, fake_client)["note"] == 0


def test_editing_a_linking_entry_or_the_note_makes_it_stale(tmp_path, fake_client):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    cfg = _with_min(cfg, 1)
    entries = copy_fixtures(cfg.vault.path)
    notes = copy_note_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    rollups.generate_rollups(cfg, conn, fake_client, period_types=("note",))
    assert _digest(conn)["stale"] == 0

    entries[0].write_text("Rewritten, and no longer about anyone.", encoding="utf-8")
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert _digest(conn)["stale"] == 1  # the old version linked John

    rollups.generate_rollups(cfg, conn, fake_client, period_types=("note",))
    mountain = _digest(conn, "bear mountain")
    assert mountain is None  # nothing links it now
    john_note = notes / "People" / "John Doe.md"
    john_note.write_text(john_note.read_text(encoding="utf-8") + "\nNew fact.", encoding="utf-8")
    entries[1].write_text("Saw [[John Doe]] again.", encoding="utf-8")
    ingester.run_backfill(cfg, conn, client=fake_client)
    rollups.generate_rollups(cfg, conn, fake_client, period_types=("note",))
    assert _digest(conn)["stale"] == 0

    entries[1].unlink()
    ingester.run_backfill(cfg, conn, client=fake_client)
    row = _digest(conn)
    assert row is None or row["stale"] == 1


def test_answers_about_a_note_read_its_digest(conn, config, fake_client):
    _john(conn)
    rollups.generate_rollups(_with_min(config, 3), conn, fake_client, period_types=("note",))
    store.upsert_note_digest(
        conn, "john doe", "John and I drifted, then grew close again.",
        store.Mentions(3, "2023-01-10", "2024-01-10"), 5.0,
    )
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual", entities=["John"])
    seen = []

    def chat(model, messages):
        seen.append(messages[-1]["content"])
        return "INSUFFICIENT" if model == config.ollama.fast_model else "From the full text."

    fake_client.chat_response = chat
    query.answer("Who is John to me?", _with_min(config, 3), conn, fake_client, today=date(2024, 6, 1))
    assert "- [[John Doe]]: John and I drifted, then grew close again." in seen[0]
    assert "Over the years: John and I drifted, then grew close again." in seen[1]


def test_note_command_prints_the_digest(tmp_path, config, conn):
    _john(conn, entries=8)
    store.upsert_note_digest(
        conn, "john doe", "John keeps turning up.", store.Mentions(8, "2023-01-10", "2024-04-10"), 6.0,
    )
    output = " ".join(invoke_cli(tmp_path, config, "note", "John Doe").output.split())
    assert "Over the years John keeps turning up." in output
    store.mark_note_digests_stale(conn, ["john doe"])
    output = " ".join(invoke_cli(tmp_path, config, "note", "John Doe").output.split())
    assert "out of date" in output


def test_note_command_says_when_one_is_due(tmp_path, config, conn):
    _john(conn, entries=8)
    output = " ".join(invoke_cli(tmp_path, config, "note", "John Doe").output.split())
    assert "No relationship digest yet" in output
