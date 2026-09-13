from datetime import date

from conftest import add_entry, add_note, copy_fixtures, copy_note_fixtures, make_config

from journal_analyzer import db as db_mod
from journal_analyzer import ingester, query
from journal_analyzer.models import QueryPlan

TODAY = date(2024, 6, 1)

CANNED_PLANS = {
    "How did my mood change over spring?": QueryPlan(
        intent="aggregate",
        date_from=date(2024, 3, 1),
        date_to=date(2024, 5, 31),
    ),
    "When did I go hiking with Sara?": QueryPlan(
        intent="factual",
        entities=["Sara"],
        keywords=["hiking"],
        needs_raw_text=False,
    ),
    "What exactly did Sara say about the garden project?": QueryPlan(
        intent="factual",
        entities=["Sara"],
        keywords=["garden"],
        needs_raw_text=True,
    ),
    "Tell me about January.": QueryPlan(
        intent="narrative",
        date_from=date(2024, 1, 1),
        date_to=date(2024, 1, 31),
    ),
}


def _router(model, messages):
    question = messages[-1]["content"]
    return CANNED_PLANS[question]


def _indexed(tmp_path, fake_client, with_notes=False):
    cfg = make_config(tmp_path, notes_paths=["Links"] if with_notes else None)
    copy_fixtures(cfg.vault.path)
    if with_notes:
        copy_note_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    fake_client.structured["QueryPlan"] = _router
    return cfg, conn


def test_router_intents_and_date_ranges(tmp_path, fake_client):
    cfg, _conn = _indexed(tmp_path, fake_client)
    plan = query._plan("How did my mood change over spring?", cfg, fake_client, TODAY)
    assert plan.intent == "aggregate"
    assert (plan.date_from, plan.date_to) == (date(2024, 3, 1), date(2024, 5, 31))
    plan = query._plan("Tell me about January.", cfg, fake_client, TODAY)
    assert plan.intent == "narrative"
    assert (plan.date_from, plan.date_to) == (date(2024, 1, 1), date(2024, 1, 31))


def test_aggregate_answers_from_sql_without_retrieval(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)
    embeds_before = len(fake_client.embed_calls)
    fake_client.chat_response = "Your mood averaged 7 in spring."
    # nothing in range, still no embedding
    result = query.answer(
        "How did my mood change over spring?", cfg, conn, fake_client, today=TODAY
    )
    assert result.stage_used == "aggregate"
    assert len(fake_client.embed_calls) == embeds_before


def test_factual_answered_from_summaries_with_citations(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)
    fake_client.chat_response = "You went hiking with Sara on 2024-01-15."
    result = query.answer(
        "When did I go hiking with Sara?", cfg, conn, fake_client, today=TODAY
    )
    assert result.stage_used == "fast_summary"
    assert date(2024, 1, 15) in result.cited_dates
    assert "[[2024-01-15]]" in result.text


def test_insufficient_escalates_to_precise_raw_text(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)

    def chat(model, messages):
        if model == cfg.ollama.fast_model:
            return "INSUFFICIENT"
        assert model == cfg.ollama.precise_model
        # full text, not the summary
        assert "garden-project" in messages[-1]["content"]
        return "Sara talked about replanting the garden-project beds."

    fake_client.chat_response = chat
    result = query.answer(
        "When did I go hiking with Sara?", cfg, conn, fake_client, today=TODAY
    )
    assert result.stage_used == "precise_raw"
    assert date(2024, 1, 15) in result.cited_dates


def test_a_dressed_up_insufficient_still_escalates(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)
    fake_client.chat_response = lambda model, messages: (
        "**INSUFFICIENT**" if model == cfg.ollama.fast_model else "From the full text."
    )
    result = query.answer(
        "When did I go hiking with Sara?", cfg, conn, fake_client, today=TODAY
    )
    assert result.stage_used == "precise_raw"


def test_explicit_day_is_added_alongside_normal_retrieval(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)
    # the router finds Meridian (01-16) but misses "January 15"
    fake_client.structured["QueryPlan"] = lambda model, messages: QueryPlan(
        intent="factual", entities=["Meridian"], keywords=["Meridian"]
    )
    fake_client.chat_response = "The Meridian deadline moved up; you also went hiking."
    result = query.answer(
        "What happened with Meridian, and also on January 15, 2024?",
        cfg, conn, fake_client, today=TODAY,
    )
    assert result.stage_used == "fast_summary"
    assert date(2024, 1, 16) in result.cited_dates
    assert date(2024, 1, 15) in result.cited_dates


def test_explicit_day_included_even_when_router_misses_range(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)
    fake_client.structured["QueryPlan"] = lambda model, messages: QueryPlan(
        intent="factual", date_from=date(2023, 11, 1), date_to=date(2023, 11, 30)
    )
    fake_client.chat_response = "You went hiking with Sara on 2024-01-15."
    result = query.answer(
        "What happened on January 15, 2024?", cfg, conn, fake_client, today=TODAY
    )
    assert result.stage_used == "fast_summary"
    assert result.cited_dates == [date(2024, 1, 15)]


def test_needs_raw_text_skips_fast_attempt(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)

    def chat(model, messages):
        assert model == cfg.ollama.precise_model
        return "She said the beds need replanting."

    fake_client.chat_response = chat
    result = query.answer(
        "What exactly did Sara say about the garden project?",
        cfg, conn, fake_client, today=TODAY,
    )
    assert result.stage_used == "precise_raw"
    assert "[[2024-01-15]]" in result.text


def test_answers_see_the_notes_the_question_and_entries_point_at(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client, with_notes=True)
    seen = {}

    def chat(model, messages):
        seen["context"] = messages[-1]["content"]
        return "You went hiking with Sara, your friend, on 2024-01-15."

    fake_client.chat_response = chat
    result = query.answer(
        "When did I go hiking with Sara?", cfg, conn, fake_client, today=TODAY
    )
    assert result.stage_used == "fast_summary"
    context = seen["context"]
    assert "Reference notes:" in context
    assert (
        "[[Sara Miller]] (people; file: Links/People/Sara Miller.md; "
        "Relationship: Friend; also called Sara" in context
    )
    assert (
        "- [2024-01-15] A pleasant day with friends outdoors. (file: 2024-01-15.md; "
        "links: Bear Mountain, Sara Miller, garden-project)" in context
    )
    assert "[[Sara Miller]]" in result.text
    assert result.cited_notes == ["Sara Miller"]


def test_notes_the_answer_names_are_cited_even_when_the_question_did_not(
    tmp_path, fake_client
):
    cfg, conn = _indexed(tmp_path, fake_client, with_notes=True)
    fake_client.chat_response = "You hiked Bear Mountain with Sara and planned the garden project."
    result = query.answer(
        "When did I go hiking with Sara?", cfg, conn, fake_client, today=TODAY
    )
    assert result.cited_notes == ["Bear Mountain", "garden-project", "Sara Miller"]


def test_a_question_only_the_notes_can_answer(tmp_path, fake_client):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    copy_note_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    fake_client.structured["QueryPlan"] = lambda model, messages: QueryPlan(
        intent="factual", entities=["Bear Mountain"]
    )
    fake_client.chat_response = "Bear Mountain is a state park an hour north."
    result = query.answer("What is Bear Mountain?", cfg, conn, fake_client, today=TODAY)
    assert result.stage_used == "fast_summary"
    assert result.cited_dates == []
    assert "Bear Mountain" in result.cited_notes


def test_precise_answers_read_the_notes_in_full(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client, with_notes=True)

    def chat(model, messages):
        assert model == cfg.ollama.precise_model
        content = messages[-1]["content"]
        assert "### Note: Sara Miller (people)\nFile: Links/People/Sara Miller.md\n" in content
        assert "### Entry 2024-01-15\nFile: 2024-01-15.md\nWent hiking" in content
        return "She said the beds need replanting."

    fake_client.chat_response = chat
    result = query.answer(
        "What exactly did Sara say about the garden project?",
        cfg, conn, fake_client, today=TODAY,
    )
    assert result.stage_used == "precise_raw"
    assert "Sara Miller" in result.cited_notes


def test_aggregate_answers_count_links_to_named_notes(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client, with_notes=True)
    fake_client.structured["QueryPlan"] = lambda model, messages: QueryPlan(
        intent="aggregate"
    )
    seen = {}

    def chat(model, messages):
        seen["facts"] = messages[-1]["content"]
        return "You mentioned Sara once."

    fake_client.chat_response = chat
    result = query.answer(
        "How often do I write about Sara Miller?", cfg, conn, fake_client, today=TODAY
    )
    assert result.stage_used == "aggregate"
    assert (
        "[[Sara Miller]] (people): linked from 1 entries, first 2024-01-15, "
        "last 2024-01-15; by year 2024: 1." in seen["facts"]
    )
    assert result.cited_notes == ["Sara Miller"]


def test_search_returns_matching_notes_alongside_entries(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client, with_notes=True)
    results = query.search("Sara hiking", cfg, conn, fake_client, limit=5)
    assert results.entries
    assert results.notes[0]["title"] == "Sara Miller"


def test_a_backwards_range_from_the_router_is_turned_around():
    plan = QueryPlan(intent="factual", date_from=date(2024, 5, 31), date_to=date(2024, 3, 1))
    assert (plan.date_from, plan.date_to) == (date(2024, 3, 1), date(2024, 5, 31))


def test_each_stage_is_announced_as_it_starts(conn, config, fake_client):
    add_entry(conn, date(2024, 1, 15), text="hiking")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = lambda model, messages: (
        "INSUFFICIENT" if model == config.ollama.fast_model else "From the full text."
    )
    stages: list[str] = []
    query.answer("q", config, conn, fake_client, today=TODAY, on_stage=stages.append)
    assert stages == [
        "Working out what you're asking...",
        "Searching entries and notes...",
        "Answering from summaries...",
        "Re-reading the full entries...",
    ]


def test_search_keywords_keep_words_that_start_with_an_accent():
    assert query._SEARCH_WORD_RE.findall("dinner with émile in zürich") == [
        "dinner", "with", "émile", "zürich"
    ]


def test_note_names_in_questions_match_whole_words_longest_first(conn):
    sara_miller = add_note(conn, "Sara Miller", aliases=["Sara"])
    sara_topic = add_note(conn, "Sara", kind="topics")
    add_note(conn, "Go", kind="games")
    work = add_note(conn, "Work", kind="topics")
    assert query.notes_named_in("Did Sara Miller go to work?", conn) == [sara_miller, work]
    assert query.notes_named_in("A trip to Saratoga", conn) == []
    assert set(query.notes_named_in("Just Sara today", conn)) == {sara_miller, sara_topic}
