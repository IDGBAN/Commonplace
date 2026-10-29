import io
import json
from datetime import date

from conftest import add_entry, copy_fixtures, invoke_cli, make_config

from journal_analyzer import db as db_mod
from journal_analyzer import ingester, mcp_server, query, quotes, serialize, store
from journal_analyzer.models import QueryPlan
from journal_analyzer.parser import Section


def test_sentences_leave_out_headings_and_list_marks():
    text = "# Morning\n- [x] pack lunch\n1. Walked to work. It rained!\n\n| a | b |\nThen home."
    assert quotes.sentences(text) == ["pack lunch", "Walked to work.", "It rained!", "Then home."]


def test_question_words_and_short_words_dont_count():
    assert quotes.terms(["When did I last see John", "hiking near the park"]) == {"see", "john", "hik", "park"}


def test_a_rare_word_beats_a_common_one():
    text = "Today was long. Today I hiked with John. Today was cold."
    _, quote = quotes.best(text, quotes.terms(["today", "john"]))
    assert quote == "Today I hiked with John."


def test_endings_dont_stop_a_match():
    assert quotes.best("We hiked up the ridge.", quotes.terms(["hiking"]))[1] == "We hiked up the ridge."


def test_no_quote_when_nothing_matches():
    assert quotes.best("Quiet day.", quotes.terms(["garden"])) is None
    assert quotes.for_days([], set()) == {}


def test_a_long_sentence_is_cut_around_the_match():
    text = "word " * 120 + "the garden project " + "word " * 120
    quote = quotes.best(text, quotes.terms(["garden"]))[1]
    assert "garden" in quote and quote.startswith("…") and quote.endswith("…")
    assert len(quote) <= quotes.QUOTE_CHARS + 2


def test_the_part_that_matched_wins_a_tie(conn):
    entry = add_entry(conn, date(2024, 3, 4), text="# Work\nReview with John.\n# Evening\nDinner with John.",
                      path="/vault/2024-03-04.md")
    store.replace_entry_sections(conn, entry, [
        Section("Work", 0, 2, "# Work\nReview with John."), Section("Evening", 2, 4, "# Evening\nDinner with John."),
    ], [None, None])
    rows = store.entries_by_ids(conn, [entry])
    part = store.sections_for_entries(conn, [entry])[entry][1]
    assert quotes.for_days(rows, quotes.terms(["john"]), {entry: part}) == {"2024-03-04": "Dinner with John."}


def test_the_best_entry_of_a_day_gives_its_quote(conn):
    add_entry(conn, date(2024, 3, 4), text="Nothing much.", path="/vault/a.md")
    add_entry(conn, date(2024, 3, 4), text="Planted the garden with Jun.", path="/vault/b.md")
    rows = store.entries_in_range(conn, date(2024, 3, 4), date(2024, 3, 4))
    assert quotes.for_days(rows, quotes.terms(["garden"])) == {"2024-03-04": "Planted the garden with Jun."}


def _indexed(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    cfg.reranking.enabled = False
    copy_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual", entities=["John"], keywords=["hiking"])
    fake_client.chat_response = "You went hiking with John on 2024-01-15."
    return cfg, conn


def test_answers_quote_the_cited_days(tmp_path, fake_client):
    cfg, conn = _indexed(tmp_path, fake_client)
    result = query.answer("When did I go hiking with John?", cfg, conn, fake_client, today=date(2024, 6, 1))
    assert result.quotes["2024-01-15"].startswith("Went hiking with John at Bear Mountain today.")
    dates = serialize.answer_json(conn, result)["sources"]["dates"]
    assert {d["date"]: d["quote"] for d in dates}["2024-01-15"] == result.quotes["2024-01-15"]


def test_ask_sources_prints_the_quotes(tmp_path, fake_client, monkeypatch):
    from journal_analyzer import cli

    cfg, conn = _indexed(tmp_path, fake_client)
    conn.close()
    monkeypatch.setattr(cli, "OllamaClient", lambda c: fake_client)
    result = invoke_cli(tmp_path, cfg, "ask", "When did I go hiking with John?", "--sources")
    assert result.exit_code == 0, result.output
    assert "What those days say" in result.output and "Went hiking with John" in result.output


def test_mcp_leaves_quotes_out_without_raw_text(tmp_path, fake_client):
    cfg, _ = _indexed(tmp_path, fake_client)
    cfg.mcp.raw_text = False
    out = io.StringIO()
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "ask", "arguments": {"question": "When did I go hiking with John?"}}}
    mcp_server.serve(cfg, io.StringIO(json.dumps(call) + "\n"), out, make_client=lambda c: fake_client)
    answer = json.loads(json.loads(out.getvalue())["result"]["content"][0]["text"])
    assert answer["sources"]["dates"] and all(d["quote"] is None for d in answer["sources"]["dates"])


def test_word_endings_match_the_same_word():
    for a, b in (("hike", "hiking"), ("run", "running"), ("call", "called"), ("pass", "passed"),
                 ("class", "classes"), ("focus", "focused"), ("note", "notes"), ("garden", "gardens")):
        assert quotes.terms([a]) == quotes.terms([b]), (a, b)
