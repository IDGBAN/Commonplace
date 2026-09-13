from datetime import date

import pytest

from journal_analyzer.query import _explicit_day

TODAY = date(2024, 6, 15)


@pytest.mark.parametrize(
    "question, expected",
    [
        ("What happened on 2024-01-15?", date(2024, 1, 15)),
        ("What did I do on January 15, 2024?", date(2024, 1, 15)),
        ("Anything about Jan... 15 January 2024?", date(2024, 1, 15)),
        ("How was March 3rd?", date(2024, 3, 3)),
        ("What about yesterday?", date(2024, 6, 14)),
        ("How was today?", TODAY),
    ],
)
def test_named_days_are_resolved(question, expected):
    assert _explicit_day(question, TODAY) == expected


@pytest.mark.parametrize(
    "question",
    [
        "What happened since July 4th?",
        "Anything between March 1 and March 5?",
        "How were things from January 15 onwards?",
    ],
)
def test_range_phrasing_is_not_a_single_day(question):
    assert _explicit_day(question, TODAY) is None


def test_bare_month_day_in_the_future_resolves_to_last_year():
    assert _explicit_day("What happened on December 20?", TODAY) == date(2023, 12, 20)


def test_explicit_year_is_honoured_even_when_future():
    assert _explicit_day("Plans for December 20, 2025?", TODAY) == date(2025, 12, 20)


def test_impossible_dates_are_rejected():
    assert _explicit_day("What about February 31?", TODAY) is None
    assert _explicit_day("What about 2024-13-01?", TODAY) is None


def test_leap_day_rollback_is_not_invented():
    # 2023 has no Feb 29
    assert _explicit_day("How was February 29?", date(2024, 1, 5)) is None


def test_no_date_mentioned():
    assert _explicit_day("How have I been feeling lately?", TODAY) is None
