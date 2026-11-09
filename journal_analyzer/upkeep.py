"""What the browser's Upkeep page shows and runs: digests, the profile, themes.

`state` works out what's out of date from the cache without calling a model.
The runners do the work the matching commands do, one step at a time, saying
how far they are through a work.Step, which also lets questions go first.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from rich.console import Console

from . import db, ingester, profile, rollups, store, themes, work
from .config import Config
from .models import JournalError
from .ollama_client import OllamaClient

Json = dict[str, Any]

# your profile has a row of its own, so "digests" leaves it out
DIGEST_KINDS = tuple(kind for kind in rollups.ORDER if kind != "profile")
_KIND_WORDS = {
    "month": ("month", "months"), "year": ("year", "years"), "week": ("week", "weeks"),
    "note": ("relationship digest", "relationship digests"), "range": ("span", "spans"),
}


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# Counting the files to read walks the vault and hashes every file, which is
# too much for a page that asks every second or two while a job runs.
FILES_FRESH_SECONDS = 60.0
_files_seen: dict[str, tuple[float, Json]] = {}
_files_guard = threading.Lock()


def forget_files(config: Config) -> None:
    with _files_guard:
        _files_seen.pop(str(config.db.path), None)


def files_state(config: Config, conn: sqlite3.Connection) -> Json:
    """How many files `journal index` would read, as its dry run counts them."""
    key = str(config.db.path)
    with _files_guard:
        seen = _files_seen.get(key)
        if seen and time.monotonic() - seen[0] < FILES_FRESH_SECONDS:
            return seen[1]
    try:
        plan = ingester.plan_backfill(config, conn)
    except JournalError as exc:
        found: Json = {"error": str(exc), "due": 0}
    else:
        by_status: dict[str, int] = {}
        for f in plan.files:
            by_status[f.status] = by_status.get(f.status, 0) + 1
        todo = plan.to_index
        found = {
            "error": None,
            "tracked": len(plan.files),
            "new": by_status.get("new", 0),
            "changed": by_status.get("changed", 0) + by_status.get("switching", 0),
            "retry": by_status.get("retry", 0),
            "gone": len(plan.dropped),
            "wont_parse": sum(1 for f in todo if f.problem),
            "calls": sum(plan.model_calls()) + plan.photo_calls(),
            "due": len(todo) + len(plan.dropped),
        }
    with _files_guard:
        _files_seen[key] = (time.monotonic(), found)
    return found


def state(config: Config) -> Json:
    """What each row of the Upkeep page says."""
    with closing(db.connect_readonly(config)) as conn:
        files = files_state(config, conn)
        plan = rollups.plan_rollups(config, conn, DIGEST_KINDS)
        by_kind: dict[str, int] = {}
        for item in plan:
            by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
        counts = profile.summary_counts(conn)
        return {
            "digests": {
                "due": len(plan),
                "kinds": [
                    {"kind": kind, "n": n, "text": _count(n, *_KIND_WORDS[kind])}
                    for kind, n in by_kind.items()
                ],
                "written": store.stats(conn).rollups,
            },
            "profile": {
                "enabled": config.profile.enabled,
                "written": profile.current(conn) is not None,
                "unread": counts.get("unread", 0),
                "unfolded": counts.get("unfolded", 0),
                "calls": profile.scan_calls(config, conn) if config.profile.enabled else 0,
                "due": config.profile.enabled and (
                    bool(counts.get("unread")) or profile.needs_update(conn, config)
                ),
            },
            "themes": themes.status_json(themes.status(conn, config)),
            "entries": store.stats(conn).entries,
            "files": files,
        }


def elsewhere(config: Config) -> Json | None:
    """Another process doing upkeep, like `journal watch` in a terminal."""
    found = work.holder(config)
    return found if found and found.get("pid") != os.getpid() else None


def describe(kind: str, params: Json) -> str:
    """A job's name as the page shows it. ValueError for one that can't be run."""
    if kind == "digest":
        period_type, key, _, _ = store.parse_period(str(params.get("key", "")), ranges=True)
        return f"Write the digest for {store.period_title(period_type, key)}"
    if kind == "index":
        return "Read every file again" if params.get("rebuild") else "Read new and changed files"
    if kind == "digests":
        return "Rewrite every digest" if params.get("rebuild") else "Write the digests that are out of date"
    if kind == "profile":
        return "Rebuild your profile" if params.get("rebuild") else "Update your profile"
    if kind == "themes":
        return "Refresh the themes"
    raise ValueError(f"There's no upkeep job called '{kind}'.")


def runners(
    config: Config,
    client: Callable[[], OllamaClient],
    then: Callable[[str], None] = lambda kind: None,
) -> dict[str, work.Runner]:
    """`then` puts another job in line, for what one job leaves for the next."""

    def with_cache(run: Callable[[sqlite3.Connection, work.Job, work.Step], str]) -> work.Runner:
        def runner(job: work.Job, step: work.Step) -> str:
            with closing(db.connect(config)) as conn:
                db.init_schema(conn)
                return run(conn, job, step)

        return runner

    def index(conn: sqlite3.Connection, job: work.Job, step: work.Step) -> str:
        force = bool(job.params.get("rebuild"))
        step(0, 0, "Looking through your journal folder")
        plan = ingester.plan_backfill(config, conn, force=force)
        todo = {f.path for f in plan.to_index}
        seen = 0

        def before(path: Path) -> None:
            nonlocal seen
            # an unchanged file is skipped without a model, so it isn't a step
            if path in todo:
                step(seen, len(todo), f"Reading {path.name}")
                seen += 1

        # the indexer prints its progress for a terminal; here the page shows it
        shown, ingester.console = ingester.console, Console(quiet=True)
        try:
            counts = ingester.run_backfill(
                config, conn, force=force, client=client(), before_file=before, profile_after=False
            )
        finally:
            ingester.console = shown
            forget_files(config)
        if counts["indexed"] and config.profile.enabled and profile.pending_entries(conn):
            then("profile")
        parts = [_count(counts["indexed"], "file read", "files read")]
        if counts["error"]:
            parts.append(_count(counts["error"], "couldn't be read", "couldn't be read"))
        if counts["private"]:
            parts.append(f"{counts['private']} private")
        if counts["dropped"]:
            parts.append(f"{counts['dropped']} no longer there")
        return ", ".join(parts) + "."

    def digest(conn: sqlite3.Connection, job: work.Job, step: work.Step) -> str:
        kind, key, start, end = store.parse_period(str(job.params["key"]), ranges=True)
        title = store.period_title(kind, key)
        if not store.entries_in_range(conn, start, end):
            raise JournalError(f"No entries in {title}.")
        if kind == "range":
            step(0, 1, f"Writing the digest for {title}")
            rollups.write_range(config, conn, client(), key)
            return f"Wrote the digest for {title}."
        kinds = ("month", "year") if kind == "year" else (kind,)
        counts = rollups.generate_rollups(config, conn, client(), kinds, True, start, end, before_item=step)
        written = sum(counts.values())
        return f"Wrote {_count(written, 'digest', 'digests')} for {title}." if written else f"{title} was already up to date."

    def digests(conn: sqlite3.Connection, job: work.Job, step: work.Step) -> str:
        counts = rollups.generate_rollups(
            config, conn, client(), DIGEST_KINDS, not job.params.get("rebuild"), before_item=step
        )
        written = sum(counts.values())
        return f"Wrote {_count(written, 'digest', 'digests')}." if written else "Every digest was already up to date."

    def profile_job(conn: sqlite3.Connection, job: work.Job, step: work.Step) -> str:
        if not config.profile.enabled:
            raise JournalError("The profile is turned off ([profile] enabled = false in config.toml).")
        rebuild = bool(job.params.get("rebuild"))
        profile.forget_gone_entries(conn)
        pending = profile.pending_entries(conn)
        batches = profile.scan_calls(config, conn)
        step(0, batches + 1, "Reading entries for your profile")
        found = profile.scan(
            config, conn, client(), pending,
            lambda done, total: step(done, total + 1, f"Reading entries for your profile ({done} of {total})"),
        ) if pending else 0
        step(batches, batches + 1, "Writing your profile")
        rewritten = profile.update(config, conn, client(), rebuild=rebuild)
        read = f"Read {_count(len(pending), 'entry', 'entries')}, found {_count(found, 'new fact', 'new facts')}. "
        return (read if pending else "") + ("Your profile was rewritten." if rewritten else "Nothing new to add.")

    def themes_job(conn: sqlite3.Connection, job: work.Job, step: work.Step) -> str:
        step(0, 0, "Grouping your entries")
        try:
            done = themes.refresh(config, conn, client(), None, on_step=step.say)
        except themes.ThemesError as exc:
            raise JournalError(str(exc)) from exc
        said = f"Found {_count(done.themes, 'theme', 'themes')} in {_count(done.entries, 'entry', 'entries')}."
        if done.naming_error:
            said += f" Named {done.named} of them before the model stopped answering ({done.naming_error})."
        return said

    return {
        "index": with_cache(index),
        "digest": with_cache(digest), "digests": with_cache(digests),
        "profile": with_cache(profile_job), "themes": with_cache(themes_job),
    }
