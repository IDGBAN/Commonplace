from datetime import date

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
    assert built.months[0] == (1, 5.5, 2) and built.months[1] == (2, None, 0)
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
