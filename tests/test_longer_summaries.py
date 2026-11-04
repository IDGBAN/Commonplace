import json
from datetime import date

import pytest
from conftest import add_entry, invoke_cli, make_config

from journal_analyzer import context, extractor, ingester, prompts, query, rollups, store
from journal_analyzer import db as db_mod
from journal_analyzer.config import IndexingConfig
from journal_analyzer.models import DetailedExtraction, Extraction, ParsedEntry, QueryPlan


def _flat(output: str) -> str:
    return " ".join(output.split())


def _words(n: int) -> str:
    return " ".join(["walked"] * n)


@pytest.mark.parametrize(
    ("words", "tier"),
    [(399, None), (400, ("2 or 3 sentences", 450)), (999, ("2 or 3 sentences", 450)),
     (1000, ("3 to 5 sentences", 800))],
)
def test_detail_tiers(config, words, tier):
    assert extractor.detail_tier(words, config) == tier


def test_zero_turns_longer_summaries_off(config):
    config.indexing.detail_min_words = 0
    assert extractor.detail_tier(5000, config) is None


def test_the_long_tier_cannot_start_below_the_short_one():
    with pytest.raises(ValueError, match="detail_long_words"):
        IndexingConfig(detail_min_words=500, detail_long_words=300)
    IndexingConfig(detail_min_words=0, detail_long_words=1)


def _ask_for(config, fake_client, text):
    seen = {}

    def respond(name, reply):
        def inner(model, messages):
            seen["schema"], seen["system"] = name, messages[0]["content"]
            return reply
        return inner

    fake_client.structured["Extraction"] = respond("Extraction", Extraction(summary="Short."))
    fake_client.structured["DetailedExtraction"] = respond(
        "DetailedExtraction", DetailedExtraction(summary="Long day.", details="word " * 300)
    )
    result = extractor.extract(ParsedEntry(date(2024, 1, 1), text, "x"), config, fake_client)
    return seen, result


def test_short_entries_keep_the_prompt_they_always_had(config, fake_client):
    seen, result = _ask_for(config, fake_client, "A short day.")
    assert seen["schema"] == "Extraction" and "details:" not in seen["system"]
    assert seen["system"] == extractor._SYSTEM_PROMPT.format(
        mood_max=config.indexing.mood_scale_max, summary_max=extractor.SUMMARY_MAX_CHARS
    )
    assert not isinstance(result, DetailedExtraction)


def test_long_entries_get_a_longer_summary_cut_to_size(config, fake_client):
    seen, result = _ask_for(config, fake_client, _words(500))
    assert seen["schema"] == "DetailedExtraction"
    assert "also write 2 or 3 sentences, at most 450 characters" in seen["system"]
    assert isinstance(result, DetailedExtraction) and len(result.details) <= 450
    seen, _ = _ask_for(config, fake_client, _words(1200))
    assert "3 to 5 sentences, at most 800 characters" in seen["system"]


def test_a_longer_summary_about_the_author_is_asked_for_again(config, fake_client):
    replies = iter([
        DetailedExtraction(summary="Long day.", details="The author describes a long walk."),
        DetailedExtraction(summary="Long day.", details="I walked for hours."),
    ])
    fake_client.structured["DetailedExtraction"] = lambda model, messages: next(replies)
    result = extractor.extract(ParsedEntry(date(2024, 1, 1), _words(500), "x"), config, fake_client)
    assert isinstance(result, DetailedExtraction) and result.details == "I walked for hours."


def test_indexing_keeps_the_longer_summary_and_embeds_with_it(tmp_path, fake_client, monkeypatch):
    cfg = make_config(tmp_path)
    (cfg.vault.path / "2024-01-01.md").write_text(_words(500), encoding="utf-8")
    (cfg.vault.path / "2024-01-02.md").write_text("Short one.", encoding="utf-8")
    leads = []
    real = ingester.embedder.entry_vector

    def entry_vector(raw_text, config, client, summary=None):
        leads.append(summary)
        return real(raw_text, config, client, summary=summary)

    monkeypatch.setattr(ingester.embedder, "entry_vector", entry_vector)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    ids = {r["entry_date"]: r["id"] for r in conn.execute("SELECT id, entry_date FROM entries")}
    details = store.details_for_entries(conn, list(ids.values()))
    assert details == {ids["2024-01-01"]: "A longer canned summary of a long day out with friends."}
    assert "A longer canned summary of a long day out with friends." in leads


def _long_and_short(conn):
    long_id = add_entry(conn, date(2024, 1, 1), text=_words(900), summary="Long day.", path="/vault/1.md")
    store.save_entry_details(conn, long_id, "I walked the whole coast path and met John at the end.")
    add_entry(conn, date(2024, 1, 2), text="Short.", summary="Quiet day.", path="/vault/2.md")
    return long_id


def test_longer_summaries_fill_the_budget_longest_entries_first(conn):
    first = add_entry(conn, date(2024, 1, 1), text=_words(900), summary="a", path="/vault/1.md")
    second = add_entry(conn, date(2024, 1, 2), text=_words(500), summary="b", path="/vault/2.md")
    store.save_entry_details(conn, first, "x" * 100)
    store.save_entry_details(conn, second, "y" * 50)
    rows = store.entries_by_ids(conn, [first, second])
    assert set(context.longer_summaries(conn, rows, 200)) == {first, second}
    assert set(context.longer_summaries(conn, rows, 120)) == {first}
    assert set(context.longer_summaries(conn, rows, 60)) == {second}
    assert context.longer_summaries(conn, rows, 0) == {}


def test_answers_digests_and_prompts_read_the_longer_summary(conn, config, fake_client):
    _long_and_short(conn)
    rows = store.entries_in_range(conn, date(2024, 1, 1), date(2024, 1, 2))
    text = query._summary_context(rows, [], QueryPlan(intent="factual"), config, conn)
    assert "- [2024-01-01] I walked the whole coast path and met John at the end." in text
    assert "- [2024-01-02] Quiet day." in text

    digest, _, _ = rollups._rollup_members_digest(conn, rows, 1000)
    assert "I walked the whole coast path" in digest
    digest, _, _ = rollups._rollup_members_digest(conn, rows, 0)
    assert "Long day." in digest and "coast path" not in digest

    recent = prompts._recent_context(conn, date(2024, 1, 1), date(2024, 1, 2), config)
    assert "coast path" in recent


def test_show_export_and_browse_carry_it(tmp_path, config, conn):
    _long_and_short(conn)
    shown = _flat(invoke_cli(tmp_path, config, "show", "2024-01-01").output)
    assert "In more detail: I walked the whole coast path" in shown
    as_json = json.loads(invoke_cli(tmp_path, config, "show", "2024-01-01", "--json").stdout)
    assert as_json["entries"][0]["details"].startswith("I walked")
    exported = json.loads(invoke_cli(tmp_path, config, "export").output)
    assert [e["details"] for e in exported["entries"]] == [
        "I walked the whole coast path and met John at the end.", None]


def test_ask_sources_show_the_longer_summary(tmp_path, config, conn, fake_client, monkeypatch):
    from journal_analyzer import cli

    _long_and_short(conn)
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You walked the coast."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    output = _flat(invoke_cli(tmp_path, config, "ask", "Where did I walk?", "--sources").output)
    assert "I walked the whole coast path" in output


def test_an_overlong_longer_summary_ends_on_a_whole_sentence():
    text = "I woke early. I walked to work over the bridge. At lunch I called John and he"
    assert extractor._clip_sentences(text, 60) == "I woke early. I walked to work over the bridge."
    # nothing whole fits in the first half, so it's cut at a word instead
    assert extractor._clip_sentences("A very long opening sentence that keeps going. End.", 30).endswith("…")
    assert extractor._clip_sentences("Short.", 30) == "Short."
