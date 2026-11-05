import json
from datetime import date
from unittest.mock import MagicMock

import ollama
import pytest
from conftest import add_entry, copy_fixtures, invoke_cli, make_config
from typer.testing import CliRunner

from journal_analyzer import (
    cli,
    db,
    ingester,
    insights,
    profile,
    query,
    review,
    rollups,
    store,
)
from journal_analyzer import prompts as writing_prompts
from journal_analyzer.models import Extraction, OllamaUnavailableError, QueryPlan
from journal_analyzer.ollama_client import OllamaClient
from journal_analyzer.profile import ProfileDigest, ScannedFact, ScannedFacts


def _flat(output: str) -> str:
    return " ".join(output.split())


def _facts_for(*facts):
    """A scan reply naming facts for whichever entries mention a keyword."""

    def respond(model, messages):
        found = []
        for block in messages[-1]["content"].split("### Entry ")[1:]:
            day, _, body = block.partition("\n")
            for keyword, category, fact in facts:
                if keyword in body:
                    found.append(ScannedFact(date=day, category=category, fact=fact))
        return ScannedFacts(facts=found)

    return respond


def _entries(conn):
    add_entry(conn, date(2024, 1, 1), text="Tea again, no coffee for me.", path="/vault/1.md")
    add_entry(conn, date(2024, 1, 2), text="Long walk with the dog.", path="/vault/2.md")
    add_entry(conn, date(2024, 1, 3), text="", path="/vault/3.md")


def test_scan_reads_entries_in_batches_and_keeps_facts_with_their_day(conn, config, fake_client):
    _entries(conn)
    config.profile.scan_batch_entries = 1
    fake_client.structured["ScannedFacts"] = _facts_for(("Tea", "preferences", "I drink tea, not coffee."))
    found = profile.scan(config, conn, fake_client, profile.pending_entries(conn))
    assert found == 1
    assert [name for _, name in fake_client.structured_calls] == ["ScannedFacts", "ScannedFacts"]
    [fact] = store.profile_facts(conn, 10)
    assert (fact["entry_date"], fact["category"], fact["fact"]) == ("2024-01-01", "preferences", "I drink tea, not coffee.")
    assert profile.pending_entries(conn) == []  # the empty entry is never read
    # a batch can list more facts than an extraction has fields
    assert fake_client.reply_tokens["ScannedFacts"] == profile.SCAN_REPLY_TOKENS


def test_scan_drops_facts_for_days_not_in_the_batch_and_repeats(conn, config, fake_client):
    _entries(conn)
    fake_client.structured["ScannedFacts"] = ScannedFacts(facts=[
        ScannedFact(date="1999-01-01", category="other", fact="Made up."),
        ScannedFact(date="2024-01-02", category="hobbies", fact="I walk the dog."),
        ScannedFact(date="2024-01-02", category="hobbies", fact="I walk the DOG."),
    ])
    assert profile.scan(config, conn, fake_client, profile.pending_entries(conn)) == 1


def test_an_edited_entry_is_read_again_and_its_old_facts_go(conn, config, fake_client):
    _entries(conn)
    fake_client.structured["ScannedFacts"] = _facts_for(("Tea", "preferences", "I drink tea."))
    profile.scan(config, conn, fake_client, profile.pending_entries(conn))
    conn.execute("UPDATE entries SET raw_text = 'Coffee now.' WHERE entry_date = '2024-01-01'")
    conn.commit()
    assert [r["entry_date"] for r in profile.pending_entries(conn)] == ["2024-01-01"]
    assert profile.forget_gone_entries(conn) == 1
    assert store.profile_facts(conn, 10) == []


def test_update_folds_new_facts_into_the_profile(conn, config, fake_client):
    _entries(conn)
    fake_client.structured["ScannedFacts"] = _facts_for(("Tea", "preferences", "I drink tea."))
    profile.scan(config, conn, fake_client, profile.pending_entries(conn))
    seen = []

    def fold(model, messages):
        seen.append((model, messages))
        return ProfileDigest(names="People call me Al.", profile="Likes and dislikes\nI drink tea.")

    fake_client.structured["ProfileDigest"] = fold
    assert profile.needs_update(conn, config)
    assert profile.update(config, conn, fake_client)
    [(model, messages)] = seen
    assert model == config.ollama.precise_model
    assert f"at most {config.profile.max_words // 108} short sentences" in messages[0]["content"]
    assert "Profile so far:\n(none yet)" in messages[-1]["content"]
    assert "- 2024-01-01 [preferences] I drink tea." in messages[-1]["content"]
    found = profile.current(conn)
    assert found is not None and found.text.endswith("I drink tea.") and found.names == "People call me Al."
    assert not profile.needs_update(conn, config)
    assert not profile.update(config, conn, fake_client)  # nothing new
    assert profile.update(config, conn, fake_client, rebuild=True)
    assert "Profile so far:\n(none yet)" in seen[-1][1][-1]["content"]


def test_many_facts_are_folded_in_order_a_chunk_at_a_time(conn, config, fake_client, monkeypatch):
    monkeypatch.setattr(profile, "FOLD_FACTS_PER_CALL", 2)
    for day in range(1, 6):
        add_entry(conn, date(2024, 1, day), text=f"fact {day}", path=f"/vault/{day}.md")
    fake_client.structured["ScannedFacts"] = _facts_for(*[(f"fact {d}", "other", f"Fact {d}.") for d in range(1, 6)])
    profile.scan(config, conn, fake_client, profile.pending_entries(conn))
    sent = []

    def fold(model, messages):
        sent.append(messages[-1]["content"])
        return ProfileDigest(names="", profile=f"after {len(sent)}")

    fake_client.structured["ProfileDigest"] = fold
    profile.update(config, conn, fake_client)
    assert len(sent) == 3
    assert "Profile so far:\nafter 1" in sent[1] and "Fact 3." in sent[1]


def test_a_profile_over_its_word_limit_still_fits_in_the_reply(conn, config, fake_client, monkeypatch):
    # models often write past max_words, and the whole profile comes back in
    # one JSON reply, so a cap sized for short replies cut it off mid-string
    _entries(conn)
    fake_client.structured["ScannedFacts"] = _facts_for(("Tea", "preferences", "I drink tea."))
    profile.scan(config, conn, fake_client, profile.pending_entries(conn))
    config.ollama.precise_model_think = False
    long_profile = " ".join(["walked"] * 700)

    def chat(**kwargs):
        reply = ProfileDigest(names="People call me Al.", profile=long_profile).model_dump_json()
        # a reply stops once it has used up num_predict, about 4 characters a token
        return {"message": {"content": reply[: kwargs["options"]["num_predict"] * 4]}}

    inner = MagicMock()
    inner.chat.side_effect = chat
    monkeypatch.setattr(ollama, "Client", lambda **kwargs: inner)
    assert profile.update(config, conn, OllamaClient(config))
    assert profile.current(conn).text == long_profile


def _words(n: int, word: str = "walked") -> str:
    return " ".join([word] * n)


def _tea_fact(conn, config, fake_client):
    _entries(conn)
    fake_client.structured["ScannedFacts"] = _facts_for(("Tea", "preferences", "I drink tea."))
    profile.scan(config, conn, fake_client, profile.pending_entries(conn))


def test_a_profile_that_runs_long_is_shortened_after_the_fold(conn, config, fake_client):
    _tea_fact(conn, config, fake_client)
    config.profile.max_words = 100
    sent = []
    fake_client.structured["ProfileDigest"] = ProfileDigest(names="Al.", profile=_words(200))

    def shorten(model, messages):
        sent.append((model, messages))
        return profile.ShortProfile(profile=_words(90, "kept"))

    fake_client.structured["ShortProfile"] = shorten
    assert profile.update(config, conn, fake_client)
    [(model, messages)] = sent
    assert model == config.ollama.precise_model
    # models don't count words, but they do keep to a number of sentences
    assert "at most 2 short sentences" in messages[0]["content"] and messages[-1]["content"] == _words(200)
    found = profile.current(conn)
    assert found.text == _words(90, "kept") and found.names == "Al."


def test_a_long_stored_profile_is_shortened_before_new_facts_go_into_it(conn, config, fake_client):
    _tea_fact(conn, config, fake_client)
    config.profile.max_words = 100
    db.set_meta(conn, "profile_text", _words(300))
    folds = []
    fake_client.structured["ShortProfile"] = profile.ShortProfile(profile=_words(90, "kept"))
    fake_client.structured["ProfileDigest"] = lambda model, messages: folds.append(messages[-1]["content"]) or ProfileDigest(profile=_words(95, "kept"))
    assert profile.update(config, conn, fake_client)
    assert folds[0].startswith(f"Profile so far:\n{_words(90, 'kept')}\n")
    assert [name for _, name in fake_client.structured_calls][-2:] == ["ShortProfile", "ProfileDigest"]


def test_a_long_profile_is_shortened_with_nothing_new_but_only_tried_once(conn, config, fake_client):
    config.profile.max_words = 100
    db.set_meta(conn, "profile_text", _words(300))
    db.set_meta(conn, "profile_note_hash", profile.text_hash(""))
    # still over the limit, but shorter, so it's kept and not tried again
    fake_client.structured["ShortProfile"] = profile.ShortProfile(profile=_words(200, "kept"))
    assert profile.needs_update(conn, config)
    assert profile.update(config, conn, fake_client)
    assert profile.current(conn).text == _words(200, "kept")
    assert not profile.needs_update(conn, config)
    assert not profile.update(config, conn, fake_client)
    assert [name for _, name in fake_client.structured_calls] == ["ShortProfile"]


def test_a_shortening_that_comes_back_longer_is_ignored(conn, config, fake_client):
    config.profile.max_words = 100
    db.set_meta(conn, "profile_text", _words(300))
    db.set_meta(conn, "profile_note_hash", profile.text_hash(""))
    fake_client.structured["ShortProfile"] = profile.ShortProfile(profile=_words(400))
    profile.update(config, conn, fake_client)
    assert profile.current(conn).text == _words(300)
    assert not profile.needs_update(conn, config)
    # a different limit asks differently, so that's worth another try
    config.profile.max_words = 60
    assert profile.needs_update(conn, config)


def test_a_fold_that_fails_partway_keeps_what_was_already_folded(conn, config, fake_client, monkeypatch):
    monkeypatch.setattr(profile, "FOLD_FACTS_PER_CALL", 2)
    for day in range(1, 5):
        add_entry(conn, date(2024, 1, day), text=f"fact {day}", path=f"/vault/{day}.md")
    fake_client.structured["ScannedFacts"] = _facts_for(*[(f"fact {d}", "other", f"Fact {d}.") for d in range(1, 5)])
    profile.scan(config, conn, fake_client, profile.pending_entries(conn))
    sent = []

    def fold(model, messages):
        sent.append(messages[-1]["content"])
        if len(sent) == 2:
            raise OllamaUnavailableError("Ollama did not answer within 240s")
        return ProfileDigest(names="People call me Al.", profile=f"after {len(sent)}")

    fake_client.structured["ProfileDigest"] = fold
    with pytest.raises(OllamaUnavailableError):
        profile.update(config, conn, fake_client)
    found = profile.current(conn)
    assert found is not None and found.text == "after 1" and found.names == "People call me Al."

    # the next run goes on from there instead of losing the first two facts
    assert profile.update(config, conn, fake_client)
    assert "Profile so far:\nafter 1" in sent[-1] and "Fact 3." in sent[-1] and "Fact 1." not in sent[-1]
    assert profile.current(conn).text == "after 3"


def test_your_own_note_goes_in_unless_it_is_private(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    cfg.profile.note = "About me.md"
    note = cfg.vault.path / "About me.md"
    note.write_text("---\npronouns: she/her\n---\nI'm a nurse. %%draft%%", encoding="utf-8")
    conn = db.connect(cfg)
    db.init_schema(conn)
    sent = []
    fake_client.structured["ProfileDigest"] = lambda model, messages: sent.append(messages[-1]["content"]) or ProfileDigest(profile="p")
    assert profile.needs_update(conn, cfg)
    assert profile.update(cfg, conn, fake_client)
    # first, so the part of the prompt that stays the same is at its start
    assert sent[0].startswith("The note I keep about myself:\npronouns: she/her\n\nI'm a nurse.")
    assert "draft" not in sent[0]
    note.write_text("I'm a nurse. #private", encoding="utf-8")
    assert profile.update(cfg, conn, fake_client)
    assert "The note I keep about myself" not in sent[-1]


def test_context_and_names_are_empty_without_a_profile_or_when_off(conn, config):
    assert profile.context(conn, config) == "" and profile.names(conn, config) == ""
    db.set_meta(conn, "profile_text", "I drink tea.")
    db.set_meta(conn, "profile_names", "People call me Al.")
    assert "I drink tea." in profile.context(conn, config)
    # the names line goes too, since nicknames often aren't in the profile text
    assert "Names: People call me Al.\n" in profile.context(conn, config)
    assert profile.names(conn, config) == "\n\nNames the writer uses: People call me Al."
    config.profile.enabled = False
    assert profile.context(conn, config) == "" and profile.names(conn, config) == ""


def _with_profile(conn):
    db.set_meta(conn, "profile_text", "PROFILE-TEXT")
    db.set_meta(conn, "profile_names", "NAMES-LINE")


def test_answers_get_the_profile_and_the_router_only_the_names(conn, config, fake_client):
    _with_profile(conn)
    add_entry(conn, date(2024, 1, 1), summary="Walked")
    routers = []
    fake_client.structured["QueryPlan"] = lambda model, messages: routers.append(messages[0]["content"]) or QueryPlan(intent="factual")
    systems = []

    def chat(model, messages):
        systems.append(messages[0]["content"])
        return "INSUFFICIENT" if model == config.ollama.fast_model else "From the text."

    fake_client.chat_response = chat
    query.answer("Where did I walk?", config, conn, fake_client, today=date(2024, 6, 1))
    assert "NAMES-LINE" in routers[0] and "PROFILE-TEXT" not in routers[0]
    assert all("PROFILE-TEXT" in s and "NAMES-LINE" in s for s in systems) and len(systems) == 2
    rewrite = []
    fake_client.chat_response = lambda model, messages: rewrite.append(messages[0]["content"]) or "q"
    query.contextualize_question("and then?", [("q", "a")], config, fake_client, profile.names(conn, config))
    assert "NAMES-LINE" in rewrite[0] and "PROFILE-TEXT" not in rewrite[0]


def test_digests_insights_and_prompts_get_the_profile(conn, config, fake_client):
    _with_profile(conn)
    for day in range(1, 5):
        add_entry(conn, date(2024, 1, day), mood=2.0 + day, tags=["walks"], summary=f"d{day}", path=f"/vault/{day}.md")
    systems = []
    fake_client.chat_response = lambda model, messages: systems.append(messages[0]["content"]) or "ok"
    rollups.generate_rollups(config, conn, fake_client, ("month", "year"))
    assert len(systems) == 2 and all("PROFILE-TEXT" in s for s in systems)
    config.insights.mood_deviation_threshold = 0
    insights.narrate(insights.compute_insights(config, conn), config, fake_client, profile.context(conn, config))
    writing_prompts.generate_prompts(config, conn, fake_client, today=date(2024, 1, 5))
    assert all("PROFILE-TEXT" in s for s in systems[2:]) and len(systems) == 4


def test_extraction_and_the_scan_never_see_the_profile(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    conn = db.connect(cfg)
    db.init_schema(conn)
    _with_profile(conn)
    seen = []

    def watch(name, reply):
        def respond(model, messages):
            seen.extend(m["content"] for m in messages)
            return reply
        return respond

    fake_client.structured["Extraction"] = watch("Extraction", Extraction(summary="ok"))
    fake_client.structured["ScannedFacts"] = watch("ScannedFacts", ScannedFacts())
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert seen and not any("PROFILE-TEXT" in text or "NAMES-LINE" in text for text in seen)


def test_indexing_reads_new_entries_and_updates_the_profile(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    copy_fixtures(cfg.vault.path)
    fake_client.structured["ScannedFacts"] = _facts_for(("hiking", "hobbies", "I hike."))
    conn = db.connect(cfg)
    db.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert profile.current(conn).text == "I like long walks."
    assert [r["fact"] for r in store.profile_facts(conn, 10)] == ["I hike."]
    calls = len(fake_client.structured_calls)
    ingester.run_backfill(cfg, conn, client=fake_client)
    assert len(fake_client.structured_calls) == calls  # nothing new to read or fold


def test_a_profile_failure_does_not_fail_the_index(tmp_path, fake_client):
    from journal_analyzer.models import ModelRequestError

    cfg = make_config(tmp_path)
    copy_fixtures(cfg.vault.path)

    def broken(model, messages):
        raise ModelRequestError("Ollama rejected the request for model 'fake-fast'")

    fake_client.structured["ScannedFacts"] = broken
    conn = db.connect(cfg)
    db.init_schema(conn)
    counts = ingester.run_backfill(cfg, conn, client=fake_client)
    assert counts["indexed"] == 2
    assert len(profile.pending_entries(conn)) == 2  # read on a later run


def test_rollup_writes_the_profile_first(conn, config, fake_client):
    _entries(conn)
    plan = rollups.plan_rollups(config, conn)
    assert plan[0].kind == "profile" and plan[0].label == "you (2 entries to read)"
    fake_client.structured["ScannedFacts"] = _facts_for(("walk", "hobbies", "I walk."))
    counts = rollups.generate_rollups(config, conn, fake_client, ("profile",))
    assert counts["profile"] == 1 and profile.current(conn) is not None
    assert rollups.plan_rollups(config, conn, ("profile",)) == []
    assert rollups.plan_rollups(config, conn, ("profile",), only_stale=False)[0].reason == "rebuild"


def test_profile_command(tmp_path, config, conn, fake_client, monkeypatch):
    _entries(conn)
    assert "No profile yet" in _flat(invoke_cli(tmp_path, config, "profile").output)
    fake_client.structured["ScannedFacts"] = _facts_for(("Tea", "preferences", "I drink tea."))
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    updated = invoke_cli(tmp_path, config, "profile", "--update")
    assert updated.exit_code == 0, updated.output
    output = _flat(updated.output)
    assert "Read 2 entries, found 1 new fact." in output and "I like long walks." in output
    assert "Names I go by Al." in output
    assert "I drink tea." in _flat(invoke_cli(tmp_path, config, "profile", "--facts").output)
    data = json.loads(invoke_cli(tmp_path, config, "profile", "--json").stdout)
    assert data["profile"]["text"] == "I like long walks." and data["facts"] == 1
    assert data["recent_facts"][0]["fact"] == "I drink tea."
    again = invoke_cli(tmp_path, config, "profile", "--rescan")
    assert again.exit_code == 0 and "Read 2 entries" in _flat(again.output)


def test_review_lists_what_was_new_about_you(conn, config, fake_client):
    _entries(conn)
    fake_client.structured["ScannedFacts"] = _facts_for(("Tea", "preferences", "I drink tea."))
    profile.scan(config, conn, fake_client, profile.pending_entries(conn))
    text = review.to_markdown(review.build(config, conn, 2024))
    assert "## New about you\n\n- I drink tea. ([[2024-01-01]])" in text


def test_profile_command_when_turned_off(tmp_path, config, conn):
    path = tmp_path / "off.toml"
    path.write_text(
        f"[vault]\npath = '{config.vault.path.as_posix()}'\n"
        f"[db]\npath = '{config.db.path.as_posix()}'\n[profile]\nenabled = false\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(cli.app, ["profile", "--config", str(path)])
    assert result.exit_code == 1 and "turned off" in _flat(result.output)


def test_watch_collects_the_files_it_indexed_for_the_profile(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    [path] = copy_fixtures(cfg.vault.path, ["2024-01-15.md"])
    conn = db.connect(cfg)
    db.init_schema(conn)
    db.ensure_vec_table(conn, 8, cfg.ollama.embed_model)
    handler = ingester._DebounceHandler(cfg)
    handler.pending = {str(path): (0.0, "change")}
    indexed: list[str] = []
    assert ingester._apply_due(handler, cfg, conn, fake_client, indexed) is None
    assert indexed == [str(path)]
    fake_client.structured["ScannedFacts"] = _facts_for(("hiking", "hobbies", "I hike."))
    assert ingester._read_for_profile(cfg, conn, fake_client, indexed) == 1
    assert ingester._read_for_profile(cfg, conn, fake_client, indexed) == 0
