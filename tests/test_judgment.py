from datetime import date

import pytest
from conftest import add_entry

from journal_analyzer import query
from journal_analyzer.models import QueryPlan

TODAY = date(2024, 6, 1)


def _systems(config, conn, fake_client, plan, question="q", fast="INSUFFICIENT"):
    add_entry(conn, date(2024, 5, 20), text="Took the credit for Ana's fix.", summary="Took credit")
    fake_client.structured["QueryPlan"] = plan
    seen: dict[str, str] = {}

    def chat(model, messages):
        seen[model] = messages[0]["content"]
        return fast if model == config.ollama.fast_model else "From the full text."

    fake_client.chat_response = chat
    result = query.answer(question, config, conn, fake_client, today=TODAY)
    return result, seen


def test_a_judgment_goes_straight_to_the_precise_model_and_is_reasoned(config, conn, fake_client):
    result, seen = _systems(config, conn, fake_client, QueryPlan(intent="factual", wants="judgment"))
    assert result.stage_used == "precise_raw"
    # summaries would have lost the facts, so the fast model never gets a try
    assert list(seen) == [config.ollama.precise_model]
    system = seen[config.ollama.precise_model]
    assert query._JUDGMENT in system and query._STRAIGHT in system
    assert "are what they thought, not evidence" in system
    assert "connect facts from different days and notes" in system
    # nothing ties the verdict to what the entries say
    assert query._RECORD not in system
    assert "does not answer the question, say so" not in system
    # advice about what's next is only for questions about now
    assert query._ADVICE not in system


def test_a_question_about_the_record_is_still_answered_from_it(config, conn, fake_client):
    result, seen = _systems(config, conn, fake_client, QueryPlan(intent="factual"))
    assert result.stage_used == "precise_raw"
    fast, precise = seen[config.ollama.fast_model], seen[config.ollama.precise_model]
    for system in (fast, precise):
        assert query._RECORD in system and query._STRAIGHT in system
        assert query._JUDGMENT not in system
        assert "Today is Saturday, 2024-06-01, so tomorrow is Sunday, 2024-06-02, and then come Monday 3," in system
    assert "does not answer the question, say so" in precise


def test_the_writers_view_is_weighed_not_taken_and_not_picked_at():
    straight = " ".join(query._STRAIGHT.split())
    # the verdict first, from the facts, whichever way the question leans
    assert "before you look at which way the writer or the question leans" in straight
    assert "isn't true because it's written there or because the question expects a yes" in straight
    assert "Don't flatter or agree to be agreeable" in straight
    # and then no more disagreement than the facts call for
    assert "it isn't wrong because it's theirs" in straight
    assert "say it holds and leave it there" in straight
    assert "once, as firmly as it matters and no more" in straight
    assert "don't hunt for faults, pile on, or disagree to show independence" in straight
    assert "advice and next steps about anything else" in straight
    # a wrong that can be put right comes with how
    assert "say where they are right and where they aren't rather than picking a side" in straight
    assert "finish with what would fix it" in straight and "about this matter only" in straight
    judgment = " ".join(query._JUDGMENT.split())
    assert "not evidence either way" in judgment and "don't set out to overturn it" in judgment


def test_every_answer_is_told_not_to_take_the_writers_word_for_it(config, conn, fake_client):
    fake_client.structured["QueryPlan"] = QueryPlan(intent="aggregate")
    add_entry(conn, date(2024, 5, 20))
    seen = []
    fake_client.chat_response = lambda model, messages: seen.append(messages[0]["content"]) or "No."
    query.answer("Why has my mood got so much worse?", config, conn, fake_client, today=TODAY)
    assert "if the question assumes a trend or a cause the numbers don't show, say so" in seen[0]
    assert "if they do show it, just answer" in seen[0]


@pytest.mark.parametrize("question", [
    "Is it ethical that I took the credit for Ana's fix?",
    "Was I wrong to leave early?",
    "What do you think about how I handled the review?",
    "Should I apologise to Ana?",
    "In your opinion, was the review fair?",
])
def test_a_plain_request_for_a_verdict_is_a_judgment_whatever_the_router_said(question):
    plan = query._settle(QueryPlan(intent="factual"), question, TODAY)
    assert plan.wants == "judgment"


@pytest.mark.parametrize("question", [
    "How do I feel about taking the credit?",
    "What did I say about the review?",
    "What are my own views on eating meat?",
    "Do I think it was fair?",
])
def test_asking_for_my_own_view_is_a_question_about_the_record(question):
    plan = query._settle(QueryPlan(intent="factual", wants="judgment"), question, TODAY)
    assert plan.wants == "record"


def test_the_router_decides_when_the_wording_does_not(config, conn, fake_client):
    for wants in ("record", "judgment"):
        plan = query._settle(QueryPlan(intent="factual", wants=wants), "Why do I keep arguing with Ana?", TODAY)
        assert plan.wants == wants
    # both kinds of wording at once is left to it too
    both = "What do I think about quitting, and what do you think?"
    assert query._settle(QueryPlan(intent="factual", wants="judgment"), both, TODAY).wants == "judgment"
    assert query._settle(QueryPlan(intent="factual"), both, TODAY).wants == "record"


def test_a_verdict_asked_for_in_plain_words_skips_the_summaries(config, conn, fake_client):
    # the router called it a lookup, the wording says otherwise
    _result, seen = _systems(
        config, conn, fake_client, QueryPlan(intent="factual"),
        question="Is it ethical that I took the credit for Ana's fix?",
    )
    assert list(seen) == [config.ollama.precise_model]
    assert query._JUDGMENT in seen[config.ollama.precise_model]


def test_the_router_is_asked_what_kind_of_answer_is_wanted(config, fake_client):
    asked = []
    fake_client.structured["QueryPlan"] = (
        lambda model, messages: asked.append(messages[0]["content"]) or QueryPlan(intent="factual")
    )
    query._plan("q", config, fake_client, TODAY)
    for field in ("wants", "current", "background"):
        assert f"- {field}:" in asked[0]
    assert "Never give a date after today." in asked[0]


@pytest.mark.parametrize("question", [
    "Was the demo going badly Tomas's fault, like I told him?",
    "Was Tomas right to move the demo?",
    "Have I been consistent with the gym this month like I said?",
    "I was right to take the credit, wasn't I?",
    "But she said she didn't mind, so it's fine, right?",
    "How do I fix things with Ana?",
    "What can I do to sleep better?",
])
def test_fault_claims_and_how_to_fix_it_are_judgments_too(question):
    assert query._settle(QueryPlan(intent="factual"), question, TODAY).wants == "judgment"


@pytest.mark.parametrize("question", [
    "Was the hotel right by the beach?",
    "Who was at the party?",
    "How do I feel about the move?",
])
def test_a_lookup_that_sounds_a_little_like_one_is_left_alone(question):
    assert query._settle(QueryPlan(intent="factual"), question, TODAY).wants == "record"


def test_a_follow_up_is_judged_by_the_message_as_typed(config, conn, fake_client):
    # the rewrite for searching turned a plea for a verdict into a lookup
    _result, seen = _systems(config, conn, fake_client, QueryPlan(intent="factual"))
    assert query._JUDGMENT not in seen[config.ollama.precise_model]
    seen.clear()
    query.answer(
        "What did Ana say about the credit?", config, conn, fake_client, today=TODAY,
        earlier=[("Was I right?", "No.")], asked="But she didn't mind, so it's fine, right?",
    )
    assert query._JUDGMENT in seen[config.ollama.precise_model]


def test_only_a_judgment_thinks(config, conn, fake_client):
    _systems(config, conn, fake_client, QueryPlan(intent="factual", wants="judgment"))
    assert fake_client.chat_asked[-1] == ("precise", None)
    # a lookup in the full text comes out the same without it, far sooner
    _systems(config, conn, fake_client, QueryPlan(intent="factual"))
    assert fake_client.chat_asked[-1] == ("precise", False)


def test_a_judgment_reads_more_days_and_a_count_reads_the_whole_stretch():
    assert query._how_many(QueryPlan(intent="factual")) == query.TOP_K
    assert query._how_many(QueryPlan(intent="factual", wants="judgment")) == query.JUDGMENT_TOP_K
    october = dict(date_from=date(2024, 10, 1), date_to=date(2024, 10, 31))
    counted = QueryPlan(intent="factual", needs_raw_text=True, **october)
    assert query._how_many(counted) == query.COUNTED_TOP_K
    # a whole year is searched, not read
    year = QueryPlan(intent="factual", needs_raw_text=True, date_from=date(2024, 1, 1), date_to=date(2024, 12, 31))
    assert query._how_many(year) == query.TOP_K


def test_advice_about_the_week_is_only_for_a_question_that_asks_what_to_do(config, conn, fake_client):
    now = QueryPlan(intent="factual", wants="judgment", current=True)
    _result, seen = _systems(config, conn, fake_client, now, question="How am I doing lately?")
    assert query._ADVICE not in seen[config.ollama.precise_model]
    _result, seen = _systems(config, conn, fake_client, now, question="What should I focus on this week?")
    assert query._ADVICE in seen[config.ollama.precise_model]


def test_sources_are_the_days_the_answer_names(config, conn, fake_client):
    add_entry(conn, date(2024, 5, 21), text="Ana went quiet.", summary="Quiet")
    add_entry(conn, date(2024, 5, 22), text="Bought bread.", summary="Bread")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual", wants="judgment")
    fake_client.chat_response = "No. On 21 May she went quiet, and by May 22nd nothing had changed."
    result = query.answer("Was I right?", config, conn, fake_client, today=TODAY)
    assert result.cited_dates == [date(2024, 5, 21), date(2024, 5, 22)]
    # an answer that names no day cites what it read
    fake_client.chat_response = "No."
    result = query.answer("Was I right?", config, conn, fake_client, today=TODAY)
    assert len(result.cited_dates) == 2


def test_i_may_one_day_is_not_the_first_of_may():
    rows = [{"entry_date": "2024-05-01"}, {"entry_date": "2024-05-20"}]
    assert query._days_drawn_on("I may 1 day regret it.", rows) == rows
    assert query._days_drawn_on("On 2024-05-20 you did.", rows) == rows[1:]


def test_counting_and_adding_up_think_as_well(config, conn, fake_client):
    # without it, ten runs scattered through a month came back as eight
    _systems(config, conn, fake_client, QueryPlan(intent="factual"), question="How far did I run in total?")
    assert fake_client.chat_asked[-1] == ("precise", None)


def test_a_follow_up_that_tells_rather_than_asks_is_reasoned_with(config, conn, fake_client):
    # read as a lookup, a correction got "the journal doesn't record that"
    _systems(config, conn, fake_client, QueryPlan(intent="factual"))
    seen = {}
    fake_client.chat_response = lambda model, messages: seen.setdefault(model, messages[0]["content"]) and "ok"
    query.answer(
        "Did I go to the gym on the 5th?", config, conn, fake_client, today=TODAY,
        earlier=[("Have I been consistent?", "No.")], asked="I also went on the 5th, I just didn't write it down.",
    )
    assert query._JUDGMENT in seen[config.ollama.precise_model]
    assert fake_client.chat_asked[-1] == ("precise", None)


def test_a_follow_up_lookup_thinks_as_well(config, conn, fake_client):
    _systems(config, conn, fake_client, QueryPlan(intent="factual"))
    query.answer(
        "Who was at the review in April?", config, conn, fake_client, today=TODAY,
        earlier=[("Who was at the review?", "Ana.")], asked="And in April?",
    )
    assert fake_client.chat_asked[-1] == ("precise", None)


def test_a_follow_up_rereads_the_days_the_last_answer_cited(config, conn, fake_client):
    add_entry(conn, date(2024, 5, 20), text="Took the credit for Ana's fix.", summary="Took credit")
    quiet = add_entry(conn, date(2024, 3, 2), text="Still awkward.", summary="Awkward", path="/vault/b.md")
    # the search for the follow-up is kept to May, so only the citation brings March back
    fake_client.structured["QueryPlan"] = QueryPlan(
        intent="factual", wants="judgment", date_from=date(2024, 5, 1), date_to=date(2024, 5, 31)
    )
    earlier = [("Was I right?", "No.\n\nSources: [[2024-03-02]] [[2024-05-20#Work]] [[Ana]]")]
    result = query.answer(
        "Is it fine now?", config, conn, fake_client, today=TODAY, earlier=earlier, asked="So it's fine?",
    )
    assert quiet in result.read_entry_ids
    assert query._days_cited(earlier) == [date(2024, 3, 2), date(2024, 5, 20)]
    assert query._days_cited([("q", "No sources here.")]) == []
