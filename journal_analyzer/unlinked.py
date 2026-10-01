"""Entries that name a note, by title or alias, without linking to it.

Mentions only count through links, so these are the days a person's or
place's numbers are missing. Matching follows query.notes_named_in: whole
words, ignoring case, and a name inside a longer name of another note
("John" in "John Lee") doesn't count.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from . import store
from .query import MIN_NAME_CHARS

SNIPPET_CHARS = 160


@dataclass(frozen=True)
class UnlinkedMention:
    note_id: int
    entry_id: int
    entry_date: str
    name: str  # the title or alias as it appears in the text
    snippet: str


@dataclass(frozen=True)
class NoteSummary:
    note_id: int
    title: str
    kind: str
    count: int
    latest: str


def _pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(key)}(?!\w)", re.IGNORECASE)


def _snippet(text: str, start: int, end: int) -> str:
    """The sentence around a match, cut down around it if it's long."""
    left = max(text.rfind(c, 0, start) for c in ".!?\n") + 1
    ends = [i for i in (text.find(c, end) for c in ".!?\n") if i != -1]
    right = min(ends) + 1 if ends else len(text)
    if len(text[left:right].strip()) <= SNIPPET_CHARS:
        return text[left:right].strip()
    lo = max(left, (start + end) // 2 - SNIPPET_CHARS // 2)
    hi = min(right, lo + SNIPPET_CHARS)
    lo = max(left, hi - SNIPPET_CHARS)
    return ("…" if lo > left else "") + text[lo:hi].strip() + ("…" if hi < right else "")


def for_note(
    conn: sqlite3.Connection,
    note_id: int,
    names: list[tuple[int, str]] | None = None,
) -> list[UnlinkedMention]:
    """Newest first. `names` is store.note_name_index, passed in when looping."""
    index = names if names is not None else [
        (r["note_id"], r["key"]) for r in store.note_name_index(conn)
    ]
    own = sorted(
        (key for nid, key in index if nid == note_id and len(key) >= MIN_NAME_CHARS),
        key=len,
        reverse=True,
    )
    if not own:
        return []
    longer = [
        key for nid, key in index if nid != note_id and any(k in key and k != key for k in own)
    ]
    candidates: set[int] = set()
    for key in own:
        candidates.update(store.fts_phrase_ids(conn, key))
    candidates -= set(store.entry_ids_mentioning(conn, [note_id]))
    found: list[UnlinkedMention] = []
    for row in store.entries_by_ids(conn, sorted(candidates)):
        text = row["raw_text"]
        covered = [m.span() for key in longer for m in _pattern(key).finditer(text)]
        hit = next(
            (
                m
                for key in own
                for m in _pattern(key).finditer(text)
                if not any(s <= m.start() and m.end() <= e for s, e in covered)
            ),
            None,
        )
        if hit is None:
            continue
        found.append(
            UnlinkedMention(
                note_id=note_id,
                entry_id=row["id"],
                entry_date=row["entry_date"],
                name=hit.group(0),
                snippet=_snippet(text, *hit.span()),
            )
        )
    found.sort(key=lambda m: (m.entry_date, m.entry_id), reverse=True)
    return found


def by_note(conn: sqlite3.Connection, kind: str | None = None) -> list[NoteSummary]:
    """Notes with unlinked mentions, the most first."""
    names = [(r["note_id"], r["key"]) for r in store.note_name_index(conn)]
    out: list[NoteSummary] = []
    for note in store.all_notes(conn):
        if kind and note["kind"] != kind.casefold():
            continue
        mentions = for_note(conn, note["id"], names)
        if mentions:
            out.append(
                NoteSummary(
                    note["id"], note["title"], note["kind"], len(mentions), mentions[0].entry_date
                )
            )
    out.sort(key=lambda s: (-s.count, s.title.casefold()))
    return out
