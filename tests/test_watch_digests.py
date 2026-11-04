from datetime import date

import pytest
from conftest import add_entry, copy_fixtures, make_config

from journal_analyzer import db as db_mod
from journal_analyzer import ingester, rollups, store
from journal_analyzer.models import OllamaUnavailableError


def _digests(conn):
    return {(r["period_type"], r["period_key"]): bool(r["stale"]) for r in conn.execute("SELECT * FROM rollups")}


def test_writes_the_missing_and_stale_digests_of_those_days(config, conn, fake_client):
    add_entry(conn, date(2024, 1, 15), path="/vault/a.md")
    add_entry(conn, date(2024, 2, 20), path="/vault/b.md")
    add_entry(conn, date(2024, 3, 5), path="/vault/c.md")
    store.upsert_rollup(conn, "month", "2024-02", "Old February.", "[]", 5.0, 1)
    store.mark_rollups_stale(conn, date(2024, 2, 20))
    store.upsert_rollup(conn, "month", "2024-03", "Fine March.", "[]", 5.0, 1)
    fake_client.chat_response = "A new digest."

    written, waiting = rollups.refresh_days(
        config, conn, fake_client, {date(2024, 2, 20), date(2024, 3, 5)}
    )
    assert written == [("month", "2024-02"), ("week", "2024-W10"), ("week", "2024-W08")]
    assert waiting == set()
    assert store.get_rollup(conn, "month", "2024-02")["summary"] == "A new digest."
    assert store.get_rollup(conn, "month", "2024-03")["summary"] == "Fine March."
    # January wasn't touched, so it waits for journal rollup
    assert ("month", "2024-01") not in _digests(conn)


def test_stops_when_told_and_says_what_still_waits(config, conn, fake_client):
    add_entry(conn, date(2024, 1, 15), path="/vault/a.md")
    add_entry(conn, date(2024, 2, 20), path="/vault/b.md")
    calls = iter([True, False])
    written, waiting = rollups.refresh_days(
        config, conn, fake_client, {date(2024, 1, 15), date(2024, 2, 20)}, keep_going=lambda: next(calls)
    )
    assert written == [("month", "2024-02")]
    assert waiting == {date(2024, 1, 15), date(2024, 2, 20)}


def test_a_day_with_no_entries_left_needs_nothing(config, conn, fake_client):
    assert rollups.refresh_days(config, conn, fake_client, {date(2024, 1, 15)}) == ([], set())


@pytest.fixture
def watched(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    paths = copy_fixtures(cfg.vault.path)
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    yield cfg, conn, paths
    conn.close()


def test_events_say_which_days_changed(watched, fake_client):
    cfg, conn, paths = watched
    touched: set[date] = set()
    paths[0].write_text("Edited.", encoding="utf-8")
    ingester._apply_event(str(paths[0]), "change", cfg, conn, fake_client, touched)
    assert touched == {date(2024, 1, 15)}
    paths[1].unlink()
    ingester._apply_event(str(paths[1]), "delete", cfg, conn, fake_client, touched)
    assert touched == {date(2024, 1, 15), date(2024, 1, 16)}


def test_a_model_outage_keeps_the_days_for_later(watched, fake_client):
    cfg, conn, _ = watched

    def down(model, messages, options=None):
        raise OllamaUnavailableError("Ollama isn't running.")

    fake_client.chat = down
    handler = ingester._DebounceHandler(cfg)
    days = {date(2024, 1, 15)}
    assert ingester._refresh_digests(cfg, conn, fake_client, days, handler) == days


def _run_watch(cfg, conn, fake_client, monkeypatch, ticks=3):
    real = ingester._apply_due
    seen = {"ticks": 0}

    def tick(_seconds):
        seen["ticks"] += 1
        if seen["ticks"] > ticks:
            raise KeyboardInterrupt

    def apply_due(handler, config, conn_, client, indexed=None, touched=None):
        if seen["ticks"] == 1 and touched is not None:
            touched.add(date(2024, 1, 15))
        return real(handler, config, conn_, client, indexed, touched)

    monkeypatch.setattr(ingester.time, "sleep", tick)
    monkeypatch.setattr(ingester, "_apply_due", apply_due)
    ingester.run_watch(cfg, conn, fake_client)


def test_watch_writes_them_once_idle_when_turned_on(watched, fake_client, monkeypatch):
    cfg, conn, _ = watched
    cfg.rollups.watch_refresh_minutes = 30
    fake_client.chat_response = "Written by watch."
    _run_watch(cfg, conn, fake_client, monkeypatch)
    assert store.get_rollup(conn, "month", "2024-01")["summary"] == "Written by watch."
    assert store.get_rollup(conn, "week", "2024-W03")["summary"] == "Written by watch."


def test_watch_leaves_digests_alone_by_default(watched, fake_client, monkeypatch):
    cfg, conn, _ = watched
    _run_watch(cfg, conn, fake_client, monkeypatch)
    assert _digests(conn) == {}
