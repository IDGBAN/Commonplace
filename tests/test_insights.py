from datetime import date, timedelta

from conftest import add_entry, add_note

from journal_analyzer import insights


def _add(conn, d, mood, tags, links=()):
    add_entry(conn, d, mood=mood, tags=tags, links=links)


def _seed(conn):
    # baseline 6.0, 'work' days low, days with Sara high
    add_note(conn, "Sara")
    day = 1
    for _ in range(3):
        _add(conn, date(2024, 1, day), 3.0, ["work"])
        day += 1
    for _ in range(3):
        _add(conn, date(2024, 1, day), 9.0, ["friends", "outdoors"], links=["Sara"])
        day += 1
    for _ in range(4):
        _add(conn, date(2024, 1, day), 6.0, ["misc"])
        day += 1


def test_baseline_and_directional_tag_findings(conn, config):
    _seed(conn)
    result = insights.compute_insights(config, conn)
    assert result.overall_avg == 6.0
    assert result.overall_count == 10
    joined = " ".join(result.evidence_lines())
    assert "work" in joined and "lower" in joined
    assert "friends" in joined and "higher" in joined
    assert "misc" not in joined


def test_linked_note_and_cooccurrence_findings(conn, config):
    _seed(conn)
    result = insights.compute_insights(config, conn)
    kinds = {f.kind for f in result.findings}
    assert "entity_mood" in kinds
    assert "cooccurrence" in kinds
    entity_line = next(f.text for f in result.findings if f.kind == "entity_mood")
    assert "Sara" in entity_line and "people" in entity_line


def test_below_min_sample_is_not_reported(conn, config):
    # two 'work' days, under the default min_sample_size of 3
    _add(conn, date(2024, 2, 1), 2.0, ["work"])
    _add(conn, date(2024, 2, 2), 2.0, ["work"])
    for d in range(3, 9):
        _add(conn, date(2024, 2, d), 6.0, ["misc"])
    result = insights.compute_insights(config, conn)
    assert all("work" not in line for line in result.evidence_lines())


def test_generate_narrates_when_findings_exist(conn, config, fake_client):
    _seed(conn)
    fake_client.chat_response = "You seem happiest around friends and outdoors."
    text, result = insights.generate_insights(config, conn, fake_client)
    assert text == "You seem happiest around friends and outdoors."
    assert result.findings
    assert fake_client.chat_calls[-1][0] == config.ollama.precise_model


def test_generate_handles_empty_and_flat_data(conn, config, fake_client):
    text, result = insights.generate_insights(config, conn, fake_client)
    assert result.overall_avg is None
    assert "nothing" in text.lower()
    assert fake_client.chat_calls == []

    # all the same mood, so nothing deviates
    for d in range(1, 6):
        _add(conn, date(2024, 3, d), 6.0, ["misc"])
    text, result = insights.generate_insights(config, conn, fake_client)
    assert result.overall_avg == 6.0
    assert result.findings == []
    assert "significance bar" in text
    assert fake_client.chat_calls == []


def test_thin_weekday_buckets_do_not_become_a_rhythm(conn, config):
    # ten days in a row, so every weekday has one or two entries, under min_sample_size
    for offset, mood in enumerate([9.0, 2.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0, 6.0]):
        _add(conn, date(2024, 1, 1) + timedelta(days=offset), mood, ["misc"])
    result = insights.compute_insights(config, conn)
    assert {f.kind for f in result.findings}.isdisjoint({"weekday", "season"})


def test_a_well_sampled_weekday_rhythm_is_reported(conn, config):
    day = date(2024, 1, 1)  # a Monday
    for week in range(4):
        _add(conn, day + timedelta(days=week * 7), 9.0, ["misc"])       # Mondays
        _add(conn, day + timedelta(days=week * 7 + 1), 3.0, ["misc"])   # Tuesdays
    result = insights.compute_insights(config, conn)
    weekday = next(f for f in result.findings if f.kind == "weekday")
    assert "Monday is the brightest" in weekday.text
    assert "Tuesday the lowest" in weekday.text


def test_date_range_scopes_the_baseline(conn, config):
    _seed(conn)
    scoped = insights.compute_insights(config, conn, date_from=date(2024, 1, 4))
    assert scoped.overall_count == 7  # the three low 'work' days are excluded
    assert scoped.date_from == date(2024, 1, 4)
