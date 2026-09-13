"""`journal insights`: stats come from SQL, the model only writes them up."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from . import store
from .config import Config
from .ollama_client import SYNTHESIS_TEMPERATURE, OllamaClient

# any more and the model just lists them back
MAX_TAG_FINDINGS = 6
MAX_ENTITY_FINDINGS = 5
MAX_COOCCURRENCE_FINDINGS = 3

FindingKind = Literal[
    "tag_mood", "entity_mood", "weekday", "season", "cooccurrence"
]

_WEEKDAYS = [
    "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
]
_MONTHS = [
    "", "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
]

_NARRATE_SYSTEM = (
    "You are a reflective journaling analyst. Below are statistical patterns "
    "computed from the reader's own journal: correlations between their mood "
    "and their tags, people, the day of week, the season, and themes that "
    "recur together. Write a warm, concrete 3-6 sentence reflection, spoken "
    'to them directly as "you", that ties the strongest signals together '
    'into observations they might not have noticed. Never call them "the '
    'user" or "the author", and do not open with a frame like "The data '
    'shows". Use ONLY the statistics given; do not invent events, causes, or '
    "numbers, and note that these are correlations, not proof of cause. If the "
    "signals are weak or few, say so plainly rather than overreaching."
)


@dataclass
class Finding:
    kind: FindingKind
    text: str
    magnitude: float  # for sorting


@dataclass
class Insights:
    overall_avg: float | None
    overall_count: int
    date_from: date | None
    date_to: date | None
    findings: list[Finding] = field(default_factory=list)

    def evidence_lines(self) -> list[str]:
        return [f.text for f in self.findings]


def _mood_findings(
    kind: FindingKind,
    rows: list[sqlite3.Row],
    baseline: float,
    threshold: float,
    label: str,
    top_n: int,
) -> list[Finding]:
    scored: list[tuple[float, Finding]] = []
    for r in rows:
        if r["avg_mood"] is None:
            continue
        dev = r["avg_mood"] - baseline
        if abs(dev) < threshold:
            continue
        direction = "higher" if dev > 0 else "lower"
        name = r["name"]
        if kind == "entity_mood":
            name = f"{name} ({r['type']})"
        text = (
            f"{label} '{name}': average mood {r['avg_mood']:g} "
            f"({direction} than the {baseline:g} baseline) across {r['n']} entries"
        )
        scored.append((abs(dev), Finding(kind, text, abs(dev))))
    scored.sort(key=lambda t: -t[0])
    return [f for _, f in scored[:top_n]]


def _rhythm_finding(
    kind: FindingKind,
    rows: list[sqlite3.Row],
    key: str,
    names: list[str],
    baseline: float,
    threshold: float,
    label: str,
    min_count: int,
) -> Finding | None:
    # one cheerful Tuesday isn't a weekly rhythm
    usable = [
        r for r in rows if r["avg_mood"] is not None and r["n"] >= min_count
    ]
    if len(usable) < 2:
        return None
    hi = max(usable, key=lambda r: r["avg_mood"])
    lo = min(usable, key=lambda r: r["avg_mood"])
    spread = hi["avg_mood"] - lo["avg_mood"]
    if spread < threshold:
        return None
    text = (
        f"{label}: {names[hi[key]]} is the brightest (avg mood {hi['avg_mood']:g}), "
        f"{names[lo[key]]} the lowest (avg mood {lo['avg_mood']:g}); "
        f"baseline {baseline:g}"
    )
    return Finding(kind, text, spread)


def compute_insights(
    config: Config,
    conn: sqlite3.Connection,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Insights:
    min_count = config.insights.min_sample_size
    threshold = config.insights.mood_deviation_threshold
    avg, count = store.overall_mood(conn, date_from, date_to)
    result = Insights(
        overall_avg=avg,
        overall_count=count,
        date_from=date_from,
        date_to=date_to,
    )
    if avg is None:
        return result

    result.findings.extend(
        _mood_findings(
            "tag_mood",
            store.tag_mood_stats(conn, date_from, date_to, min_count),
            avg, threshold, "Days tagged", top_n=MAX_TAG_FINDINGS,
        )
    )
    result.findings.extend(
        _mood_findings(
            "entity_mood",
            store.entity_mood_stats(conn, date_from, date_to, min_count),
            avg, threshold, "Days mentioning", top_n=MAX_ENTITY_FINDINGS,
        )
    )
    rhythms = [
        _rhythm_finding(
            "weekday",
            store.weekday_mood(conn, date_from, date_to),
            "dow", _WEEKDAYS, avg, threshold, "Weekly rhythm", min_count,
        ),
        _rhythm_finding(
            "season",
            store.month_of_year_mood(conn, date_from, date_to),
            "moy", _MONTHS, avg, threshold, "Seasonal rhythm", min_count,
        ),
    ]
    result.findings.extend(f for f in rhythms if f is not None)

    cooccurring = store.tag_cooccurrence(conn, date_from, date_to, min_count)
    for r in cooccurring[:MAX_COOCCURRENCE_FINDINGS]:
        result.findings.append(
            Finding(
                "cooccurrence",
                f"Themes '{r['tag_a']}' and '{r['tag_b']}' recur together "
                f"in {r['n']} entries",
                float(r["n"]),
            )
        )
    return result


def narrate(insights: Insights, config: Config, client: OllamaClient) -> str:
    span = "the whole journal"
    if insights.date_from or insights.date_to:
        span = f"{insights.date_from or 'start'} to {insights.date_to or 'now'}"
    facts = [
        f"Range: {span}.",
        f"Baseline mood: {insights.overall_avg:g} over "
        f"{insights.overall_count} mood-scored entries "
        f"(scale 1..{config.indexing.mood_scale_max}).",
        "Patterns:",
    ]
    facts.extend(f"- {line}" for line in insights.evidence_lines())
    return client.chat(
        model=config.ollama.precise_model,
        messages=[
            {"role": "system", "content": _NARRATE_SYSTEM},
            {"role": "user", "content": "\n".join(facts)},
        ],
        options={"temperature": SYNTHESIS_TEMPERATURE},
    ).strip()


def generate_insights(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    date_from: date | None = None,
    date_to: date | None = None,
) -> tuple[str, Insights]:
    insights = compute_insights(config, conn, date_from, date_to)
    if insights.overall_avg is None:
        return (
            "No mood-scored entries in this range yet, so there is nothing "
            "to correlate. Index more of your journal and try again.",
            insights,
        )
    if not insights.findings:
        return (
            "No patterns cleared the significance bar for this range. Mood looks "
            "fairly even across tags, people and the calendar "
            f"(baseline {insights.overall_avg:g}). Try a wider date range or a "
            "lower insights.mood_deviation_threshold.",
            insights,
        )
    return narrate(insights, config, client), insights
