"""Mood and tags as stored: the model's reading, adjusted by frontmatter and [tags].

The model's own reading is kept in entry_readings, so when these settings
change, `refresh` recomputes every entry without calling a model.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from . import db, parser, store
from .config import Config
from .models import Extraction, FrontmatterValue

_SETTINGS_META_KEY = "reading_settings"
# "7", "7.5", "7,5" or "7/10"
_SCORE_RE = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*(?:/\s*(\d+(?:[.,]\d+)?))?\s*$")


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


def final_tags(
    reading_tags: list[str], fields: dict[str, FrontmatterValue], config: Config
) -> list[str]:
    candidates = list(reading_tags)
    if config.tags.from_frontmatter:
        candidates += parser.frontmatter_tags(fields)
    ignored = set(config.tags.ignore)
    seen: dict[str, None] = {}
    for raw in candidates:
        tag = raw.strip().lstrip("#").lower()
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
        },
        sort_keys=True,
    )


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
    for row in store.stored_readings(conn):
        final = final_reading(row.reading, row.frontmatter, config)
        if final.mood_score == row.mood_score and sorted(final.tags) == sorted(row.tags):
            continue
        store.update_entry_mood_and_tags(conn, row.entry_id, final.mood_score, final.tags)
        store.mark_rollups_stale(conn, row.entry_date)
        changed += 1
    if changed:
        store.prune_unused_tags(conn)
    db.set_meta(conn, _SETTINGS_META_KEY, fingerprint)
    return changed
