"""Indexing. Backfill and watch both go through process_file."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from threading import Lock
from typing import Literal

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import db, embedder, extractor, fields, parser, store
from .config import Config
from .models import ConfigError, OllamaUnavailableError, ParsedEntry
from .ollama_client import OllamaClient

console = Console()

FileResult = Literal["indexed", "skipped", "error"]
EventKind = Literal["change", "delete"]

_RESULT_STYLE: dict[FileResult, str] = {"indexed": "green", "skipped": "dim", "error": "red"}

WATCH_POLL_SECONDS = 1.0


def process_file(
    path: Path,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    force: bool = False,
) -> FileResult:
    path = Path(path)
    key = str(path)
    try:
        data = parser.read_bytes(path)
    except OSError as exc:
        store.upsert_file(conn, key, "", "error", f"unreadable: {exc}")
        return "error"
    content_hash = parser.compute_hash(data)

    kind = parser.note_kind(path, config)
    record = store.get_file_record(conn, key)
    if (
        record is not None
        and record["content_hash"] == content_hash
        and record["status"] == "indexed"
        and not force
        # notes.paths may have turned it from entries into a note, or back
        and (kind is not None) == store.note_exists(conn, key)
    ):
        return "skipped"

    try:
        if kind is None:
            _index_entries(path, key, content_hash, config, conn, client)
        else:
            _index_note(path, kind, key, content_hash, config, conn, client)
        store.upsert_file(conn, key, content_hash, "indexed")
        return "indexed"
    except OllamaUnavailableError:
        raise  # not this file's fault, so stop the run
    except Exception as exc:  # noqa: BLE001 - one bad file shouldn't stop the run
        store.upsert_file(conn, key, content_hash, "error", str(exc))
        console.print(f"[red]error[/red] {escape(str(path))}: {escape(str(exc))}")
        return "error"


def _clear_file(conn: sqlite3.Connection, key: str) -> None:
    store.mark_note_digests_stale(conn, store.link_targets_for_file(conn, key))
    # clear both kinds, since notes.paths can change what a file is
    for d in store.delete_entries_for_file(conn, key):
        store.mark_rollups_stale(conn, d)
    store.delete_note_for_file(conn, key)
    store.delete_file_frontmatter(conn, key)


def _forget_file(conn: sqlite3.Connection, key: str) -> None:
    store.mark_note_digests_stale(conn, store.link_targets_for_file(conn, key))
    for d in store.delete_file(conn, key):
        store.mark_rollups_stale(conn, d)


def _index_entries(
    path: Path,
    key: str,
    content_hash: str,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
) -> None:
    entries = parser.parse_file(path, config)
    known = config.tags.known_in_prompt
    known_tags = [r["name"] for r in store.top_tags(conn, None, None, limit=known)] if known else []
    # Do the model calls before deleting anything, so if one fails the old
    # entries are still there.
    readings = [extractor.extract(entry, config, client, known_tags) for entry in entries]
    vectors = [
        embedder.entry_vector(entry.raw_text, config, client, summary=reading.summary)
        for entry, reading in zip(entries, readings, strict=True)
    ]
    _clear_file(conn, key)
    # entries have a foreign key on files
    store.upsert_file(conn, key, content_hash, "pending")
    if entries:
        store.save_file_frontmatter(conn, key, entries[0].frontmatter)
    for entry, reading, vector in zip(entries, readings, vectors, strict=True):
        entry_id = store.save_entry(
            conn,
            entry,
            fields.final_reading(reading, entry.frontmatter, config),
            model_used=config.ollama.fast_model,
        )
        store.save_entry_reading(conn, entry_id, reading)
        if vector is not None:
            store.save_entry_vector(conn, entry_id, vector)
        store.mark_rollups_stale(conn, entry.entry_date)
        store.mark_note_digests_stale(conn, entry.links)


def _index_note(
    path: Path,
    kind: str,
    key: str,
    content_hash: str,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
) -> None:
    note = parser.parse_note(path, kind, config)
    summary, model_used = extractor.summarize_note(note, config, client)
    vector = embedder.note_vector(note, summary, config, client)
    _clear_file(conn, key)
    store.upsert_file(conn, key, content_hash, "pending")
    note_id = store.save_note(conn, note, summary, model_used)
    store.save_note_vector(conn, note_id, vector)
    store.mark_note_digests_stale(conn, [note.title.casefold()])


def _is_ignored(path: Path, config: Config) -> bool:
    patterns = config.vault.ignore_patterns
    if not patterns:
        return False
    try:
        rel_parts = path.relative_to(config.vault.path).parts
    except ValueError:
        rel_parts = path.parts
    rel_str = "/".join(rel_parts)
    for raw in patterns:
        pattern = raw.strip().strip("/")
        if not pattern:
            continue
        if "/" in pattern:
            if fnmatch.fnmatch(rel_str, pattern) or fnmatch.fnmatch(rel_str, f"{pattern}/*"):
                return True
        elif any(fnmatch.fnmatch(part, pattern) for part in rel_parts):
            return True
    return False


def _candidate_files(config: Config) -> set[Path]:
    root = config.vault.path
    found = {p for p in root.glob(config.vault.daily_note_glob) if p.is_file()}
    # notes paths don't have to fall under daily_note_glob
    for configured in config.notes.paths:
        target = root / configured
        if target.is_file():
            found.add(target)
        elif target.is_dir():
            found.update(p for p in target.rglob("*.md") if p.is_file())
    return found


def _vault_files(config: Config) -> list[Path]:
    return sorted(p for p in _candidate_files(config) if not _is_ignored(p, config))


def _require_vault(config: Config) -> None:
    if not config.vault.path.is_dir():
        raise ConfigError(
            f"There is no folder at {config.vault.path}. Point vault.path in "
            "config.toml at the root folder of your journal."
        )


def _vanished(config: Config, conn: sqlite3.Connection, files: list[Path]) -> list[str]:
    # An empty vault is much more likely an unmounted drive or a half-finished
    # sync than a deleted journal.
    if not files:
        return []
    walked = {str(p) for p in files}
    return [
        key
        for key in store.tracked_paths(conn)
        if key not in walked
        # files left over from `index --vault` elsewhere only go once deleted
        and (PurePath(key).is_relative_to(config.vault.path) or not Path(key).exists())
    ]


def _prune_vanished(config: Config, conn: sqlite3.Connection, files: list[Path]) -> int:
    gone = _vanished(config, conn, files)
    for key in gone:
        _forget_file(conn, key)
    return len(gone)


PlanStatus = Literal["new", "changed", "retry", "switching", "forced", "unchanged"]


@dataclass
class PlannedFile:
    path: Path
    kind: str | None  # None for journal entries
    status: PlanStatus
    entries: list[ParsedEntry] = field(default_factory=list)
    problem: str | None = None
    extractions: int = 0
    summaries: int = 0
    embeddings: int = 0


@dataclass
class BackfillPlan:
    files: list[PlannedFile]
    ignored: int
    dropped: list[str]

    @property
    def to_index(self) -> list[PlannedFile]:
        return [f for f in self.files if f.status != "unchanged"]

    def model_calls(self) -> tuple[int, int, int]:
        todo = self.to_index
        return (
            sum(f.extractions for f in todo),
            sum(f.summaries for f in todo),
            sum(f.embeddings for f in todo),
        )


def _plan_status(
    path: Path, kind: str | None, data: bytes, conn: sqlite3.Connection | None, force: bool
) -> PlanStatus:
    record = store.get_file_record(conn, str(path)) if conn is not None else None
    if conn is None or record is None:
        return "new"
    if record["status"] == "error":
        return "retry"
    if record["content_hash"] != parser.compute_hash(data):
        return "changed"
    if (kind is not None) != store.note_exists(conn, str(path)):
        return "switching"
    return "forced" if force else "unchanged"


def _plan_file(
    path: Path, config: Config, conn: sqlite3.Connection | None, force: bool
) -> PlannedFile:
    kind = parser.note_kind(path, config)
    try:
        data = parser.read_bytes(path)
    except OSError as exc:
        return PlannedFile(path, kind, "retry", problem=f"unreadable: {exc}")
    planned = PlannedFile(path, kind, _plan_status(path, kind, data, conn, force))
    if planned.status == "unchanged":
        return planned
    try:
        if kind is None:
            planned.entries = parser.parse_file(path, config)
            texts = [e.raw_text.strip() for e in planned.entries]
            # empty entries skip both the model and the embedding
            planned.extractions = planned.embeddings = sum(1 for t in texts if t)
        else:
            note = parser.parse_note(path, kind, config)
            text = note.raw_text.strip()
            planned.summaries = int(len(text) > config.notes.summarize_over_chars)
            planned.embeddings = 1
    except Exception as exc:  # noqa: BLE001 - reported, the same way index would mark it
        planned.problem = str(exc)
    return planned


def plan_backfill(
    config: Config, conn: sqlite3.Connection | None, force: bool = False
) -> BackfillPlan:
    """What run_backfill would do, without calling a model or writing anything."""
    _require_vault(config)
    candidates = _candidate_files(config)
    files = sorted(p for p in candidates if not _is_ignored(p, config))
    return BackfillPlan(
        files=[_plan_file(p, config, conn, force) for p in files],
        ignored=len(candidates) - len(files),
        dropped=_vanished(config, conn, files) if conn is not None else [],
    )


def run_backfill(
    config: Config,
    conn: sqlite3.Connection,
    force: bool = False,
    client: OllamaClient | None = None,
) -> dict[str, int]:
    _require_vault(config)
    client = client or OllamaClient(config)
    _ensure_vec(config, conn, client)
    # notes first so entry links resolve even if the run gets cut short
    files = sorted(_vault_files(config), key=lambda p: parser.note_kind(p, config) is None)
    counts = {
        "indexed": 0,
        "skipped": 0,
        "error": 0,
        "dropped": _prune_vanished(config, conn, files),
    }
    _refresh_readings(config, conn)
    with Progress(console=console) as progress:
        task = progress.add_task("Indexing", total=len(files))
        for path in files:
            progress.update(task, description=f"Indexing {escape(path.name)}")
            result = process_file(path, config, conn, client, force=force)
            counts[result] += 1
            progress.advance(task)
    dropped = f", {counts['dropped']} dropped" if counts["dropped"] else ""
    console.print(
        f"Done: {counts['indexed']} indexed, {counts['skipped']} skipped, "
        f"{counts['error']} errored{dropped}."
    )
    return counts


def _refresh_readings(config: Config, conn: sqlite3.Connection) -> None:
    changed = fields.refresh(config, conn)
    if changed:
        console.print(
            f"Applied the frontmatter and tag settings to {changed} "
            f"{'entry' if changed == 1 else 'entries'}."
        )


def _ensure_vec(config: Config, conn: sqlite3.Connection, client: OllamaClient) -> None:
    db.ensure_vec_table(
        conn, embedder.probe_dimension(config, client), config.ollama.embed_model
    )


def _matches_glob(path: Path, config: Config) -> bool:
    # Has to agree with what run_backfill picks up. PurePath.match treats "**"
    # like "*" and misses nested files, so this uses fnmatch, where "*" crosses
    # slashes. The copy without "**/" catches files at the top level.
    try:
        rel = PurePath(path).relative_to(config.vault.path)
    except ValueError:
        return False
    rel_str = "/".join(rel.parts)
    glob = config.vault.daily_note_glob
    candidates = {glob, glob.replace("**/", "")}
    matched = any(fnmatch.fnmatch(rel_str, pattern) for pattern in candidates)
    if not matched and rel.suffix.lower() == ".md":
        matched = parser.note_kind(path, config) is not None
    return matched and not _is_ignored(path, config)


class _DebounceHandler(FileSystemEventHandler):
    def __init__(self, config: Config):
        self.config = config
        self.pending: dict[str, tuple[float, EventKind]] = {}
        self.lock = Lock()

    def _queue(self, path: Path, kind: EventKind) -> None:
        with self.lock:
            self.pending[str(path)] = (time.monotonic(), kind)

    # A folder moved or deleted can show up as one event with nothing for the
    # files inside, and Windows reports a removed folder as a deleted file. So
    # anything that leaves is treated as a possible folder, and folders that
    # arrive get walked.
    def _record_arrival(self, src: str | bytes) -> None:
        path = Path(os.fsdecode(src))
        if _matches_glob(path, self.config):
            self._queue(path, "change")
            return
        with contextlib.suppress(OSError):
            if path.is_dir():
                for inner in path.rglob("*"):
                    if inner.is_file() and _matches_glob(inner, self.config):
                        self._queue(inner, "change")

    def _record_gone(self, src: str | bytes) -> None:
        path = Path(os.fsdecode(src))
        vault = self.config.vault.path
        if path != vault and path.is_relative_to(vault) and not _is_ignored(path, self.config):
            self._queue(path, "delete")

    def on_created(self, event: FileSystemEvent) -> None:
        self._record_arrival(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        path = Path(os.fsdecode(event.src_path))
        if not event.is_directory and _matches_glob(path, self.config):
            self._queue(path, "change")

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._record_gone(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._record_gone(event.src_path)
        self._record_arrival(event.dest_path)

    def take_due(self, debounce_seconds: float) -> list[tuple[str, EventKind]]:
        now = time.monotonic()
        due: list[tuple[str, EventKind]] = []
        with self.lock:
            for path, (ts, kind) in list(self.pending.items()):
                if now - ts >= debounce_seconds:
                    due.append((path, kind))
                    del self.pending[path]
        return due

    def requeue(self, path: str, kind: EventKind) -> None:
        # a newer event for the same path wins
        with self.lock:
            self.pending.setdefault(path, (time.monotonic(), kind))


def run_watch(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient | None = None,
) -> None:
    _require_vault(config)
    client = client or OllamaClient(config)
    _ensure_vec(config, conn, client)
    _refresh_readings(config, conn)
    handler = _DebounceHandler(config)
    observer = Observer()
    observer.schedule(handler, str(config.vault.path), recursive=True)
    observer.start()
    debounce = config.indexing.watch_debounce_seconds
    console.print(
        f"Watching {config.vault.path} (debounce {debounce:g}s). Ctrl-C to stop."
    )
    try:
        while True:
            time.sleep(WATCH_POLL_SECONDS)
            outage = _apply_due(handler, config, conn, client)
            if outage is not None:
                console.print(
                    f"[yellow]{escape(str(outage))}[/yellow]\n"
                    f"Changed files stay queued; retrying in {debounce:g}s."
                )
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


def _apply_due(
    handler: _DebounceHandler,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
) -> OllamaUnavailableError | None:
    due = handler.take_due(config.indexing.watch_debounce_seconds)
    for i, (path, kind) in enumerate(due):
        try:
            _apply_event(path, kind, config, conn, client)
        except OllamaUnavailableError as exc:
            # requeue the rest so restarting Ollama doesn't end the watch
            for queued_path, queued_kind in due[i:]:
                handler.requeue(queued_path, queued_kind)
            return exc
    return None


def _apply_event(
    path: str,
    kind: EventKind,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
) -> None:
    if kind == "delete" or not Path(path).exists():
        # whole vault gone, probably an unplugged drive
        if not config.vault.path.is_dir():
            return
        # could be a folder, so drop anything under it that's really gone
        root = PurePath(path)
        for key in store.tracked_paths(conn):
            if PurePath(key).is_relative_to(root) and not Path(key).exists():
                _forget_file(conn, key)
                console.print(f"[yellow]removed[/yellow] {escape(key)}")
        return
    result = process_file(Path(path), config, conn, client)
    style = _RESULT_STYLE[result]
    console.print(f"[{style}]{result}[/{style}] {escape(path)}")
