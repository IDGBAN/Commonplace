from datetime import date, timedelta

from conftest import add_entry, add_note

from journal_analyzer import query, store
from journal_analyzer.models import NotePicks, QueryPlan, StructuredOutputError

TODAY = date(2024, 6, 12)
QUESTION = "What do you think I should do tomorrow?"


def _week(conn, days=range(6, 13), **kwargs):
    return [
        add_entry(conn, date(2024, 6, d), text=f"Day {d} text.", summary=f"day {d}", path=f"/vault/{d}.md", **kwargs)
        for d in days
    ]


def _ask(config, conn, fake_client, plan, question=QUESTION, fast="INSUFFICIENT", **kwargs):
    fake_client.structured["QueryPlan"] = plan
    seen: dict[str, list[dict]] = {}

    def chat(model, messages):
        seen[model] = messages
        return fast if model == config.ollama.fast_model else "Finish the report first."

    fake_client.chat_response = chat
    result = query.answer(question, config, conn, fake_client, today=TODAY, **kwargs)
    return result, seen


def test_dates_that_have_not_happened_mean_a_question_about_now():
    tomorrow = TODAY + timedelta(days=1)
    plan = query._settle(
        QueryPlan(intent="factual", date_from=tomorrow, date_to=tomorrow), "q", TODAY
    )
    assert plan.current and plan.date_from is None and plan.date_to is None


def test_a_range_that_runs_past_today_ends_today():
    plan = query._settle(
        QueryPlan(intent="narrative", date_from=date(2024, 6, 10), date_to=date(2024, 6, 16)),
        "How is this week going?", TODAY,
    )
    assert not plan.current
    assert (plan.date_from, plan.date_to) == (date(2024, 6, 10), TODAY)


def test_words_for_days_to_come_mean_a_question_about_now():
    for question in (
        "What should I do tomorrow?", "What should I work on next week?",
        "Any advice for this weekend?", "What are my plans for the rest of the week?",
    ):
        assert query._settle(QueryPlan(intent="factual"), question, TODAY).current, question
    assert not query._settle(QueryPlan(intent="factual"), "When did I hike?", TODAY).current


def test_a_search_that_mentions_the_future_is_still_a_search():
    plan = QueryPlan(intent="factual", entities=["John"])
    assert not query._settle(plan, "What did John say about next year?", TODAY).current
    # with a period that's over, it's that period being asked about
    march = QueryPlan(intent="factual", date_from=date(2024, 3, 1), date_to=date(2024, 3, 31))
    assert not query._settle(march, "What did I plan for next year?", TODAY).current
    assert not query._settle(march.model_copy(update={"current": True}), "q", TODAY).current


def test_a_question_about_now_drops_the_routers_dates_for_the_latest_days():
    plan = QueryPlan(intent="factual", current=True, date_from=date(2024, 6, 10), date_to=TODAY)
    settled = query._settle(plan, "What should I focus on this week?", TODAY)
    assert settled.current and settled.date_from is None and settled.date_to is None


def test_tomorrow_reads_the_past_week_in_order_not_whatever_matches(config, conn, fake_client):
    old = add_entry(conn, date(2024, 1, 5), text="What I should do tomorrow, I thought.", summary="old", path="/vault/old.md")
    week = _week(conn)
    embeds = len(fake_client.embed_calls)
    tomorrow = TODAY + timedelta(days=1)
    # the router did what it used to: took "tomorrow" for the dates to read
    plan = QueryPlan(intent="factual", wants="judgment", date_from=tomorrow, date_to=tomorrow)
    stages: list[str] = []
    result, seen = _ask(config, conn, fake_client, plan, on_stage=stages.append)
    assert result.stage_used == "precise_raw"
    assert set(result.read_entry_ids) == set(week) and old not in result.read_entry_ids
    assert stages[1] == "Gathering your latest entries and notes..."
    system, content = (m["content"] for m in seen[config.ollama.precise_model])
    days = [f"### Entry 2024-06-{d:02d}" for d in range(6, 13)]
    assert [content.index(d) for d in days] == sorted(content.index(d) for d in days)
    assert query._JUDGMENT in system and query._ADVICE in system
    assert "Today is Wednesday, 2024-06-12, so tomorrow is Thursday, 2024-06-13, and then come Friday 14," in system
    # the question isn't searched for by meaning; only the background subjects are embedded
    assert fake_client.embed_calls[embeds:] == [", ".join(query._CURRENT_BACKGROUND)]


def test_it_reads_notes_about_goals_and_the_notes_the_week_links(config, conn, fake_client):
    goals = add_note(conn, "Goals", kind="topics", text="Run a 10k by autumn. Ship the report.")
    improve = add_note(conn, "Self", kind="topics", text="Things to improve: sleep earlier.")
    ana = add_note(conn, "Ana", text="My manager.")
    add_note(conn, "Bear Mountain", kind="places", text="A state park.")
    _week(conn, links=["Ana"])
    result, seen = _ask(config, conn, fake_client, QueryPlan(intent="factual", wants="judgment", current=True))
    assert set(result.read_note_ids) == {goals, improve, ana}
    content = seen[config.ollama.precise_model][-1]["content"]
    assert "### Note: Goals (topics)" in content and "Run a 10k by autumn." in content
    assert "Bear Mountain" not in content


def test_the_nearest_notes_are_only_offered_and_the_fast_model_picks(config, conn, fake_client):
    goals = add_note(conn, "Goals", kind="topics", text="Run a 10k by autumn.", summary="Run a 10k by autumn.")
    bread = add_note(conn, "Sourdough", kind="topics", text="Feed the starter twice a day.")
    # something is always nearest by meaning, whatever it's about
    store.save_note_vector(conn, bread, fake_client.embed("m", "bread"))
    _week(conn)
    offered = {}

    def pick(model, messages):
        assert model == config.ollama.fast_model
        offered["system"], offered["notes"] = messages[0]["content"], messages[-1]["content"]
        return NotePicks(numbers=[1, 9])  # a number that isn't in the list is passed over

    fake_client.structured["NotePicks"] = pick
    result, seen = _ask(config, conn, fake_client, QueryPlan(intent="factual", wants="judgment", current=True))
    assert offered["system"] == query._PICK_SYSTEM
    assert f"Question: {QUESTION}\nSubjects: goals, plans, priorities" in offered["notes"]
    assert "1. Goals.md, Goals: Run a 10k by autumn." in offered["notes"]
    assert "2. Sourdough.md, Sourdough: Feed the starter twice a day." in offered["notes"]
    assert result.read_note_ids == [goals]
    assert "Sourdough" not in seen[config.ollama.precise_model][-1]["content"]


def test_when_the_pick_fails_the_notes_with_the_subjects_words_are_read(config, conn, fake_client):
    goals = add_note(conn, "Goals", kind="topics", text="Run a 10k by autumn.")
    bread = add_note(conn, "Sourdough", kind="topics", text="Feed the starter twice a day.")
    store.save_note_vector(conn, bread, fake_client.embed("m", "bread"))
    _week(conn)

    def broken(model, messages):
        raise StructuredOutputError("not JSON")

    fake_client.structured["NotePicks"] = broken
    result, _seen = _ask(config, conn, fake_client, QueryPlan(intent="factual", wants="judgment", current=True))
    assert result.read_note_ids == [goals]


def test_no_notes_to_offer_means_no_one_is_asked_to_pick(config, conn, fake_client):
    _week(conn)
    _ask(config, conn, fake_client, QueryPlan(intent="factual", wants="judgment", current=True))
    assert "NotePicks" not in [schema for _model, schema in fake_client.structured_calls]


def test_the_routers_own_subjects_are_looked_for_first(config, conn, fake_client):
    thesis = add_note(conn, "Thesis", kind="topics", text="Chapter three is due in July.")
    add_note(conn, "Goals", kind="topics", text="Run a 10k.")
    _week(conn)
    embeds = len(fake_client.embed_calls)
    plan = QueryPlan(intent="factual", wants="judgment", current=True, background=["thesis", "deadlines"])
    result, _seen = _ask(config, conn, fake_client, plan)
    assert result.read_note_ids == [thesis]
    assert fake_client.embed_calls[embeds:] == ["thesis, deadlines"]


def test_a_quiet_week_still_reads_the_latest_few_entries(config, conn, fake_client):
    ids = [
        add_entry(conn, date(2024, m, 1), text=f"month {m}", summary=f"m{m}", path=f"/vault/m{m}.md")
        for m in (1, 2, 3, 4)
    ]
    result, _seen = _ask(config, conn, fake_client, QueryPlan(intent="factual", wants="judgment", current=True))
    assert set(result.read_entry_ids) == set(ids[-query.MIN_RECENT_ENTRIES:])


def test_a_long_week_reads_only_its_newest_entries(config, conn):
    for n in range(query.MAX_RECENT_ENTRIES + 3):
        add_entry(conn, TODAY, text=f"entry {n}", summary=f"e{n}", path=f"/vault/e{n}.md")
    rows = query._recent_entries(conn, TODAY, None, None, None)
    assert len(rows) == query.MAX_RECENT_ENTRIES
    assert rows[0]["summary"] == f"e{query.MAX_RECENT_ENTRIES + 2}"  # newest first


def test_the_newest_entries_linking_a_named_note_come_along(config, conn, fake_client):
    add_note(conn, "Ana")
    older = [
        add_entry(conn, date(2024, m, 3), text="Ana again.", summary="ana", links=["Ana"], path=f"/vault/a{m}.md")
        for m in range(1, 6)
    ]
    week = _week(conn)
    result, _seen = _ask(
        config, conn, fake_client, QueryPlan(intent="factual", wants="judgment", current=True),
        question="What should I say to Ana tomorrow?",
    )
    assert set(result.read_entry_ids) == {*week, *older[-query.CURRENT_TOPIC_ENTRIES:]}
    # the latest days first, so they're the last to go when room runs out
    assert result.read_entry_ids[: len(week)] == week[::-1]


def test_dates_and_filters_the_person_gave_still_apply(config, conn, fake_client):
    _week(conn, days=range(6, 10))
    tagged = _week(conn, days=range(10, 13), tags=["work"])
    plan = QueryPlan(intent="factual", wants="judgment", current=True)
    result, _seen = _ask(config, conn, fake_client, plan, only=store.EntryFilter(tag="work"))
    assert set(result.read_entry_ids) == set(tagged)
    result, _seen = _ask(config, conn, fake_client, plan, until=date(2024, 6, 9))
    read = {r["entry_date"] for r in store.entries_by_ids(conn, result.read_entry_ids)}
    assert read == {f"2024-06-0{d}" for d in range(6, 10)}


def test_what_led_up_to_the_week_is_given_in_brief(config, conn, fake_client):
    week = _week(conn)
    # the two weeks before 2024-06-06 start on 05-23
    lead = [
        add_entry(conn, date(2024, 5, d), text="Every word of it.", summary=f"may {d}", path=f"/vault/may{d}.md")
        for d in (20, 24, 30)
    ]
    # W21 runs into those two weeks, and W20 is the last to end before them
    for key in ("2024-W17", "2024-W18", "2024-W19", "2024-W20", "2024-W21", "2024-W23"):
        store.upsert_rollup(conn, "week", key, f"about {key}", "[]", None, 1)
    result, seen = _ask(config, conn, fake_client, QueryPlan(intent="factual", wants="judgment", current=True))
    content = seen[config.ollama.precise_model][-1]["content"]
    assert (
        "### The two weeks before these entries, a line each\n"
        "- [2024-05-24] may 24\n- [2024-05-30] may 30\n\n"
        "### The weeks before that, in brief\n"
        "- [week 2024-W18] about 2024-W18\n- [week 2024-W19] about 2024-W19\n"
        "- [week 2024-W20] about 2024-W20\n\n### Entry 2024-06-06 (Thursday)" in content
    )
    for left_out in ("W17", "W21", "W23", "may 20", "Every word of it."):
        assert left_out not in content
    # the days it got a line of count as read, but only the ones read in full are cited
    assert set(result.read_entry_ids) == {*week, *lead[1:]}
    assert result.cited_dates == [date(2024, 6, d) for d in range(6, 13)]


def test_entries_say_which_day_of_the_week_they_are_and_which_is_today(config, conn, fake_client):
    _week(conn)
    _result, seen = _ask(config, conn, fake_client, QueryPlan(intent="factual", wants="judgment", current=True))
    system, content = (m["content"] for m in seen[config.ollama.precise_model])
    assert "### Entry 2024-06-10 (Monday)\n" in content
    assert "### Entry 2024-06-11 (Tuesday, yesterday)\n" in content
    assert "### Entry 2024-06-12 (Wednesday, today)\n" in content
    assert '"today" and "tomorrow" count from that date, not from today' in system


def test_a_question_about_how_things_stand_can_be_answered_from_summaries(config, conn, fake_client):
    week = _week(conn)
    store.upsert_rollup(conn, "week", "2024-W20", "about W20", "[]", None, 1)
    result, seen = _ask(
        config, conn, fake_client, QueryPlan(intent="narrative", current=True),
        question="What's been going on with me lately?", fast="Mostly the report.",
    )
    assert result.stage_used == "fast_summary" and set(result.read_entry_ids) == set(week)
    content = seen[config.ollama.fast_model][-1]["content"]
    assert "The latest journal entries, oldest first:\n- [2024-06-06] day 6" in content
    assert content.index("[2024-06-06]") < content.index("[2024-06-12]")
    assert "The weeks before these entries, in brief:\n- [week 2024-W20] about W20" in content


def test_what_to_do_next_is_never_answered_by_counting(config, conn, fake_client):
    _week(conn)
    plan = QueryPlan(intent="aggregate", wants="judgment", current=True)
    result, _seen = _ask(config, conn, fake_client, plan)
    assert result.stage_used == "precise_raw"
    # a question about the numbers lately still is
    result, _seen = _ask(
        config, conn, fake_client, QueryPlan(intent="aggregate", current=True),
        question="How has my mood been lately?", fast="About 6.",
    )
    assert result.stage_used == "aggregate"


def test_subjects_are_looked_for_in_the_singular_and_the_plural():
    # the notes index doesn't stem, so "goals" alone would miss a note called Goal
    assert query._word_forms(["goals", "plan", "bus"]) == ["goals", "goal", "plan", "plans", "bus", "buss"]
