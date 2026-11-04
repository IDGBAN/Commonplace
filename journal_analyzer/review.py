"""`journal review`: one year as a Markdown document, from what's already stored.

No model is called. The year digest is used if `journal rollup` has written it.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from . import store
from .config import Config

TOP_DAYS = 5
TOP_NOTES = 10
TOP_TAGS = 10
NEW_NOTES = 10
# a tag "fell away" when it had at least this many entries the year before
# and this year has half as many or fewer
FELL_AWAY_MIN = 3
FELL_AWAY_SHOWN = 5
# facts the profile scan found in that year's entries
NEW_ABOUT_YOU = 12
BAR_WIDTH = 20


@dataclass
class YearReview:
    year: int
    scale: int
    digest: str | None
    digest_stale: bool
    writing: store.WritingStats
    avg_mood: float | None
    scored: int
    prev_avg_mood: float | None
    months: list[tuple[int, float | None, int]]  # month number, mood, entries
    best: list[sqlite3.Row]
    hardest: list[sqlite3.Row]
    notes: list[sqlite3.Row]
    new_notes: list[sqlite3.Row]
    tags: list[tuple[str, int, int]]  # name, this year, the year before
    fell_away: list[tuple[str, int, int]]
    has_earlier: bool  # entries before this year, so "new" and "the year before" mean something
    extra: list[tuple[str, list[str]]] = field(default_factory=list)  # heading, lines


def build(config: Config, conn: sqlite3.Connection, year: int) -> YearReview | None:
    start, end = date(year, 1, 1), date(year, 12, 31)
    writing = store.writing_stats(conn, start, end)
    if not writing.entries:
        return None
    avg, scored = store.overall_mood(conn, start, end)
    prev_avg, _ = store.overall_mood(conn, date(year - 1, 1, 1), date(year - 1, 12, 31))
    by_month = {r["month"]: r for r in store.monthly_mood(conn, start, end)}
    months = []
    for m in range(1, 13):
        row = by_month.get(f"{year}-{m:02d}")
        months.append((m, row["avg_mood"] if row else None, row["entry_count"] if row else 0))
    this_year = {r["name"]: r["n"] for r in store.top_tags(conn, start, end, limit=10_000)}
    before = {
        r["name"]: r["n"]
        for r in store.top_tags(conn, date(year - 1, 1, 1), date(year - 1, 12, 31), limit=10_000)
    }
    tags = [(name, n, before.get(name, 0)) for name, n in list(this_year.items())[:TOP_TAGS]]
    fell_away = sorted(
        (
            (name, prev, this_year.get(name, 0))
            for name, prev in before.items()
            if prev >= FELL_AWAY_MIN and this_year.get(name, 0) * 2 <= prev
        ),
        key=lambda t: (t[2] - t[1], t[0]),
    )[:FELL_AWAY_SHOWN]
    digest = store.get_rollup(conn, "year", str(year))
    first, _ = store.date_bounds(conn, None, None)
    return YearReview(
        year=year,
        scale=config.indexing.mood_scale_max,
        digest=digest["summary"] if digest else None,
        digest_stale=bool(digest["stale"]) if digest else False,
        writing=writing,
        avg_mood=avg,
        scored=scored,
        prev_avg_mood=prev_avg,
        months=months,
        best=store.entries_by_mood(conn, start, end, TOP_DAYS, highest=True),
        hardest=store.entries_by_mood(conn, start, end, TOP_DAYS, highest=False),
        notes=store.top_entities(conn, start, end, limit=TOP_NOTES),
        new_notes=store.notes_first_linked_in(conn, start, end, NEW_NOTES),
        tags=tags,
        fell_away=fell_away,
        has_earlier=first is not None and first < start.isoformat(),
        extra=[("New about you", about)] if (about := _new_about_you(conn, start, end)) else [],
    )


def _mood(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "-"


def _day_lines(rows: list[sqlite3.Row]) -> list[str]:
    return [
        f"- [[{r['entry_date']}]] (mood {_mood(r['mood_score'])}): {r['summary'] or '(no summary)'}"
        for r in rows
    ]


def _new_about_you(conn: sqlite3.Connection, start: date, end: date) -> list[str]:
    seen: set[str] = set()
    lines = []
    for row in reversed(store.profile_facts(conn, 1000, start, end)):  # oldest first
        key = row["fact"].casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {row['fact']} ([[{row['entry_date']}]])")
    return lines[:NEW_ABOUT_YOU]


def to_markdown(review: YearReview) -> str:
    w = review.writing
    lines = [f"# {review.year} in review", ""]
    if review.digest:
        lines += [review.digest, ""]
        if review.digest_stale:
            lines += [
                f"*Some entries changed after this was written. `journal digest {review.year} "
                "--refresh` rewrites it.*",
                "",
            ]
    else:
        lines += [
            f"*There's no year digest yet. `journal digest {review.year} --refresh` writes one.*",
            "",
        ]

    lines += ["## In numbers", ""]
    lines.append(f"- {w.entries} entries on {w.days_written} days, {w.words:,} words")
    if w.longest_streak_end is not None:
        lines.append(
            f"- longest streak: {w.longest_streak} days, ending [[{w.longest_streak_end.isoformat()}]]"
        )
    if review.avg_mood is not None:
        mood = f"- average mood {_mood(review.avg_mood)} of {review.scale}, from {review.scored} scored entries"
        if review.prev_avg_mood is not None:
            mood += f" ({_mood(review.prev_avg_mood)} the year before)"
        lines.append(mood)
    lines.append("")

    lines += ["## Mood by month", "", "| Month | Mood | | Entries |", "|---|---|---|---|"]
    for month, month_mood, count in review.months:
        bar = "█" * round(month_mood / review.scale * BAR_WIDTH) if month_mood is not None else ""
        lines.append(f"| {calendar.month_name[month]} | {_mood(month_mood)} | {bar} | {count} |")
    lines.append("")

    if review.best:
        lines += ["## Best days", "", *_day_lines(review.best), ""]
    if review.hardest:
        lines += ["## Hardest days", "", *_day_lines(review.hardest), ""]
    if review.notes:
        lines += ["## Who and what came up most", ""]
        lines += [f"- [[{r['name']}]] ({r['type']}): {r['n']} entries" for r in review.notes]
        lines.append("")
    if review.new_notes and review.has_earlier:
        lines += ["## New this year", ""]
        lines += [
            f"- [[{r['title']}]] ({r['kind']}), first on [[{r['first']}]], {r['n']} entries"
            for r in review.new_notes
        ]
        lines.append("")
    if review.tags and review.has_earlier:
        lines += ["## Tags", "", "| Tag | Entries | The year before |", "|---|---|---|"]
        lines += [f"| {name} | {n} | {prev or '-'} |" for name, n, prev in review.tags]
        lines.append("")
    elif review.tags:
        lines += ["## Tags", "", "| Tag | Entries |", "|---|---|"]
        lines += [f"| {name} | {n} |" for name, n, _ in review.tags]
        lines.append("")
    if review.fell_away:
        lines += ["## Fell away", ""]
        lines += [f"- {name}: {n} entries, down from {prev}" for name, prev, n in review.fell_away]
        lines.append("")
    for heading, extra in review.extra:
        lines += [f"## {heading}", "", *extra, ""]
    return "\n".join(lines).rstrip() + "\n"
