from datetime import date

from conftest import add_entry

from journal_analyzer import query, store
from journal_analyzer.models import QueryPlan


def _months(conn, year: int, count: int) -> None:
    for i in range(count):
        y, month = year + i // 12, i % 12 + 1
        key = f"{y}-{month:02d}"
        add_entry(conn, date(y, month, 10))
        store.upsert_rollup(conn, "month", key, f"month {key}", "[]", None, 1)


def test_a_narrative_question_without_dates_reads_the_month_digests(conn):
    _months(conn, 2024, 4)
    lines = query._digest_lines(QueryPlan(intent="narrative"), conn)
    assert lines == [f"- [month 2024-0{m}] month 2024-0{m}" for m in (1, 2, 3, 4)]


def test_a_factual_question_without_dates_gets_no_digests(conn):
    _months(conn, 2024, 4)
    assert query._digest_lines(QueryPlan(intent="factual"), conn) == []


def test_a_span_of_a_few_days_is_left_to_its_entries(conn):
    for day in (3, 5, 9):
        add_entry(conn, date(2024, 1, day))
    store.upsert_rollup(conn, "week", "2024-W01", "week one", "[]", None, 3)
    plan = QueryPlan(intent="narrative", date_from=date(2024, 1, 1), date_to=date(2024, 1, 9))
    assert query._digest_lines(plan, conn) == []


def test_a_span_of_a_few_weeks_reads_the_week_digests(conn):
    _months(conn, 2024, 2)
    for key in ("2024-W02", "2024-W04", "2024-W06", "2023-W50"):
        store.upsert_rollup(conn, "week", key, f"week {key}", "[]", None, 1)
    plan = QueryPlan(intent="narrative", date_from=date(2024, 1, 1), date_to=date(2024, 2, 28))
    assert query._digest_lines(plan, conn) == [
        "- [week 2024-W02] week 2024-W02",
        "- [week 2024-W04] week 2024-W04",
        "- [week 2024-W06] week 2024-W06",
    ]


def test_only_the_latest_weeks_are_used(conn):
    add_entry(conn, date(2024, 1, 1))
    add_entry(conn, date(2024, 3, 1))
    for week in range(1, 10 + query.MAX_WEEK_DIGESTS):
        store.upsert_rollup(conn, "week", f"2024-W{week:02d}", f"w{week}", "[]", None, 1)
    lines = query._digest_lines(QueryPlan(intent="narrative"), conn)
    assert len(lines) == query.MAX_WEEK_DIGESTS
    assert lines[-1] == "- [week 2024-W09] w9"


def test_the_span_is_measured_on_the_entries_not_the_question(conn):
    # a year-long range with only a few weeks of entries in it
    _months(conn, 2024, 1)
    add_entry(conn, date(2024, 1, 30))
    plan = QueryPlan(intent="factual", date_from=date(2024, 1, 1), date_to=date(2024, 12, 31))
    assert query._digest_lines(plan, conn) == []


def test_years_stand_in_for_months_across_a_long_span(conn):
    _months(conn, 2022, 24)
    for year in ("2022", "2023"):
        store.upsert_rollup(conn, "year", year, f"year {year}", "[]", None, 12)
    lines = query._digest_lines(QueryPlan(intent="narrative"), conn)
    assert lines == ["- [year 2022] year 2022", "- [year 2023] year 2023"]


def test_without_year_digests_only_the_latest_months_are_used(conn):
    _months(conn, 2022, 24)
    lines = query._digest_lines(QueryPlan(intent="narrative"), conn)
    assert len(lines) == query.MAX_MONTH_DIGESTS
    assert lines[-1] == "- [month 2023-12] month 2023-12"


def test_narrative_answers_are_given_the_digests(conn, config, fake_client):
    _months(conn, 2024, 4)
    fake_client.structured["QueryPlan"] = QueryPlan(intent="narrative")
    seen = []

    def chat(model, messages):
        seen.append(messages[-1]["content"])
        return "ok"

    fake_client.chat_response = chat
    query.answer("How has this year gone?", config, conn, fake_client, today=date(2024, 6, 1))
    assert "- [month 2024-03] month 2024-03" in seen[0]
