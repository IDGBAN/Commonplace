from datetime import date

from conftest import make_config

from journal_analyzer import db, ingester, parser, query, store
from journal_analyzer.models import QueryPlan

LONG_DAY = """Up early.

# Morning
Coffee on the balcony and a long talk with my sister about the move, the boxes and the money.

# Work
The quarterly review ran three hours. My manager liked the migration plan and the budget passed.

## Lunch
Noodles with the team, then back to the spreadsheets for the afternoon.

# Evening
Read until late.
"""


def test_entries_are_cut_at_their_top_level_headings():
    parts = parser.split_sections(LONG_DAY, 40)
    assert [p.heading for p in parts] == ["Morning", "Work"]
    # the short line before the first heading joins it, and the short last part joins the one before
    assert parts[0].text.startswith("Up early.\n\n# Morning")
    assert parts[1].text.endswith("Read until late.")
    assert "## Lunch" in parts[1].text
    assert (parts[0].start, parts[1].end) == (0, len(LONG_DAY.split("\n")))


def test_entries_with_one_part_are_not_cut():
    assert parser.split_sections("Just a few lines.\nNothing else.", 10) == []
    assert parser.split_sections("# Only\nOne heading here.", 0) == []
    assert parser.split_sections("# A\nshort\n# B\nshort", 100) == []
    fenced = "Text.\n```\n# not a heading\n```\n# Real\nMore text here."
    assert [p.heading for p in parser.split_sections(fenced, 0)] == ["", "Real"]


def test_second_level_headings_count_when_there_are_no_first():
    text = "## Morning\n" + "a " * 60 + "\n## Night\n" + "b " * 60
    assert [p.heading for p in parser.split_sections(text, 50)] == ["Morning", "Night"]


def _indexed(tmp_path, fake_client, **indexing):
    cfg = make_config(tmp_path)
    for key, value in indexing.items():
        setattr(cfg.indexing, key, value)
    (cfg.vault.path / "2024-03-04.md").write_text(LONG_DAY, encoding="utf-8")
    (cfg.vault.path / "2024-03-05.md").write_text("A short quiet day at home.", encoding="utf-8")
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    return cfg, conn


def _entry_id(conn, day):
    return conn.execute("SELECT id FROM entries WHERE entry_date = ?", (day,)).fetchone()["id"]


def test_indexing_keeps_the_parts_with_a_vector_each(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client, section_min_chars=40)
    long_day = _entry_id(conn, "2024-03-04")
    parts = store.sections_for_entries(conn, [long_day, _entry_id(conn, "2024-03-05")])
    assert [p["heading"] for p in parts[long_day]] == ["Morning", "Work"]
    assert conn.execute("SELECT COUNT(*) FROM sections_vec").fetchone()[0] == 2
    assert sum("quarterly review" in t for t in fake_client.embed_calls) == 2  # the entry and its part

    store.delete_entries_for_file(conn, str(cfg.vault.path / "2024-03-04.md"))
    assert conn.execute("SELECT COUNT(*) FROM entry_sections").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sections_vec").fetchone()[0] == 0
    conn.close()


def test_turning_parts_off_and_on_needs_no_reindex(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client, section_min_chars=40)
    cfg.indexing.sections = False
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert conn.execute("SELECT COUNT(*) FROM entry_sections").fetchone()[0] == 0
    extractions = len(fake_client.structured_calls)
    cfg.indexing.sections = True
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert conn.execute("SELECT COUNT(*) FROM entry_sections").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM sections_vec").fetchone()[0] == 2
    assert len(fake_client.structured_calls) == extractions
    conn.close()


def test_dry_run_counts_the_part_vectors(tmp_path):
    cfg = make_config(tmp_path)
    cfg.indexing.section_min_chars = 40
    (cfg.vault.path / "2024-03-04.md").write_text(LONG_DAY, encoding="utf-8")
    plan = ingester.plan_backfill(cfg, None)
    assert plan.model_calls() == (1, 0, 3)


def test_search_finds_the_part_that_matched(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client, section_min_chars=40)
    cfg.reranking.enabled = False
    long_day = _entry_id(conn, "2024-03-04")
    found = query.search("quarterly review", cfg, conn, fake_client)
    assert found.entries[0]["id"] == long_day
    assert found.parts[long_day]["heading"] == "Work"
    keywords = query.keyword_search("balcony", conn)
    assert keywords.parts[long_day]["heading"] == "Morning"
    conn.close()


def test_answers_read_and_cite_the_part(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client, section_min_chars=40)
    cfg.reranking.enabled = False
    seen = []

    def reply(model, messages):
        seen.append(messages[-1]["content"])
        return "The review was in the morning meeting on 2024-03-04."

    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual", keywords=["quarterly", "review"])
    fake_client.chat_response = reply
    answer = query.answer("How did the quarterly review go?", cfg, conn, fake_client)
    assert answer.stage_used == "fast_summary"
    assert answer.cited_parts == {"2024-03-04": "Work"}
    assert "Sources: [[2024-03-04#Work]]" in answer.text
    assert 'The part of that entry that matches, under "Work": # Work The quarterly review' in seen[-1]
    conn.close()


def test_sources_only_name_parts_of_cited_days():
    answer = query._finalize("x", "fast_summary", cited_dates=[date(2024, 3, 4)],
                             cited_parts={"2024-03-04": "Work", "2024-03-09": "Gone"})
    assert answer.cited_parts == {"2024-03-04": "Work"}
    assert answer.text.endswith("Sources: [[2024-03-04#Work]]")
