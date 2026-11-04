from datetime import date

from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import cli, rollups, store


def _flat(output: str) -> str:
    return " ".join(output.split())


def _seed(conn):
    for d in (date(2024, 1, 15), date(2024, 1, 16), date(2024, 3, 5), date(2025, 2, 1)):
        add_entry(conn, d, mood=6.0, tags=["work"], summary=f"day {d}", path=f"/vault/{d}.md")


def _keys(plan):
    return [(p.kind, p.key, p.reason) for p in plan]


def test_plan_lists_what_is_missing_in_writing_order(conn, config):
    _seed(conn)
    plan = rollups.plan_rollups(config, conn)
    kinds = [p.kind for p in plan]
    assert kinds == sorted(kinds, key=rollups.ORDER.index)
    assert ("month", "2024-01", "missing") in _keys(plan)
    assert ("year", "2025", "missing") in _keys(plan)


def test_plan_filters_by_date_and_kind(conn, config):
    _seed(conn)
    plan = rollups.plan_rollups(config, conn, ("month", "week"), True, date(2024, 2, 1), date(2024, 12, 31))
    assert _keys(plan) == [("month", "2024-03", "missing"), ("week", "2024-W10", "missing")]


def test_plan_reasons(conn, config):
    _seed(conn)
    store.upsert_rollup(conn, "month", "2024-01", "s", "[]", 6.0, 2)
    store.upsert_rollup(conn, "month", "2024-03", "s", "[]", 6.0, 1)
    store.mark_rollups_stale(conn, date(2024, 3, 5))
    months = rollups.plan_rollups(config, conn, ("month",))
    assert _keys(months) == [("month", "2024-03", "stale"), ("month", "2025-02", "missing")]
    rebuild = rollups.plan_rollups(config, conn, ("month",), only_stale=False)
    assert ("month", "2024-01", "rebuild") in _keys(rebuild)


def test_note_digests_ignore_the_date_range(conn, config):
    add_note(conn, "John")
    for i in range(8):
        add_entry(conn, date(2020, 1, i + 1), links=["John"], path=f"/vault/s{i}.md")
    plan = rollups.plan_rollups(config, conn, ("note",), True, date(2024, 1, 1), None)
    assert _keys(plan) == [("note", "john", "missing")]


def test_generate_writes_only_the_planned_periods(conn, config, fake_client):
    _seed(conn)
    fake_client.chat_response = "A digest."
    counts = rollups.generate_rollups(
        config, conn, fake_client, ("month",), True, date(2024, 3, 1), date(2024, 3, 31)
    )
    assert counts == {"profile": 0, "month": 1, "year": 0, "note": 0, "week": 0, "range": 0}
    assert [r["period_key"] for r in store.all_rollups(conn)] == ["2024-03"]


def test_a_year_needs_its_months_first(conn, config, fake_client):
    _seed(conn)
    assert rollups.generate_rollups(config, conn, fake_client, ("year",))["year"] == 0
    assert rollups.generate_rollups(config, conn, fake_client, ("month", "year"))["year"] == 2


def test_digest_command_shows_a_stored_digest(tmp_path, config, conn):
    _seed(conn)
    store.upsert_rollup(conn, "month", "2024-01", "A calm January.", '["work"]', 6.0, 2)
    result = invoke_cli(tmp_path, config, "digest", "2024-01")
    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert "January 2024 2024-01-01 .. 2024-01-31" in output
    assert "2 entries, mood 6" in output and "tags: work" in output
    assert "A calm January." in output


def test_digest_command_says_how_to_write_a_missing_one(tmp_path, config, conn):
    _seed(conn)
    output = _flat(invoke_cli(tmp_path, config, "digest", "2024-w03").output)
    assert "Week 3 of 2024" in output
    assert "journal digest 2024-W03 --refresh" in output


def test_refresh_writes_a_year_and_its_months(tmp_path, config, conn, fake_client, monkeypatch):
    _seed(conn)
    fake_client.chat_response = "Written."
    monkeypatch.setattr(cli, "OllamaClient", lambda cfg: fake_client)
    result = invoke_cli(tmp_path, config, "digest", "2024", "--refresh")
    assert result.exit_code == 0, result.output
    assert "Writing 3 digests" in _flat(result.output)
    assert {(r["period_type"], r["period_key"]) for r in store.all_rollups(conn)} == {
        ("month", "2024-01"), ("month", "2024-03"), ("year", "2024")}
    again = invoke_cli(tmp_path, config, "digest", "2024", "--refresh")
    assert "Already up to date." in _flat(again.output)


def test_digest_command_refuses_bad_periods(tmp_path, config, conn):
    _seed(conn)
    for period in ("March", "2024-13", "2024-W60"):
        result = invoke_cli(tmp_path, config, "digest", period)
        assert result.exit_code == 1, period
        assert "isn't a period" in _flat(result.output)
    assert invoke_cli(tmp_path, config, "digest", "1999").exit_code == 1


def test_rollup_dry_run_and_only(tmp_path, config, conn):
    _seed(conn)
    result = invoke_cli(tmp_path, config, "rollup", "--dry-run", "--only", "year,month", "--since", "2025-01-01")
    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert "month │ 2025-02 │ not written yet" in output
    assert "year │ 2025 │ not written yet" in output
    assert "2024" not in output
    assert "2 calls to the precise model." in output
    assert store.all_rollups(conn) == []
    bad = invoke_cli(tmp_path, config, "rollup", "--only", "months")
    assert bad.exit_code == 1 and "--only takes" in _flat(bad.output)
