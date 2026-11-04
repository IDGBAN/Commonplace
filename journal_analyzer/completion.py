"""Tab completion from the cache: note names, tags, metrics, periods and days.

Each completer opens the cache read-only and gives up quietly when it can't,
since pressing Tab shouldn't print errors. `journal --install-completion`
sets it up for the shell you're in.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date

import typer

from . import db, store
from .config import load_config
from .models import JournalError

# more than a shell can usefully show
MAX_CHOICES = 50

Choices = list[tuple[str, str]]


@contextmanager
def _cache(ctx: typer.Context) -> Iterator[sqlite3.Connection | None]:
    try:
        config = load_config(ctx.params.get("config") or None)
        conn = db.connect_readonly(config)
    except (JournalError, OSError, sqlite3.Error, ValueError):
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()


def _matching(choices: Iterable[tuple[str, str]], incomplete: str) -> Choices:
    """Choices that start with what's typed, ignoring case.

    Typer keeps only completions that start with exactly what was typed, so
    the typed part stays as it was: "ja" completes to "jane Doe", which finds
    Jane Doe since names are looked up ignoring case.
    """
    typed = incomplete.casefold()
    found: dict[str, str] = {}
    for value, help_text in choices:
        if value.casefold().startswith(typed):
            found.setdefault(incomplete + value[len(incomplete):], help_text)
    return list(found.items())[:MAX_CHOICES]


def notes(ctx: typer.Context, incomplete: str) -> Choices:
    """Titles, aliases and the names learned from link text."""
    with _cache(ctx) as conn:
        if conn is None:
            return []
        titles = {r["id"]: (r["title"], r["kind"]) for r in store.all_notes(conn)}
        names = store.note_name_index(conn)
    choices = [(title, kind) for title, kind in titles.values()]
    for row in names:
        title, kind = titles.get(row["note_id"], ("", ""))
        if row["name"] != title:
            choices.append((row["name"], f"{title} ({kind})"))
    return _matching(choices, incomplete)


def tags(ctx: typer.Context, incomplete: str) -> Choices:
    with _cache(ctx) as conn:
        rows = store.tag_stats(conn) if conn is not None else []
    return _matching(((r["name"], f"{r['n']} entries") for r in rows), incomplete.lstrip("#"))


def metrics(ctx: typer.Context, incomplete: str) -> Choices:
    with _cache(ctx) as conn:
        rows = store.metric_keys(conn) if conn is not None else []
    return _matching(((r["key"], f"{r['n']} entries") for r in rows), incomplete)


def _dates(ctx: typer.Context) -> list[date]:
    with _cache(ctx) as conn:
        if conn is None:
            return []
        return [date.fromisoformat(r["entry_date"]) for r in store.entry_days(conn)]


def days(ctx: typer.Context, incomplete: str) -> Choices:
    found = sorted({d.isoformat() for d in _dates(ctx)}, reverse=True)
    return _matching(((d, "") for d in found), incomplete)


def periods(ctx: typer.Context, incomplete: str) -> Choices:
    found = _dates(ctx)
    years = sorted({store.year_key(d) for d in found}, reverse=True)
    months = sorted({store.month_key(d) for d in found}, reverse=True)
    choices = [(y, "year") for y in years] + [(m, "month") for m in months]
    if "w" in incomplete.casefold():
        weeks = sorted({store.week_key(d) for d in found}, reverse=True)
        choices += [(w, "week") for w in weeks]
    return _matching(choices, incomplete)


def kinds(ctx: typer.Context, incomplete: str) -> Choices:
    with _cache(ctx) as conn:
        rows = store.notes_by_kind(conn) if conn is not None else []
    return _matching(((r["kind"], f"{r['n']} notes") for r in rows), incomplete)


def properties(ctx: typer.Context, incomplete: str) -> Choices:
    with _cache(ctx) as conn:
        rows = store.property_keys(conn) if conn is not None else []
    return _matching(((r["name"], f"{r['notes']} notes") for r in rows), incomplete)


def groups(ctx: typer.Context, incomplete: str) -> Choices:
    """property=value pairs; the values once a property and = are typed."""
    key, sep, _ = incomplete.partition("=")
    with _cache(ctx) as conn:
        if conn is None:
            return []
        if not sep:
            rows = store.property_keys(conn)
            return _matching(((f"{r['name']}=", f"{r['values']} values") for r in rows), incomplete)
        rows = store.property_groups(conn, key)
    return _matching(((f"{key}={r['value']}", f"{r['notes']} notes") for r in rows), incomplete)


def broken_links(ctx: typer.Context, incomplete: str) -> Choices:
    with _cache(ctx) as conn:
        rows = store.unresolved_links(conn, MAX_CHOICES * 4) if conn is not None else []
    return _matching(((r["target"], f"{r['n']} entries") for r in rows), incomplete)
