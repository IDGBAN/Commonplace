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


def test_the_rewrite_is_asked_to_reason_first_and_is_only_for_searching(config, fake_client):
    asked = {}

    def follow_up(model, messages):
        asked["system"] = messages[0]["content"]
        return FollowUp(question="q")

    fake_client.structured["FollowUp"] = follow_up
    query.contextualize_question("but why?", [("q", "a")], config, fake_client)
    system = asked["system"]
    assert system.index("`reasoning`") < system.index("`question`")
    assert "write the question to search the journal with" in system
    # it doesn't have to carry the conversation, which the answering model gets itself
    assert "this is only for finding material" in system
    assert "Keep every name, date and particular the latest message itself gives" in system
    # the reasoning is worked out before the question in the reply as well
    assert list(FollowUp.model_fields) == ["reasoning", "question"]


def test_earlier_follow_ups_are_shown_as_typed_with_what_they_were_searched_for(config, fake_client):
    history = [
        ("How was March 2024?", "Calm."),
        ("and April?", "Busier.", "How was April 2024?"),
        ("What about work?", "Fine.", ""),
    ]
    captured = {}

    def follow_up(model, messages):
        captured["content"] = messages[-1]["content"]
        return FollowUp(question="How was May 2024?")

    fake_client.structured["FollowUp"] = follow_up
    query.contextualize_question("and May?", history, config, fake_client)
    assert captured["content"] == (
        "Conversation so far:\n"
        "User: How was March 2024?\nAssistant: Calm.\n"
        "User: and April?\n(searched for as: How was April 2024?)\nAssistant: Busier.\n"
        "User: What about work?\nAssistant: Fine.\n\n"
        "Latest message: and May?"
    )


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


SEARCHED = "Was April 2024 as calm as March 2024 was?"
REPLY = "and April? I ask because Ana said the deadline moved, and I slept badly all that month."


def _asked(config, conn, fake_client, earlier, fast="Calm.", question=SEARCHED, asked=REPLY):
    add_entry(conn, date(2024, 3, 1), text="March.", summary="March")
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    seen = []

    def chat(model, messages):
        seen.append(messages)
        return fast if model == config.ollama.fast_model else "From the full text."

    fake_client.chat_response = chat
    query.answer(question, config, conn, fake_client, today=TODAY, earlier=earlier, asked=asked)
    return seen


def test_a_follow_up_is_answered_from_the_conversation_itself(config, conn, fake_client):
    earlier = [
        ("How was February?", "Busy."),
        ("How was March?", "March was calm.\n\nSources: [[2024-03-01]]", "How was March 2024?"),
    ]
    [messages] = _asked(config, conn, fake_client, earlier)
    system, *turns, last = messages
    # the turns as they were typed and answered, without their Sources lines
    assert turns == [
        {"role": "user", "content": "How was February?"},
        {"role": "assistant", "content": "Busy."},
        {"role": "user", "content": "How was March?"},
        {"role": "assistant", "content": "March was calm."},
    ]
    # then the reply word for word, not the question it was searched with
    assert last["role"] == "user"
    assert last["content"].startswith(
        f"{REPLY}\n\nFrom the journal, looked up for this message (searched for: {SEARCHED}):\n\n"
    )
    assert "Journal entries:" in last["content"]
    assert query._FOLLOW_UP in system["content"]


def test_the_precise_model_gets_the_conversation_too(config, conn, fake_client):
    earlier = [("How was March?", "March was calm.")]
    fast, precise = _asked(config, conn, fake_client, earlier, fast="INSUFFICIENT")
    for messages in (fast, precise):
        assert messages[1:3] == [
            {"role": "user", "content": "How was March?"},
            {"role": "assistant", "content": "March was calm."},
        ]
        assert messages[-1]["content"].startswith(f"{REPLY}\n\nFrom the journal")
        assert query._FOLLOW_UP in messages[0]["content"]
    assert "### Entry 2024-03-01" in precise[-1]["content"]


def test_numbers_are_put_into_words_knowing_the_conversation(config, conn, fake_client):
    add_entry(conn, date(2024, 3, 1))
    fake_client.structured["QueryPlan"] = QueryPlan(intent="aggregate")
    seen = []
    fake_client.chat_response = lambda model, messages: seen.append(messages) or "About 6."
    query.answer(
        "How was my mood in April 2024?", config, conn, fake_client, today=TODAY,
        earlier=[("How was my mood in March?", "About 6.")], asked="and April?",
    )
    [messages] = seen
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[-1]["content"].startswith("and April?\n\nFrom the journal")
    assert "Statistics:" in messages[-1]["content"]


def test_a_first_question_is_asked_as_before(config, conn, fake_client):
    [messages] = _asked(config, conn, fake_client, [], question="Was April calm too?", asked=None)
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[-1]["content"].startswith("Question: Was April calm too?\n\n")
    assert query._FOLLOW_UP not in messages[0]["content"]


def test_a_reply_searched_for_as_typed_says_so_only_once(config, conn, fake_client):
    [messages] = _asked(
        config, conn, fake_client, [("q", "a")], question="Was April calm?", asked="Was April calm?"
    )
    assert messages[-1]["content"].startswith(
        "Was April calm?\n\nFrom the journal, looked up for this message:\n\n"
    )


def test_the_answer_being_replied_to_goes_in_whole(config, conn, fake_client):
    long_answer = "word " * 2000  # far more than a summary of it would keep
    [messages] = _asked(config, conn, fake_client, [("q", long_answer)])
    assert messages[2] == {"role": "assistant", "content": long_answer.strip()}


def test_older_turns_are_left_out_once_the_conversation_is_too_long():
    big = "x" * (query.CONVERSATION_CHARS // 2)
    earlier = [("first", big), ("second", big), ("third", "short")]
    kept = query._conversation(earlier)
    # newest first into the room there is, and a turn is whole or absent
    assert [m["content"] for m in kept if m["role"] == "user"] == ["second", "third"]
    # the turn being replied to is never dropped, only cut, however long it is
    huge = query._conversation([("only", "y" * (query.CONVERSATION_CHARS * 2))])
    assert huge[0]["content"] == "only"
    assert len(huge[1]["content"]) == query.CONVERSATION_CHARS - len("only")


def test_only_the_last_few_turns_are_passed_on():
    earlier = [(f"q{n}", f"a{n}") for n in range(query.CHAT_HISTORY_TURNS + 3)]
    kept = query._conversation(earlier)
    assert len(kept) == 2 * query.CHAT_HISTORY_TURNS
    assert kept[0]["content"] == "q3"
