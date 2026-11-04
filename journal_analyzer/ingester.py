"""Indexing. Backfill and watch both go through process_file."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import os
import sqlite3
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePath
from threading import Lock
from typing import Literal

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from . import (
    context,
    db,
    embedder,
    extractor,
    fields,
    nicknames,
    parser,
    photos,
    profile,
    rollups,
    store,
)
from .config import Config
from .models import (
    ConfigError,
    DetailedExtraction,
    Extraction,
    JournalError,
    OllamaUnavailableError,
    ParsedEntry,
)
from .ollama_client import OllamaClient

console = Console()

FileResult = Literal["indexed", "private", "skipped", "error"]
EventKind = Literal["change", "delete"]

_RESULT_STYLE: dict[FileResult, str] = {
    "indexed": "green", "private": "dim", "skipped": "dim", "error": "red",
}
_PRIVATE_META_KEY = "private_settings"
_LINK_DETAILS_META_KEY = "link_details"
# raised whenever save_entry starts keeping more about links, so entries
# indexed before that get it from their files once
LINK_DETAILS_VERSION = "2"
_NOTE_PROPERTIES_META_KEY = "note_properties"
NOTE_PROPERTIES_VERSION = "1"
_SECTIONS_META_KEY = "section_settings"
_PHOTO_META_KEY = "photo_settings"

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
        and record["status"] in ("indexed", "private")
        and not force
        # notes.paths may have turned it from entries into a note, or back
        and (
            (kind is not None) == store.note_exists(conn, key)
            or record["status"] == "private"
        )
    ):
        return "skipped"

    try:
        if kind is None:
            stored = _index_entries(path, key, content_hash, config, conn, client)
        else:
            stored = _index_note(path, kind, key, content_hash, config, conn, client)
        status: FileResult = "indexed" if stored else "private"
        store.upsert_file(conn, key, content_hash, status)
        return status
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
) -> bool:
    """False when the whole file is private, so nothing was stored."""
    entries = parser.parse_file(path, config)
    if not entries:
        _clear_file(conn, key)
        return False
    known = config.tags.known_in_prompt
    known_tags = [r["name"] for r in store.top_tags(conn, None, None, limit=known)] if known else []
    vault = config.vault.path
    chars = config.indexing.linked_note_chars
    # Do the model calls before deleting anything, so if one fails the old
    # entries are still there.
    described = [photos.describe(entry, config, client) for entry in entries]
    readings = [
        extractor.extract(
            entry, config, client, known_tags, context.entry_links(conn, entry, vault, chars),
            photos.block(seen),
        )
        for entry, seen in zip(entries, described, strict=True)
    ]
    vectors = [
        # an entry too long to embed whole leads with its fuller summary
        embedder.entry_vector(
            "\n\n".join(t for t in (entry.raw_text, photos.block(seen)) if t), config, client,
            summary=_details(reading) or reading.summary,
        )
        for entry, reading, seen in zip(entries, readings, described, strict=True)
    ]
    sections = [_sections(entry.raw_text, config) for entry in entries]
    section_vectors = [embedder.section_vectors(parts, config, client) for parts in sections]
    _clear_file(conn, key)
    # entries have a foreign key on files
    store.upsert_file(conn, key, content_hash, "pending")
    store.save_file_frontmatter(conn, key, entries[0].frontmatter)
    for entry, reading, vector, parts, part_vectors, seen in zip(
        entries, readings, vectors, sections, section_vectors, described, strict=True
    ):
        entry_id = store.save_entry(
            conn,
            entry,
            fields.final_reading(reading, entry.frontmatter, config),
            model_used=config.ollama.fast_model,
        )
        store.save_entry_reading(conn, entry_id, reading)
        if _details(reading):
            store.save_entry_details(conn, entry_id, _details(reading))
        store.replace_entry_metrics(conn, entry_id, fields.entry_metrics(entry.frontmatter, config))
        if vector is not None:
            store.save_entry_vector(conn, entry_id, vector)
        if parts:
            store.replace_entry_sections(conn, entry_id, parts, part_vectors)
        if seen:
            store.save_entry_photos(conn, entry_id, seen)
        store.mark_rollups_stale(conn, entry.entry_date)
        store.mark_note_digests_stale(conn, entry.links)
    store.mark_note_digests_stale(conn, store.note_keys_linking_file_days(conn, key))
    return True


def _sections(text: str, config: Config) -> list[parser.Section]:
    if not config.indexing.sections:
        return []
    return parser.split_sections(text, config.indexing.section_min_chars)


def _refresh_sections(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    just_indexed: Collection[str] = (),
) -> None:
    # Entries indexed before sections existed, or with other section settings,
    # get them from their stored text. Only the vectors need the model, and
    # files indexed in this run already have theirs.
    settings = json.dumps({"on": config.indexing.sections, "min": config.indexing.section_min_chars})
    if db.get_meta(conn, _SECTIONS_META_KEY) == settings:
        return
    rows = [r for r in store.all_entry_texts(conn) if r["file_path"] not in just_indexed]
    split = [(r["id"], _sections(r["raw_text"], config)) for r in rows]
    todo = sum(1 for _, parts in split if parts)
    with Progress(console=console, transient=True) as progress:
        task = progress.add_task("Splitting entries into parts", total=len(split))
        for entry_id, parts in split:
            store.replace_entry_sections(
                conn, entry_id, parts, embedder.section_vectors(parts, config, client)
            )
            progress.advance(task)
    db.set_meta(conn, _SECTIONS_META_KEY, settings)
    if todo:
        console.print(f"Split {todo} {'entry' if todo == 1 else 'entries'} into parts for search.")


def _details(reading: Extraction) -> str:
    return reading.details if isinstance(reading, DetailedExtraction) else ""


def _index_note(
    path: Path,
    kind: str,
    key: str,
    content_hash: str,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
) -> bool:
    note = parser.parse_note(path, kind, config)
    if note.private:
        _clear_file(conn, key)
        return False
    summary, model_used = extractor.summarize_note(note, config, client)
    vector = embedder.note_vector(note, summary, config, client)
    _clear_file(conn, key)
    store.upsert_file(conn, key, content_hash, "pending")
    note_id = store.save_note(conn, note, summary, model_used)
    store.save_note_vector(conn, note_id, vector)
    store.mark_note_digests_stale(conn, [note.title.casefold()])
    return True


def is_ignored(path: Path, config: Config) -> bool:
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
    if config.notes.other_files:
        found.update(
            p for p in root.rglob("*.md") if p.is_file() and parser.note_kind(p, config) is not None
        )
    return found


def vault_files(config: Config) -> list[Path]:
    return sorted(p for p in _candidate_files(config) if not is_ignored(p, config))


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


@dataclass
class Targets:
    """The files `journal index PATH...` names."""

    files: list[Path]
    # cache keys of named files that aren't there any more
    gone: list[str] = field(default_factory=list)


def _named_path(raw: Path, config: Config) -> Path:
    # the vault root first, since that's how Obsidian shows paths, then the
    # current folder, so a path Tab completed in the shell works too
    path = raw.expanduser()
    if not path.is_absolute():
        in_vault = config.vault.path / path
        path = in_vault if in_vault.exists() or not path.exists() else path
    return path.resolve()


def _not_indexed_reason(path: Path, config: Config) -> str:
    rel = parser.relative_path(path, config.vault.path)
    if is_ignored(path, config):
        return f"{rel} is left out by ignore_patterns in config.toml."
    return (
        f"{rel} isn't a journal entry or a note: daily_note_glob "
        f"('{config.vault.daily_note_glob}') doesn't match it, and it isn't under notes.paths."
    )


def resolve_targets(config: Config, conn: sqlite3.Connection | None, paths: list[Path]) -> Targets:
    """Files as they are and folders by the files in them, each checked the
    way a full run would pick it. A file that's gone but still in the cache
    is to be dropped. Raises ConfigError for anything a run wouldn't index."""
    _require_vault(config)
    vault = config.vault.path
    tracked = store.tracked_paths(conn) if conn is not None else []
    files: dict[Path, None] = {}
    gone: dict[str, None] = {}
    walked: list[Path] | None = None
    for raw in paths:
        path = _named_path(raw, config)
        if not PurePath(path).is_relative_to(vault):
            raise ConfigError(f"{raw} isn't in the vault, which is {vault}.")
        if path.is_dir():
            walked = vault_files(config) if walked is None else walked
            inside = [p for p in walked if PurePath(p).is_relative_to(path)]
            if not inside:
                rel = parser.relative_path(path, vault)
                raise ConfigError(
                    f"Nothing in {'the vault' if rel == '.' else rel} is a journal entry or a "
                    "note. daily_note_glob, notes.paths and ignore_patterns in config.toml "
                    "decide which files are read."
                )
            files.update(dict.fromkeys(inside))
        elif path.is_file():
            if not _matches_glob(path, config):
                raise ConfigError(_not_indexed_reason(path, config))
            files[path] = None
        else:
            # a file or folder deleted since, as watch would have seen it
            missing = [k for k in tracked if PurePath(k).is_relative_to(path) and not Path(k).exists()]
            if not missing:
                raise ConfigError(f"There's no file or folder at {path}.")
            gone.update(dict.fromkeys(missing))
    return Targets(list(files), list(gone))


PlanStatus = Literal["new", "changed", "retry", "switching", "forced", "unchanged"]


@dataclass
class PlannedFile:
    path: Path
    kind: str | None  # None for journal entries
    status: PlanStatus
    entries: list[ParsedEntry] = field(default_factory=list)
    problem: str | None = None
    private: bool = False
    extractions: int = 0
    summaries: int = 0
    embeddings: int = 0
    photos: int = 0  # for the vision model


@dataclass
class BackfillPlan:
    files: list[PlannedFile]
    ignored: int
    dropped: list[str]
    targeted: bool = False  # only the files `journal index PATH...` named

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

    def photo_calls(self) -> int:
        return sum(f.photos for f in self.to_index)


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
            planned.private = not planned.entries
            texts = [e.raw_text.strip() for e in planned.entries]
            # empty entries skip both the model and the embedding
            planned.extractions = planned.embeddings = sum(1 for t in texts if t)
            planned.embeddings += sum(len(_sections(e.raw_text, config)) for e in planned.entries)
            planned.photos = sum(len(photos.to_describe(e, config)) for e in planned.entries)
        else:
            note = parser.parse_note(path, kind, config)
            planned.private = note.private
            if not note.private:
                text = note.raw_text.strip()
                planned.summaries = int(len(text) > config.notes.summarize_over_chars)
                planned.embeddings = 1
    except Exception as exc:  # noqa: BLE001 - reported, the same way index would mark it
        planned.problem = str(exc)
    return planned


def plan_backfill(
    config: Config,
    conn: sqlite3.Connection | None,
    force: bool = False,
    targets: Targets | None = None,
) -> BackfillPlan:
    """What run_backfill would do, without calling a model or writing anything."""
    _require_vault(config)
    if targets is not None:
        return BackfillPlan(
            files=[_plan_file(p, config, conn, force) for p in sorted(targets.files)],
            ignored=0,
            dropped=list(targets.gone),
            targeted=True,
        )
    candidates = _candidate_files(config)
    files = sorted(p for p in candidates if not is_ignored(p, config))
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
    targets: Targets | None = None,
) -> dict[str, int]:
    """With `targets`, only those files are indexed and only the gone ones
    among them dropped; the rest of the vault isn't walked."""
    _require_vault(config)
    client = client or OllamaClient(config)
    _ensure_vec(config, conn, client)
    # notes first so entry links resolve even if the run gets cut short
    files = sorted(
        vault_files(config) if targets is None else targets.files,
        key=lambda p: parser.note_kind(p, config) is None,
    )
    if targets is None:
        gone = _prune_vanished(config, conn, files)
    else:
        for key in targets.gone:
            _forget_file(conn, key)
            console.print(f"[yellow]removed[/yellow] {escape(key)}")
        gone = len(targets.gone)
    counts = {
        "indexed": 0,
        "private": 0,
        "skipped": 0,
        "error": 0,
        "dropped": gone,
    }
    _apply_private_settings(config, conn)
    _apply_photo_settings(config, conn)
    _refresh_readings(config, conn)
    _refresh_link_details(config, conn)
    _refresh_note_properties(config, conn)
    indexed: list[str] = []
    with Progress(console=console) as progress:
        task = progress.add_task("Indexing", total=len(files))
        for path in files:
            progress.update(task, description=f"Indexing {escape(path.name)}")
            result = process_file(path, config, conn, client, force=force)
            counts[result] += 1
            if result == "indexed":
                indexed.append(str(path))
            progress.advance(task)
    _refresh_sections(config, conn, client, set(indexed))
    _refresh_links(config, conn)
    dropped = f", {counts['dropped']} dropped" if counts["dropped"] else ""
    private = f", {counts['private']} private" if counts["private"] else ""
    console.print(
        f"Done: {counts['indexed']} indexed, {counts['skipped']} skipped, "
        f"{counts['error']} errored{private}{dropped}."
    )
    if _read_for_profile(config, conn, client, indexed):
        _update_profile(config, conn, client)
    return counts


def _read_for_profile(
    config: Config, conn: sqlite3.Connection, client: OllamaClient, paths: list[str]
) -> int:
    """Reads the new entries in these files for the profile. Returns facts found.

    A failure here leaves the entries indexed; they're read on a later run.
    """
    if not config.profile.enabled or not paths:
        return 0
    wanted = set(paths)
    rows = [r for r in profile.pending_entries(conn) if r["file_path"] in wanted]
    if not rows:
        return 0
    try:
        with Progress(console=console, transient=True) as progress:
            task = progress.add_task("Reading new entries for your profile", total=None)

            def advance(done: int, total: int) -> None:
                progress.update(task, total=total, completed=done)

            found = profile.scan(config, conn, client, rows, advance)
    except JournalError as exc:
        console.print(f"[yellow]Couldn't read entries for your profile: {escape(str(exc))}[/yellow]")
        return 0
    if found:
        console.print(f"Found {found} new {'thing' if found == 1 else 'things'} about you.")
    return found


def _update_profile(config: Config, conn: sqlite3.Connection, client: OllamaClient) -> bool:
    try:
        with console.status("[dim]Updating your profile...[/dim]", spinner="line"):
            rewritten = profile.update(config, conn, client)
    except JournalError as exc:
        console.print(f"[yellow]Couldn't update your profile: {escape(str(exc))}[/yellow]")
        return False
    if rewritten:
        console.print("Your profile is up to date ('journal profile' shows it).")
    return rewritten


def _private_now(path: Path, config: Config, stored_entries: int) -> bool | None:
    """True if the file is all private now, False if it should be indexed again,
    None if nothing changed or it's for the index run to sort out."""
    kind = parser.note_kind(path, config)
    try:
        if kind is not None:
            return True if parser.parse_note(path, kind, config).private else None
        entries = parser.parse_file(path, config)
    except (OSError, JournalError):
        return None
    if not entries:
        return True
    return False if len(entries) != stored_entries else None


def _apply_private_settings(config: Config, conn: sqlite3.Connection) -> None:
    # A changed private_tag can hide files that haven't changed. Those come out
    # of the cache now, without any model call; files that stop being private
    # are queued for the run. A cache from before this setting existed gets the
    # same check, so entries already tagged private leave it. Comments only
    # change the text of files that have them, so only those are indexed again,
    # and only when the setting is switched rather than on that first run.
    vault = config.vault
    settings = json.dumps({"tag": vault.private_tag, "comments": vault.strip_comments})
    previous = db.get_meta(conn, _PRIVATE_META_KEY)
    db.set_meta(conn, _PRIVATE_META_KEY, settings)
    if previous == settings:
        return
    old = json.loads(previous) if previous else None
    comments_changed = old is not None and old["comments"] != vault.strip_comments
    tag_changed = old is None or old["tag"] != vault.private_tag
    hidden = queued = 0
    for row in store.file_states(conn):
        path = Path(row["path"])
        if row["status"] not in ("indexed", "private") or not path.exists():
            continue
        if comments_changed and row["status"] == "indexed":
            with contextlib.suppress(OSError):
                if b"%%" in parser.read_bytes(path):
                    store.upsert_file(conn, row["path"], row["content_hash"], "pending")
                    queued += 1
                    continue
        if not tag_changed:
            continue
        now = _private_now(path, config, row["entries"])
        if row["status"] == "private" and now is not True:
            store.upsert_file(conn, row["path"], row["content_hash"], "pending")
            queued += 1
        elif row["status"] == "indexed" and now is True:
            _clear_file(conn, row["path"])
            store.upsert_file(conn, row["path"], row["content_hash"], "private")
            hidden += 1
        elif row["status"] == "indexed" and now is False:
            store.upsert_file(conn, row["path"], row["content_hash"], "pending")
            queued += 1
    if hidden or queued:
        console.print(
            f"Private and comment settings changed: {hidden} files taken out of the "
            f"cache, {queued} to index again."
        )


def _apply_photo_settings(config: Config, conn: sqlite3.Connection) -> None:
    # A new vision model, or turning it on or off, changes what's read for
    # entries with photos, so just those files are indexed again.
    settings = json.dumps({"model": config.ollama.vision_model, "per_entry": config.indexing.photos_per_entry})
    previous = db.get_meta(conn, _PHOTO_META_KEY)
    db.set_meta(conn, _PHOTO_META_KEY, settings)
    if previous == settings or (previous is None and not config.ollama.vision_model):
        return
    queued = 0
    for row in store.entry_files(conn):
        try:
            data = parser.read_bytes(Path(row["path"]))
        except OSError:
            continue
        text = data.decode("utf-8", errors="replace")
        if parser.compute_hash(data) == row["content_hash"] and parser.has_images(text):
            store.upsert_file(conn, row["path"], row["content_hash"], "pending")
            queued += 1
    if queued:
        console.print(
            f"Photo settings changed: {queued} {'file' if queued == 1 else 'files'} with photos to index again."
        )


def _refresh_readings(config: Config, conn: sqlite3.Connection) -> None:
    changed = fields.refresh(config, conn)
    if changed:
        console.print(
            f"Applied the frontmatter and tag settings to {changed} "
            f"{'entry' if changed == 1 else 'entries'}."
        )


def _refresh_links(config: Config, conn: sqlite3.Connection) -> None:
    """What other files decide about links: learned names, and days notes link to."""
    nicknames.refresh(config, conn)
    _refresh_day_mentions(config, conn)


def _refresh_day_mentions(config: Config, conn: sqlite3.Connection) -> None:
    wanted = store.note_day_links(conn) if config.notes.day_links_count else set()
    had = store.note_day_mentions(conn)
    if wanted == had:
        return
    store.replace_note_day_mentions(conn, wanted)
    changed = wanted ^ had
    for row in store.entries_by_ids(conn, sorted({entry_id for entry_id, _ in changed})):
        store.mark_rollups_stale(conn, date.fromisoformat(row["entry_date"]))
    notes = store.notes_by_ids(conn, sorted({note_id for _, note_id in changed}))
    store.mark_note_digests_stale(conn, [r["link_key"] for r in notes])


def _refresh_note_properties(config: Config, conn: sqlite3.Connection) -> None:
    # Notes indexed before each property value was kept separately. Ones that
    # changed since get re-indexed anyway.
    if db.get_meta(conn, _NOTE_PROPERTIES_META_KEY) == NOTE_PROPERTIES_VERSION:
        return
    for row in store.all_notes(conn):
        record = store.get_file_record(conn, row["file_path"])
        path = Path(row["file_path"])
        try:
            data = parser.read_bytes(path)
        except OSError:
            continue
        if record is None or parser.compute_hash(data) != record["content_hash"]:
            continue
        store.save_note_property_values(conn, row["id"], parser.parse_note(path, row["kind"], config))
    db.set_meta(conn, _NOTE_PROPERTIES_META_KEY, NOTE_PROPERTIES_VERSION)


def _refresh_link_details(config: Config, conn: sqlite3.Connection) -> None:
    # Stored text has its links written out, so what they pointed at comes from
    # the files. Ones that changed since get re-indexed anyway.
    if db.get_meta(conn, _LINK_DETAILS_META_KEY) == LINK_DETAILS_VERSION:
        return
    files = 0
    for row in store.entry_files(conn):
        path = Path(row["path"])
        try:
            data = parser.read_bytes(path)
        except OSError:
            continue
        if parser.compute_hash(data) != row["content_hash"] or b"[[" not in data:
            continue
        try:
            parsed = parser.parse_file(path, config)
        except JournalError:
            continue
        stored = store.entries_for_file(conn, row["path"])
        # The stored text can be written out differently from a fresh read, so
        # entries match by position, as long as the file splits the same way.
        if [r["entry_date"] for r in stored] != [e.entry_date.isoformat() for e in parsed]:
            continue
        for r, entry in zip(stored, parsed, strict=True):
            store.save_link_details(conn, r["id"], entry)
        files += 1
    db.set_meta(conn, _LINK_DETAILS_META_KEY, LINK_DETAILS_VERSION)
    if files:
        console.print(
            f"Read the links in {files} {'file' if files == 1 else 'files'} again, "
            "without calling a model."
        )


def _ensure_vec(config: Config, conn: sqlite3.Connection, client: OllamaClient) -> None:
    db.ensure_vec_table(
        conn, embedder.probe_dimension(config, client), config.ollama.embed_model
    )


def _matches_glob(path: Path, config: Config) -> bool:
    # has to agree with what run_backfill picks up
    if not PurePath(path).is_relative_to(config.vault.path):
        return False
    matched = parser.matches_entry_glob(path, config)
    if not matched and PurePath(path).suffix.lower() == ".md":
        matched = parser.note_kind(path, config) is not None
    return matched and not is_ignored(path, config)


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
        if path != vault and path.is_relative_to(vault) and not is_ignored(path, self.config):
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
    _apply_private_settings(config, conn)
    _apply_photo_settings(config, conn)
    _refresh_readings(config, conn)
    _refresh_link_details(config, conn)
    _refresh_note_properties(config, conn)
    _refresh_sections(config, conn, client)
    _refresh_links(config, conn)
    handler = _DebounceHandler(config)
    observer = Observer()
    observer.schedule(handler, str(config.vault.path), recursive=True)
    observer.start()
    debounce = config.indexing.watch_debounce_seconds
    console.print(
        f"Watching {config.vault.path} (debounce {debounce:g}s). Ctrl-C to stop."
    )
    # new facts wait until the queue is empty, and the profile is rewritten at
    # most every watch_update_minutes, since each rewrite is a precise-model call
    facts_waiting = profile.needs_update(conn, config)
    last_rewrite = float("-inf")
    # the same goes for digests, for the days this run indexed or dropped
    touched: set[date] = set()
    last_digests = float("-inf")
    try:
        while True:
            time.sleep(WATCH_POLL_SECONDS)
            indexed: list[str] = []
            outage = _apply_due(handler, config, conn, client, indexed, touched)
            if outage is not None:
                console.print(
                    f"[yellow]{escape(str(outage))}[/yellow]\n"
                    f"Changed files stay queued; retrying in {debounce:g}s."
                )
            if _read_for_profile(config, conn, client, indexed):
                facts_waiting = True
            gap = config.profile.watch_update_minutes * 60
            if facts_waiting and not handler.pending and time.monotonic() - last_rewrite >= gap:
                last_rewrite = time.monotonic()
                facts_waiting = not _update_profile(config, conn, client) and profile.needs_update(
                    conn, config
                )
            digest_gap = config.rollups.watch_refresh_minutes * 60
            if (
                digest_gap and touched and not handler.pending
                and time.monotonic() - last_digests >= digest_gap
            ):
                last_digests = time.monotonic()
                touched = _refresh_digests(config, conn, client, touched, handler)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


def _refresh_digests(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    days: set[date],
    handler: _DebounceHandler,
) -> set[date]:
    """Writes the digests these days need until a file changes. Returns the days still waiting."""
    try:
        written, waiting = rollups.refresh_days(
            config, conn, client, days, keep_going=lambda: not handler.pending
        )
    except JournalError as exc:
        console.print(f"[yellow]Couldn't write the digests: {escape(str(exc))}[/yellow]")
        return days
    for kind, key in written:
        console.print(f"[green]digest[/green] {kind} {key}")
    return waiting


def _file_days(conn: sqlite3.Connection, key: str) -> set[date]:
    return {date.fromisoformat(r["entry_date"]) for r in store.entries_for_file(conn, key)}


def _apply_due(
    handler: _DebounceHandler,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    indexed: list[str] | None = None,
    touched: set[date] | None = None,
) -> OllamaUnavailableError | None:
    """`indexed` collects the files that were indexed and `touched` the
    days whose entries changed, when given."""
    due = handler.take_due(config.indexing.watch_debounce_seconds)
    try:
        for i, (path, kind) in enumerate(due):
            try:
                if _apply_event(path, kind, config, conn, client, touched) == "indexed" and indexed is not None:
                    indexed.append(path)
            except OllamaUnavailableError as exc:
                # requeue the rest so restarting Ollama doesn't end the watch
                for queued_path, queued_kind in due[i:]:
                    handler.requeue(queued_path, queued_kind)
                return exc
    finally:
        if due:
            _refresh_links(config, conn)
    return None


def _apply_event(
    path: str,
    kind: EventKind,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    touched: set[date] | None = None,
) -> FileResult | None:
    if kind == "delete" or not Path(path).exists():
        # whole vault gone, probably an unplugged drive
        if not config.vault.path.is_dir():
            return None
        # could be a folder, so drop anything under it that's really gone
        root = PurePath(path)
        for key in store.tracked_paths(conn):
            if PurePath(key).is_relative_to(root) and not Path(key).exists():
                if touched is not None:
                    touched |= _file_days(conn, key)
                _forget_file(conn, key)
                console.print(f"[yellow]removed[/yellow] {escape(key)}")
        return None
    before = _file_days(conn, path) if touched is not None else set()
    result = process_file(Path(path), config, conn, client)
    if touched is not None:
        touched |= before | _file_days(conn, path)
    style = _RESULT_STYLE[result]
    console.print(f"[{style}]{result}[/{style}] {escape(path)}")
    return result
