from datetime import date

from conftest import add_entry

from journal_analyzer import query
from journal_analyzer.models import FollowUp, QueryPlan, StructuredOutputError

TODAY = date(2024, 6, 1)


def test_no_history_returns_question_unchanged_without_calling_model(config, fake_client):
    out = query.contextualize_question("How was March?", [], config, fake_client)
    assert out == "How was March?"
    assert fake_client.chat_calls == [] and fake_client.structured_calls == []


def test_followup_is_rewritten_from_history(config, fake_client):
    history = [("How was March 2024?", "March was calm and productive.")]

    def follow_up(model, messages):
        assert model == config.ollama.fast_model
        content = messages[-1]["content"]
        assert "March" in content and "the month after" in content
        return FollowUp(reasoning="The month after March 2024 is April.", question="How was April 2024?")

    fake_client.structured["FollowUp"] = follow_up
    out = query.contextualize_question(
        "what about the month after that?", history, config, fake_client
    )
    assert out == "How was April 2024?"


def test_the_rewrite_is_asked_to_reason_first_and_to_carry_the_last_answer_over(config, fake_client):
    asked = {}

    def follow_up(model, messages):
        asked["system"] = messages[0]["content"]
        return FollowUp(question="q")

    fake_client.structured["FollowUp"] = follow_up
    query.contextualize_question("but why?", [("q", "a")], config, fake_client)
    system = asked["system"]
    assert system.index("`reasoning`") < system.index("`question`")
    assert "the point of the last answer they are reacting to" in system
    assert "so it isn't gone over again" in system
    # the reasoning is worked out before the question in the reply as well
    assert list(FollowUp.model_fields) == ["reasoning", "question"]


def test_a_rewrite_of_several_sentences_comes_back_as_one_line(config, fake_client):
    fake_client.structured["FollowUp"] = FollowUp(
        question="You said March was calm.\nWas April calm too,\n  or did work pick up?"
    )
    out = query.contextualize_question("and April?", [("How was March?", "Calm.")], config, fake_client)
    assert out == "You said March was calm. Was April calm too, or did work pick up?"


def test_blank_rewrite_falls_back_to_original(config, fake_client):
    history = [("How was March?", "Fine.")]
    fake_client.structured["FollowUp"] = FollowUp(reasoning="unsure", question="   ")
    out = query.contextualize_question("and April?", history, config, fake_client)
    assert out == "and April?"


def test_a_rewrite_that_is_not_valid_falls_back_to_original(config, fake_client):
    def broken(model, messages):
        raise StructuredOutputError("not JSON")

    fake_client.structured["FollowUp"] = broken
    out = query.contextualize_question("and April?", [("How was March?", "Fine.")], config, fake_client)
    assert out == "and April?"


def test_the_last_answer_is_read_nearly_whole_and_older_ones_from_their_start(config, fake_client):
    history = [("first", "o" * 5000), ("second", "x" * 5000)]
    captured = {}

    def follow_up(model, messages):
        captured["content"] = messages[-1]["content"]
        return FollowUp(question="rewritten")

    fake_client.structured["FollowUp"] = follow_up
    query.contextualize_question("follow up", history, config, fake_client)
    content = captured["content"]
    assert "o" * query._HISTORY_ANSWER_CHARS in content
    assert "o" * (query._HISTORY_ANSWER_CHARS + 1) not in content
    assert "x" * query.LAST_ANSWER_CHARS in content
    assert "x" * (query.LAST_ANSWER_CHARS + 1) not in content


def _asked(config, conn, fake_client, earlier, fast="Calm."):
    add_entry(conn, date(2024, 3, 1), text="March.", summary="March")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    seen = []

    def chat(model, messages):
        seen.append(messages[-1]["content"])
        return fast if model == config.ollama.fast_model else "From the full text."

    fake_client.chat_response = chat
    query.answer("Was April calm too?", config, conn, fake_client, today=TODAY, earlier=earlier)
    return seen


def test_a_follow_up_is_answered_knowing_the_answer_before_it(config, conn, fake_client):
    earlier = [("How was February?", "Busy."), ("How was March?", "March was calm.\n\nSources: [[2024-03-01]]")]
    [content] = _asked(config, conn, fake_client, earlier)
    start = content.split("Question: ")[0]
    # only the exchange being followed, without its Sources line
    assert "they asked: How was March?" in start
    assert "You answered: March was calm.\n" in start
    assert "February" not in start and "Sources" not in start
    assert "Don't repeat that answer." in start


def test_the_precise_model_gets_the_earlier_answer_too(config, conn, fake_client):
    earlier = [("How was March?", "March was calm.")]
    fast, precise = _asked(config, conn, fake_client, earlier, fast="INSUFFICIENT")
    assert "You answered: March was calm." in fast
    assert "You answered: March was calm." in precise
    assert precise.index("You answered") < precise.index("Question: Was April calm too?")


def test_a_first_question_is_asked_as_before(config, conn, fake_client):
    [content] = _asked(config, conn, fake_client, [])
    assert content.startswith("Question: Was April calm too?")


def test_a_long_earlier_answer_is_cut_short_for_the_answering_model(config, conn, fake_client):
    [content] = _asked(config, conn, fake_client, [("q", "word " * 2000)])
    said = content.split("You answered: ")[1].split("\n")[0]
    assert len(said) <= query._EARLIER_ANSWER_CHARS
