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
        assert "Today is Saturday, 2024-06-01." in system
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
    assert "advice and next steps nobody asked for" in straight
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
