"""Week and month digests are built from entry summaries, years from month digests.

Notes linked from many entries also get a relationship digest, built from the
note itself and the summaries of the entries that link to it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import date

from rich.console import Console
from rich.status import Status

from . import store
from .config import Config
from .ollama_client import OllamaClient

console = Console()

DOMINANT_TAG_COUNT = 5
# the note's own text that goes in with the entries; its summary when it has one
NOTE_DIGEST_TEXT_CHARS = 1500

_VOICE = (
    "Write it in the journal writer's own first-person voice (I, me, my), as "
    "if they were looking back on it themselves, and open with what happened "
    'rather than a frame: never "This period", "This week", "The author" or '
    '"The user".'
)

_WEEK_MONTH_PROMPT = (
    "You summarize a period of someone's journal from per-entry one-line "
    "summaries, mood scores, tags, and the notes each entry links to (the "
    "people, places and topics involved). Write a cohesive 2-4 sentence "
    "digest of the period: main themes, notable events, and the overall "
    f"emotional arc. {_VOICE} Do not invent events not present in the input."
)

_YEAR_PROMPT = (
    "You summarize a year of someone's journal from its monthly digests. "
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


def _writing(period_type: str, period_key: str) -> Status:
    return console.status(
        f"[dim]Writing the {period_type} {period_key} digest...[/dim]", spinner="line"
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


def _needs_generation(
    conn: sqlite3.Connection, period_type: str, period_key: str, only_stale: bool
) -> bool:
    row = conn.execute(
        "SELECT stale FROM rollups WHERE period_type = ? AND period_key = ?",
        (period_type, period_key),
    ).fetchone()
    if row is None:
        return True
    return (not only_stale) or bool(row["stale"])


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


def _rollup_members_digest(
    conn: sqlite3.Connection, members: list[sqlite3.Row]
) -> tuple[str, list[str], float | None]:
    lines: list[str] = []
    ids = [r["id"] for r in members]
    tags_by_entry = store.tags_for_entries(conn, ids)
    notes_by_entry = store.entities_for_entries(conn, ids)
    for row in members:
        tags = tags_by_entry.get(row["id"], [])
        mood = row["mood_score"]
        mood_str = f"{mood:g}" if mood is not None else "-"
        names = [n["name"] for n in notes_by_entry.get(row["id"], [])]
        links = f" (links: {', '.join(names)})" if names else ""
        lines.append(
            f"- {row['entry_date']} (mood {mood_str}) "
            f"[{', '.join(tags)}]{links}: {row['summary'] or '(no summary)'}"
        )
    dominant, avg_mood = _member_stats(members, tags_by_entry)
    return "\n".join(lines), dominant, avg_mood


def _generate_periods(
    period_type: str,
    groups: dict[str, list[sqlite3.Row]],
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    only_stale: bool,
) -> int:
    generated = 0
    for period_key, members in sorted(groups.items()):
        if not _needs_generation(conn, period_type, period_key, only_stale):
            continue
        digest, dominant, avg_mood = _rollup_members_digest(conn, members)
        with _writing(period_type, period_key):
            summary = client.chat(
                model=config.ollama.precise_model,
                messages=[
                    {"role": "system", "content": _WEEK_MONTH_PROMPT},
                    {"role": "user", "content": f"Period {period_key} entries:\n{digest}"},
                ],
            ).strip()
        store.upsert_rollup(
            conn, period_type, period_key, summary,
            json.dumps(dominant), avg_mood, len(members),
        )
        generated += 1
        console.print(f"rollup {period_type} {period_key}")
    return generated


def _generate_years(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    only_stale: bool,
    month_groups: dict[str, list[sqlite3.Row]],
) -> int:
    month_rollups = conn.execute(
        "SELECT * FROM rollups WHERE period_type = 'month' ORDER BY period_key"
    ).fetchall()
    by_year: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in month_rollups:
        by_year[row["period_key"][:4]].append(row)
    generated = 0
    for year, months in sorted(by_year.items()):
        if not _needs_generation(conn, "year", year, only_stale):
            continue
        lines = []
        for m in months:
            mood_str = f"{m['avg_mood']:g}" if m["avg_mood"] is not None else "-"
            lines.append(f"- {m['period_key']} (avg mood {mood_str}): {m['summary']}")
        with _writing("year", year):
            summary = client.chat(
                model=config.ollama.precise_model,
                messages=[
                    {"role": "system", "content": _YEAR_PROMPT},
                    {"role": "user", "content": f"Year {year} months:\n" + "\n".join(lines)},
                ],
            ).strip()
        # The text comes from month digests, but the numbers come from the
        # entries: an average of monthly averages would let a one-entry month
        # count as much as a full one.
        members = [
            row
            for key, rows in sorted(month_groups.items())
            if key.startswith(f"{year}-")
            for row in rows
        ]
        tags_by_entry = store.tags_for_entries(conn, [r["id"] for r in members])
        dominant, avg_mood = _member_stats(members, tags_by_entry)
        store.upsert_rollup(
            conn, "year", year, summary, json.dumps(dominant), avg_mood, len(members)
        )
        generated += 1
        console.print(f"rollup year {year}")
    return generated


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


def _generate_note_digests(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    only_stale: bool,
) -> int:
    minimum = config.rollups.note_min_mentions
    if minimum == 0:
        return 0
    notes = store.notes_linked_at_least(conn, minimum)
    pruned = store.prune_note_digests(conn, {n["link_key"] for n in notes})
    if pruned:
        console.print(f"pruned {pruned} note digests for notes no longer linked often enough")
    generated = 0
    for note in notes:
        existing = store.get_note_digest(conn, note["link_key"])
        if existing is not None and only_stale and not existing["stale"]:
            continue
        text, mentions, avg_mood = note_digest_input(config, conn, note)
        if mentions is None:
            continue
        with _writing("note", note["title"]):
            summary = client.chat(
                model=config.ollama.precise_model,
                messages=[
                    {"role": "system", "content": _NOTE_PROMPT},
                    {"role": "user", "content": text},
                ],
            ).strip()
        store.upsert_note_digest(conn, note["link_key"], summary, mentions, avg_mood)
        generated += 1
        console.print(f"rollup note {note['title']}")
    return generated


def generate_rollups(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    period_types: tuple[str, ...] = ("week", "month", "year", "note"),
    only_stale: bool = True,
) -> dict[str, int]:
    # Weeks go last. Most answers read months and years (weeks only cover
    # spans of a few weeks), and a run can take hours, so if it gets stopped
    # those should already be done.
    counts = {"week": 0, "month": 0, "year": 0, "note": 0}
    groups = _entry_periods(conn)
    pruned = _prune_orphan_rollups(conn, groups)
    if pruned:
        console.print(f"pruned {pruned} rollups for periods with no entries")
    if "month" in period_types:
        counts["month"] = _generate_periods(
            "month", groups["month"], config, conn, client, only_stale
        )
    if "year" in period_types:
        counts["year"] = _generate_years(config, conn, client, only_stale, groups["month"])
    if "note" in period_types:
        counts["note"] = _generate_note_digests(config, conn, client, only_stale)
    if "week" in period_types:
        counts["week"] = _generate_periods(
            "week", groups["week"], config, conn, client, only_stale
        )
    return counts
