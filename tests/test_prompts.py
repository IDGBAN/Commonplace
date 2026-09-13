from datetime import date

from conftest import add_entry, add_note

from journal_analyzer import prompts

TODAY = date(2024, 6, 15)


def _seed_recent(conn):
    add_note(conn, "Sara")
    add_note(conn, "Garden", kind="topics")
    add_entry(
        conn, date(2024, 6, 10), summary="Started the garden project.",
        tags=["garden"], links=["Sara", "Garden"],
    )
    add_entry(conn, date(2024, 6, 12), summary="Stressful day at work.", tags=["work"])


def test_prompts_parsed_and_capped(conn, config, fake_client):
    _seed_recent(conn)
    fake_client.chat_response = (
        "1. How is the garden project coming along?\n"
        "2. What made work stressful this week?\n"
        "3. When will you next see Sara?\n"
        "4. What are you looking forward to?\n"
        "5. One extra prompt beyond the count."
    )
    out = prompts.generate_prompts(config, conn, fake_client, today=TODAY, count=4)
    assert len(out) == 4
    assert out[0] == "How is the garden project coming along?"  # numbering stripped
    sent = fake_client.chat_calls[-1][1][-1]["content"]
    assert "garden project" in sent.lower() and "who/what: Sara, Garden" in sent


def test_no_recent_entries_returns_empty(conn, config, fake_client):
    _seed_recent(conn)
    # 2 days back from TODAY misses the June 10 and 12 entries
    out = prompts.generate_prompts(config, conn, fake_client, today=TODAY, days=2)
    assert out == []
    assert fake_client.chat_calls == []  # no context, so no model call


def test_preamble_lines_are_not_mistaken_for_prompts(conn, config, fake_client):
    _seed_recent(conn)
    fake_client.chat_response = (
        "Here are four prompts based on your recent entries:\n"
        "1. How is the garden project coming along?\n"
        "2. What made work stressful this week?\n"
    )
    out = prompts.generate_prompts(config, conn, fake_client, today=TODAY, count=4)
    assert out == [
        "How is the garden project coming along?",
        "What made work stressful this week?",
    ]


def test_bullet_lists_are_accepted_too(conn, config, fake_client):
    _seed_recent(conn)
    fake_client.chat_response = "- First prompt?\n* Second prompt?\n"
    out = prompts.generate_prompts(config, conn, fake_client, today=TODAY)
    assert out == ["First prompt?", "Second prompt?"]


def test_unmarked_lines_are_kept_when_nothing_is_marked(conn, config, fake_client):
    _seed_recent(conn)
    fake_client.chat_response = "First prompt?\n\nSecond prompt?\n"
    out = prompts.generate_prompts(config, conn, fake_client, today=TODAY)
    assert out == ["First prompt?", "Second prompt?"]


def test_the_precise_model_writes_the_prompts(conn, config, fake_client):
    _seed_recent(conn)
    fake_client.chat_response = "1. A prompt?"
    prompts.generate_prompts(config, conn, fake_client, today=TODAY)
    assert fake_client.chat_calls[-1][0] == config.ollama.precise_model
