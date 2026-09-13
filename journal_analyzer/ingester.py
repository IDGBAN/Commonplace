"""Indexing. Backfill and watch both go through process_file."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import sqlite3
import time
from pathlib import Path, PurePath
from threading import Lock
from typing import Literal

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import db, embedder, extractor, parser, store
from .config import Config
from .models import ConfigError, OllamaUnavailableError
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
    # clear both kinds, since notes.paths can change what a file is
    for d in store.delete_entries_for_file(conn, key):
        store.mark_rollups_stale(conn, d)
    store.delete_note_for_file(conn, key)


def _forget_file(conn: sqlite3.Connection, key: str) -> None:
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
    # Do the model calls before deleting anything, so if one fails the old
    # entries are still there.
    extractions = [extractor.extract(entry, config, client) for entry in entries]
    vectors = [
        embedder.entry_vector(entry.raw_text, config, client, summary=extraction.summary)
        for entry, extraction in zip(entries, extractions, strict=True)
    ]
    _clear_file(conn, key)
    # entries have a foreign key on files
    store.upsert_file(conn, key, content_hash, "pending")
    for entry, extraction, vector in zip(entries, extractions, vectors, strict=True):
        entry_id = store.save_entry(
            conn, entry, extraction, model_used=config.ollama.fast_model
        )
        if vector is not None:
            store.save_entry_vector(conn, entry_id, vector)
        store.mark_rollups_stale(conn, entry.entry_date)


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


def _vault_files(config: Config) -> list[Path]:
    root = config.vault.path
    found = {p for p in root.glob(config.vault.daily_note_glob) if p.is_file()}
    # notes paths don't have to fall under daily_note_glob
    for configured in config.notes.paths:
        target = root / configured
        if target.is_file():
            found.add(target)
        elif target.is_dir():
            found.update(p for p in target.rglob("*.md") if p.is_file())
    return sorted(p for p in found if not _is_ignored(p, config))


def _require_vault(config: Config) -> None:
    if not config.vault.path.is_dir():
        raise ConfigError(
            f"There is no folder at {config.vault.path}. Point vault.path in "
            "config.toml at the root folder of your journal."
        )


def _prune_vanished(config: Config, conn: sqlite3.Connection, files: list[Path]) -> int:
    # An empty vault is much more likely an unmounted drive or a half-finished
    # sync than a deleted journal.
    if not files:
        return 0
    walked = {str(p) for p in files}
    forgotten = 0
    for key in store.tracked_paths(conn):
        if key in walked:
            continue
        # files left over from `index --vault` elsewhere only go once deleted
        if PurePath(key).is_relative_to(config.vault.path) or not Path(key).exists():
            _forget_file(conn, key)
            forgotten += 1
    return forgotten


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
