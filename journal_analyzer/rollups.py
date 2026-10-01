"""Week and month digests are built from entry summaries, years from month digests.

Notes linked from many entries also get a relationship digest, built from the
note itself and the summaries of the entries that link to it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Literal

from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
)

from . import context, store
from .config import Config
from .ollama_client import OllamaClient

console = Console()

DOMINANT_TAG_COUNT = 5
# the note's own text that goes in with the entries; its summary when it has one
NOTE_DIGEST_TEXT_CHARS = 1500
# of each linked note that goes in as background
LINKED_NOTE_CHARS = 600

_VOICE = (
    "Write it in the journal writer's own first-person voice (I, me, my), as "
    "if they were looking back on it themselves, and open with what happened "
    'rather than a frame: never "This period", "This week", "The author" or '
    '"The user".'
)

_LINKED_HEADER = (
    "Notes about the people, places and topics these entries link to, as "
    "background. They aren't events of this period:"
)

_WEEK_MONTH_PROMPT = (
    "You summarize a period of someone's journal from per-entry one-line "
    "summaries, mood scores, tags, and the notes each entry links to (the "
    "people, places and topics involved, with what the writer's notes say "
    "about them). Write a cohesive 2-4 sentence "
    "digest of the period: main themes, notable events, and the overall "
    f"emotional arc. {_VOICE} Do not invent events not present in the input."
)

_YEAR_PROMPT = (
    "You summarize a year of someone's journal from its monthly digests, with "
    "notes about the people, places and topics it links to most as background. "
    "Write a cohesive 3-6 sentence digest of the year: dominant themes, key "
    f"turning points, and the overall emotional arc. {_VOICE} Do not invent "
    "events not present in the input."
)


_NOTE_PROMPT = (
    "You write about one person, place, group, project or topic from someone's "
    "journal, using the note they keep about it and one-line summaries of the "
    "journal entries that link to it, grouped by year. Write 3-6 sentences on "
    "how it figures in their life over time: when it first shows up, what they "
    "do or feel around it, how that changes from year to year, and where things "
    "stand in the latest entries. Write it in the journal writer's own "
    "first-person voice (I, me, my) and start with the subject by name, never "
    'with "This note", "The author" or "The user". Do not invent events not '
    "present in the input."
)


def _entry_periods(conn: sqlite3.Connection) -> dict[str, dict[str, list[sqlite3.Row]]]:
    rows = conn.execute(
        "SELECT id, entry_date, mood_score, summary FROM entries ORDER BY entry_date"
    ).fetchall()
    groups: dict[str, dict[str, list[sqlite3.Row]]] = {
        "week": defaultdict(list),
        "month": defaultdict(list),
    }
    for row in rows:
        d = date.fromisoformat(row["entry_date"])
        groups["week"][store.week_key(d)].append(row)
        groups["month"][store.month_key(d)].append(row)
    return groups


def _prune_orphan_rollups(
    conn: sqlite3.Connection, groups: dict[str, dict[str, list[sqlite3.Row]]]
) -> int:
    # Periods with no entries left never get regenerated, so their old digests
    # would stay around and keep showing up in answers.
    keep = {("week", k) for k in groups["week"]}
    keep |= {("month", k) for k in groups["month"]}
    keep |= {("year", k[:4]) for k in groups["month"]}
    doomed = [
        (r["period_type"], r["period_key"])
        for r in conn.execute("SELECT period_type, period_key FROM rollups")
        if (r["period_type"], r["period_key"]) not in keep
    ]
    if doomed:
        with conn:
            conn.executemany(
                "DELETE FROM rollups WHERE period_type = ? AND period_key = ?",
                doomed,
            )
    return len(doomed)


def _member_stats(
    members: list[sqlite3.Row], tags_by_entry: dict[int, list[str]]
) -> tuple[list[str], float | None]:
    tag_counter: Counter[str] = Counter()
    for row in members:
        tag_counter.update(tags_by_entry.get(row["id"], []))
    moods = [r["mood_score"] for r in members if r["mood_score"] is not None]
    dominant = [t for t, _ in tag_counter.most_common(DOMINANT_TAG_COUNT)]
    avg_mood = round(sum(moods) / len(moods), 2) if moods else None
    return dominant, avg_mood


def _with_linked_notes(
    text: str, config: Config, conn: sqlite3.Connection, entry_ids: list[int]
) -> str:
    blocks = context.linked_notes(
        conn, entry_ids, config.vault.path,
        config.rollups.linked_notes_chars, LINKED_NOTE_CHARS,
    )
    return f"{text}\n\n{_LINKED_HEADER}\n" + "\n".join(blocks) if blocks else text


def _rollup_members_digest(
    conn: sqlite3.Connection, members: list[sqlite3.Row]
) -> tuple[str, list[str], float | None]:
    lines: list[str] = []
    ids = [r["id"] for r in members]
    tags_by_entry = store.tags_for_entries(conn, ids)
    notes_by_entry = store.entities_for_entries(conn, ids)
    metrics = store.metrics_for_entries(conn, ids)
    for row in members:
        tags = tags_by_entry.get(row["id"], [])
        mood = row["mood_score"]
        mood_str = f"{mood:g}" if mood is not None else "-"
        numbers = "".join(f", {k} {v:g}" for k, v in metrics.get(row["id"], {}).items())
        names = [n["name"] for n in notes_by_entry.get(row["id"], [])]
        links = f" (links: {', '.join(names)})" if names else ""
        lines.append(
            f"- {row['entry_date']} (mood {mood_str}{numbers}) "
            f"[{', '.join(tags)}]{links}: {row['summary'] or '(no summary)'}"
        )
    dominant, avg_mood = _member_stats(members, tags_by_entry)
    return "\n".join(lines), dominant, avg_mood


def _write_period(
    period_type: str,
    period_key: str,
    members: list[sqlite3.Row],
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
) -> None:
    digest, dominant, avg_mood = _rollup_members_digest(conn, members)
    text = _with_linked_notes(
        f"Period {period_key} entries:\n{digest}", config, conn, [r["id"] for r in members]
    )
    summary = client.chat(
        model=config.ollama.precise_model,
        messages=[
            {"role": "system", "content": _WEEK_MONTH_PROMPT},
            {"role": "user", "content": text},
        ],
    ).strip()
    store.upsert_rollup(
        conn, period_type, period_key, summary, json.dumps(dominant), avg_mood, len(members)
    )


def _write_year(
    year: str,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    month_groups: dict[str, list[sqlite3.Row]],
) -> bool:
    months = conn.execute(
        "SELECT * FROM rollups WHERE period_type = 'month' AND period_key LIKE ? "
        "ORDER BY period_key",
        (f"{year}-%",),
    ).fetchall()
    if not months:
        return False  # a year is written from its month digests
    lines = []
    for m in months:
        mood_str = f"{m['avg_mood']:g}" if m["avg_mood"] is not None else "-"
        lines.append(f"- {m['period_key']} (avg mood {mood_str}): {m['summary']}")
    # The text comes from month digests, but the numbers come from the
    # entries: an average of monthly averages would let a one-entry month
    # count as much as a full one.
    members = [
        row
        for key, rows in sorted(month_groups.items())
        if key.startswith(f"{year}-")
        for row in rows
    ]
    text = _with_linked_notes(
        f"Year {year} months:\n" + "\n".join(lines), config, conn, [r["id"] for r in members]
    )
    summary = client.chat(
        model=config.ollama.precise_model,
        messages=[
            {"role": "system", "content": _YEAR_PROMPT},
            {"role": "user", "content": text},
        ],
    ).strip()
    tags_by_entry = store.tags_for_entries(conn, [r["id"] for r in members])
    dominant, avg_mood = _member_stats(members, tags_by_entry)
    store.upsert_rollup(conn, "year", year, summary, json.dumps(dominant), avg_mood, len(members))
    return True


def _spread(rows: list[sqlite3.Row], limit: int) -> list[sqlite3.Row]:
    """At most `limit` rows, evenly spaced, keeping the first and last."""
    if len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[-1]]
    step = (len(rows) - 1) / (limit - 1)
    return [rows[round(i * step)] for i in range(limit)]


def note_digest_input(
    config: Config, conn: sqlite3.Connection, note: sqlite3.Row
) -> tuple[str, store.Mentions | None, float | None]:
    parsed = store.note_from_row(note, config.vault.path)
    own = note["summary"] or parsed.raw_text or "(the note has no text of its own)"
    lines = [f"Note: {parsed.header()}", f"What the note says: {own[:NOTE_DIGEST_TEXT_CHARS]}"]
    outlinks = context.fit(
        [
            context.note_block(r, config.vault.path, LINKED_NOTE_CHARS)
            for r in store.note_outlinks(conn, note["id"])
        ],
        config.rollups.linked_notes_chars,
    )
    if outlinks:
        lines.append("Notes it links to:\n" + "\n".join(outlinks))
    mentions = store.mention_stats(conn, [note["id"]]).get(note["id"])
    rows = store.entries_mentioning(conn, note["id"], limit=-1)[::-1]  # oldest first
    moods = [r["mood_score"] for r in rows if r["mood_score"] is not None]
    avg_mood = round(sum(moods) / len(moods), 2) if moods else None
    baseline, _ = store.overall_mood(conn, None, None)
    if mentions is not None:
        mood = f"; average mood {avg_mood:g}" if avg_mood is not None else ""
        usual = f" (my usual is {baseline:g})" if baseline is not None and avg_mood is not None else ""
        lines.append(
            f"Linked from {mentions.count} entries, {mentions.first} to {mentions.last}{mood}{usual}."
        )
    by_year: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_year[row["entry_date"][:4]].append(row)
    tags_by_entry = store.tags_for_entries(conn, [r["id"] for r in rows])
    for year, members in sorted(by_year.items()):
        scored = [r["mood_score"] for r in members if r["mood_score"] is not None]
        mood = f", average mood {sum(scored) / len(scored):.1f}" if scored else ""
        shown = _spread(members, config.rollups.note_entries_per_year)
        sampled = f", {len(shown)} shown" if len(shown) < len(members) else ""
        lines.append(f"\n{year} ({len(members)} entries{mood}{sampled}):")
        for row in shown:
            mood_str = f"{row['mood_score']:g}" if row["mood_score"] is not None else "-"
            tags = ", ".join(tags_by_entry.get(row["id"], []))
            lines.append(
                f"- {row['entry_date']} (mood {mood_str}) [{tags}]: {row['summary'] or '(no summary)'}"
            )
    return "\n".join(lines), mentions, avg_mood


def _write_note_digest(
    link_key: str, config: Config, conn: sqlite3.Connection, client: OllamaClient
) -> bool:
    found = store.notes_by_link_key(conn, link_key)
    if found is None:
        return False
    text, mentions, avg_mood = note_digest_input(config, conn, found)
    if mentions is None:
        return False
    summary = client.chat(
        model=config.ollama.precise_model,
        messages=[
            {"role": "system", "content": _NOTE_PROMPT},
            {"role": "user", "content": text},
        ],
    ).strip()
    store.upsert_note_digest(conn, link_key, summary, mentions, avg_mood)
    return True


# Weeks go last. Most answers read months and years (weeks only cover spans
# of a few weeks), and a run can take hours, so if it gets stopped those
# should already be done. Years need their months written first.
ORDER = ("month", "year", "note", "week")
Reason = Literal["missing", "stale", "rebuild"]


@dataclass(frozen=True)
class PlannedDigest:
    kind: str  # week, month, year or note
    key: str  # a period key, or a note's link key
    label: str  # the period key, or the note's title
    reason: Reason


def _reason(row: sqlite3.Row | None, only_stale: bool) -> Reason | None:
    if row is None:
        return "missing"
    if row["stale"]:
        return "stale"
    return None if only_stale else "rebuild"


def _overlaps(kind: str, key: str, since: date | None, until: date | None) -> bool:
    start, end = store.period_bounds(kind, key)
    return (until is None or start <= until) and (since is None or end >= since)


def plan_rollups(
    config: Config,
    conn: sqlite3.Connection,
    period_types: tuple[str, ...] = ORDER,
    only_stale: bool = True,
    since: date | None = None,
    until: date | None = None,
) -> list[PlannedDigest]:
    """What generate_rollups would write, in the order it writes them.

    The date range picks periods that overlap it. Note digests span the whole
    journal, so the range doesn't apply to them.
    """
    groups = _entry_periods(conn)
    planned: list[PlannedDigest] = []
    for kind in ORDER:
        if kind not in period_types:
            continue
        if kind == "note":
            minimum = config.rollups.note_min_mentions
            for note in store.notes_linked_at_least(conn, minimum) if minimum else []:
                reason = _reason(store.get_note_digest(conn, note["link_key"]), only_stale)
                if reason:
                    planned.append(PlannedDigest("note", note["link_key"], note["title"], reason))
            continue
        keys = sorted({k[:4] for k in groups["month"]}) if kind == "year" else sorted(groups[kind])
        for key in keys:
            if not _overlaps(kind, key, since, until):
                continue
            reason = _reason(store.get_rollup(conn, kind, key), only_stale)
            if reason:
                planned.append(PlannedDigest(kind, key, key, reason))
    return planned


def generate_rollups(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    period_types: tuple[str, ...] = ORDER,
    only_stale: bool = True,
    since: date | None = None,
    until: date | None = None,
) -> dict[str, int]:
    counts = dict.fromkeys(ORDER, 0)
    groups = _entry_periods(conn)
    pruned = _prune_orphan_rollups(conn, groups)
    if pruned:
        console.print(f"pruned {pruned} rollups for periods with no entries")
    if "note" in period_types and config.rollups.note_min_mentions:
        keep = {n["link_key"] for n in store.notes_linked_at_least(conn, config.rollups.note_min_mentions)}
        pruned = store.prune_note_digests(conn, keep)
        if pruned:
            console.print(f"pruned {pruned} note digests for notes no longer linked often enough")
    plan = plan_rollups(config, conn, period_types, only_stale, since, until)
    if not plan:
        return counts
    with Progress(
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Writing digests", total=len(plan))
        for item in plan:
            progress.update(task, description=f"Writing the {item.kind} {escape(item.label)} digest")
            if item.kind == "note":
                written = _write_note_digest(item.key, config, conn, client)
            elif item.kind == "year":
                written = _write_year(item.key, config, conn, client, groups["month"])
            else:
                _write_period(item.kind, item.key, groups[item.kind][item.key], config, conn, client)
                written = True
            if written:
                counts[item.kind] += 1
                progress.console.print(f"rollup {item.kind} {escape(item.label)}")
            progress.advance(task)
    return counts
