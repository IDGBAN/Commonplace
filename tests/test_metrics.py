import json
from datetime import date

import pytest
from conftest import add_entry, invoke_cli, make_config

from journal_analyzer import db as db_mod
from journal_analyzer import export, fields, ingester, insights, query, rollups, store
from journal_analyzer.models import QueryPlan


def _flat(output: str) -> str:
    return " ".join(output.split())


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("7", 7.0), ("7.5", 7.5), ("7,5", 7.5), ("-2", -2.0), ("7h", 7.0), ("7.5 hours", 7.5),
        ("30 min", 30.0), ("8k", 8000.0), ("7:30", 7.5), ("true", 1.0), ("Yes", 1.0),
        ("false", 0.0), (["6"], 6.0), ("6-7", None), ("tired", None), ("2024-01-15", None),
        ("", None), ([], None), (None, None),
    ],
)
def test_metric_values(raw, value):
    assert fields.metric_value(raw) == value


def _metrics_config(config, **values):
    return config.model_copy(update={"metrics": config.metrics.model_copy(update=values)})


def test_every_numeric_property_counts_unless_left_out(config):
    cfg = config.model_copy(
        update={"vault": config.vault.model_copy(update={"frontmatter_mood_key": "mood"})}
    )
    front = {"Sleep": "7", "mood": "6", "date": "2024-01-01", "tags": ["x"], "energy": "high",
             "Exercise": "true", "week": "3"}
    assert fields.entry_metrics(front, cfg) == {"sleep": 7.0, "exercise": 1.0, "week": 3.0}
    assert fields.entry_metrics(front, _metrics_config(cfg, ignore=["week"])) == {
        "sleep": 7.0, "exercise": 1.0}
    assert fields.entry_metrics(front, _metrics_config(cfg, keys=["sleep"])) == {"sleep": 7.0}


def test_indexing_stores_metrics_and_settings_apply_without_models(tmp_path, fake_client):
    cfg = make_config(tmp_path)
    (cfg.vault.path / "2024-03-01.md").write_text(
        "---\nsleep: 6.5\nweek: 9\n---\nSlow start.", encoding="utf-8"
    )
    conn = db_mod.connect(cfg)
    db_mod.init_schema(conn)
    ingester.run_backfill(cfg, conn, client=fake_client)
    entry_id = conn.execute("SELECT id FROM entries").fetchone()["id"]
    assert store.metrics_for_entries(conn, [entry_id]) == {entry_id: {"sleep": 6.5, "week": 9.0}}
    calls = len(fake_client.structured_calls)
    assert fields.refresh(_metrics_config(cfg, ignore=["week"]), conn) == 1
    assert store.metrics_for_entries(conn, [entry_id]) == {entry_id: {"sleep": 6.5}}
    assert len(fake_client.structured_calls) == calls


def _sleepy(conn, with_metrics=True):
    # better sleep, better mood
    for day, (sleep, mood) in enumerate([(5, 3.0), (5.5, 4.0), (6, 4.0), (8, 8.0), (8.5, 7.0), (9, 8.0)], 1):
        entry_id = add_entry(conn, date(2024, 3, day), mood=mood, summary=f"day {day}",
                             path=f"/vault/2024-03-0{day}.md")
        if with_metrics:
            store.replace_entry_metrics(conn, entry_id, {"sleep": sleep, "gym": float(day in (1, 4, 5))})


def test_insights_compare_mood_on_either_side_of_a_metric(conn, config):
    _sleepy(conn)
    found = insights.compute_insights(config, conn)
    texts = [f.text for f in found.findings if f.kind == "metric_mood"]
    assert any(t.startswith("Days with 'sleep' below its median of 8") for t in texts)
    assert not any("'gym'" in t for t in texts)  # moods match on gym and rest days


def test_aggregate_answers_get_the_named_metric_by_month(conn, config, fake_client):
    _sleepy(conn)
    fake_client.structured["QueryPlan"] = QueryPlan(intent="aggregate")
    seen = []
    fake_client.chat_response = lambda model, messages: seen.append(messages[-1]["content"]) or "ok"
    query.answer("How did I sleep in March?", config, conn, fake_client, today=date(2024, 6, 1))
    assert "Monthly average of 'sleep', a number recorded in the entries:" in seen[0]
    assert "- 2024-03: 7 over 6 entries" in seen[0]


def test_summaries_and_digests_carry_the_numbers(conn, config, fake_client):
    _sleepy(conn)
    rows = store.entries_in_range(conn, date(2024, 3, 1), date(2024, 3, 1))
    context = query._summary_context(rows, [], QueryPlan(intent="factual"), config, conn)
    assert "recorded: gym 1, sleep 5" in context
    text, _, _ = rollups._rollup_members_digest(conn, rows)
    assert "(mood 3, gym 1, sleep 5)" in text


def test_export_includes_metrics(conn):
    _sleepy(conn)
    entries = export.collect_entries(conn)
    assert entries[0]["metrics"] == {"gym": 1.0, "sleep": 5.0}
    assert "gym 1 · sleep 5" in export.to_markdown(entries)


def test_metric_command(tmp_path, config, conn):
    assert "No numbers found" in _flat(invoke_cli(tmp_path, config, "metric").output)
    _sleepy(conn)
    listing = _flat(invoke_cli(tmp_path, config, "metric").output)
    assert "sleep │ 6 │ 5 │ 9 │ 7 │ 2024-03-01 .. 2024-03-06" in listing
    monthly = invoke_cli(tmp_path, config, "metric", "Sleep")
    assert monthly.exit_code == 0, monthly.output
    assert "2024-03 │ 7 │" in _flat(monthly.output)
    unknown = invoke_cli(tmp_path, config, "metric", "steps")
    assert unknown.exit_code == 1 and "Known: gym, sleep." in _flat(unknown.output)


def test_show_prints_the_numbers(tmp_path, config, conn):
    _sleepy(conn)
    assert "numbers: gym 1, sleep 5" in _flat(invoke_cli(tmp_path, config, "show", "2024-03-01").output)


def test_export_json_round_trips(tmp_path, config, conn):
    _sleepy(conn)
    data = json.loads(invoke_cli(tmp_path, config, "export").output)
    assert data["entries"][-1]["metrics"] == {"gym": 0.0, "sleep": 9.0}


def test_a_yes_no_metric_compares_days_with_and_without():
    pairs = [(1.0, 8.0)] * 3 + [(0.0, 5.0)] * 3
    found = insights._metric_finding("exercise", pairs, threshold=0.8, min_count=3)
    assert found is not None
    assert found.text == "Days with 'exercise': average mood 8.0, against 5.0 on days without (3 and 3 entries)"
    assert insights._metric_finding("exercise", pairs[:4], threshold=0.8, min_count=3) is None
