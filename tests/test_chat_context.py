from journal_analyzer import query


def test_no_history_returns_question_unchanged_without_calling_model(config, fake_client):
    out = query.contextualize_question("How was March?", [], config, fake_client)
    assert out == "How was March?"
    assert fake_client.chat_calls == []


def test_followup_is_rewritten_from_history(config, fake_client):
    history = [("How was March 2024?", "March was calm and productive.")]

    def chat(model, messages):
        assert model == config.ollama.fast_model
        content = messages[-1]["content"]
        assert "March" in content and "the month after" in content
        return "How was April 2024?"

    fake_client.chat_response = chat
    out = query.contextualize_question(
        "what about the month after that?", history, config, fake_client
    )
    assert out == "How was April 2024?"


def test_blank_rewrite_falls_back_to_original(config, fake_client):
    history = [("How was March?", "Fine.")]
    fake_client.chat_response = "   "
    out = query.contextualize_question("and April?", history, config, fake_client)
    assert out == "and April?"


def test_history_window_truncates_long_answers(config, fake_client):
    long_answer = "x" * 5000
    history = [("q", long_answer)]
    captured = {}

    def chat(model, messages):
        captured["content"] = messages[-1]["content"]
        return "rewritten"

    fake_client.chat_response = chat
    query.contextualize_question("follow up", history, config, fake_client)
    assert "x" * query._HISTORY_ANSWER_CHARS in captured["content"]
    assert "x" * (query._HISTORY_ANSWER_CHARS + 1) not in captured["content"]
