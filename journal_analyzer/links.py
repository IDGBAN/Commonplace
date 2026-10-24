"""Links that count for nothing, and why.

Mentions only come from links, so a link that finds no note is missing from
that person's or place's numbers. Each one gets the likeliest reason, checked
in this order: a Markdown file by that name that isn't read as a note, a note
that failed to index or is private, an alias two notes share, a date with no
entry, a near miss of a name that exists, and last, no note at all.

`journal browse` never walks the vault, so it leaves out the first check.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from . import ingester, parser, store
from .config import Config

# how alike (0..1) a broken link and a note's name must be to suggest the note
CLOSE_MATCH_CUTOFF = 0.85
# shorter names make too many near misses
MIN_TYPO_CHARS = 4

Reason = Literal[
    "outside", "hidden", "ignored", "errored", "private", "shared_alias",
    "no_entry", "not_a_date", "typo", "missing",
]

_ISO_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class BrokenLink:
    target: str  # the link key, casefolded
    entries: int  # entries that write it
    notes: int  # notes that write it
    last: str | None  # the latest entry that does
    reason: Reason
    why: str
    fix: str
    path: str | None = None  # the vault file it names, when there is one
    like: str | None = None  # the note it looks like a typo of
    like_id: int | None = None


@dataclass
class _Uses:
    entries: int = 0
    notes: int = 0
    last: str | None = None


def vault_markdown(config: Config) -> dict[str, list[Path]]:
    """Every Markdown file in the vault by link key, hidden folders too. Names only."""
    found: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(config.vault.path.rglob("*.md")):
        if path.is_file():
            found[parser.file_link_key(path)].append(path)
    return dict(found)


def _file_reason(path: Path, config: Config) -> tuple[Reason, str, str]:
    rel = parser.relative_path(path, config.vault.path)
    parts = Path(rel).parts
    hidden = next((p for p in parts[:-1] if p.startswith(".")), None)
    if hidden is not None:
        return (
            "hidden",
            f"The only file by that name is {rel}, in {hidden}, which Obsidian leaves out of the vault.",
            f"Move it out of {hidden} if you still use it.",
        )
    if ingester.is_ignored(path, config):
        return (
            "ignored",
            f"{rel} is left out by ignore_patterns.",
            "Take it out of ignore_patterns in config.toml if it should count.",
        )
    return (
        "outside",
        f"{rel} isn't in your note folders, so it isn't read as a note.",
        f'Add "{rel}" to [notes] paths, or set other_files = true.',
    )


def _day_reason(target: str) -> tuple[Reason, str, str] | None:
    if not _ISO_DAY_RE.fullmatch(target):
        return None
    try:
        date.fromisoformat(target)
    except ValueError:
        return "not_a_date", f"{target} isn't a real date.", "Fix the date in the link."
    return "no_entry", f"There's no entry dated {target}.", "Write that day's entry, or fix the date."


def diagnose(
    config: Config,
    conn: sqlite3.Connection,
    files: dict[str, list[Path]] | None = None,
) -> list[BrokenLink]:
    """Every link that finds nothing, most written first.

    `files` is vault_markdown; without it, files outside the note folders
    can't be told apart from notes that don't exist.
    """
    by_target: dict[str, _Uses] = {}
    for r in store.unresolved_links(conn, -1):
        by_target[r["target"]] = _Uses(entries=r["n"], last=r["last"])
    for r in store.unresolved_note_links(conn):
        by_target.setdefault(r["target"], _Uses()).notes = r["n"]

    tracked = {
        parser.file_link_key(r["path"]): r
        for r in store.files_with_status(conn, ["error", "private"])
    }
    shared = store.shared_aliases(conn)
    names = store.note_names_by_key(conn)
    out = []
    for target, uses in by_target.items():
        reason: Reason
        path = like = None
        like_id = None
        if target in tracked:
            row = tracked[target]
            path = parser.relative_path(row["path"], config.vault.path)
            if row["status"] == "private":
                reason, why, fix = (
                    "private", f"{path} is marked private, so links to it never count.",
                    "Nothing to fix, unless it shouldn't be private.",
                )
            else:
                reason, why, fix = (
                    "errored", f"{path} couldn't be indexed: {row['error_message']}",
                    "Fix the file and run journal index.",
                )
        elif files is not None and target in files:
            file = files[target][0]
            path = parser.relative_path(file, config.vault.path)
            reason, why, fix = _file_reason(file, config)
        elif target in shared:
            titles = shared[target]
            reason, why, fix = (
                "shared_alias",
                f"It's an alias of {_and(titles)}, so it can't tell which one you mean.",
                "Link the note you mean by its name, or give the alias to one note only.",
            )
        elif (day := _day_reason(target)) is not None:
            reason, why, fix = day
        else:
            close = (
                difflib.get_close_matches(target, list(names), n=1, cutoff=CLOSE_MATCH_CUTOFF)
                if len(target) >= MIN_TYPO_CHARS else []
            )
            if close:
                note = names[close[0]]
                like, like_id = note["title"], note["id"]
                reason, why, fix = (
                    "typo",
                    f"No note is called that, but {note['name']} is close.",
                    f"Did you mean [[{note['title']}]]?",
                )
            else:
                reason, why, fix = (
                    "missing",
                    "No note is called that yet.",
                    "Create the note and the links count from the next index.",
                )
        out.append(
            BrokenLink(
                target=target,
                entries=uses.entries,
                notes=uses.notes,
                last=uses.last,
                reason=reason,
                why=why,
                fix=fix,
                path=path,
                like=like,
                like_id=like_id,
            )
        )
    out.sort(key=lambda b: (-b.entries, -b.notes, b.target))
    return out


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
