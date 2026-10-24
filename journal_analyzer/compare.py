"""`journal compare`: two periods side by side, from what's already stored.

No model is called. Tags and notes are compared by the share of entries
that have them, since two periods rarely hold the same number of entries:
a tag on 10 of 20 entries is more of that period than one on 30 of 300.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import store

# rows in each list of what rose and fell
TOP_N = 8
# a tag or note needs this many entries in a period before it counts as rising,
# falling, new or gone, so one mention isn't a change
MIN_COUNT = 2


@dataclass
class Side:
    label: str
    start: date
    end: date
    writing: store.WritingStats
    mood: float | None
    scored: int
    tags: dict[str, int]
    notes: dict[str, tuple[str, int]]  # title -> (kind, entries)
    metrics: dict[str, tuple[float, int]]  # key -> (average, entries)


@dataclass(frozen=True)
class Change:
    name: str
    a: int  # entries in the first period
    b: int  # and in the second
    a_share: float  # of the period's entries, 0..1
    b_share: float
    kind: str | None = None


@dataclass
class Comparison:
    a: Side
    b: Side
    tags_up: list[Change] = field(default_factory=list)
    tags_down: list[Change] = field(default_factory=list)
    notes_up: list[Change] = field(default_factory=list)
    notes_down: list[Change] = field(default_factory=list)
    notes_new: list[Change] = field(default_factory=list)
    notes_gone: list[Change] = field(default_factory=list)


def previous(kind: str, start: date) -> tuple[str, date, date]:
    """The period of the same kind just before the one starting on `start`."""
    if kind == "year":
        key = str(start.year - 1)
    elif kind == "month":
        key = store.month_key(start - timedelta(days=1))
    else:
        key = store.week_key(start - timedelta(days=7))
    begin, end = store.period_bounds(kind, key)
    return key, begin, end


def _side(conn: sqlite3.Connection, label: str, start: date, end: date) -> Side:
    mood, scored = store.overall_mood(conn, start, end)
    return Side(
        label=label,
        start=start,
        end=end,
        writing=store.writing_stats(conn, start, end),
        mood=mood,
        scored=scored,
        tags={r["name"]: r["n"] for r in store.top_tags(conn, start, end, limit=-1)},
        notes={r["name"]: (r["type"], r["n"]) for r in store.top_entities(conn, start, end, limit=-1)},
        metrics=store.metric_averages(conn, start, end),
    )


def _changes(
    a: dict[str, int], b: dict[str, int], a_total: int, b_total: int, kinds: dict[str, str]
) -> list[Change]:
    out = []
    for name in sorted(set(a) | set(b)):
        n_a, n_b = a.get(name, 0), b.get(name, 0)
        out.append(
            Change(
                name, n_a, n_b,
                n_a / a_total if a_total else 0.0,
                n_b / b_total if b_total else 0.0,
                kinds.get(name),
            )
        )
    return out


def _rising(changes: list[Change]) -> list[Change]:
    up = [c for c in changes if c.b >= MIN_COUNT and c.a and c.b_share > c.a_share]
    return sorted(up, key=lambda c: (c.a_share - c.b_share, c.name))[:TOP_N]


def _falling(changes: list[Change]) -> list[Change]:
    down = [c for c in changes if c.a >= MIN_COUNT and c.b and c.b_share < c.a_share]
    return sorted(down, key=lambda c: (c.b_share - c.a_share, c.name))[:TOP_N]


def compare(
    conn: sqlite3.Connection,
    first: tuple[str, date, date],
    second: tuple[str, date, date],
) -> Comparison:
    """`first` and `second` are (label, start, end)."""
    a = _side(conn, *first)
    b = _side(conn, *second)
    tags = _changes(a.tags, b.tags, a.writing.entries, b.writing.entries, {})
    kinds = {name: kind for side in (a, b) for name, (kind, _) in side.notes.items()}
    notes = _changes(
        {k: n for k, (_, n) in a.notes.items()},
        {k: n for k, (_, n) in b.notes.items()},
        a.writing.entries, b.writing.entries, kinds,
    )
    return Comparison(
        a=a,
        b=b,
        tags_up=_rising(tags),
        tags_down=_falling(tags),
        notes_up=_rising(notes),
        notes_down=_falling(notes),
        notes_new=sorted(
            (c for c in notes if not c.a and c.b >= MIN_COUNT), key=lambda c: (-c.b, c.name)
        )[:TOP_N],
        notes_gone=sorted(
            (c for c in notes if not c.b and c.a >= MIN_COUNT), key=lambda c: (-c.a, c.name)
        )[:TOP_N],
    )
