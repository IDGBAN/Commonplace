"""Answers that outlive the page that asked, and upkeep jobs from the browser."""

import json
import os
import threading
import time
from datetime import date

import pytest
from conftest import add_entry, add_note, copy_fixtures
from test_browse import Browser

from journal_analyzer import browse, history, store, upkeep, work
from journal_analyzer.models import QueryPlan


@pytest.fixture
def served(config, conn, fake_client):
    add_note(conn, "John Doe")
    add_entry(conn, date(2024, 1, 15), text="Hiked with John Doe.", mood=8.0, tags=["hiking"],
              links=["John Doe"], summary="Hiked with John", path="/vault/2024-01-15.md")
    add_entry(conn, date(2024, 2, 3), text="Rain.", mood=4.0, summary="Stayed in", path="/vault/2024-02-03.md")
    config.reranking.enabled = False
    config.history.enabled = True
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    server = browse.make_server(config, port=0, make_client=lambda cfg: fake_client)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Browser(server.port, server.token), server
    server.shutdown()
    server.server_close()


def _wait(check, seconds=10.0):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        found = check()
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("waited too long")


def _events(response) -> list[tuple[str, dict]]:
    out = []
    raw = response.read().decode()
    for block in raw.split("\n\n"):
        # not splitlines(), which also breaks on characters JSON leaves as they are
        lines = dict(line.split(": ", 1) for line in block.split("\n") if line and not line.startswith(":"))
        if lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def _run_id(response) -> str:
    """Reads a stream as far as the id of the answer it's following."""
    assert response.readline() == b"event: run\n"
    return json.loads(response.readline().split(b": ", 1)[1])["id"]


def test_an_answer_carries_on_when_the_page_goes_away(served, config, fake_client):
    browser, _ = served
    writing, go_on = threading.Event(), threading.Event()

    def chat(model, messages):
        writing.set()
        assert go_on.wait(10)
        return "You hiked with John on 2024-01-15."

    fake_client.chat_response = chat
    response = browser.ask("When did I hike?")
    run = _run_id(response)
    assert writing.wait(10)
    # the page reloads: its connection is gone while the model is still writing
    response.close()
    go_on.set()

    again = browser.request(f"/api/ask/{run}/events")
    events = _events(again)
    assert events[0] == ("run", {"id": run})
    assert events[-1][0] == "answer" and events[-1][1]["body"] == "You hiked with John on 2024-01-15."
    assert "".join(d["text"] for kind, d in events if kind == "text") == "You hiked with John on 2024-01-15."
    # and it was kept, though nobody was listening when it finished
    assert [k.question for k in history.recent(config)] == ["When did I hike?"]
    # a page that only missed the end can ask for the events after the ones it has
    tail = _events(browser.request(f"/api/ask/{run}/events?from={len(events) - 2}"))
    assert [kind for kind, _ in tail] == ["run", "answer"]


def test_cancel_stops_the_answer_being_written(served, config, fake_client):
    browser, _ = served
    writing, go_on = threading.Event(), threading.Event()

    def chat(model, messages):
        writing.set()
        assert go_on.wait(10)
        return "An answer nobody wants any more."

    fake_client.chat_response = chat
    response = browser.ask("When did I hike?")
    run = _run_id(response)
    assert writing.wait(10)
    assert browser.post(f"/api/ask/{run}/cancel") == {"cancelled": run}
    go_on.set()
    events = _events(response)
    assert events[-1] == ("cancelled", {})
    assert "answer" not in [kind for kind, _ in events]
    assert history.recent(config) == []


def test_an_answer_that_is_gone_says_so(served):
    browser, _ = served
    gone = "0" * 32
    assert "restarted" in browser.get(f"/api/ask/{gone}/events", status=404)["error"]
    assert "restarted" in browser.post(f"/api/ask/{gone}/cancel", status=404)["error"]
    # following an answer is reading; stopping one has to come from the page
    response = browser.request(f"/api/ask/{gone}/cancel")
    response.read()
    assert response.status == 405


def test_upkeep_says_what_is_out_of_date_and_writes_it(served, conn, fake_client):
    browser, _ = served
    fake_client.chat_response = "A month of hiking."
    before = browser.get("/api/upkeep")
    assert before["running"] is None and before["queued"] == [] and before["elsewhere"] is None
    assert before["digests"]["due"] > 0 and before["profile"]["unread"] == 2
    assert {"kind": "month", "n": 2, "text": "2 months"} in before["digests"]["kinds"]

    job = browser.post("/api/upkeep/start", {"kind": "digest", "key": "2024-01"})
    assert job["label"] == "Write the digest for January 2024" and job["params"] == {"key": "2024-01"}
    done = _wait(lambda: next((j for j in browser.get("/api/upkeep")["recent"] if j["id"] == job["id"]), None))
    assert (done["status"], done["error"]) == ("done", None)
    assert "Wrote 1 digest for January 2024" in done["result"]
    assert store.get_rollup(conn, "month", "2024-01")["summary"] == "A month of hiking."
    assert store.get_rollup(conn, "month", "2024-02") is None

    rest = browser.post("/api/upkeep/start", {"kind": "digests"})
    done = _wait(lambda: next((j for j in browser.get("/api/upkeep")["recent"] if j["id"] == rest["id"]), None))
    assert done["status"] == "done" and done["done"] == done["total"] - 1
    assert store.get_rollup(conn, "month", "2024-02") is not None
    assert browser.get("/api/upkeep")["digests"]["due"] == 0


def test_upkeep_refuses_what_it_cannot_do(served, monkeypatch):
    browser, server = served
    for body in ({"kind": "watch"}, {"kind": "digest", "key": "sometime"}, {}):
        assert "upkeep can do" in browser.post("/api/upkeep/start", body, status=400)["error"]
    assert "isn't waiting" in browser.post("/api/upkeep/9/remove", status=404)["error"]
    assert browser.post("/api/upkeep/stop") == {"stopping": False}
    # `journal watch` in a terminal holds the lock, so nothing starts on top of it
    monkeypatch.setattr(work, "holder", lambda config: {"pid": os.getpid() + 1, "what": "journal watch", "since": None})
    assert browser.get("/api/upkeep")["elsewhere"]["what"] == "journal watch"
    assert "journal watch is already working" in browser.post("/api/upkeep/start", {"kind": "themes"}, status=409)["error"]
    assert server.jobs.idle()


def test_a_job_that_fails_says_why_and_the_next_one_runs(served, fake_client):
    browser, _ = served

    def down(model, messages):
        raise RuntimeError("the model fell over")

    fake_client.chat_response = down
    bad = browser.post("/api/upkeep/start", {"kind": "digest", "key": "2024-01"})
    failed = _wait(lambda: next((j for j in browser.get("/api/upkeep")["recent"] if j["id"] == bad["id"]), None))
    assert failed["status"] == "failed" and "fell over" in failed["error"]
    fake_client.chat_response = "Fine now."
    good = browser.post("/api/upkeep/start", {"kind": "digest", "key": "2024-01"})
    assert _wait(lambda: next((j for j in browser.get("/api/upkeep")["recent"] if j["id"] == good["id"]), None))["status"] == "done"


def _jobs(config, runner):
    lane = work.Lane()
    return work.Jobs(config, lane, {"slow": runner}), lane


def test_jobs_run_one_at_a_time_and_stop_between_steps(config):
    at_step, go_on = threading.Event(), threading.Event()

    def slow(job, step):
        for i in range(3):
            step(i, 3, f"step {i}")
            at_step.set()
            assert go_on.wait(10)
        return "all three"

    jobs, _ = _jobs(config, slow)
    first = jobs.add("slow", "First")
    assert jobs.add("slow", "First again") is first  # the same job isn't put in line twice
    second = jobs.add("slow", "Second", {"n": 2})
    third = jobs.add("slow", "Third", {"n": 3})
    assert at_step.wait(10)
    shown = jobs.snapshot()
    assert shown["running"]["id"] == first.id and shown["running"]["message"] == "step 0"
    assert [j["id"] for j in shown["queued"]] == [second.id, third.id]
    assert jobs.remove(third.id) and not jobs.remove(first.id)

    assert jobs.stop()
    go_on.set()
    _wait(jobs.idle)
    recent = {j["id"]: j for j in jobs.snapshot()["recent"]}
    assert recent[first.id]["status"] == "stopped" and recent[first.id]["done"] == 1
    assert recent[second.id]["status"] == "done" and recent[second.id]["result"] == "all three"
    assert third.id not in recent
    # nothing holds the lock once the line is empty
    assert work.holder(config) is None


def test_a_question_goes_ahead_of_the_next_step(config):
    order: list[str] = []
    at_step, asked = threading.Event(), threading.Event()

    def slow(job, step):
        step(0, 2, "one")
        order.append("step one")
        at_step.set()
        assert asked.wait(10)
        time.sleep(0.05)  # the question is waiting for the model by now
        step(1, 2, "two")
        order.append("step two")
        return ""

    jobs, lane = _jobs(config, slow)
    job = jobs.add("slow", "Slow")
    assert at_step.wait(10)

    def question():
        asked.set()
        with lane.asking():
            order.append("answer")

    answering = threading.Thread(target=question)
    answering.start()
    answering.join(10)
    _wait(jobs.idle)
    assert order == ["step one", "answer", "step two"]
    assert jobs.snapshot()["recent"][0]["id"] == job.id


def test_the_lock_says_who_is_working_and_lets_go(config):
    assert work.holder(config) is None
    with work.hold(config, "journal index") as ours:
        assert ours
        found = work.holder(config)
        assert found["what"] == "journal index" and found["pid"] == os.getpid()
        # held by this process, so it isn't something going on elsewhere
        assert upkeep.elsewhere(config) is None
        with work.hold(config, "journal watch") as second:
            assert not second
    assert work.holder(config) is None
    with work.hold(config, "journal rollup") as ours:
        assert ours and work.holder(config)["what"] == "journal rollup"


def test_new_files_are_read_from_the_upkeep_page(served, config, conn, fake_client):
    browser, _ = served
    upkeep.forget_files(config)
    copy_fixtures(config.vault.path)
    files = browser.get("/api/upkeep")["files"]
    # the two entries the fixture put in the cache have no file behind them
    assert (files["new"], files["gone"], files["error"]) == (2, 3, None)
    assert files["due"] == 5 and files["calls"] > 0

    job = browser.post("/api/upkeep/start", {"kind": "index"})
    assert job["label"] == "Read new and changed files"
    done = _wait(lambda: next((j for j in browser.get("/api/upkeep")["recent"] if j["id"] == job["id"]), None))
    assert (done["status"], done["error"]) == ("done", None)
    assert done["result"] == "2 files read, 3 no longer there."
    assert (done["done"], done["total"]) == (1, 2)
    assert [r["entry_date"] for r in store.entry_days(conn)] == ["2024-01-15", "2024-01-16"]
    # reading them for the profile is left to a job of its own, put in line behind
    _wait(lambda: any(j["kind"] == "profile" for j in browser.get("/api/upkeep")["recent"]))
    after = browser.get("/api/upkeep")
    assert after["files"]["due"] == 0 and after["files"]["tracked"] == 2
    assert after["profile"]["unread"] == 0


def test_a_missing_journal_folder_is_said_on_the_page(served, config):
    browser, _ = served
    upkeep.forget_files(config)
    config.vault.path.rmdir()
    files = browser.get("/api/upkeep")["files"]
    assert files["due"] == 0 and str(config.vault.path) in files["error"]


def test_keeping_up_reads_a_file_when_it_is_saved(served, config, conn, fake_client):
    browser, server = served
    upkeep.forget_files(config)
    assert browser.get("/api/upkeep")["watching"]["on"] is False
    on = browser.post("/api/upkeep/watch", {"on": True})
    assert on["on"] is True and on["debounce"] == 0.1
    assert browser.post("/api/upkeep/watch", {"on": True})["on"] is True  # twice is once
    # turning it on looks for what changed before; an empty folder drops nothing
    _wait(lambda: server.watcher.read >= 1 and server.jobs.idle())
    assert len(store.entry_days(conn)) == 2

    copy_fixtures(config.vault.path, ["2024-01-16.md"])
    _wait(lambda: [r for r in store.entry_days(conn) if r["entry_date"] == "2024-01-16"], seconds=20)
    jobs = browser.get("/api/upkeep")["recent"]
    assert jobs[0]["label"] == "Read the files you saved" and jobs[0]["status"] == "done"
    # the profile isn't rewritten on every save: journal watch's own gap applies
    assert config.profile.watch_update_minutes > 0
    assert [j["kind"] for j in jobs].count("profile") <= 1

    assert browser.post("/api/upkeep/watch", {"on": False})["on"] is False
    assert browser.get("/api/upkeep")["watching"]["on"] is False


def test_keeping_up_gives_way_to_journal_watch(served, config, monkeypatch):
    browser, server = served
    monkeypatch.setattr(work, "holder", lambda config: {"pid": os.getpid() + 1, "what": "journal watch", "since": None})
    assert "another window" in browser.post("/api/upkeep/watch", {"on": True}, status=409)["error"]
    assert server.watcher.state()["on"] is False
    monkeypatch.undo()
    config.vault.path.rmdir()
    assert "no folder" in browser.post("/api/upkeep/watch", {"on": True}, status=503)["error"]
