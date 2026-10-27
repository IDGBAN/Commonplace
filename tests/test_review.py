import json
from datetime import date

import pytest
from conftest import add_entry, add_note, invoke_cli

from journal_analyzer import review, store


def _seed(conn):
    add_note(conn, "John Doe")
    add_note(conn, "Jun Park")
    add_entry(conn, date(2023, 5, 1), mood=5.0, tags=["climbing", "work"], links=["Jun Park"],
              summary="Bouldering with Jun", path="/vault/2023-05-01.md")
    for i, tag in enumerate(["climbing", "climbing", "climbing"]):
        add_entry(conn, date(2023, 6, i + 1), mood=6.0, tags=[tag], path=f"/vault/2023-06-0{i + 1}.md")
    add_entry(conn, date(2024, 1, 1), mood=9.0, tags=["work"], links=["John Doe"],
              summary="New year walk with John", path="/vault/2024-01-01.md")
    add_entry(conn, date(2024, 1, 2), mood=2.0, tags=["work"], summary="Rough day",
              path="/vault/2024-01-02.md")
    add_entry(conn, date(2024, 3, 9), mood=7.0, tags=["garden"], links=["John Doe"],
              summary="Planted beans", path="/vault/2024-03-09.md")


def test_build_gathers_the_year(conn, config):
    _seed(conn)
    built = review.build(config, conn, 2024)
    assert built is not None
    assert (built.writing.entries, built.writing.days_written, built.writing.longest_streak) == (3, 3, 2)
    assert built.avg_mood == 6.0 and built.prev_avg_mood == 5.75
    assert [r["entry_date"] for r in built.best][:1] == ["2024-01-01"]
    assert [r["entry_date"] for r in built.hardest][:1] == ["2024-01-02"]
    jan, feb = built.parts[:2]
    assert (jan.label, jan.mood, jan.entries) == ("January", 5.5, 2)
    assert (feb.label, feb.mood, feb.entries) == ("February", None, 0)
    assert len(built.parts) == 12 and built.days == []
    assert [(r["title"], r["n"]) for r in built.new_notes] == [("John Doe", 2)]
    assert ("work", 2, 1) in built.tags
    assert built.fell_away == [("climbing", 4, 0)]
    assert built.has_earlier


def test_markdown_links_days_and_notes(conn, config):
    _seed(conn)
    store.upsert_rollup(conn, "year", "2024", "A year of beans.", "[]", 6.0, 3)
    text = review.to_markdown(review.build(config, conn, 2024))
    assert text.startswith("# 2024 in review\n\nA year of beans.")
    assert "- 3 entries on 3 days" in text
    assert "(5.8 the year before)" in text
    assert "- [[2024-01-01]] (mood 9.0): New year walk with John" in text
    assert "- [[John Doe]] (people), first on [[2024-01-01]], 2 entries" in text
    assert "| work | 2 | 1 |" in text
    assert "- climbing: 0 entries, down from 4" in text
    assert "| January | 5.5 |" in text


def test_a_first_year_has_nothing_to_compare_with(conn, config):
    _seed(conn)
    text = review.to_markdown(review.build(config, conn, 2023))
    assert "New this year" not in text and "The year before" not in text
    assert "There's no year digest yet. `journal digest 2023 --refresh` writes one." in text


def test_an_empty_year_is_none(conn, config):
    _seed(conn)
    assert review.build(config, conn, 2019) is None


def test_review_command(tmp_path, config, conn):
    _seed(conn)
    result = invoke_cli(tmp_path, config, "review")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# 2024 in review")
    out = tmp_path / "out" / "2023.md"
    result = invoke_cli(tmp_path, config, "review", "2023", "--out", str(out))
    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8").startswith("# 2023 in review")
    inside = config.vault.path / "review.md"
    assert invoke_cli(tmp_path, config, "review", "2023", "--out", str(inside)).exit_code == 1
    assert not inside.exists()
    assert invoke_cli(tmp_path, config, "review", "2019").exit_code == 1


def test_a_month_is_split_into_its_weeks(conn, config):
    _seed(conn)
    add_entry(conn, date(2024, 1, 30), mood=4.0, tags=["work"], summary="Late January",
              path="/vault/2024-01-30.md")
    built = review.build(config, conn, "2024-01")
    assert built is not None and built.title == "January 2024"
    # 1 January 2024 is a Monday, so the month starts on week 1 and ends partway through week 5
    assert [(p.label, p.entries) for p in built.parts] == [
        ("Week 1 (1 to 7 January)", 2), ("Week 2 (8 to 14 January)", 0),
        ("Week 3 (15 to 21 January)", 0), ("Week 4 (22 to 28 January)", 0),
        ("Week 5 (29 to 31 January)", 1),
    ]
    assert built.parts[0].mood == 5.5
    assert [r["entry_date"] for r in built.best] == ["2024-01-01", "2024-01-30", "2024-01-02"]
    assert built.prev_key == "2023-12" and built.prev_avg_mood is None
    text = review.to_markdown(built)
    assert text.startswith("# January 2024 in review\n\n*There's no month digest yet. "
                           "`journal digest 2024-01 --refresh` writes one.*")
    assert "## Mood by week" in text and "| Week 5 (29 to 31 January) | 4.0 |" in text
    assert "| Tag | Entries | The month before |" in text
    assert "## New this month" in text


def test_a_week_lists_each_day(conn, config):
    _seed(conn)
    built = review.build(config, conn, "2024-w1")
    assert built is not None and built.key == "2024-W01" and built.title == "Week 1 of 2024"
    assert built.parts == [] and built.best == [] and built.hardest == []
    text = review.to_markdown(built)
    assert "## Day by day" in text
    assert "- Monday [[2024-01-01]] (mood 9.0): New year walk with John" in text
    assert "- Tuesday [[2024-01-02]] (mood 2.0): Rough day" in text
    assert "Best days" not in text
    assert "- [[John Doe]] (people): 1 entry" in text


def test_a_week_that_crosses_a_month_takes_its_weeks_days_from_both(conn, config):
    add_entry(conn, date(2024, 1, 31), summary="End of January", path="/vault/a.md")
    add_entry(conn, date(2024, 2, 2), summary="Start of February", path="/vault/b.md")
    built = review.build(config, conn, "2024-W05")
    assert [r["entry_date"] for r in built.days] == ["2024-01-31", "2024-02-02"]
    feb = review.build(config, conn, "2024-02")
    assert feb.parts[0].label == "Week 5 (1 to 4 February)"
    assert feb.parts[0].entries == 1


def test_a_period_that_isnt_one_is_refused(conn, config):
    with pytest.raises(ValueError):
        review.build(config, conn, "spring")


def test_json_has_the_same_review(conn, config):
    _seed(conn)
    data = review.to_json(conn, review.build(config, conn, 2024))
    assert (data["kind"], data["key"], data["start"], data["end"]) == ("year", "2024", "2024-01-01", "2024-12-31")
    assert data["previous"] == {"key": "2023", "start": "2023-01-01", "end": "2023-12-31"}
    assert data["mood"] == {"average": 6.0, "scored": 3, "scale": 10, "previous": 5.75}
    assert data["parts"][0] == {"label": "January", "start": "2024-01-01", "end": "2024-01-31",
                                "mood": 5.5, "entries": 2}
    assert data["best"][0]["date"] == "2024-01-01" and data["best"][0]["tags"] == ["work"]
    assert data["notes"][0]["title"] == "John Doe" and data["notes"][0]["id"] is not None
    assert data["new_notes"][0]["title"] == "John Doe" and data["new_notes"][0]["entries"] == 2
    assert {"name": "climbing", "entries": 0, "previous": 4} in data["fell_away"]
    json.dumps(data)


def test_review_command_takes_months_and_weeks(tmp_path, config, conn):
    _seed(conn)
    result = invoke_cli(tmp_path, config, "review", "2024-03")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("# March 2024 in review")
    result = invoke_cli(tmp_path, config, "review", "2024-W01", "--json")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["title"] == "Week 1 of 2024"
    result = invoke_cli(tmp_path, config, "review", "2019-05")
    assert result.exit_code == 1 and "No entries in May 2019." in result.output
    result = invoke_cli(tmp_path, config, "review", "spring", "--json")
    assert result.exit_code == 1 and "isn't a period" in json.loads(result.output)["error"]
    result = invoke_cli(tmp_path, config, "review", "2024", "--json", "--out", str(tmp_path / "x.md"))
    assert result.exit_code == 1 and not (tmp_path / "x.md").exists()
