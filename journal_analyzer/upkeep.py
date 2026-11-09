"""What the browser's Upkeep page shows and runs: digests, the profile, themes.

`state` works out what's out of date from the cache without calling a model.
The runners do the work the matching commands do, one step at a time, saying
how far they are through a work.Step, which also lets questions go first.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from typing import Any

from . import db, profile, rollups, store, themes, work
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


def state(config: Config) -> Json:
    """What each row of the Upkeep page says."""
    with closing(db.connect_readonly(config)) as conn:
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
    if kind == "digests":
        return "Rewrite every digest" if params.get("rebuild") else "Write the digests that are out of date"
    if kind == "profile":
        return "Rebuild your profile" if params.get("rebuild") else "Update your profile"
    if kind == "themes":
        return "Refresh the themes"
    raise ValueError(f"There's no upkeep job called '{kind}'.")


def runners(config: Config, client: Callable[[], OllamaClient]) -> dict[str, work.Runner]:
    def with_cache(run: Callable[[sqlite3.Connection, work.Job, work.Step], str]) -> work.Runner:
        def runner(job: work.Job, step: work.Step) -> str:
            with closing(db.connect(config)) as conn:
                db.init_schema(conn)
                return run(conn, job, step)

        return runner

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
        "digest": with_cache(digest), "digests": with_cache(digests),
        "profile": with_cache(profile_job), "themes": with_cache(themes_job),
    }
