"""Week and month digests are built from entry summaries, years from month digests."""

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


def _rollup_members_digest(
    conn: sqlite3.Connection, members: list[sqlite3.Row]
) -> tuple[str, list[str], float | None]:
    lines: list[str] = []
    tag_counter: Counter[str] = Counter()
    moods: list[float] = []
    ids = [r["id"] for r in members]
    tags_by_entry = store.tags_for_entries(conn, ids)
    notes_by_entry = store.entities_for_entries(conn, ids)
    for row in members:
        tags = tags_by_entry.get(row["id"], [])
        tag_counter.update(tags)
        mood = row["mood_score"]
        if mood is not None:
            moods.append(mood)
        mood_str = f"{mood:g}" if mood is not None else "-"
        names = [n["name"] for n in notes_by_entry.get(row["id"], [])]
        links = f" (links: {', '.join(names)})" if names else ""
        lines.append(
            f"- {row['entry_date']} (mood {mood_str}) "
            f"[{', '.join(tags)}]{links}: {row['summary'] or '(no summary)'}"
        )
    dominant = [t for t, _ in tag_counter.most_common(5)]
    avg_mood = round(sum(moods) / len(moods), 2) if moods else None
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
    config: Config, conn: sqlite3.Connection, client: OllamaClient, only_stale: bool
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
        tag_counter: Counter[str] = Counter()
        moods: list[float] = []
        total_entries = 0
        for m in months:
            tag_counter.update(json.loads(m["dominant_tags"] or "[]"))
            if m["avg_mood"] is not None:
                moods.append(m["avg_mood"])
            total_entries += m["entry_count"] or 0
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
        avg_mood = round(sum(moods) / len(moods), 2) if moods else None
        dominant = [t for t, _ in tag_counter.most_common(5)]
        store.upsert_rollup(
            conn, "year", year, summary, json.dumps(dominant), avg_mood, total_entries
        )
        generated += 1
        console.print(f"rollup year {year}")
    return generated


def generate_rollups(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    period_types: tuple[str, ...] = ("week", "month", "year"),
    only_stale: bool = True,
) -> dict[str, int]:
    # Weeks go last. Answers only read months and years, and a run can take
    # hours, so if it gets stopped those should already be done.
    counts = {"week": 0, "month": 0, "year": 0}
    groups = _entry_periods(conn)
    pruned = _prune_orphan_rollups(conn, groups)
    if pruned:
        console.print(f"pruned {pruned} rollups for periods with no entries")
    if "month" in period_types:
        counts["month"] = _generate_periods(
            "month", groups["month"], config, conn, client, only_stale
        )
    if "year" in period_types:
        counts["year"] = _generate_years(config, conn, client, only_stale)
    if "week" in period_types:
        counts["week"] = _generate_periods(
            "week", groups["week"], config, conn, client, only_stale
        )
    return counts
