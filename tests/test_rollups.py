import json
from datetime import date

from conftest import add_entry, add_note

from journal_analyzer import rollups, store


def _add(conn, d, mood=6.0, tags=("work",), links=()):
    add_entry(conn, d, mood=mood, tags=tags, links=links, summary=f"day {d}")


def _seed(conn):
    _add(conn, date(2024, 1, 15), 4.0, ("work",))
    _add(conn, date(2024, 1, 16), 8.0, ("work", "friends"))
    _add(conn, date(2024, 2, 20), 6.0, ("rest",))


def _rollup(conn, period_type, period_key):
    return conn.execute(
        "SELECT * FROM rollups WHERE period_type = ? AND period_key = ?",
        (period_type, period_key),
    ).fetchone()


def test_generates_week_month_and_year_digests(conn, config, fake_client):
    _seed(conn)
    fake_client.chat_response = "A digest."
    counts = rollups.generate_rollups(config, conn, fake_client)
    assert counts == {"week": 2, "month": 2, "year": 1}
    month = _rollup(conn, "month", "2024-01")
    assert month["summary"] == "A digest."
    assert month["entry_count"] == 2
    assert month["avg_mood"] == 6.0
    assert json.loads(month["dominant_tags"])[0] == "work"
    assert month["stale"] == 0


def test_years_are_built_from_month_digests_not_entries(conn, config, fake_client):
    _seed(conn)
    seen = []

    def chat(model, messages):
        seen.append(messages[-1]["content"])
        return "A digest."

    fake_client.chat_response = chat
    rollups.generate_rollups(config, conn, fake_client)
    [year_prompt] = [p for p in seen if p.startswith("Year ")]
    assert year_prompt.startswith("Year 2024 months:")
    assert "2024-01 (avg mood 6)" in year_prompt
    assert "day 2024-01-15" not in year_prompt


def test_only_stale_skips_fresh_digests(conn, config, fake_client):
    _seed(conn)
    fake_client.chat_response = "A digest."
    rollups.generate_rollups(config, conn, fake_client)
    assert rollups.generate_rollups(config, conn, fake_client) == {
        "week": 0, "month": 0, "year": 0
    }
    store.mark_rollups_stale(conn, date(2024, 1, 15))
    counts = rollups.generate_rollups(config, conn, fake_client)
    assert counts == {"week": 1, "month": 1, "year": 1}


def test_rebuild_all_regenerates_everything(conn, config, fake_client):
    _seed(conn)
    fake_client.chat_response = "A digest."
    rollups.generate_rollups(config, conn, fake_client)
    counts = rollups.generate_rollups(config, conn, fake_client, only_stale=False)
    assert counts == {"week": 2, "month": 2, "year": 1}


def test_digests_for_emptied_periods_are_pruned(conn, config, fake_client):
    _seed(conn)
    fake_client.chat_response = "A digest."
    rollups.generate_rollups(config, conn, fake_client)
    assert _rollup(conn, "month", "2024-02") is not None

    conn.execute("DELETE FROM entries WHERE entry_date LIKE '2024-02%'")
    conn.commit()
    rollups.generate_rollups(config, conn, fake_client)
    assert _rollup(conn, "month", "2024-02") is None
    assert _rollup(conn, "month", "2024-01") is not None
    assert _rollup(conn, "year", "2024") is not None


def test_months_and_years_are_written_before_weeks(conn, config, fake_client):
    _seed(conn)
    keys = []

    def chat(model, messages):
        keys.append(messages[-1]["content"].split()[1])
        return "A digest."

    fake_client.chat_response = chat
    rollups.generate_rollups(config, conn, fake_client)
    assert keys == ["2024-01", "2024-02", "2024", "2024-W03", "2024-W08"]


def test_no_entries_generates_nothing(conn, config, fake_client):
    assert rollups.generate_rollups(config, conn, fake_client) == {
        "week": 0, "month": 0, "year": 0
    }
    assert fake_client.chat_calls == []


def test_period_digests_name_the_notes_entries_link_to(conn, config, fake_client):
    add_note(conn, "Sara Miller")
    _add(conn, date(2024, 1, 15), links=["Sara Miller"])
    prompts = []

    def chat(model, messages):
        prompts.append(messages[-1]["content"])
        return "A digest."

    fake_client.chat_response = chat
    rollups.generate_rollups(config, conn, fake_client, period_types=("week",))
    assert "(links: Sara Miller)" in prompts[0]
