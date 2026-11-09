from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, nullcontext, redirect_stdout
from dataclasses import asdict
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.status import Status
from rich.style import Style
from rich.table import Table
from rich.text import Text

from . import (
    browse,
    compare,
    completion,
    db,
    embedder,
    export,
    fields,
    history,
    ingester,
    insights,
    links,
    mcp_server,
    memo,
    obsidian,
    parser,
    profile,
    prompts,
    query,
    review,
    rollups,
    serialize,
    store,
    themes,
    unlinked,
    work,
)
from .config import Config, load_config
from .models import Answer, JournalError, OllamaUnavailableError
from .ollama_client import OllamaClient, same_model

app = typer.Typer(help="Ask questions about an Obsidian journal using local Ollama models.")
console = Console()

BYTES_PER_MB = 1_048_576
MOOD_BAR_WIDTH = 20
# `journal lapsed` defaults
LAPSED_MIN_LINKS = 5
LAPSED_QUIET_MONTHS = 6
# notes `journal note` lists as often linked with it; `entities --with` lists all
LINKED_WITH_SHOWN = 8


class ExportFormat(StrEnum):
    json = "json"
    markdown = "markdown"
    csv = "csv"


ConfigOpt = Annotated[
    Path | None,
    typer.Option("--config", help="Path to config.toml (default ./config.toml)."),
]
SinceOpt = Annotated[
    str | None, typer.Option("--since", help="Only entries on/after this ISO date.")
]
UntilOpt = Annotated[
    str | None, typer.Option("--until", help="Only entries on/before this ISO date.")
]
AboutOpt = Annotated[
    str | None,
    typer.Option(
        "--about", help="Only entries that link this note (its title or an alias).",
        autocompletion=completion.notes,
    ),
]
TagOpt = Annotated[
    str | None,
    typer.Option("--tag", help="Only entries with this tag.", autocompletion=completion.tags),
]
GroupOpt = Annotated[
    str | None,
    typer.Option(
        "--group",
        help="Only entries that link a note with this property value, e.g. relationship=family.",
        autocompletion=completion.groups,
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON instead of tables.")]
NoMemoOpt = Annotated[
    bool,
    typer.Option("--no-memo", help="Ask the models again instead of reusing their kept results."),
]


def _memo(cfg: Config, no_memo: bool) -> Config:
    if not no_memo:
        return cfg
    return cfg.model_copy(update={"db": cfg.db.model_copy(update={"memo": False})})


class _Output:
    # set by each command that takes --json, so errors come out in the same form
    as_json = False


OUT = _Output()


@app.callback()
def _utf8_output() -> None:
    OUT.as_json = False
    # Windows encodes redirected output in the ANSI code page, so
    # `journal export > out.json` crashed on the first emoji.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8")


def _emit(data: object) -> None:
    # plain print: rich would read [brackets] as markup and rewrap long lines
    print(json.dumps(data, ensure_ascii=False, indent=2, default=serialize.json_default))


def _fail(message: str, extra: dict[str, object] | None = None, red: bool = True) -> NoReturn:
    if OUT.as_json:
        _emit({"error": message, **(extra or {})})
    elif red:
        console.print(f"[red]{escape(message)}[/red]")
    else:
        console.print(escape(message))
    raise typer.Exit(1)


@contextmanager
def _guard() -> Iterator[None]:
    try:
        yield
    except JournalError as exc:
        if OUT.as_json:
            _emit({"error": str(exc)})
        else:
            # escaped, or a config key like [db] gets eaten as markup
            console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc


def _working(message: str) -> Status:
    return console.status(f"[dim]{escape(message)}[/dim]", spinner="line")


def _show_stage(status: Status) -> Callable[[str], None]:
    return lambda stage: status.update(f"[dim]{escape(stage)}[/dim]")


def _parse_date(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(f"{flag} must be an ISO date (YYYY-MM-DD), got '{value}'.")


def _parse_range(since: str | None, until: str | None) -> tuple[date | None, date | None]:
    date_from = _parse_date(since, "--since") if since is not None else None
    date_to = _parse_date(until, "--until") if until is not None else None
    if date_from and date_to and date_from > date_to:
        _fail(f"--since {date_from} is after --until {date_to}.")
    return date_from, date_to


_NO_NOTE = "No reference note called '{name}'. Notes come from the folders listed in notes.paths in config.toml."


def _one_note(conn: sqlite3.Connection, name: str, red: bool = True) -> sqlite3.Row:
    """The note a title or alias names, or a clear failure when it names none or several."""
    matches = store.find_notes(conn, name)
    if not matches:
        _fail(_NO_NOTE.format(name=name), red=red)
    if len(matches) > 1:
        message = f"'{name}' matches {len(matches)} notes; use a full title"
        if OUT.as_json:
            _fail(message, {"matches": [serialize.note_ref(m) for m in matches]})
        console.print(f"{escape(message)}:")
        console.print(_note_table(conn, matches))
        raise typer.Exit(1)
    return matches[0]


def _property_names(conn: sqlite3.Connection) -> str:
    return ", ".join(r["name"] for r in store.property_keys(conn)) or "none yet"


def _group(conn: sqlite3.Connection, value: str) -> tuple[tuple[str, str], tuple[int, ...]]:
    """`relationship=family` as the group and its notes, or a clear failure."""
    key, sep, wanted = value.partition("=")
    if not sep:
        key, sep, wanted = value.partition(":")
    key, wanted = key.strip(), wanted.strip()
    if not sep or not key or not wanted:
        _fail(f"--group takes a property and a value, like relationship=family, not '{value}'.")
    groups = store.property_groups(conn, key)
    if not groups:
        _fail(f"No note has a '{key}' property. Properties: {_property_names(conn)}.")
    ids = tuple(store.group_note_ids(conn, key, wanted))
    if not ids:
        values = ", ".join(r["value"] for r in groups)
        _fail(f"No note's {key} is '{wanted}'. Its values: {values}.")
    shown = next((r["value"] for r in groups if r["value"].casefold() == wanted.casefold()), wanted)
    return (key, shown), ids


def _entry_filter(
    conn: sqlite3.Connection, about: str | None, tag: str | None, group: str | None = None
) -> store.EntryFilter | None:
    note_ids: tuple[int, ...] = ()
    if about is not None:
        note_ids = (_one_note(conn, about)["id"],)
    name = tag.strip().lstrip("#").strip().lower() if tag is not None else None
    if name is not None and not store.tag_exists(conn, name):
        _fail(f"No entry is tagged '{name}'. 'journal tags' lists them.")
    found, group_ids = _group(conn, group) if group is not None else (None, ())
    if not (name or note_ids or found):
        return None
    return store.EntryFilter(name, note_ids, found, group_ids)


def _filter_json(conn: sqlite3.Connection, only: store.EntryFilter | None) -> dict[str, object]:
    return {
        "about": [serialize.note_ref(r) for r in store.notes_by_ids(conn, list(only.note_ids))]
        if only else [],
        "tag": only.tag if only else None,
        "group": {
            "key": only.group[0], "value": only.group[1],
            "notes": [serialize.note_ref(r) for r in store.notes_by_ids(conn, list(only.group_ids))],
        } if only and only.group else None,
    }


def _filter_label(conn: sqlite3.Connection, only: store.EntryFilter | None) -> str:
    if not only:
        return ""
    parts = []
    if only.note_ids:
        parts.append("linking " + ", ".join(r["title"] for r in store.notes_by_ids(conn, list(only.note_ids))))
    if only.group:
        parts.append(f"linking a note whose {only.group[0]} is {only.group[1]}")
    if only.tag:
        parts.append(f"tagged {only.tag}")
    return " (entries " + " and ".join(parts) + ")"


@contextmanager
def _open(config_path: Path | None) -> Iterator[tuple[Config, sqlite3.Connection]]:
    cfg = load_config(config_path)
    with closing(db.connect(cfg)) as conn:
        db.init_schema(conn)
        yield cfg, conn


def _mood_str(mood: object) -> str:
    return f"{mood:g}" if mood is not None else "-"


def _entry_table(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    title: str | None = None,
    longer: bool = False,
    parts: dict[int, sqlite3.Row] | None = None,
) -> Table:
    """With `longer`, long entries show their longer summary, and with `parts`
    (search results) the heading of the part that matched goes under the date."""
    tags_by_entry = store.tags_for_entries(conn, [r["id"] for r in rows])
    details = store.details_for_entries(conn, [r["id"] for r in rows]) if longer else {}
    table = Table(show_lines=False, title=title)
    table.add_column("Date", style="cyan", no_wrap=True)
    table.add_column("Mood", justify="right")
    table.add_column("Tags")
    table.add_column("Summary")
    for r in rows:
        part = (parts or {}).get(r["id"])
        table.add_row(
            r["entry_date"] + (f"\n#{escape(part['heading'])}" if part is not None and part["heading"] else ""),
            _mood_str(r["mood_score"]),
            escape(", ".join(tags_by_entry.get(r["id"], []))),
            escape(details.get(r["id"]) or r["summary"] or "(no summary)"),
        )
    return table


def _note_table(
    conn: sqlite3.Connection, rows: list[sqlite3.Row], title: str | None = None
) -> Table:
    mentions = store.mention_stats(conn, [r["id"] for r in rows])
    table = Table(title=title)
    table.add_column("Note", style="cyan")
    table.add_column("Kind")
    table.add_column("Entries", justify="right")
    table.add_column("Summary")
    for r in rows:
        linked = mentions.get(r["id"])
        table.add_row(
            escape(r["title"]),
            escape(r["kind"]),
            str(linked.count if linked else 0),
            escape(r["summary"] or "-"),
        )
    return table


@app.command()
def init(config: ConfigOpt = None) -> None:
    """Create the cache database and check the embedding size."""
    with _guard(), _open(config) as (cfg, conn):
        console.print(f"Schema ready at {escape(str(cfg.db.path))}")
        try:
            with _working("Asking the embedding model for its vector size..."):
                dim = embedder.probe_dimension(cfg, OllamaClient(cfg))
        except OllamaUnavailableError as exc:
            console.print(f"[yellow]Warning:[/yellow] {escape(str(exc))}")
            console.print(
                "The vector table will be created by the first 'journal index' "
                "once Ollama is running."
            )
            return
        db.ensure_vec_table(conn, dim, cfg.ollama.embed_model)
        console.print(f"Vector table ready (dimension {dim}).")


@app.command()
def index(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            help="Only these files or folders, from the vault root or the current folder.",
            show_default=False,
        ),
    ] = None,
    config: ConfigOpt = None,
    vault: Annotated[
        Path | None, typer.Option(help="Override the vault path from config.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Re-process files even if unchanged.")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would be indexed and which files won't parse, without Ollama.",
        ),
    ] = False,
    no_memo: NoMemoOpt = False,
) -> None:
    """Index the vault. Unchanged files are skipped, so re-running is cheap."""
    if dry_run:
        with _guard():
            cfg = _with_vault(load_config(config), vault)
            if not db.cache_has_data(cfg):
                targets = ingester.resolve_targets(cfg, None, paths) if paths else None
                _print_plan(
                    ingester.plan_backfill(cfg, None, force=force, targets=targets),
                    cfg.vault.path, cache=False,
                )
                return
            with closing(db.connect_readonly(cfg)) as conn:
                targets = ingester.resolve_targets(cfg, conn, paths) if paths else None
                _print_plan(
                    ingester.plan_backfill(cfg, conn, force=force, targets=targets),
                    cfg.vault.path, cache=True,
                )
        return
    with _guard(), _open(config) as (cfg, conn):
        cfg = _memo(_with_vault(cfg, vault), no_memo)
        targets = ingester.resolve_targets(cfg, conn, paths) if paths else None
        with work.hold(cfg, "journal index"):
            ingester.run_backfill(cfg, conn, force=force, targets=targets)


def _with_vault(cfg: Config, vault: Path | None) -> Config:
    if vault is None:
        return cfg
    cfg = cfg.model_copy(deep=True)
    cfg.vault.path = vault.expanduser().resolve()
    return cfg


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _print_plan(plan: ingester.BackfillPlan, vault: Path, cache: bool) -> None:
    console.print("[dim]Dry run: nothing is written and no model is called.[/dim]")
    if not cache:
        console.print("[dim]There's no cache yet, so every file counts as new.[/dim]")
    notes = sum(1 for f in plan.files if f.kind is not None)
    console.print(
        f"Files {'named' if plan.targeted else 'found'}     : {len(plan.files)} "
        f"({len(plan.files) - notes} journal, {notes} notes)"
        + ("" if plan.targeted else f", {plan.ignored} ignored")
    )
    by_status = {s: sum(1 for f in plan.files if f.status == s) for s in (
        "new", "changed", "retry", "switching", "forced", "unchanged")}
    todo = [
        f"{by_status['new']} new",
        f"{by_status['changed']} changed",
        f"{by_status['retry']} errored last time",
    ]
    if by_status["switching"]:
        todo.append(f"{by_status['switching']} switching between entry and note")
    if by_status["forced"]:
        todo.append(f"{by_status['forced']} unchanged but forced")
    console.print(f"Would index     : {', '.join(todo)}")
    private = sum(1 for f in plan.to_index if f.private)
    if private:
        console.print(f"Private         : {_count(private, 'file')} left out, nothing read by a model")
    console.print(f"Unchanged       : {by_status['unchanged']} (skipped)")
    if plan.dropped:
        console.print(f"Would drop      : {_count(len(plan.dropped), 'file')} no longer in the vault")
    extractions, summaries, embeddings = plan.model_calls()
    console.print(
        f"Model calls     : at least {extractions + summaries + embeddings} "
        f"({_count(extractions, 'extraction')}, {_count(summaries, 'note summary', 'note summaries')}, "
        f"{_count(embeddings, 'embedding')})"
    )
    if plan.photo_calls():
        console.print(
            f"Photos          : {_count(plan.photo_calls(), 'description')} by the vision model, "
            "fewer for photos it has described before"
        )

    problems = [f for f in plan.to_index if f.problem]
    if problems:
        table = Table(title="Files that would fail")
        table.add_column("File", style="cyan")
        table.add_column("Problem")
        for f in problems:
            table.add_row(escape(parser.relative_path(f.path, vault)), escape(f.problem or ""))
        console.print(table)
    split = [f for f in plan.to_index if len(f.entries) > 1]
    if split:
        table = Table(title="Files split into several entries")
        table.add_column("File", style="cyan")
        table.add_column("Entries", justify="right")
        table.add_column("Dates")
        for f in split:
            dates = [e.entry_date.isoformat() for e in f.entries]
            shown = ", ".join(dates[:4]) + (f" and {len(dates) - 4} more" if len(dates) > 4 else "")
            table.add_row(escape(parser.relative_path(f.path, vault)), str(len(dates)), shown)
        console.print(table)
    today = date.today()
    for f in plan.to_index:
        for e in f.entries:
            if e.entry_date > today:
                console.print(
                    f"  [yellow]dated in the future[/yellow] {e.entry_date}: "
                    f"{escape(parser.relative_path(f.path, vault))}"
                )
    for key in plan.dropped:
        console.print(f"  [yellow]would drop[/yellow] {escape(key)}")


@app.command()
def watch(config: ConfigOpt = None, no_memo: NoMemoOpt = False) -> None:
    """Keep indexing files as they change, until Ctrl-C."""
    with _guard(), _open(config) as (cfg, conn), work.hold(cfg, "journal watch"):
        ingester.run_watch(_memo(cfg, no_memo), conn)


def _source_link(label: str, path: str | None) -> Text:
    return Text(label, style=Style(link=parser.obsidian_url(path)) if path else "")


class _Streamed:
    """Prints an answer as the model writes it, once the spinner is out of the way,
    and the model's reasoning before it when that's shown."""

    def __init__(self, status: Status):
        self.status = status
        self.started = False
        self._thinking = False

    def __call__(self, text: str) -> None:
        if not self.started:
            self._end_thinking()
            self.status.stop()
            self.started = True
        console.print(text, end="", markup=False, highlight=False, soft_wrap=True)

    def think(self, text: str) -> None:
        if self.started:
            return
        if not self._thinking:
            self.status.stop()
            self._thinking = True
        console.print(text, end="", style="dim italic", markup=False, highlight=False, soft_wrap=True)

    def stage(self, text: str) -> None:
        if self.started:
            return
        # a fast answer that thought and then gave up hands over to the precise model
        if self._thinking and not text.startswith("Thinking"):
            self._end_thinking()
            self.status.start()
        self.status.update(f"[dim]{escape(text)}[/dim]")

    def _end_thinking(self) -> None:
        if self._thinking:
            console.print("\n")
            self._thinking = False


def _print_answer(
    conn: sqlite3.Connection, answer: Answer, stage_label: str, streamed: bool = False
) -> None:
    if streamed:
        console.print()  # finish the streamed line
    else:
        console.print(escape(answer.body))
    links: list[Text] = []
    for day in answer.cited_dates:
        rows = store.entries_in_range(conn, day, day)
        part = answer.cited_parts.get(day.isoformat())
        label = f"[[{day.isoformat()}#{part}]]" if part else f"[[{day.isoformat()}]]"
        links.append(_source_link(label, rows[0]["file_path"] if rows else None))
    for title in answer.cited_notes:
        notes = store.find_notes(conn, title)
        links.append(_source_link(f"[[{title}]]", notes[0]["file_path"] if notes else None))
    if links:
        console.print()
        console.print(Text(" ").join([Text("Sources:"), *links]))
    by = f", by {answer.model}" if answer.model else ""
    console.print(f"[dim]({stage_label}: {answer.stage_used}{escape(by)})[/dim]")
    took = query.timing_line(answer.timings)
    if took:
        console.print(f"[dim]{escape(took)}[/dim]")


def _print_reading(conn: sqlite3.Connection, answer: Answer) -> None:
    entries = store.entries_by_ids(conn, answer.read_entry_ids)
    notes = store.notes_by_ids(conn, answer.read_note_ids)
    console.print()
    if answer.stage_used == "aggregate":
        console.print(
            "Worked from statistics over your entries (mood by month, tags and how "
            "often notes are linked), not from their text."
        )
    elif not entries and not notes:
        console.print("Nothing matched, so the model read nothing.")
    if answer.quotes:
        table = Table(title="What those days say, by the question's words", show_header=False)
        table.add_column("Date", style="cyan", no_wrap=True)
        table.add_column("Quote")
        for day, quote in sorted(answer.quotes.items()):
            table.add_row(day, escape(f"“{quote}”"))
        console.print(table)
    if entries:
        console.print(_entry_table(conn, entries, title="Entries it read", longer=True))
    if notes:
        console.print(_note_table(conn, notes, title="Notes it read"))


@app.command()
def ask(
    question: Annotated[
        str | None,
        typer.Argument(
            help="The question, in plain words. Leave it out to ask one after another, with follow-ups.",
            show_default=False,
        ),
    ] = None,
    config: ConfigOpt = None,
    sources: Annotated[
        bool,
        typer.Option("--sources", help="Also list the entries and notes the answer was built from."),
    ] = False,
    since: SinceOpt = None,
    until: UntilOpt = None,
    about: AboutOpt = None,
    tag: TagOpt = None,
    group: GroupOpt = None,
    after: Annotated[
        str | None,
        typer.Option(
            "--after",
            help="Follow up on question N from 'journal history', or 'last' for the newest. "
            "Needs [history] enabled = true.",
            show_default=False,
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Ask a question about your journal, or leave the question out to ask several in a row.

    In a row, follow-ups like "what about the month after?" build on the answers
    before them. --after N follows up on a question you asked earlier, kept in
    your history. --since, --until, --about, --tag and --group replace whatever
    the question's wording suggests, and apply to every question in a row.
    """
    OUT.as_json = as_json
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        only = _entry_filter(conn, about, tag, group)
        earlier = _earlier_turns(cfg, after) if after is not None else []
        client = OllamaClient(cfg)
        if question is None:
            if as_json:
                _fail("--json needs a question, since there's no one to ask the next one.")
            _ask_loop(cfg, conn, client, date_from, date_to, only, earlier)
            return
        turns = _turns(earlier)
        conversation = earlier[-1].conversation if earlier else history.new_conversation()
        with _working("Resolving your follow-up..." if turns else "Thinking...") as status:
            stream = None if as_json else _Streamed(status)
            result, standalone = _answer_turn(
                cfg, conn, client, question, turns, date_from, date_to, only, status, stream
            )
        kept = history.record(
            cfg, question, result, "ask", conversation, standalone,
            history.scope_text(conn, only, date_from, date_to),
        )
        if as_json:
            _emit({
                "question": question,
                **({"read_as": standalone} if standalone != question else {}),
                **serialize.answer_json(conn, result), "history_id": kept,
            })
            return
        assert stream is not None
        _print_answer(conn, result, "answered by stage", streamed=stream.started)
        if standalone != question:
            console.print(f"[dim]Read as: {escape(standalone)}[/dim]")
        if sources:
            _print_reading(conn, result)


def _earlier_turns(cfg: Config, after: str) -> list[history.Kept]:
    """The conversation --after points at, up to and including that question."""
    if not cfg.history.enabled:
        _fail(
            "--after follows up on a question kept in your history, and history is off. "
            "Set [history] enabled = true in config.toml to keep questions; until then, "
            "'journal ask' with no question asks several in a row with follow-ups.",
            red=False,
        )
    if after.strip().lower() == "last":
        kept_id = history.latest_id(cfg)
        if kept_id is None:
            _fail("There are no questions in your history yet to follow up on.", red=False)
    else:
        try:
            kept_id = int(after)
        except ValueError:
            _fail(f"--after takes a question number from 'journal history', or 'last', not '{after}'.")
    earlier = history.conversation_until(cfg, kept_id)
    if not earlier:
        _fail(f"There's no question {kept_id} in your history.", red=False)
    return earlier


def _turns(earlier: list[history.Kept]) -> list[tuple[str, str]]:
    turns = [(k.standalone or k.question, k.answer) for k in earlier]
    return turns[-query.CHAT_HISTORY_TURNS:]


def _answer_turn(
    cfg: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    question: str,
    turns: list[tuple[str, str]],
    since: date | None,
    until: date | None,
    only: store.EntryFilter | None,
    status: Status,
    stream: _Streamed | None,
) -> tuple[Answer, str]:
    """The answer, and the question as it was read once earlier turns were
    taken into account."""
    started = time.monotonic()
    standalone = query.contextualize_question(question, turns, cfg, client, profile.names(conn, cfg))
    rewriting = time.monotonic() - started
    result = query.answer(
        standalone, cfg, conn, client,
        on_stage=_show_stage(status) if stream is None else stream.stage,
        since=since, until=until, only=only, on_text=stream,
        on_thinking=stream.think if stream is not None else None,
    )
    if turns:
        result = query.with_step(result, "reading your follow-up", rewriting)
    return result, standalone


def _parse_only(value: str | None) -> tuple[str, ...]:
    if value is None:
        return rollups.ORDER
    kinds = tuple(dict.fromkeys(k.strip().lower() for k in value.split(",") if k.strip()))
    unknown = [k for k in kinds if k not in rollups.ORDER]
    if unknown or not kinds:
        console.print(
            f"[red]--only takes a comma list of {', '.join(rollups.ORDER)}, got "
            f"'{escape(value)}'.[/red]"
        )
        raise typer.Exit(1)
    return kinds


_REASONS = {"missing": "not written yet", "stale": "out of date", "rebuild": "rewriting"}


def _print_rollup_plan(plan: list[rollups.PlannedDigest], scan_calls: int = 0) -> None:
    console.print("[dim]Dry run: nothing is written and no model is called.[/dim]")
    if not plan:
        console.print("Every digest is up to date.")
        return
    table = Table(title="Digests that would be written, in order")
    table.add_column("Kind")
    table.add_column("Period or note", style="cyan")
    table.add_column("Why")
    for item in plan:
        table.add_row(item.kind, escape(item.label), _REASONS[item.reason])
    console.print(table)
    reading = f", and {_count(scan_calls, 'call')} to the fast model to read entries for your profile" if scan_calls else ""
    console.print(f"{_count(len(plan), 'call')} to the precise model{reading}.")


@app.command()
def rollup(
    config: ConfigOpt = None,
    rebuild_all: Annotated[
        bool, typer.Option("--rebuild-all", help="Regenerate all digests, not just stale ones.")
    ] = False,
    since: Annotated[
        str | None, typer.Option("--since", help="Only periods that end on/after this ISO date.")
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="Only periods that start on/before this ISO date.")
    ] = None,
    only: Annotated[
        str | None,
        typer.Option("--only", help="Comma list of profile, month, year, note, week, range. Default all."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List what would be written, without Ollama.")
    ] = False,
    no_memo: NoMemoOpt = False,
) -> None:
    """Write any missing or outdated digests: your profile, months, years, often-linked notes, weeks."""
    date_from, date_to = _parse_range(since, until)
    kinds = _parse_only(only)
    with _guard(), _open(config) as (cfg, conn):
        if dry_run:
            plan = rollups.plan_rollups(cfg, conn, kinds, not rebuild_all, date_from, date_to)
            reading = any(p.kind == "profile" for p in plan)
            _print_rollup_plan(plan, profile.scan_calls(cfg, conn) if reading else 0)
            return
        cfg = _memo(cfg, no_memo)
        with work.hold(cfg, "journal rollup"):
            counts = rollups.generate_rollups(
                cfg, conn, OllamaClient(cfg), kinds, not rebuild_all, date_from, date_to
            )
    console.print(
        f"Generated {counts['week']} week, {counts['month']} month, "
        f"{counts['year']} year and {counts['note']} relationship rollups."
        + (f" Rewrote {_count(counts['range'], 'span digest')}." if counts["range"] else "")
        + (" Your profile was rewritten." if counts["profile"] else "")
    )


def _parse_period(value: str, ranges: bool = False) -> tuple[str, str, date, date]:
    """With `ranges`, a span of days like 2024-03-02..2024-03-11 too."""
    try:
        return store.parse_period(value, ranges=ranges)
    except ValueError as exc:
        if ranges and ".." in value:
            _fail(f"{str(exc)[0].upper()}{str(exc)[1:]}.")
    _fail(
        f"'{value}' isn't a period. Use a year (2024), a month (2024-03)"
        + (", an ISO week (2024-W10) or a span of days (2024-03-02..2024-03-11)." if ranges
           else " or an ISO week (2024-W10).")
    )


def _when(stamp: str | None) -> str:
    if not stamp:
        return "at an unknown time"
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp


@app.command(name="digest")
def digest_cmd(
    period: Annotated[
        str,
        typer.Argument(
            help="A year (2024), month (2024-03), ISO week (2024-W10) or span of days (2024-03-02..2024-03-11).",
            autocompletion=completion.periods,
        ),
    ],
    config: ConfigOpt = None,
    refresh: Annotated[
        bool,
        typer.Option("--refresh", help="Write it first if it's missing or out of date."),
    ] = False,
    as_json: JsonOpt = False,
    no_memo: NoMemoOpt = False,
) -> None:
    """Show the digest for one week, month, year or span of days."""
    OUT.as_json = as_json
    kind, key, start, end = _parse_period(period, ranges=True)
    title = store.period_title(kind, key)
    with _guard(), _open(config) as (cfg, conn):
        members = store.entries_in_range(conn, start, end)
        row = store.get_rollup(conn, kind, key)
        if not members and row is None:
            _fail(f"No entries in {title} ({start} to {end}).", red=False)
        if refresh and kind == "range":
            with redirect_stdout(sys.stderr) if as_json else nullcontext():
                if row is not None and not row["stale"]:
                    console.print("[dim]Already up to date.[/dim]")
                else:
                    console.print("[dim]Writing 1 digest...[/dim]")
                    rollups.write_range(cfg, conn, OllamaClient(_memo(cfg, no_memo)), key)
            row = store.get_rollup(conn, kind, key)
        elif refresh:
            kinds = ("month", "year") if kind == "year" else (kind,)
            plan = rollups.plan_rollups(cfg, conn, kinds, True, start, end)
            # progress goes to stderr when stdout is the JSON
            with redirect_stdout(sys.stderr) if as_json else nullcontext():
                if not plan:
                    console.print("[dim]Already up to date.[/dim]")
                else:
                    console.print(f"[dim]Writing {_count(len(plan), 'digest')}...[/dim]")
                    rollups.generate_rollups(
                        cfg, conn, OllamaClient(_memo(cfg, no_memo)), kinds, True, start, end
                    )
            row = store.get_rollup(conn, kind, key)
        moods = [r["mood_score"] for r in members if r["mood_score"] is not None]
        if as_json:
            _emit({
                "type": kind, "key": key, "title": title, "start": start, "end": end,
                "entries": len(members),
                "mood": round(sum(moods) / len(moods), 2) if moods else None,
                "digest": serialize.rollup_json(row) if row else None,
            })
            return
        console.print(f"[bold cyan]{escape(title)}[/bold cyan]  {start} .. {end}")
        avg = f"{sum(moods) / len(moods):.2f}".rstrip("0").rstrip(".") if moods else "-"
        console.print(f"  {_count(len(members), 'entry', 'entries')}, mood {avg}")
        if row is None:
            console.print(
                f"\nNo digest yet. Run 'journal digest {key} --refresh' to write it "
                "(one call to the precise model, more for a year whose months aren't written)."
            )
            return
        tags = json.loads(row["dominant_tags"] or "[]")
        if tags:
            console.print(f"  tags: {escape(', '.join(tags))}")
        state = " [yellow](out of date)[/yellow]" if row["stale"] else ""
        console.print(f"  [dim]written {_when(row['generated_at'])}[/dim]{state}")
        console.print()
        console.print(escape(row["summary"] or "(empty)"))
        if row["stale"] and not refresh:
            console.print(f"\n[dim]'journal digest {key} --refresh' rewrites it.[/dim]")


@app.command()
def stats(config: ConfigOpt = None, as_json: JsonOpt = False) -> None:
    """Show counts, the date range, failed files and the cache size."""
    OUT.as_json = as_json
    with _guard(), _open(config) as (cfg, conn):
        s = store.stats(conn)
        kept = memo.stats(cfg)
        asked = history.stats(cfg)
        if as_json:
            _emit({
                **asdict(s),
                "memo": None if kept is None else {
                    "path": str(memo.memo_path(cfg)), "results": kept[0], "bytes": kept[1],
                },
                "history": None if asked is None else {
                    "path": str(history.history_path(cfg)), "questions": asked[0], "bytes": asked[1],
                    "enabled": cfg.history.enabled,
                },
                "db_path": str(cfg.db.path),
                "db_bytes": cfg.db.path.stat().st_size if cfg.db.path.exists() else 0,
                "note_kinds": [dict(r) for r in store.notes_by_kind(conn)],
                "errored_files": [
                    {"path": r["path"], "error": r["error_message"]} for r in store.errored_files(conn)
                ],
                "future_entries": [
                    {"date": r["entry_date"], "path": r["file_path"]}
                    for r in store.entries_dated_after(conn, date.today())
                ],
            })
            return
        size_mb = cfg.db.path.stat().st_size / BYTES_PER_MB if cfg.db.path.exists() else 0
        kinds = ", ".join(f"{r['kind']} {r['n']}" for r in store.notes_by_kind(conn))
        console.print(f"Entries indexed : {s.entries} ({s.words:,} words)")
        console.print(f"Notes indexed   : {s.notes}" + (f" ({escape(kinds)})" if kinds else ""))
        private = f", {s.private_files} private" if s.private_files else ""
        console.print(f"Files tracked   : {s.files} ({s.errored_files} errored{private})")
        console.print(f"Date range      : {s.date_from or '-'} .. {s.date_to or '-'}")
        console.print(f"Tags / links    : {s.tags} / {s.links}")
        console.print(f"Rollup digests  : {s.rollups} (and {s.note_digests} for notes)")
        console.print(f"DB size         : {size_mb:.2f} MB ({escape(str(cfg.db.path))})")
        if kept is not None:
            console.print(
                f"Model memo      : {kept[0]:,} results, {kept[1] / BYTES_PER_MB:.2f} MB "
                f"({escape(str(memo.memo_path(cfg)))})"
            )
        elif not cfg.db.memo:
            console.print("Model memo      : off")
        if asked is not None:
            console.print(
                f"Question history: {_count(asked[0], 'question')}, {asked[1] / BYTES_PER_MB:.2f} MB "
                f"({escape(str(history.history_path(cfg)))})"
                + ("" if cfg.history.enabled else ", and history is off: 'journal history --clear' deletes it")
            )
        for row in store.errored_files(conn):
            console.print(
                f"  [red]error[/red] {escape(row['path'])}: "
                f"{escape(row['error_message'] or '')}"
            )
        # usually a typo in the file name, and it messes up streaks and year guessing
        for row in store.entries_dated_after(conn, date.today()):
            console.print(
                f"  [yellow]dated in the future[/yellow] {row['entry_date']}: "
                f"{escape(row['file_path'])}"
            )


@app.command()
def search(
    text: Annotated[str, typer.Argument(help="What to look for.")],
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max entries.")] = 10,
    since: SinceOpt = None,
    until: UntilOpt = None,
    about: AboutOpt = None,
    tag: TagOpt = None,
    group: GroupOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Search entries and notes by keyword and meaning."""
    OUT.as_json = as_json
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        only = _entry_filter(conn, about, tag, group)
        with _working("Searching..."):
            results = query.search(
                text, cfg, conn, OllamaClient(cfg),
                limit=limit, date_from=date_from, date_to=date_to, only=only,
            )
        if as_json:
            _emit({
                "query": text,
                "entries": serialize.entry_briefs(conn, results.entries, results.parts),
                "notes": serialize.note_briefs(conn, results.notes),
            })
            return
        if not results.entries and not results.notes:
            console.print("No matching entries or notes.")
            return
        if results.notes:
            console.print(_note_table(conn, results.notes, title="Related notes"))
        if results.entries:
            console.print(_entry_table(conn, results.entries, parts=results.parts))


@app.command()
def show(
    day: Annotated[
        str, typer.Argument(help="Entry date (YYYY-MM-DD).", autocompletion=completion.days)
    ],
    config: ConfigOpt = None,
    raw: Annotated[
        bool, typer.Option("--raw", help="Also print the stored entry text.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Show what's stored for one day."""
    OUT.as_json = as_json
    d = _parse_date(day, "DATE")
    with _guard(), _open(config) as (cfg, conn):
        rows = store.entries_in_range(conn, d, d)
        if not rows:
            _fail(f"No indexed entry on {d}.", red=False)
        if as_json:
            _emit({"date": d, "entries": [_entry_json(cfg, conn, r, raw) for r in rows]})
            return
        for r in rows:
            own = fields.frontmatter_mood(store.file_frontmatter(conn, r["file_path"]), cfg)
            source = " (yours, from the frontmatter)" if own is not None else ""
            console.print(f"[bold cyan]{r['entry_date']}[/bold cyan]  "
                          f"mood {_mood_str(r['mood_score'])}{source}  "
                          f"({r['word_count']} words)  {escape(r['file_path'])}")
            tags = store.tags_for_entry(conn, r["id"])
            if tags:
                console.print(f"  tags: {escape(', '.join(tags))}")
            numbers = store.metrics_for_entries(conn, [r["id"]]).get(r["id"])
            if numbers:
                console.print(
                    "  numbers: " + escape(", ".join(f"{k} {v:g}" for k, v in numbers.items()))
                )
            ents = store.entities_for_entry(conn, r["id"])
            parts = serialize.note_parts(conn, r["id"])
            if ents:
                console.print(
                    "  mentions: "
                    + escape(", ".join(
                        f"{e['name']} ({'; '.join([e['type'], *_labels(parts.get(e['id'], []))])})"
                        for e in ents
                    ))
                )
            days = serialize.days_linked_from(conn, r["id"])
            if days:
                console.print(
                    "  other days: "
                    + escape(", ".join(
                        d["date"] + (f" ({', '.join(_labels(d['sections']))})" if d["sections"] else "")
                        for d in days
                    ))
                )
            linking = store.notes_linking_day(conn, r)
            if linking:
                console.print("  linked from notes: " + escape(", ".join(n["title"] for n in linking)))
            for photo in store.photos_for_entries(conn, [r["id"]]).get(r["id"], []):
                console.print(f"  photo {escape(photo['name'])}: {escape(photo['description'])}")
            console.print(f"  {escape(r['summary'] or '(no summary)')}")
            longer = store.details_for_entries(conn, [r["id"]]).get(r["id"])
            if longer:
                console.print(f"  [dim]In more detail:[/dim] {escape(longer)}")
            if raw:
                console.print()
                console.print(escape(r["raw_text"]))


def _labels(sections: list[dict[str, str]]) -> list[str]:
    return [s["label"] for s in sections]


def _entry_json(cfg: Config, conn: sqlite3.Connection, row: sqlite3.Row, raw: bool) -> dict[str, object]:
    own = fields.frontmatter_mood(store.file_frontmatter(conn, row["file_path"]), cfg)
    parts = serialize.note_parts(conn, row["id"])
    data: dict[str, object] = {
        **serialize.entry_briefs(conn, [row])[0],
        "path": row["file_path"],
        "mood_from_frontmatter": own is not None,
        "notes": [
            {"id": e["id"], "title": e["name"], "kind": e["type"], "sections": parts.get(e["id"], [])}
            for e in store.entities_for_entry(conn, row["id"])
        ],
        "links_to_days": serialize.days_linked_from(conn, row["id"]),
        "linked_from_notes": [serialize.note_ref(n) for n in store.notes_linking_day(conn, row)],
        "photos": [
            {"name": p["name"], "path": p["path"], "description": p["description"]}
            for p in store.photos_for_entries(conn, [row["id"]]).get(row["id"], [])
        ],
        "metrics": store.metrics_for_entries(conn, [row["id"]]).get(row["id"], {}),
        "details": store.details_for_entries(conn, [row["id"]]).get(row["id"]),
    }
    if raw:
        data["raw_text"] = row["raw_text"]
    return data


@app.command()
def note(
    name: Annotated[
        str, typer.Argument(help="Note title or alias.", autocompletion=completion.notes)
    ],
    config: ConfigOpt = None,
    raw: Annotated[
        bool, typer.Option("--raw", help="Also print the note's own text.")
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="How many linking entries to list.")
    ] = 10,
    as_json: JsonOpt = False,
) -> None:
    """Show a reference note and the entries that link to it."""
    OUT.as_json = as_json
    with _guard(), _open(config) as (cfg, conn):
        row = _one_note(conn, name, red=False)
        if as_json:
            parsed = store.note_from_row(row)
            data: dict[str, object] = {
                **serialize.note_briefs(conn, [row])[0],
                "path": row["file_path"],
                "learned_names": serialize.learned_names(conn, [row["id"]]).get(row["id"], []),
                "properties": parsed.properties,
                "by_year": [dict(r) for r in store.mentions_by_year(conn, row["id"])],
                "linked_with": [
                    dict(r) for r in store.notes_linked_with(conn, row["id"], LINKED_WITH_SHOWN)
                ],
                "outlinks": [serialize.note_ref(r) for r in store.note_outlinks(conn, row["id"])],
                "backlinks": [serialize.note_ref(r) for r in store.note_backlinks(conn, row["id"])],
                "linked_days": serialize.entry_briefs(conn, store.days_linked_from_note(conn, row["id"])),
                "digest": serialize.note_digest_json(store.get_note_digest(conn, row["link_key"])),
                "entries": serialize.entry_briefs(conn, store.entries_mentioning(conn, row["id"], limit)),
            }
            if raw:
                data["raw_text"] = parsed.raw_text
            _emit(data)
            return
        _print_note(cfg, conn, row, raw=raw, limit=limit)


def _print_note(
    cfg: Config, conn: sqlite3.Connection, row: sqlite3.Row, raw: bool, limit: int
) -> None:
    parsed = store.note_from_row(row)
    console.print(
        f"[bold cyan]{escape(parsed.title)}[/bold cyan]  ({escape(parsed.kind)})  "
        f"{escape(parsed.source_path)}"
    )
    if parsed.aliases:
        console.print(f"  also called: {escape(', '.join(parsed.aliases))}")
    learned = serialize.learned_names(conn, [row["id"]]).get(row["id"], [])
    if learned:
        console.print(
            "  you also write: "
            + escape(", ".join(f"{n['name']} ({_count(n['entries'], 'entry', 'entries')})" for n in learned))
        )
    for key, value in parsed.properties.items():
        console.print(f"  {escape(key)}: {escape(value)}")
    if row["summary"]:
        console.print(f"  {escape(row['summary'])}")
    mentions = store.mention_stats(conn, [row["id"]]).get(row["id"])
    if mentions is None:
        console.print("  Not linked from any indexed entry.")
    else:
        console.print(
            f"  Linked from {mentions.count} entries, {mentions.first} .. {mentions.last}"
        )
    together = store.notes_linked_with(conn, row["id"], LINKED_WITH_SHOWN)
    if together:
        console.print(
            "  often linked with: " + escape(", ".join(f"{r['title']} ({r['n']})" for r in together))
        )
    outlinks = store.note_outlinks(conn, row["id"])
    if outlinks:
        console.print("  links to: " + escape(", ".join(r["title"] for r in outlinks)))
    days = store.days_linked_from_note(conn, row["id"])
    if days:
        console.print("  links to days: " + ", ".join(r["entry_date"] for r in days))
    backlinks = store.note_backlinks(conn, row["id"])
    if backlinks:
        console.print(
            "  linked from notes: " + escape(", ".join(r["title"] for r in backlinks))
        )
    digest = store.get_note_digest(conn, row["link_key"])
    minimum = cfg.rollups.note_min_mentions
    if digest is not None and digest["summary"]:
        stale = " [yellow](out of date, run 'journal rollup')[/yellow]" if digest["stale"] else ""
        console.print(f"\n[bold]Over the years[/bold]{stale}")
        console.print(f"  {escape(digest['summary'])}")
    elif minimum and mentions is not None and mentions.count >= minimum:
        console.print(
            "\n[dim]No relationship digest yet. 'journal rollup' writes one for notes "
            f"linked from {minimum} or more entries.[/dim]"
        )
    recent = store.entries_mentioning(conn, row["id"], limit)
    if recent:
        console.print("\n[bold]Most recent entries linking to it:[/bold]")
        console.print(_entry_table(conn, recent))
    if raw and parsed.raw_text:
        console.print()
        console.print(escape(parsed.raw_text))


@app.command()
def tags(
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many tags.")] = 20,
    since: SinceOpt = None,
    until: UntilOpt = None,
    similar: Annotated[
        bool,
        typer.Option("--similar", help="List tags that look like the same thing, to merge."),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """List the most used tags."""
    OUT.as_json = as_json
    date_from, date_to = _parse_range(since, until)
    if similar:
        with _guard(), _open(config) as (_, conn):
            pairs = fields.similar_tags(store.tag_stats(conn))
        if as_json:
            _emit({
                "similar": [
                    {"tag": rare, "entries": n_rare, "like": common, "like_entries": n_common}
                    for rare, n_rare, common, n_common in pairs
                ],
                "merge": _merge_table(pairs),
            })
            return
        _print_similar_tags(pairs)
        return
    with _guard(), _open(config) as (_, conn):
        rows = store.top_tags(conn, date_from, date_to, limit=limit)
    if as_json:
        _emit({"tags": [{"name": r["name"], "n": r["n"]} for r in rows]})
        return
    if not rows:
        console.print("No tags yet. Run 'journal index' first.")
        return
    table = Table()
    table.add_column("Tag", style="cyan")
    table.add_column("Entries", justify="right")
    for r in rows:
        table.add_row(escape(r["name"]), str(r["n"]))
    console.print(table)


def _print_similar_tags(pairs: list[tuple[str, int, str, int]]) -> None:
    if not pairs:
        console.print("No tags look like duplicates of each other.")
        return
    table = Table(title="Tags that may be the same thing")
    table.add_column("Tag", style="cyan")
    table.add_column("Entries", justify="right")
    table.add_column("Looks like", style="cyan")
    table.add_column("Entries", justify="right")
    for rare, n_rare, common, n_common in pairs:
        table.add_row(escape(rare), str(n_rare), escape(common), str(n_common))
    console.print(table)
    console.print(
        "\nTo count them as one, add the ones you agree with to config.toml and run "
        "'journal index' (no model calls needed):\n"
    )
    lines = [f'{_toml_key(rare)} = "{common}"' for rare, common in _merge_table(pairs).items()]
    # plain print so the [tags] header isn't read as markup
    print("[tags]\nmerge = { " + ", ".join(lines) + " }")


def _merge_table(pairs: list[tuple[str, int, str, int]]) -> dict[str, str]:
    merge: dict[str, str] = {}
    for rare, _, common, _ in pairs:
        merge.setdefault(rare, common)
    return merge


def _toml_key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else f'"{name}"'


@app.command(name="unlinked")
def unlinked_cmd(
    name: Annotated[
        str | None,
        typer.Argument(
            help="A note's title or alias. Leave out to list every note with some.",
            autocompletion=completion.notes,
        ),
    ] = None,
    config: ConfigOpt = None,
    type_: Annotated[
        str | None,
        typer.Option(
            "--type", help="Only notes of this kind, meaning their folder (e.g. people).",
            autocompletion=completion.kinds,
        ),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many rows.")] = 20,
    as_json: JsonOpt = False,
) -> None:
    """Entries that name a note without linking it, so its numbers miss them."""
    OUT.as_json = as_json
    with _guard(), _open(config) as (_, conn):
        if name is None:
            with _working("Reading entries for names..."):
                found = unlinked.by_note(conn, type_)
            if as_json:
                _emit({"notes": [asdict(row) for row in found[:limit]], "total": len(found)})
                return
            if not found:
                console.print("Every entry that names a note also links it.")
                return
            table = Table(title="Notes named without a link")
            table.add_column("Note", style="cyan")
            table.add_column("Kind")
            table.add_column("Entries", justify="right")
            table.add_column("Latest", no_wrap=True)
            for row in found[:limit]:
                table.add_row(escape(row.title), escape(row.kind), str(row.count), row.latest)
            console.print(table)
            console.print(
                f"[dim]{len(found)} notes in all. 'journal unlinked NAME' lists one note's entries.[/dim]"
            )
            return
        note = _one_note(conn, name, red=False)
        found_entries = unlinked.for_note(conn, note["id"])
        if as_json:
            _emit({
                "note": serialize.note_ref(note),
                "entries": [asdict(m) for m in found_entries[:limit]],
                "total": len(found_entries),
            })
            return
        if not found_entries:
            console.print(f"Every entry that names {escape(note['title'])} also links it.")
            return
        paths = {r["id"]: r["file_path"] for r in store.entries_by_ids(
            conn, [m.entry_id for m in found_entries[:limit]])}
        table = Table(title=f"Entries that name {escape(note['title'])} without linking it")
        table.add_column("Date", no_wrap=True)
        table.add_column("Named as", style="cyan")
        table.add_column("Where")
        for m in found_entries[:limit]:
            table.add_row(_source_link(m.entry_date, paths.get(m.entry_id)), escape(m.name), escape(m.snippet))
        console.print(table)
        if len(found_entries) > limit:
            console.print(f"[dim]{len(found_entries) - limit} more; raise --limit to see them.[/dim]")
        console.print(f"[dim]Link them as [[{escape(note['title'])}]] in Obsidian and they count from the next index.[/dim]")


@app.command(name="links")
def links_cmd(
    target: Annotated[
        str | None,
        typer.Argument(
            help="A link as you write it, e.g. 'Jon Doe'. Leave out to list them all.",
            autocompletion=completion.broken_links,
        ),
    ] = None,
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many rows.")] = 20,
    as_json: JsonOpt = False,
) -> None:
    """Links that find no note or day, why, and what to do about each."""
    OUT.as_json = as_json
    with _guard(), _open(config) as (cfg, conn):
        files = links.vault_markdown(cfg) if cfg.vault.path.is_dir() else None
        found = links.diagnose(cfg, conn, files)
        if target is None:
            if as_json:
                _emit({"links": [asdict(b) for b in found[:limit]], "total": len(found)})
                return
            _print_broken_links(found, limit)
            return
        key = parser.link_key(target) or target.strip().casefold()
        broken = next((b for b in found if b.target == key), None)
        entries = store.entries_linking(conn, key)
        notes = store.notes_linking(conn, key)
        if broken is None and not entries and not notes:
            _fail(f"No entry or note links [[{target}]].", red=False)
        if as_json:
            _emit({
                "link": asdict(broken) if broken else None,
                "target": key,
                "entries": serialize.entry_briefs(conn, entries[:limit]),
                "notes": [serialize.note_ref(n) for n in notes],
                "total": len(entries),
            })
            return
        if broken is None:
            console.print(f"[[{escape(target)}]] finds a note or a day, so it counts.")
        else:
            console.print(f"[bold cyan][[{escape(target)}]][/bold cyan] counts for nothing. {escape(broken.why)}")
            console.print(f"  {escape(broken.fix)}")
        if entries:
            paths = {r["id"]: r["file_path"] for r in entries}
            table = Table(title=f"Entries that link [[{escape(target)}]], newest first")
            table.add_column("Date", no_wrap=True)
            table.add_column("Summary")
            for r in entries[:limit]:
                table.add_row(_source_link(r["entry_date"], paths[r["id"]]), escape(r["summary"] or "-"))
            console.print(table)
            if len(entries) > limit:
                console.print(f"[dim]{len(entries) - limit} more; raise --limit to see them.[/dim]")
        if notes:
            console.print("Notes that link it: " + escape(", ".join(n["title"] for n in notes)))


def _print_broken_links(found: list[links.BrokenLink], limit: int) -> None:
    if not found:
        console.print("Every link finds a note or a day.")
        return
    table = Table(title="Links that find nothing")
    table.add_column("Link", style="cyan")
    table.add_column("Entries", justify="right")
    table.add_column("Notes", justify="right")
    table.add_column("Last", no_wrap=True)
    table.add_column("Why, and what to do")
    for b in found[:limit]:
        table.add_row(
            escape(f"[[{b.target}]]"), str(b.entries), str(b.notes), b.last or "-",
            f"{escape(b.why)}\n[dim]{escape(b.fix)}[/dim]",
        )
    console.print(table)
    console.print(
        f"[dim]{_count(len(found), 'link')} in all. 'journal links NAME' lists the entries "
        "that write one.[/dim]"
    )


@app.command()
def entities(
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many notes.")] = 20,
    type_: Annotated[
        str | None,
        typer.Option(
            "--type", help="Only notes of this kind, meaning their folder (e.g. people).",
            autocompletion=completion.kinds,
        ),
    ] = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    with_: Annotated[
        str | None,
        typer.Option(
            "--with", help="Only notes linked from the same entries as this one (title or alias).",
            autocompletion=completion.notes,
        ),
    ] = None,
    by: Annotated[
        str | None,
        typer.Option(
            "--by", help="Group the notes by one of their properties, e.g. relationship.",
            autocompletion=completion.properties,
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """List the notes your entries link to most."""
    OUT.as_json = as_json
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        if with_ is not None:
            _print_linked_with(conn, _one_note(conn, with_), limit, date_from, date_to, type_)
            return
        if by is not None:
            _print_groups(cfg, conn, by, limit, date_from, date_to)
            return
        rows = store.top_entities(conn, date_from, date_to, limit=limit, entity_type=type_)
        kinds = ", ".join(r["kind"] for r in store.notes_by_kind(conn))
    if as_json:
        _emit({"notes": [{"name": r["name"], "kind": r["type"], "n": r["n"]} for r in rows]})
        return
    if not rows:
        if type_ and kinds:
            console.print(f"No linked notes of kind '{escape(type_)}'. Kinds: {escape(kinds)}.")
        else:
            console.print(
                "No linked notes yet. Point notes.paths in config.toml at your "
                "reference notes and run 'journal index'."
            )
        return
    table = Table()
    table.add_column("Note", style="cyan")
    table.add_column("Kind")
    table.add_column("Entries", justify="right")
    for r in rows:
        table.add_row(escape(r["name"]), escape(r["type"]), str(r["n"]))
    console.print(table)


def _print_groups(
    cfg: Config,
    conn: sqlite3.Connection,
    key: str,
    limit: int,
    date_from: date | None,
    date_to: date | None,
) -> None:
    rows = store.property_groups(conn, key, date_from, date_to)
    if not rows:
        _fail(f"No note has a '{key}' property. Properties: {_property_names(conn)}.")
    if OUT.as_json:
        _emit({"by": key, "groups": [dict(r) for r in rows[:limit]], "total": len(rows)})
        return
    scale = cfg.indexing.mood_scale_max
    table = Table(title=f"Notes by {escape(key)}")
    table.add_column(escape(key.capitalize()), style="cyan")
    table.add_column("Notes", justify="right")
    table.add_column("Entries", justify="right")
    table.add_column(f"Mood (1..{scale})", justify="right")
    table.add_column("Last linked", no_wrap=True)
    for r in rows[:limit]:
        table.add_row(
            escape(r["value"]), str(r["notes"]), str(r["entries"]), _mood_str(r["avg_mood"]),
            r["last"] or "-",
        )
    console.print(table)
    console.print(
        f"[dim]--group {escape(key)}=VALUE filters mood, search, ask and the rest to one of these.[/dim]"
    )


def _print_linked_with(
    conn: sqlite3.Connection,
    note: sqlite3.Row,
    limit: int,
    date_from: date | None,
    date_to: date | None,
    kind: str | None,
) -> None:
    rows = store.notes_linked_with(conn, note["id"], limit, date_from, date_to, kind)
    if OUT.as_json:
        _emit({"with": serialize.note_ref(note), "notes": [dict(r) for r in rows]})
        return
    if not rows:
        console.print(f"No other note is linked from the entries that link {escape(note['title'])}.")
        return
    table = Table(title=f"Linked from the same entries as {escape(note['title'])}")
    table.add_column("Note", style="cyan")
    table.add_column("Kind")
    table.add_column("Shared entries", justify="right")
    for r in rows:
        table.add_row(escape(r["title"]), escape(r["kind"]), str(r["n"]))
    console.print(table)


@app.command()
def mood(
    config: ConfigOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    about: AboutOpt = None,
    tag: TagOpt = None,
    group: GroupOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Average mood per month."""
    OUT.as_json = as_json
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        only = _entry_filter(conn, about, tag, group)
        rows = store.monthly_mood(conn, date_from, date_to, only)
        label = _filter_label(conn, only)
        if as_json:
            _emit({
                "scale": cfg.indexing.mood_scale_max,
                **_filter_json(conn, only),
                "months": [dict(r) for r in rows],
            })
            return
    if not any(r["avg_mood"] is not None for r in rows):
        console.print("No mood-scored entries in this range yet.")
        return
    scale = cfg.indexing.mood_scale_max
    table = Table(title=f"Average mood by month (1..{scale}){escape(label)}")
    table.add_column("Month", style="cyan", no_wrap=True)
    table.add_column("Mood", justify="right")
    table.add_column("")
    table.add_column("Entries", justify="right")
    for r in rows:
        avg = r["avg_mood"]
        bar = "█" * round((avg / scale) * MOOD_BAR_WIDTH) if avg is not None else ""
        table.add_row(r["month"], _mood_str(avg), f"[green]{bar}[/green]",
                      str(r["entry_count"]))
    console.print(table)


@app.command()
def metric(
    key: Annotated[
        str | None,
        typer.Argument(help="Which number, e.g. sleep. Leave out to list them.", autocompletion=completion.metrics),
    ] = None,
    config: ConfigOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    about: AboutOpt = None,
    tag: TagOpt = None,
    group: GroupOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Numbers you record in entry frontmatter, like sleep: 7, by month."""
    OUT.as_json = as_json
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (_, conn):
        known = store.metric_keys(conn)
        if as_json and key is None:
            _emit({"metrics": [dict(r) for r in known]})
            return
        if not known and not as_json:
            console.print(
                "No numbers found in your entries' frontmatter. Add one like 'sleep: 7' "
                "to a daily note and run 'journal index'."
            )
            return
        if key is None:
            table = Table(title="Numbers in your entries")
            table.add_column("Metric", style="cyan")
            for name in ("Entries", "Min", "Max", "Average"):
                table.add_column(name, justify="right")
            table.add_column("First .. last", no_wrap=True)
            for r in known:
                table.add_row(
                    escape(r["key"]), str(r["n"]), f"{r['lo']:g}", f"{r['hi']:g}",
                    f"{r['avg']:g}", f"{r['first']} .. {r['last']}",
                )
            console.print(table)
            return
        assert key is not None
        name = key.strip().casefold()
        if name not in {r["key"] for r in known}:
            names = [r["key"] for r in known]
            _fail(
                f"No entry records '{name}'. Known: {', '.join(names) or 'none yet'}.",
                {"known": names},
            )
        only = _entry_filter(conn, about, tag, group)
        rows = store.monthly_metric(conn, name, date_from, date_to, only)
        label = _filter_label(conn, only)
        if as_json:
            _emit({"metric": name, **_filter_json(conn, only), "months": [dict(r) for r in rows]})
            return
    if not rows:
        console.print(f"No entries record '{escape(name)}' in this range.")
        return
    top = max(abs(r["avg"]) for r in rows) or 1
    table = Table(title=f"Average {escape(name)} by month{escape(label)}")
    table.add_column("Month", style="cyan", no_wrap=True)
    table.add_column(escape(name), justify="right")
    table.add_column("")
    table.add_column("Entries", justify="right")
    for r in rows:
        bar = "█" * round(abs(r["avg"]) / top * MOOD_BAR_WIDTH)
        table.add_row(r["month"], f"{r['avg']:g}", f"[cyan]{bar}[/cyan]", str(r["n"]))
    console.print(table)


@app.command()
def lapsed(
    config: ConfigOpt = None,
    type_: Annotated[
        str | None,
        typer.Option(
            "--type", help="Only notes of this kind, meaning their folder (e.g. people).",
            autocompletion=completion.kinds,
        ),
    ] = None,
    min_links: Annotated[
        int, typer.Option("--min", min=1, help="Linked from at least this many entries.")
    ] = LAPSED_MIN_LINKS,
    quiet: Annotated[
        int, typer.Option("--quiet", min=1, help="Months without a link, counted back from your latest entry.")
    ] = LAPSED_QUIET_MONTHS,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many notes.")] = 20,
    as_json: JsonOpt = False,
) -> None:
    """People and places you used to write about often and haven't lately."""
    OUT.as_json = as_json
    with _guard(), _open(config) as (_, conn):
        _, latest = store.date_bounds(conn, None, None)
        if latest is None:
            if as_json:
                _emit({"anchor": None, "min": min_links, "quiet_months": quiet, "notes": []})
            else:
                console.print("No entries indexed yet. Run 'journal index' first.")
            return
        anchor = date.fromisoformat(latest)
        rows = store.lapsed_notes(conn, min_links, store.months_before(anchor, quiet), type_)
    if as_json:
        _emit({
            "anchor": anchor, "min": min_links, "quiet_months": quiet,
            "notes": [
                {**asdict(r), "months_quiet": store.months_between(date.fromisoformat(r.last), anchor)}
                for r in rows[:limit]
            ],
        })
        return
    if not rows:
        console.print(
            f"Nothing you linked {min_links} or more times has gone "
            f"{_count(quiet, 'month')} without a link."
        )
        return
    table = Table(
        title=f"Linked {min_links}+ times, but not in the {quiet} months before {anchor}"
    )
    table.add_column("Note", style="cyan")
    table.add_column("Kind")
    table.add_column("Entries", justify="right")
    table.add_column("Busiest year", no_wrap=True)
    table.add_column("Last linked", no_wrap=True)
    table.add_column("Quiet for", justify="right", no_wrap=True)
    for r in rows[:limit]:
        months = store.months_between(date.fromisoformat(r.last), anchor)
        table.add_row(
            escape(r.title), escape(r.kind), str(r.count),
            f"{r.busiest_year} ({r.busiest_count})", r.last, _count(months, "month"),
        )
    console.print(table)


def _profile_progress() -> tuple[Callable[[int, int], None], Callable[[], None]]:
    status = console.status("[dim]Reading entries for your profile...[/dim]", spinner="line")
    status.start()

    def advance(done: int, total: int) -> None:
        status.update(f"[dim]Reading entries for your profile ({done}/{total} batches)...[/dim]")

    return advance, status.stop


@app.command(name="profile")
def profile_cmd(
    config: ConfigOpt = None,
    facts: Annotated[
        bool, typer.Option("--facts", help="List the facts it was built from, newest first.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many facts.")] = 50,
    update: Annotated[
        bool,
        typer.Option("--update", help="Read any entries not read yet and fold in what's new."),
    ] = False,
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Write it again from all the facts, oldest first.")
    ] = False,
    rescan: Annotated[
        bool,
        typer.Option("--rescan", help="Forget the facts, read every entry again, and rebuild."),
    ] = False,
    as_json: JsonOpt = False,
    no_memo: NoMemoOpt = False,
) -> None:
    """What your journal says about you: names, likes, habits and the like."""
    OUT.as_json = as_json
    with _guard(), _open(config) as (cfg, conn):
        if not cfg.profile.enabled:
            _fail("The profile is turned off ([profile] enabled = false in config.toml).", red=False)
        if rescan:
            store.forget_profile_scans(conn)
        if update or rebuild or rescan:
            client = OllamaClient(_memo(cfg, no_memo))
            with redirect_stdout(sys.stderr) if as_json else nullcontext():
                profile.forget_gone_entries(conn)
                pending = profile.pending_entries(conn)
                if pending:
                    advance, stop = _profile_progress()
                    try:
                        found = profile.scan(cfg, conn, client, pending, advance)
                    finally:
                        stop()
                    console.print(
                        f"Read {_count(len(pending), 'entry', 'entries')}, "
                        f"found {_count(found, 'new fact')}."
                    )
                with _working("Writing your profile..."):
                    rewritten = profile.update(cfg, conn, client, rebuild=rebuild or rescan)
                if not rewritten:
                    console.print("[dim]Nothing new to add.[/dim]")
        if as_json:
            _emit(profile.to_json(conn, cfg, limit))
            return
        found_profile = profile.current(conn)
        counts = profile.summary_counts(conn)
        fact_rows = profile.facts_json(conn, limit) if facts else []
    if facts:
        if not fact_rows:
            console.print("No facts yet. 'journal profile --update' reads your entries for them.")
            return
        table = Table(title="What your entries say about you, newest first")
        table.add_column("Date", no_wrap=True)
        table.add_column("About")
        table.add_column("Fact")
        for f in fact_rows:
            table.add_row(str(f["date"]), str(f["category"]), escape(str(f["fact"])))
        console.print(table)
        return
    if found_profile is None:
        console.print(
            "No profile yet. 'journal profile --update' reads your entries and writes one "
            f"({_count(counts['unread'], 'entry', 'entries')} to read)."
        )
        return
    console.print(escape(found_profile.text))
    if found_profile.names:
        console.print(f"\n[bold]Names[/bold]  {escape(found_profile.names)}")
    console.print(
        f"\n[dim]Written {_when(found_profile.updated_at)} from {_count(counts['facts'], 'fact')}"
        + (f", with your note {escape(cfg.profile.note)}" if cfg.profile.note else "") + ".[/dim]"
    )
    waiting = []
    if counts["unread"]:
        waiting.append(f"{_count(counts['unread'], 'entry', 'entries')} not read yet")
    if counts["unfolded"]:
        waiting.append(f"{_count(counts['unfolded'], 'new fact')} not added yet")
    if waiting:
        console.print(f"[dim]{', '.join(waiting)}: 'journal profile --update' catches up.[/dim]")


@app.command()
def streaks(config: ConfigOpt = None, as_json: JsonOpt = False) -> None:
    """Writing streaks and word counts."""
    OUT.as_json = as_json
    with _guard(), _open(config) as (_, conn):
        s = store.writing_stats(conn)
    if as_json:
        _emit(asdict(s))
        return
    if not s.entries:
        console.print("No entries indexed yet. Run 'journal index' first.")
        return
    console.print(f"Entries         : {s.entries} across {s.days_written} days")
    console.print(f"Words           : {s.words:,} (avg {s.avg_words:g}/entry)")
    console.print(f"First / last    : {s.first_day} .. {s.last_day}")
    console.print(
        f"Current streak  : {s.current_streak} day(s), ending {s.last_day}"
    )
    console.print(
        f"Longest streak  : {s.longest_streak} day(s), ending {s.longest_streak_end}"
    )


@app.command(name="on-this-day")
def on_this_day(
    config: ConfigOpt = None,
    day: Annotated[
        str | None,
        typer.Option("--date", help="Anchor date (YYYY-MM-DD); default today.", autocompletion=completion.days),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """What you wrote on this date in earlier years."""
    OUT.as_json = as_json
    anchor = _parse_date(day, "--date") if day is not None else date.today()
    with _guard(), _open(config) as (_, conn):
        rows = [
            r for r in store.entries_on_month_day(conn, anchor.month, anchor.day)
            if r["entry_date"] != anchor.isoformat()
        ]
        if as_json:
            _emit({"date": anchor, "entries": serialize.entry_briefs(conn, rows)})
            return
        if not rows:
            console.print(
                f"Nothing written on {anchor.strftime('%B %d')} in other years."
            )
            return
        console.print(f"[bold]On {anchor.strftime('%B %d')} in other years:[/bold]")
        console.print(_entry_table(conn, rows))


@app.command()
def similar(
    day: Annotated[
        str, typer.Argument(help="Entry date (YYYY-MM-DD).", autocompletion=completion.days)
    ],
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max results.")] = 5,
    as_json: JsonOpt = False,
) -> None:
    """Find entries similar to the one on a given day."""
    OUT.as_json = as_json
    d = _parse_date(day, "DATE")
    with _guard(), _open(config) as (_, conn):
        rows = store.similar_to_day(conn, d, limit)
        if rows is None:
            _fail(f"No indexed entry on {d}.", red=False)
        if as_json:
            _emit({"date": d, "entries": serialize.entry_briefs(conn, rows)})
            return
        if not rows:
            console.print(
                "No similar entries found. If the entry is new, run 'journal index' "
                "so it gets embedded."
            )
            return
        console.print(f"[bold]Entries similar to {d}:[/bold]")
        console.print(_entry_table(conn, rows))


@app.command(name="browse")
def browse_cmd(
    config: ConfigOpt = None,
    port: Annotated[
        int, typer.Option("--port", min=0, max=65535, help="Port on 127.0.0.1.")
    ] = browse.DEFAULT_PORT,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Don't open a browser window.")
    ] = False,
) -> None:
    """Browse everything the cache knows, and ask about it, in a local web page."""
    with _guard():
        cfg = load_config(config)

        def ready(url: str) -> None:
            console.print(f"Journal browser running at [bold cyan]{url}[/bold cyan]")
            console.print("[dim]Your notes are never changed. Press Ctrl-C to stop.[/dim]")

        browse.serve(cfg, port=port, open_browser=not no_open, on_ready=ready)


@app.command(name="mcp")
def mcp_cmd(config: ConfigOpt = None) -> None:
    """Serve the journal as read-only MCP tools on stdin and stdout, for a local MCP client."""
    with _guard():
        cfg = load_config(config)
        # fail here, where the client shows it, rather than on every call
        db.connect_readonly(cfg).close()
        mcp_server.serve(cfg)


@app.command(name="export")
def export_cmd(
    config: ConfigOpt = None,
    fmt: Annotated[
        ExportFormat, typer.Option("--format", help="Output format.")
    ] = ExportFormat.json,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write to this file instead of stdout."),
    ] = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    about: AboutOpt = None,
    tag: TagOpt = None,
    group: GroupOpt = None,
    include_raw: Annotated[
        bool, typer.Option("--include-raw", help="Include full entry text.")
    ] = False,
) -> None:
    """Export entries as JSON, Markdown or CSV."""
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        only = _entry_filter(conn, about, tag, group)
        entries = export.collect_entries(
            conn, date_from, date_to, include_raw=include_raw, only=only
        )
    if not entries:
        console.print("No entries in the requested range.")
        raise typer.Exit(1)
    if fmt is ExportFormat.csv:
        # print ends the last row itself
        text = export.to_csv(entries) if out is not None else export.to_csv(entries).removesuffix("\n")
        # Excel reads a CSV file without one in the ANSI code page
        bom = out is not None
    else:
        text = export.to_json(entries) if fmt is ExportFormat.json else export.to_markdown(entries)
        bom = False
    written = _write_out(cfg, out, text, bom=bom)
    if written is not None:
        console.print(f"Wrote {len(entries)} entries to {escape(str(written))}")


def _write_out(cfg: Config, out: Path | None, text: str, bom: bool = False) -> Path | None:
    """Print to stdout, or write to a file outside the vault."""
    if out is None:
        # plain print so rich doesn't read markup or rewrap the text
        print(text)
        return None
    out = out.expanduser().resolve()
    if out.is_relative_to(cfg.vault.path):
        console.print(
            "[red]Not writing inside the vault. Pick a path outside it.[/red]"
        )
        raise typer.Exit(1)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8-sig" if bom else "utf-8")
    except OSError as exc:
        console.print(f"[red]Could not write {escape(str(out))}: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    return out


@app.command(name="compare")
def compare_cmd(
    first: Annotated[
        str,
        typer.Argument(
            help="A year (2024), month (2024-03) or ISO week (2024-W10).",
            autocompletion=completion.periods,
        ),
    ],
    second: Annotated[
        str | None,
        typer.Argument(
            help="The period to compare it with. Leave out for the one just before.",
            autocompletion=completion.periods,
        ),
    ] = None,
    config: ConfigOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Two periods side by side: writing, mood, metrics, tags and the notes you linked."""
    OUT.as_json = as_json
    kind, key, start, end = _parse_period(first)
    if second is None:
        periods = (compare.previous(kind, start), (key, start, end))
    else:
        _, other, other_start, other_end = _parse_period(second)
        periods = ((key, start, end), (other, other_start, other_end))
    with _guard(), _open(config) as (cfg, conn):
        result = compare.compare(conn, *periods)
    a, b = result.a, result.b
    if not a.writing.entries and not b.writing.entries:
        _fail(f"No entries in {a.label} or {b.label}.", red=False)
    if as_json:
        _emit(asdict(result))
        return
    _print_comparison(cfg, result)


def _share(n: int, share: float) -> str:
    return f"{n} ({share:.0%})" if n else "-"


def _print_comparison(cfg: Config, result: compare.Comparison) -> None:
    a, b = result.a, result.b
    table = Table(title=f"{escape(a.label)} and {escape(b.label)}")
    table.add_column("")
    table.add_column(escape(a.label), justify="right")
    table.add_column(escape(b.label), justify="right")
    rows = [
        ("Entries", a.writing.entries, b.writing.entries),
        ("Days written", a.writing.days_written, b.writing.days_written),
        ("Words", f"{a.writing.words:,}", f"{b.writing.words:,}"),
        ("Words per entry", f"{a.writing.avg_words:g}", f"{b.writing.avg_words:g}"),
        (f"Average mood (1..{cfg.indexing.mood_scale_max})", _mood_str(a.mood), _mood_str(b.mood)),
        ("Longest streak", _count(a.writing.longest_streak, "day"), _count(b.writing.longest_streak, "day")),
    ]
    for key in sorted(set(a.metrics) | set(b.metrics)):
        before, after = a.metrics.get(key), b.metrics.get(key)
        rows.append((
            f"{key} (average)",
            f"{before[0]:g}" if before else "-",
            f"{after[0]:g}" if after else "-",
        ))
    for label, x, y in rows:
        table.add_row(escape(label), str(x), str(y))
    console.print(table)
    if not a.writing.entries or not b.writing.entries:
        empty = a if not a.writing.entries else b
        console.print(f"[dim]No entries in {escape(empty.label)}, so there's nothing to set against it.[/dim]")
        return
    lists = (
        ("Tags you wrote more of", result.tags_up),
        ("Tags you wrote less of", result.tags_down),
        ("Notes you linked more", result.notes_up),
        ("Notes you linked less", result.notes_down),
        (f"Notes new in {b.label}", result.notes_new),
        (f"Notes not linked in {b.label}", result.notes_gone),
    )
    for title, changes in lists:
        if not changes:
            continue
        part = Table(title=escape(title))
        part.add_column("Name", style="cyan")
        part.add_column(escape(a.label), justify="right")
        part.add_column(escape(b.label), justify="right")
        for c in changes:
            part.add_row(escape(c.name), _share(c.a, c.a_share), _share(c.b, c.b_share))
        console.print(part)
    console.print("[dim]Shares are of each period's entries, so periods with more writing compare fairly.[/dim]")


@app.command(name="review")
def review_cmd(
    period: Annotated[
        str | None,
        typer.Argument(
            help="A year (2025), month (2025-03) or ISO week (2025-W10). Default: the latest year you wrote in.",
            autocompletion=completion.periods,
            show_default=False,
        ),
    ] = None,
    config: ConfigOpt = None,
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the Markdown to this file instead of stdout."),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """A year, month or week in review as Markdown: mood, best and hardest days, people, tags."""
    OUT.as_json = as_json
    if as_json and out is not None:
        _fail("--out writes Markdown. Leave out --json, or send the JSON to a file with >.")
    if period is not None:
        _parse_period(period)
    with _guard(), _open(config) as (cfg, conn):
        if period is None:
            _, last = store.date_bounds(conn, None, None)
            if last is None:
                _fail("No entries indexed yet. Run 'journal index' first.", red=False)
            period = last[:4]
        built = review.build(cfg, conn, period)
        kind, key, _, _ = store.parse_period(period)
        if built is None:
            _fail(f"No entries in {store.period_title(kind, key)}.", red=False)
        if as_json:
            _emit(review.to_json(conn, built))
            return
    written = _write_out(cfg, out, review.to_markdown(built))
    if written is not None:
        console.print(f"Wrote the {built.title} review to {escape(str(written))}")


def _themes_note(found: themes.Status) -> str:
    made = f"Worked out {_when(found.made_at)} from {_count(found.grouped, 'entry', 'entries')}"
    if found.placed:
        made += f"; {_count(found.placed, 'entry', 'entries')} written since go with the nearest theme"
    return made + "."


@app.command(name="themes")
def themes_cmd(
    theme_id: Annotated[
        int | None, typer.Argument(help="Show this theme: what it's about, when, and its entries.", show_default=False)
    ] = None,
    config: ConfigOpt = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Group the entries again and name the groups (one fast-model call each).")
    ] = False,
    count: Annotated[
        int | None,
        typer.Option("--count", min=2, max=40, help="How many themes to look for with --refresh. Default: [themes] count.", show_default=False),
    ] = None,
    as_json: JsonOpt = False,
    no_memo: NoMemoOpt = False,
) -> None:
    """Threads that run through the journal, found from what the entries are about."""
    OUT.as_json = as_json
    if count is not None and not refresh:
        _fail("--count only goes with --refresh.")
    with _guard(), _open(config) as (cfg, conn):
        if refresh:
            try:
                with redirect_stdout(sys.stderr) if as_json else nullcontext(), _working("Grouping your entries...") as status:
                    done = themes.refresh(
                        cfg, conn, OllamaClient(_memo(cfg, no_memo)), count, on_step=_show_stage(status),
                    )
            except themes.ThemesError as exc:
                _fail(str(exc), red=False)
            with redirect_stdout(sys.stderr) if as_json else nullcontext():
                console.print(f"Found {_count(done.themes, 'theme')} in {_count(done.entries, 'entry', 'entries')}.")
                if done.naming_error:
                    console.print(
                        f"[yellow]Named {done.named} of them before the model stopped answering "
                        f"({escape(done.naming_error)}). 'journal themes --refresh' names the rest "
                        "once Ollama is back; the ones named already come from the memo.[/yellow]"
                    )
        found = themes.status(conn, cfg)
        if theme_id is not None:
            shown = themes.detail(conn, theme_id)
            if shown is None:
                _fail(f"There's no theme {theme_id}. 'journal themes' lists them.", red=False)
            if as_json:
                _emit({
                    **themes.theme_json(shown.theme),
                    "central": serialize.entry_briefs(conn, shown.central),
                    "newest": serialize.entry_briefs(conn, shown.newest),
                    "tags": [{"name": n, "entries": c} for n, c in shown.tags],
                    "notes": [{"id": i, "title": t, "kind": k, "entries": c} for i, t, k, c in shown.notes],
                    "status": themes.status_json(found),
                })
                return
            t = shown.theme
            console.print(f"[bold cyan]{escape(t.label)}[/bold cyan]")
            if t.about:
                console.print(escape(t.about))
            console.print(
                f"[dim]{_count(t.entries, 'entry', 'entries')}, {t.first} .. {t.last}, "
                f"average mood {_mood_str(t.mood)}[/dim]"
            )
            years: dict[str, int] = {}
            for month, n in t.months.items():
                years[month[:4]] = years.get(month[:4], 0) + n
            top = max(years.values(), default=1)
            for year, n in years.items():
                console.print(f"  {year}  [cyan]{'█' * max(1, round(n / top * MOOD_BAR_WIDTH))}[/cyan] {n}")
            if shown.tags:
                console.print(f"\n[bold]Tags[/bold]  {escape(', '.join(f'{n} ({c})' for n, c in shown.tags))}")
            if shown.notes:
                console.print(f"[bold]Notes[/bold] {escape(', '.join(f'{t} ({c})' for _, t, _, c in shown.notes))}")
            console.print()
            console.print(_entry_table(conn, shown.central, title="Closest to its middle"))
            return
        listed = themes.overview(conn)
        if as_json:
            _emit({"status": themes.status_json(found), "themes": [themes.theme_json(t) for t in listed]})
            return
        if not listed:
            console.print(
                "No themes yet. 'journal themes --refresh' groups your entries by what they're about "
                "(no model needed for that) and names each group with one call to the fast model."
            )
            return
        table = Table(title="Themes, biggest first")
        table.add_column("#", justify="right", style="cyan")
        table.add_column("Theme")
        table.add_column("Entries", justify="right")
        table.add_column("First .. last", no_wrap=True)
        table.add_column("Busiest month", no_wrap=True)
        table.add_column("Mood", justify="right")
        for t in listed:
            busiest = max(t.months.items(), key=lambda kv: (kv[1], kv[0]), default=None)
            table.add_row(
                str(t.id), escape(t.label), str(t.entries), f"{t.first} .. {t.last}",
                f"{busiest[0]} ({busiest[1]})" if busiest else "-", _mood_str(t.mood),
            )
        console.print(table)
        console.print(f"[dim]{escape(_themes_note(found))} 'journal themes N' shows one.[/dim]")
        if found.stale:
            console.print("[yellow]That's a lot written since; 'journal themes --refresh' groups them again.[/yellow]")


_HISTORY_OFF = (
    "History is off, so no questions are kept. Set [history] enabled = true in "
    "config.toml to keep them."
)


@app.command(name="history")
def history_cmd(
    kept_id: Annotated[
        int | None, typer.Argument(help="Show this question and its answer again.", show_default=False)
    ] = None,
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many questions to list.")] = 20,
    delete: Annotated[
        int | None, typer.Option("--delete", help="Forget this question and its answer for good.", show_default=False)
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Delete the whole history file, and everything in it, for good.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask before --clear.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Your earlier questions and answers, kept with [history] enabled = true."""
    OUT.as_json = as_json
    with _guard():
        cfg = load_config(config)
        path = history.history_path(cfg)
        if clear:
            if not path.is_file():
                if as_json:
                    _emit({"cleared": 0, "path": str(path)})
                else:
                    console.print("There's no history file, so there's nothing to delete.")
                return
            if not yes:
                if as_json:
                    _fail("--clear with --json needs --yes, since there's no one to ask.")
                if not typer.confirm(f"Delete {path} and every question in it, for good?"):
                    raise typer.Exit(1)
            try:
                held = history.clear(cfg)
            except OSError as exc:
                _fail(f"Couldn't delete {path}: {exc}. Is 'journal browse' or 'journal ask' still running?")
            if as_json:
                _emit({"cleared": held, "path": str(path)})
            else:
                what = "" if held is None else f", and the {_count(held, 'question')} in it"
                console.print(f"Deleted {path}{what}.")
            return
        if delete is not None:
            if not history.delete(cfg, delete):
                _fail(f"There's no question {delete} in your history.", red=False)
            if as_json:
                _emit({"deleted": delete})
            else:
                console.print(f"Forgot question {delete} and its answer.")
            return
        if kept_id is not None:
            found = history.get(cfg, kept_id)
            if found is None:
                _fail(f"There's no question {kept_id} in your history.", red=False)
            if as_json:
                _emit(found.as_json())
                return
            console.print(f"[bold cyan]{escape(found.question)}[/bold cyan]")
            details = [f"asked {_when(found.asked_at)} in journal {found.source}"]
            if found.scope:
                details.append(f"only {found.scope}")
            console.print(f"[dim]{escape('; '.join(details))}[/dim]")
            if found.standalone:
                console.print(f"[dim]read as: {escape(found.standalone)}[/dim]")
            console.print()
            console.print(escape(found.answer))
            by = f", by {found.model}" if found.model else ""
            console.print(f"[dim](answered by stage: {found.stage}{escape(by)})[/dim]")
            took = query.timing_line(found.timings)
            if took:
                console.print(f"[dim]{escape(took)}[/dim]")
            return
        rows = history.recent(cfg, limit)
        if as_json:
            _emit({
                "enabled": cfg.history.enabled, "path": str(path),
                "questions": [r.as_json() for r in rows],
            })
            return
        if not rows:
            console.print(_HISTORY_OFF if not cfg.history.enabled else "No questions kept yet.")
            return
        table = Table(title="Questions you asked, newest first")
        table.add_column("#", justify="right", style="cyan")
        table.add_column("When", no_wrap=True)
        table.add_column("Question")
        table.add_column("From")
        for r in rows:
            table.add_row(str(r.id), _when(r.asked_at), escape(r.question), r.source)
        console.print(table)
        console.print(
            "[dim]'journal history N' shows one again; 'journal ask --after N' follows it up; "
            "--delete N forgets one, --clear all of them.[/dim]"
        )
        if not cfg.history.enabled:
            console.print(
                "[yellow]History is off, so nothing new is kept, but these still are. "
                "'journal history --clear' deletes them.[/yellow]"
            )


@app.command(name="insights")
def insights_cmd(
    config: ConfigOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
) -> None:
    """Look for mood patterns across tags, people and time."""
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn), _working("Looking for patterns..."):
        text, found = insights.generate_insights(
            cfg, conn, OllamaClient(cfg), date_from, date_to
        )
    console.print(escape(text))
    if found.findings:
        console.print("\n[dim]Signals considered:[/dim]")
        for line in found.evidence_lines():
            console.print(f"[dim]- {escape(line)}[/dim]")


def _ask_loop(
    cfg: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    since: date | None = None,
    until: date | None = None,
    only: store.EntryFilter | None = None,
    earlier: list[history.Kept] | None = None,
) -> None:
    console.print(
        "Ask one question after another; follow-ups like 'what about the month after "
        "that?' work. Type 'sources' to see what the last answer read, and 'exit' or "
        "Ctrl-D to quit."
    )
    turns = _turns(earlier or [])
    conversation = earlier[-1].conversation if earlier else history.new_conversation()
    if earlier:
        console.print(
            f"[dim]Following on from {_when(earlier[-1].asked_at)}, after "
            f"\"{escape(earlier[-1].question)}\".[/dim]"
        )
    scope = history.scope_text(conn, only, since, until)
    last: Answer | None = None
    while True:
        try:
            raw = console.input("[bold cyan]you>[/bold cyan] ")
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        question = raw.strip()
        if not question:
            continue
        if question.lower() in {"exit", "quit", ":q"}:
            break
        if question.lower() in {"sources", ":sources"}:
            if last is None:
                console.print("Ask something first, then 'sources' lists what the answer read.")
            else:
                _print_reading(conn, last)
            continue
        stream: _Streamed | None = None
        try:
            with _working("Resolving your follow-up..." if turns else "Thinking...") as status:
                stream = _Streamed(status)
                result, standalone = _answer_turn(
                    cfg, conn, client, question, turns, since, until, only, status, stream
                )
        except JournalError as exc:
            if stream is not None and stream.started:
                console.print()
            console.print(f"[red]{escape(str(exc))}[/red]")
            continue
        except KeyboardInterrupt:
            if stream is not None and stream.started:
                console.print()
            # Ctrl-C drops this answer but keeps the conversation
            console.print("[dim](cancelled)[/dim]")
            continue
        _print_answer(conn, result, "stage", streamed=stream.started)
        last = result
        history.record(cfg, question, result, "ask", conversation, standalone, scope)
        # keep the rewritten question so the next follow-up can build on it
        turns.append((standalone, result.text))
        del turns[: -query.CHAT_HISTORY_TURNS]


@app.command()
def prompt(
    config: ConfigOpt = None,
    days: Annotated[
        int, typer.Option("--days", min=1, help="How many days back to draw threads from.")
    ] = 14,
    count: Annotated[
        int, typer.Option("--count", min=1, help="How many prompts to suggest.")
    ] = 4,
) -> None:
    """Suggest things to write about, based on recent entries."""
    with (
        _guard(),
        _open(config) as (cfg, conn),
        _working("Reading your recent entries..."),
    ):
        suggestions = prompts.generate_prompts(
            cfg, conn, OllamaClient(cfg), days=days, count=count
        )
    if not suggestions:
        console.print(f"No entries in the last {days} days to draw prompts from.")
        return
    console.print(f"Prompts for your next entry (from the last {days} days):\n")
    for i, text in enumerate(suggestions, start=1):
        console.print(f"  {i}. {escape(text)}")


@app.command()
def doctor(config: ConfigOpt = None) -> None:
    """Check the vault, Ollama, the configured models and sqlite-vec."""
    with _guard():
        cfg = load_config(config)
        # don't stop at the first failure
        checks = [_report_vault(cfg), _report_ollama(cfg), _report_sqlite_vec()]
        if cfg.vault.path.is_dir():
            _report_obsidian(cfg)
        if not all(checks):
            raise typer.Exit(1)


def _report_obsidian(cfg: Config) -> None:
    advice = obsidian.check(cfg)
    if advice is None:
        console.print("[dim]No .obsidian folder in the vault, so no Obsidian settings to check.[/dim]")
        return
    for item in advice:
        label = "[green]OK[/green]" if item.ok else "[yellow]WARN[/yellow]"
        console.print(f"{label} {escape(item.message)}")
        if item.toml:
            console.print(f"     in config.toml: {escape(item.toml)}")


def _report_ollama(cfg: Config) -> bool:
    try:
        available = OllamaClient(cfg).list_models()
    except JournalError as exc:
        console.print(f"[red]FAIL[/red] {escape(str(exc))}")
        return False
    console.print(f"[green]OK[/green] Ollama reachable at {escape(cfg.ollama.host)}")
    roles = (
        ("fast_model", cfg.ollama.fast_model),
        ("precise_model", cfg.ollama.precise_model),
        ("embed_model", cfg.ollama.embed_model),
        *((("vision_model", cfg.ollama.vision_model),) if cfg.ollama.vision_model else ()),
    )
    # a list so all() doesn't stop at the first missing model
    return all([_report_model(role, name, available) for role, name in roles])


def _report_sqlite_vec() -> bool:
    try:
        # in memory, so doctor doesn't create the cache file
        with closing(sqlite3.connect(":memory:")) as probe:
            db.load_vec(probe)
            probe.execute("SELECT vec_version()")
    except (JournalError, sqlite3.Error) as exc:
        console.print(f"[red]FAIL[/red] sqlite-vec: {escape(str(exc))}")
        return False
    console.print("[green]OK[/green] sqlite-vec extension loads")
    return True


def _report_vault(cfg: Config) -> bool:
    root = cfg.vault.path
    if not root.is_dir():
        console.print(f"[red]FAIL[/red] vault.path: there is no folder at {escape(str(root))}")
        return False
    console.print(f"[green]OK[/green] vault found at {escape(str(root))}")
    for configured in cfg.notes.paths:
        if not (root / configured).exists():
            console.print(
                f"[yellow]WARN[/yellow] notes path '{escape(configured)}' "
                "is not in the vault"
            )
    return True


def _report_model(role: str, name: str, available: list[str]) -> bool:
    pulled = any(same_model(m, name) for m in available)
    if pulled:
        console.print(f"[green]OK[/green] {role} '{escape(name)}' is pulled")
    else:
        console.print(
            f"[red]MISSING[/red] {role} '{escape(name)}', run: ollama pull {escape(name)}"
        )
    return pulled


if __name__ == "__main__":
    app()
