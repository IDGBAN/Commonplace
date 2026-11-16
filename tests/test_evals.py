"""The made-up journal and its runner, with the models mocked."""

import re
from datetime import date, timedelta

import pytest
from conftest import make_config

from evals import journal, report, runner
from evals.__main__ import main
from evals.scenarios import BY_ID, SCENARIOS
from journal_analyzer import query
from journal_analyzer.models import FollowUp, QueryPlan


@pytest.fixture
def work(tmp_path):
    return tmp_path / "work"


@pytest.fixture
def eval_config(tmp_path, work):
    return runner._in_work_folder(make_config(tmp_path).model_dump(mode="json"), work)


def test_the_run_never_opens_the_real_vault_or_cache(tmp_path, work, eval_config):
    real = make_config(tmp_path)
    assert eval_config.vault.path == work / "vault" != real.vault.path
    assert eval_config.db.path.parent == work and eval_config.db.path != real.db.path
    # the models are still the ones the config names
    assert eval_config.ollama == real.ollama
    assert not eval_config.profile.enabled and not eval_config.history.enabled


def test_scenarios_hang_together():
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    for scenario in SCENARIOS:
        assert scenario.want and scenario.tags
        if scenario.after:
            assert scenario.after in BY_ID and not BY_ID[scenario.after].after
        for pattern in (*scenario.must, *scenario.must_not):
            re.compile(pattern)


def test_the_facts_the_scenarios_turn_on_are_in_the_journal():
    days = [day for day, *_ in journal.ENTRIES]
    assert days == sorted(days) and days[-1] == journal.TODAY - timedelta(days=1)
    assert journal.TODAY.strftime("%A") == "Monday"
    october = [
        int(km)
        for day, text, *_ in journal.ENTRIES if day.month == 10
        for km in re.findall(r"Ran (\d+) km", text)
    ]
    assert (len(october), sum(october)) == (10, 64)
    # every linked note exists
    notes = {title for title, *_ in journal.NOTES}
    assert {name for *_, links in journal.ENTRIES for name in links} <= notes
    text = {day: body for day, body, *_ in journal.ENTRIES}
    assert "wrote the fix herself" in text[date(2026, 10, 13)]
    assert "Wednesday the 18th" in text[date(2026, 11, 12)]


def test_a_run_keeps_each_answer_and_a_follow_up_gets_its_first_turn(work, eval_config, fake_client):
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.structured["FollowUp"] = FollowUp(question="How much does Luke owe after the cash?")
    seen = []

    def chat(model, messages):
        seen.append(messages)
        return "He owes £150." if len(messages) > 2 else "He owes £200."

    fake_client.chat_response = chat
    said = []
    out = runner.run(
        "try", eval_config, fake_client, runner.chosen(["owes_correction"]), work=work, say=said.append
    )
    got = report.answers("try", work)
    assert [g["id"] for g in got] == ["owes_correction"] and (out / "run.json").is_file()
    (answer,) = got
    assert answer["tripped"] == [] and answer["searched"] == "How much does Luke owe after the cash?"
    assert answer["stage"] == "precise_raw" and answer["read_entries"] > 0
    # a follow-up that tells rather than asks is reasoned with, and the run says so
    assert answer["plan"]["wants"] == "judgment"
    # the first turn was asked first, and its answer is the conversation the follow-up joins
    _system, asked_before, answered_before, _last = seen[-1]
    assert asked_before["content"] == BY_ID["owes"].question
    assert answered_before["content"] == "He owes £200."
    assert said == ["owes_correction [0] precise_raw 0s, no tripwire"]
    shown = report.show(["try"], work=work)
    assert BY_ID["owes_correction"].want in shown and "He owes £150." in shown
    assert "owes_correction" in report.summary(["try"], work)


def test_the_cache_is_built_once_and_again_when_the_journal_changes(work, eval_config, fake_client, monkeypatch):
    runner.build(eval_config, fake_client).close()
    embedded = len(fake_client.embed_calls)
    assert embedded >= len(journal.ENTRIES) + len(journal.NOTES)
    runner.build(eval_config, fake_client).close()
    assert len(fake_client.embed_calls) == embedded
    monkeypatch.setattr(journal, "TODAY", journal.TODAY + timedelta(days=1))
    changed = runner._in_work_folder(eval_config.model_dump(mode="json"), work)
    assert changed.db.path != eval_config.db.path


def test_tripwires_and_choosing():
    owes = BY_ID["owes"]
    assert runner.tripped(owes, "He owes you £200.") == []
    assert runner.tripped(owes, "He owes you nothing.") == ["missing /200/"]
    assert runner.tripped(BY_ID["tomorrow"], "The report, mum, and the dentist.") == ["has /dentist/"]
    assert [s.id for s in runner.chosen(["ridge", "runs"])] == ["runs", "ridge"]
    assert {s.id for s in runner.chosen(tags=["lookup"])} == {"ridge", "mum_gift"}
    with pytest.raises(ValueError, match="No scenario called nope"):
        runner.chosen(["nope"])


def test_a_constant_can_be_set_for_one_run(monkeypatch):
    monkeypatch.setattr(query, "JUDGMENT_TOP_K", query.JUDGMENT_TOP_K)
    assert runner.override(["JUDGMENT_TOP_K=24"]) == {"JUDGMENT_TOP_K": 24}
    assert query.JUDGMENT_TOP_K == 24
    with pytest.raises(ValueError, match="nothing called NOT_THERE"):
        runner.override(["NOT_THERE=1"])


def test_the_command_lists_scenarios_and_says_when_a_run_is_missing(work, capsys):
    assert main(["list"]) == 0
    assert "demo_fault" in capsys.readouterr().out
    assert main(["--work", str(work), "show", "nothing"]) == 1
    assert "No run called nothing" in capsys.readouterr().err
