"""`journal review`: a year, month or week as a Markdown document, from what's already stored.

No model is called. The period's digest is used if `journal rollup` has written it.
"""

from __future__ import annotations

import calendar
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from . import compare, serialize, store
from .config import Config

# best and hardest days shown; a week lists every day instead
TOP_DAYS = {"year": 5, "month": 3, "week": 0}
TOP_NOTES = 10
TOP_TAGS = 10
NEW_NOTES = 10
# a tag "fell away" when it had at least this many entries the period before
# and this one has half as many or fewer
FELL_AWAY_MIN = 3
FELL_AWAY_SHOWN = 5
# facts the profile scan found in the period's entries
NEW_ABOUT_YOU = 12
BAR_WIDTH = 20

# what each kind of period is split into, and what "the year before" says
_PART_NAMES = {"year": "month", "month": "week"}


@dataclass(frozen=True)
class Part:
    """A month of a year or a week of a month, cut to the period it's in."""

    label: str
    start: date
    end: date
    mood: float | None
    entries: int


@dataclass
class Review:
    kind: str  # year, month or week
    key: str  # the way rollups store it, like 2025-03 or 2025-W10
    title: str  # 2025, March 2025, Week 10 of 2025
    start: date
    end: date
    scale: int
    digest: str | None
    digest_stale: bool
    writing: store.WritingStats
    avg_mood: float | None
    scored: int
    prev_key: str
    prev_avg_mood: float | None
    parts: list[Part]
    days: list[sqlite3.Row]  # a week's entries, listed one by one
    best: list[sqlite3.Row]
    hardest: list[sqlite3.Row]
    notes: list[sqlite3.Row]
    new_notes: list[sqlite3.Row]
    tags: list[tuple[str, int, int]]  # name, this period, the one before
    fell_away: list[tuple[str, int, int]]
    has_earlier: bool  # entries before this period, so "new" and "the year before" mean something
    about_you: list[tuple[str, str]]  # fact, the day it's from


def _mood_of(rows: list[sqlite3.Row]) -> float | None:
    scores = [r["mood_score"] for r in rows if r["mood_score"] is not None]
    return round(sum(scores) / len(scores), 2) if scores else None


def _months(conn: sqlite3.Connection, start: date, end: date) -> list[Part]:
    by_month = {r["month"]: r for r in store.monthly_mood(conn, start, end)}
    parts = []
    for m in range(1, 13):
        first, last = store.period_bounds("month", f"{start.year}-{m:02d}")
        row = by_month.get(f"{start.year}-{m:02d}")
        parts.append(Part(
            calendar.month_name[m], first, last,
            row["avg_mood"] if row else None, row["entry_count"] if row else 0,
        ))
    return parts


def _weeks(rows: list[sqlite3.Row], start: date, end: date) -> list[Part]:
    parts = []
    day = start
    while day <= end:
        first, last = store.period_bounds("week", store.week_key(day))
        first, last = max(first, start), min(last, end)
        inside = [r for r in rows if first.isoformat() <= r["entry_date"] <= last.isoformat()]
        span = f"{first.day}" if first == last else f"{first.day} to {last.day}"
        label = f"Week {day.isocalendar().week} ({span} {calendar.month_name[first.month]})"
        parts.append(Part(label, first, last, _mood_of(inside), len(inside)))
        day = date.fromordinal(last.toordinal() + 1)
    return parts


def build(config: Config, conn: sqlite3.Connection, period: str | int) -> Review | None:
    """`period` as typed, like 2025, 2025-03 or 2025-W10. ValueError if it's
    none of those; None when nothing was written in it."""
    kind, key, start, end = store.parse_period(str(period))
    writing = store.writing_stats(conn, start, end)
    if not writing.entries:
        return None
    rows = store.entries_in_range(conn, start, end)
    avg, scored = store.overall_mood(conn, start, end)
    prev_key, prev_start, prev_end = compare.previous(kind, start)
    prev_avg, _ = store.overall_mood(conn, prev_start, prev_end)
    this_period = {r["name"]: r["n"] for r in store.top_tags(conn, start, end, limit=-1)}
    before = {r["name"]: r["n"] for r in store.top_tags(conn, prev_start, prev_end, limit=-1)}
    tags = [(name, n, before.get(name, 0)) for name, n in list(this_period.items())[:TOP_TAGS]]
    fell_away = sorted(
        (
            (name, prev, this_period.get(name, 0))
            for name, prev in before.items()
            if prev >= FELL_AWAY_MIN and this_period.get(name, 0) * 2 <= prev
        ),
        key=lambda t: (t[2] - t[1], t[0]),
    )[:FELL_AWAY_SHOWN]
    digest = store.get_rollup(conn, kind, key)
    first, _ = store.date_bounds(conn, None, None)
    parts = _months(conn, start, end) if kind == "year" else _weeks(rows, start, end) if kind == "month" else []
    top_days = TOP_DAYS[kind]
    return Review(
        kind=kind,
        key=key,
        title=store.period_title(kind, key),
        start=start,
        end=end,
        scale=config.indexing.mood_scale_max,
        digest=digest["summary"] if digest else None,
        digest_stale=bool(digest["stale"]) if digest else False,
        writing=writing,
        avg_mood=avg,
        scored=scored,
        prev_key=prev_key,
        prev_avg_mood=prev_avg,
        parts=parts,
        days=rows if kind == "week" else [],
        best=store.entries_by_mood(conn, start, end, top_days, highest=True) if top_days else [],
        hardest=store.entries_by_mood(conn, start, end, top_days, highest=False) if top_days else [],
        notes=store.top_entities(conn, start, end, limit=TOP_NOTES),
        new_notes=store.notes_first_linked_in(conn, start, end, NEW_NOTES),
        tags=tags,
        fell_away=fell_away,
        has_earlier=first is not None and first < start.isoformat(),
        about_you=_new_about_you(conn, start, end),
    )


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _mood(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "-"


def _day_lines(rows: list[sqlite3.Row]) -> list[str]:
    return [
        f"- [[{r['entry_date']}]] (mood {_mood(r['mood_score'])}): {r['summary'] or '(no summary)'}"
        for r in rows
    ]


def _new_about_you(conn: sqlite3.Connection, start: date, end: date) -> list[tuple[str, str]]:
    seen: set[str] = set()
    found = []
    for row in reversed(store.profile_facts(conn, 1000, start, end)):  # oldest first
        key = row["fact"].casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append((row["fact"], row["entry_date"]))
    return found[:NEW_ABOUT_YOU]


def to_markdown(review: Review) -> str:
    w = review.writing
    before = f"the {review.kind} before"
    lines = [f"# {review.title} in review", ""]
    if review.digest:
        lines += [review.digest, ""]
        if review.digest_stale:
            lines += [
                f"*Some entries changed after this was written. `journal digest {review.key} "
                "--refresh` rewrites it.*",
                "",
            ]
    else:
        lines += [
            f"*There's no {review.kind} digest yet. `journal digest {review.key} --refresh` "
            "writes one.*",
            "",
        ]

    lines += ["## In numbers", ""]
    lines.append(
        f"- {_count(w.entries, 'entry', 'entries')} on {_count(w.days_written, 'day', 'days')}, "
        f"{w.words:,} {'word' if w.words == 1 else 'words'}"
    )
    if w.longest_streak_end is not None:
        lines.append(
            f"- longest streak: {_count(w.longest_streak, 'day', 'days')}, ending [[{w.longest_streak_end.isoformat()}]]"
        )
    if review.avg_mood is not None:
        mood = f"- average mood {_mood(review.avg_mood)} of {review.scale}, from {_count(review.scored, 'scored entry', 'scored entries')}"
        if review.prev_avg_mood is not None:
            mood += f" ({_mood(review.prev_avg_mood)} {before})"
        lines.append(mood)
    lines.append("")

    if review.parts:
        part = _PART_NAMES[review.kind]
        lines += [f"## Mood by {part}", "", f"| {part.capitalize()} | Mood | | Entries |", "|---|---|---|---|"]
        for p in review.parts:
            bar = "█" * round(p.mood / review.scale * BAR_WIDTH) if p.mood is not None else ""
            lines.append(f"| {p.label} | {_mood(p.mood)} | {bar} | {p.entries} |")
        lines.append("")
    if review.days:
        lines += ["## Day by day", ""]
        lines += [
            f"- {calendar.day_name[date.fromisoformat(r['entry_date']).weekday()]} "
            f"[[{r['entry_date']}]] (mood {_mood(r['mood_score'])}): {r['summary'] or '(no summary)'}"
            for r in review.days
        ]
        lines.append("")

    if review.best:
        lines += ["## Best days", "", *_day_lines(review.best), ""]
    if review.hardest:
        lines += ["## Hardest days", "", *_day_lines(review.hardest), ""]
    if review.notes:
        lines += ["## Who and what came up most", ""]
        lines += [f"- [[{r['name']}]] ({r['type']}): {_count(r['n'], 'entry', 'entries')}" for r in review.notes]
        lines.append("")
    if review.new_notes and review.has_earlier:
        lines += [f"## New this {review.kind}", ""]
        lines += [
            f"- [[{r['title']}]] ({r['kind']}), first on [[{r['first']}]], {_count(r['n'], 'entry', 'entries')}"
            for r in review.new_notes
        ]
        lines.append("")
    if review.tags and review.has_earlier:
        lines += ["## Tags", "", f"| Tag | Entries | {before.capitalize()} |", "|---|---|---|"]
        lines += [f"| {name} | {n} | {prev or '-'} |" for name, n, prev in review.tags]
        lines.append("")
    elif review.tags:
        lines += ["## Tags", "", "| Tag | Entries |", "|---|---|"]
        lines += [f"| {name} | {n} |" for name, n, _ in review.tags]
        lines.append("")
    if review.fell_away:
        lines += ["## Fell away", ""]
        lines += [f"- {name}: {_count(n, 'entry', 'entries')}, down from {prev}" for name, prev, n in review.fell_away]
        lines.append("")
    if review.about_you:
        lines += ["## New about you", ""]
        lines += [f"- {fact} ([[{day}]])" for fact, day in review.about_you]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_json(conn: sqlite3.Connection, review: Review) -> dict[str, Any]:
    """The same review for --json, MCP and the browser. Entries have the browse API's shape."""
    prev_start, prev_end = store.period_bounds(review.kind, review.prev_key)
    return {
        "kind": review.kind,
        "key": review.key,
        "title": review.title,
        "start": review.start.isoformat(),
        "end": review.end.isoformat(),
        "digest": {"summary": review.digest, "stale": review.digest_stale} if review.digest else None,
        "writing": {
            k: v.isoformat() if isinstance(v, date) else v for k, v in asdict(review.writing).items()
        },
        "mood": {
            "average": review.avg_mood,
            "scored": review.scored,
            "scale": review.scale,
            "previous": review.prev_avg_mood,
        },
        "previous": {
            "key": review.prev_key, "start": prev_start.isoformat(), "end": prev_end.isoformat(),
        },
        "parts": [
            {**asdict(p), "start": p.start.isoformat(), "end": p.end.isoformat()} for p in review.parts
        ],
        "days": serialize.entry_briefs(conn, review.days),
        "best": serialize.entry_briefs(conn, review.best),
        "hardest": serialize.entry_briefs(conn, review.hardest),
        "notes": [
            {"id": r["id"], "title": r["name"], "kind": r["type"], "entries": r["n"]} for r in review.notes
        ],
        "new_notes": [
            {**serialize.note_ref(r), "first": r["first"], "entries": r["n"]} for r in review.new_notes
        ],
        "tags": [{"name": n, "entries": a, "previous": b} for n, a, b in review.tags],
        "fell_away": [{"name": n, "entries": b, "previous": a} for n, a, b in review.fell_away],
        "about_you": [{"fact": f, "date": d} for f, d in review.about_you],
        "has_earlier": review.has_earlier,
    }
