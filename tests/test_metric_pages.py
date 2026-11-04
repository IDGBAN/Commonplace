from datetime import date

import pytest
from conftest import add_entry

from journal_analyzer import browse, insights, store


def _journal(conn):
    days = [(date(2024, 1, 1), 6.0, 8, 1), (date(2024, 1, 2), 3.0, 5, 0), (date(2024, 2, 1), 7.0, 7.5, 1),
            (date(2024, 2, 2), 4.0, 6, 0), (date(2024, 2, 3), None, 9, 1)]
    for day, mood, sleep, ran in days:
        entry = add_entry(conn, day, mood=mood, summary=f"Day {day}", path=f"/vault/{day}.md")
        store.replace_entry_metrics(conn, entry, {"sleep": sleep, "ran": ran})
    # a second entry the same day averages with the first
    extra = add_entry(conn, date(2024, 2, 3), mood=8.0, summary="Later", path="/vault/2024-02-03b.md")
    store.replace_entry_metrics(conn, extra, {"sleep": 7})


@pytest.fixture
def req(config, conn):
    _journal(conn)
    return browse.Request(config, conn, {}, lambda c: None)


def test_yes_no_numbers_are_told_apart(conn):
    _journal(conn)
    assert store.metric_is_yes_no(conn, "ran")
    assert not store.metric_is_yes_no(conn, "sleep")
    assert not store.metric_is_yes_no(conn, "nothing")


def test_days_average_their_entries(conn):
    _journal(conn)
    days = {r["date"]: r["value"] for r in store.metric_days(conn, "sleep")}
    assert days["2024-02-03"] == 8.0 and days["2024-01-01"] == 8.0


def test_highest_and_lowest_carry_the_value(conn):
    _journal(conn)
    top = store.entries_by_metric(conn, "sleep", 2, highest=True)
    assert [(r["entry_date"], r["value"]) for r in top] == [("2024-02-03", 9.0), ("2024-01-01", 8.0)]
    low = store.entries_by_metric(conn, "sleep", 1, highest=False)
    assert [(r["entry_date"], r["value"]) for r in low] == [("2024-01-02", 5.0)]


def test_mood_by_month_only_counts_entries_that_record_it(conn):
    _journal(conn)
    add_entry(conn, date(2024, 3, 1), mood=1.0, path="/vault/march.md")
    months = [dict(r) for r in store.monthly_mood_with_metric(conn, "ran")]
    assert months == [{"month": "2024-01", "avg_mood": 4.5, "entry_count": 2},
                      {"month": "2024-02", "avg_mood": 5.5, "entry_count": 3}]


def test_the_split_matches_what_insights_reports():
    yes_no = insights.mood_split([(1, 7.0), (1, 6.0), (0, 3.0)])
    assert (yes_no.yes_no, yes_no.low, yes_no.high) == (True, [3.0], [7.0, 6.0])
    by_median = insights.mood_split([(5, 3.0), (6, 4.0), (8, 6.0), (7.5, 7.0)])
    assert (by_median.median, by_median.low, by_median.high) == (7.5, [3.0, 4.0], [6.0, 7.0])
    assert insights.mood_split([(7, 3.0), (7, 4.0)]) is None


def test_the_numbers_page_lists_each_one(req):
    listed = {m["key"]: m for m in browse.metrics(req)["metrics"]}
    assert listed["sleep"]["n"] == 6 and listed["sleep"]["yes_no"] is False
    assert listed["ran"]["yes_no"] is True and listed["ran"]["avg"] == 0.6


def test_a_numbers_page_has_months_moods_and_extremes(req):
    page = browse.metric(req, "Sleep")
    assert page["key"] == "sleep" and page["yes_no"] is False
    assert [m["month"] for m in page["months"]] == [m["month"] for m in page["mood_months"]] == ["2024-01", "2024-02"]
    assert page["highest"][0]["date"] == "2024-02-03" and page["highest"][0]["value"] == 9.0
    assert page["lowest"][0]["value"] == 5.0
    split = page["split"]
    assert split["median"] == 7.0 and split["high"]["days"] == 3 and split["low"]["days"] == 2
    assert split["enough"] is False  # min_sample_size is 3
    assert {k: page["days"][-1][k] for k in ("date", "value")} == {"date": "2024-02-03", "value": 8.0}


def test_an_unknown_number_is_a_404(req):
    with pytest.raises(browse.ApiError) as raised:
        browse.metric(req, "steps")
    assert raised.value.status == 404 and "No entry records 'steps'" in str(raised.value)
