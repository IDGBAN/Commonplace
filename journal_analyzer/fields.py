"""What's stored for each entry beyond its text: mood, tags and metrics.

Mood and tags are the model's reading adjusted by frontmatter and [tags];
metrics are numbers from the frontmatter. The model's own reading is kept in
entry_readings and the frontmatter in file_frontmatter, so when these
settings change, `refresh` recomputes every entry without calling a model.
"""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
from pathlib import Path

from . import db, parser, store
from .config import Config
from .models import Extraction, FrontmatterValue

_SETTINGS_META_KEY = "reading_settings"
# how alike two tag names must be (0..1) for `journal tags --similar` to pair them
SIMILAR_TAG_RATIO = 0.85
# "7", "7.5", "7,5" or "7/10"
_SCORE_RE = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*(?:/\s*(\d+(?:[.,]\d+)?))?\s*$")
# "7", "7.5h", "30 min", "8k"
_AMOUNT_RE = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*([a-z%]*)\s*$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):([0-5]\d)\s*$")
_TRUE = frozenset({"true", "yes", "y", "done"})
_FALSE = frozenset({"false", "no", "n"})
# Obsidian's own keys, and ones that are never a measure
_NOT_METRICS = frozenset(
    {"aliases", "alias", "tags", "tag", "cssclasses", "cssclass", "publish", "position", "date",
     parser.PRIVATE_KEY}
)


def _first(value: FrontmatterValue | None) -> str | None:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _number(text: str) -> float:
    return float(text.replace(",", "."))


def frontmatter_mood(fields: dict[str, FrontmatterValue], config: Config) -> float | None:
    """Your own score from the frontmatter, on the 1..mood_scale_max scale."""
    vault = config.vault
    if not vault.frontmatter_mood_key:
        return None
    raw = _first(parser.frontmatter_value(fields, vault.frontmatter_mood_key))
    m = _SCORE_RE.match(raw) if raw else None
    if m is None:
        return None
    value = _number(m.group(1))
    lo = vault.frontmatter_mood_min
    hi = _number(m.group(2)) if m.group(2) else vault.frontmatter_mood_max
    if hi <= lo:
        return None
    clamped = min(max(value, lo), hi)
    top = config.indexing.mood_scale_max
    return round(1 + (clamped - lo) * (top - 1) / (hi - lo), 2)


def metric_value(raw: FrontmatterValue | None) -> float | None:
    """A number from a frontmatter value, or None if it isn't one."""
    text = _first(raw)
    if text is None:
        return None
    word = text.strip().casefold()
    if word in _TRUE:
        return 1.0
    if word in _FALSE:
        return 0.0
    clock = _CLOCK_RE.match(text)
    if clock:
        return round(int(clock.group(1)) + int(clock.group(2)) / 60, 2)
    amount = _AMOUNT_RE.match(text)
    if amount is None:
        return None
    value = _number(amount.group(1))
    return value * 1000 if amount.group(2).casefold() == "k" else value


def entry_metrics(fields: dict[str, FrontmatterValue], config: Config) -> dict[str, float]:
    skip = set(_NOT_METRICS) | set(config.metrics.ignore)
    skip.add(config.vault.frontmatter_date_key.casefold())
    if config.vault.frontmatter_mood_key:
        skip.add(config.vault.frontmatter_mood_key.casefold())
    wanted = set(config.metrics.keys)
    found: dict[str, float] = {}
    for name, raw in fields.items():
        key = name.strip().casefold()
        if key in skip or (wanted and key not in wanted):
            continue
        value = metric_value(raw)
        if value is not None:
            found[key] = value
    return found


def final_tags(
    reading_tags: list[str], fields: dict[str, FrontmatterValue], config: Config
) -> list[str]:
    candidates = list(reading_tags)
    if config.tags.from_frontmatter:
        candidates += parser.frontmatter_tags(fields)
    ignored = set(config.tags.ignore)
    merge = config.tags.merge
    seen: dict[str, None] = {}
    for raw in candidates:
        tag = raw.strip().lstrip("#").lower()
        tag = merge.get(tag, tag)
        if tag and tag not in ignored:
            seen.setdefault(tag, None)
    return list(seen)


def final_reading(
    reading: Extraction, fields: dict[str, FrontmatterValue], config: Config
) -> Extraction:
    own = frontmatter_mood(fields, config)
    return reading.model_copy(
        update={
            "mood_score": own if own is not None else reading.mood_score,
            "tags": final_tags(reading.tags, fields, config),
        }
    )


def settings_fingerprint(config: Config) -> str:
    return json.dumps(
        {
            "mood_key": config.vault.frontmatter_mood_key.casefold(),
            "mood_min": config.vault.frontmatter_mood_min,
            "mood_max": config.vault.frontmatter_mood_max,
            "scale": config.indexing.mood_scale_max,
            "frontmatter_tags": config.tags.from_frontmatter,
            "ignore": sorted(config.tags.ignore),
            "merge": sorted(config.tags.merge.items()),
            "date_key": config.vault.frontmatter_date_key.casefold(),
            "metric_keys": sorted(config.metrics.keys),
            "metric_ignore": sorted(config.metrics.ignore),
        },
        sort_keys=True,
    )


def _plural_pair(a: str, b: str) -> bool:
    return b in (a + "s", a + "es") or (a.endswith("y") and b == a[:-1] + "ies")


def _close(a: str, b: str) -> bool:
    # the quick ratios are upper bounds on ratio(), and rule out most pairs cheaply
    m = difflib.SequenceMatcher(None, a, b)
    return (
        m.real_quick_ratio() >= SIMILAR_TAG_RATIO
        and m.quick_ratio() >= SIMILAR_TAG_RATIO
        and m.ratio() >= SIMILAR_TAG_RATIO
    )


def similar_tags(rows: list[sqlite3.Row]) -> list[tuple[str, int, str, int]]:
    """Pairs of tags that look like the same thing: (rarer, count, more used, count)."""
    counts = {r["name"]: r["n"] for r in rows}
    names = sorted(counts)
    pairs: list[tuple[str, int, str, int]] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            short, long = sorted((a, b), key=len)
            related = (
                _plural_pair(short, long)
                or (len(short) >= 4 and long.startswith(short))
                or (min(len(a), len(b)) >= 5 and _close(a, b))
            )
            if related:
                rare, common = sorted((a, b), key=lambda t: (counts[t], t == short))
                pairs.append((rare, counts[rare], common, counts[common]))
    return sorted(pairs, key=lambda p: (-p[3], p[0]))


def _load_missing_frontmatter(conn: sqlite3.Connection) -> int:
    # Files indexed before frontmatter was kept. One that changed since gets
    # re-indexed anyway, so it's left for that.
    loaded = 0
    for row in store.entry_files_without_frontmatter(conn):
        try:
            data = parser.read_bytes(Path(row["path"]))
        except OSError:
            continue
        if parser.compute_hash(data) != row["content_hash"]:
            continue
        store.save_file_frontmatter(conn, row["path"], parser.read_frontmatter(data))
        loaded += 1
    return loaded


def refresh(config: Config, conn: sqlite3.Connection) -> int:
    """Re-apply frontmatter and [tags] settings. Returns how many entries changed."""
    added = store.backfill_entry_readings(conn)
    loaded = _load_missing_frontmatter(conn)
    fingerprint = settings_fingerprint(config)
    if not added and not loaded and db.get_meta(conn, _SETTINGS_META_KEY) == fingerprint:
        return 0
    changed = 0
    rows = store.stored_readings(conn)
    current_metrics = store.metrics_for_entries(conn, [r.entry_id for r in rows])
    for row in rows:
        final = final_reading(row.reading, row.frontmatter, config)
        metrics = entry_metrics(row.frontmatter, config)
        same_reading = final.mood_score == row.mood_score and sorted(final.tags) == sorted(row.tags)
        if same_reading and metrics == current_metrics.get(row.entry_id, {}):
            continue
        if not same_reading:
            store.update_entry_mood_and_tags(conn, row.entry_id, final.mood_score, final.tags)
        store.replace_entry_metrics(conn, row.entry_id, metrics)
        store.mark_rollups_stale(conn, row.entry_date)
        store.mark_note_digests_stale(conn, store.link_targets_for_entries(conn, [row.entry_id]))
        changed += 1
    if changed:
        store.prune_unused_tags(conn)
    db.set_meta(conn, _SETTINGS_META_KEY, fingerprint)
    return changed
