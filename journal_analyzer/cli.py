from __future__ import annotations

import io
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.status import Status
from rich.style import Style
from rich.table import Table
from rich.text import Text

from . import (
    browse,
    db,
    embedder,
    export,
    fields,
    ingester,
    insights,
    parser,
    prompts,
    query,
    rollups,
    store,
    unlinked,
)
from .config import Config, load_config
from .models import Answer, JournalError, OllamaUnavailableError
from .ollama_client import OllamaClient, same_model

app = typer.Typer(help="Ask questions about an Obsidian journal using local Ollama models.")
console = Console()

BYTES_PER_MB = 1_048_576
MOOD_BAR_WIDTH = 20


class ExportFormat(StrEnum):
    json = "json"
    markdown = "markdown"


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
    typer.Option("--about", help="Only entries that link this note (its title or an alias)."),
]
TagOpt = Annotated[str | None, typer.Option("--tag", help="Only entries with this tag.")]


@app.callback()
def _utf8_output() -> None:
    # Windows encodes redirected output in the ANSI code page, so
    # `journal export > out.json` crashed on the first emoji.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8")


@contextmanager
def _guard() -> Iterator[None]:
    try:
        yield
    except JournalError as exc:
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
        console.print(
            f"[red]{flag} must be an ISO date (YYYY-MM-DD), got '{escape(value)}'.[/red]"
        )
        raise typer.Exit(1) from None


def _parse_range(since: str | None, until: str | None) -> tuple[date | None, date | None]:
    date_from = _parse_date(since, "--since") if since is not None else None
    date_to = _parse_date(until, "--until") if until is not None else None
    if date_from and date_to and date_from > date_to:
        console.print(f"[red]--since {date_from} is after --until {date_to}.[/red]")
        raise typer.Exit(1)
    return date_from, date_to


def _entry_filter(
    conn: sqlite3.Connection, about: str | None, tag: str | None
) -> store.EntryFilter | None:
    note_ids: tuple[int, ...] = ()
    if about is not None:
        matches = store.find_notes(conn, about)
        if not matches:
            console.print(
                f"[red]No reference note called '{escape(about)}'. Notes come from the "
                "folders listed in notes.paths in config.toml.[/red]"
            )
            raise typer.Exit(1)
        if len(matches) > 1:
            console.print(f"'{escape(about)}' matches {len(matches)} notes; use a full title:")
            console.print(_note_table(conn, matches))
            raise typer.Exit(1)
        note_ids = (matches[0]["id"],)
    name = tag.strip().lstrip("#").strip().lower() if tag is not None else None
    if name is not None and not store.tag_exists(conn, name):
        console.print(f"[red]No entry is tagged '{escape(name)}'. 'journal tags' lists them.[/red]")
        raise typer.Exit(1)
    return store.EntryFilter(name, note_ids) if name or note_ids else None


def _filter_label(conn: sqlite3.Connection, only: store.EntryFilter | None) -> str:
    if not only:
        return ""
    parts = []
    if only.note_ids:
        parts.append("linking " + ", ".join(r["title"] for r in store.notes_by_ids(conn, list(only.note_ids))))
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
    conn: sqlite3.Connection, rows: list[sqlite3.Row], title: str | None = None
) -> Table:
    tags_by_entry = store.tags_for_entries(conn, [r["id"] for r in rows])
    table = Table(show_lines=False, title=title)
    table.add_column("Date", style="cyan", no_wrap=True)
    table.add_column("Mood", justify="right")
    table.add_column("Tags")
    table.add_column("Summary")
    for r in rows:
        table.add_row(
            r["entry_date"],
            _mood_str(r["mood_score"]),
            escape(", ".join(tags_by_entry.get(r["id"], []))),
            escape(r["summary"] or "(no summary)"),
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
) -> None:
    """Index the vault. Unchanged files are skipped, so re-running is cheap."""
    if dry_run:
        with _guard():
            cfg = _with_vault(load_config(config), vault)
            if not db.cache_has_data(cfg):
                _print_plan(
                    ingester.plan_backfill(cfg, None, force=force), cfg.vault.path, cache=False
                )
                return
            with closing(db.connect_readonly(cfg)) as conn:
                _print_plan(
                    ingester.plan_backfill(cfg, conn, force=force), cfg.vault.path, cache=True
                )
        return
    with _guard(), _open(config) as (cfg, conn):
        ingester.run_backfill(_with_vault(cfg, vault), conn, force=force)


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
        f"Files found     : {len(plan.files)} ({len(plan.files) - notes} journal, "
        f"{notes} notes), {plan.ignored} ignored"
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
    console.print(f"Unchanged       : {by_status['unchanged']} (skipped)")
    if plan.dropped:
        console.print(f"Would drop      : {_count(len(plan.dropped), 'file')} no longer in the vault")
    extractions, summaries, embeddings = plan.model_calls()
    console.print(
        f"Model calls     : at least {extractions + summaries + embeddings} "
        f"({_count(extractions, 'extraction')}, {_count(summaries, 'note summary', 'note summaries')}, "
        f"{_count(embeddings, 'embedding')})"
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
def watch(config: ConfigOpt = None) -> None:
    """Keep indexing files as they change, until Ctrl-C."""
    with _guard(), _open(config) as (cfg, conn):
        ingester.run_watch(cfg, conn)


def _source_link(label: str, path: str | None) -> Text:
    return Text(label, style=Style(link=parser.obsidian_url(path)) if path else "")


def _print_answer(conn: sqlite3.Connection, answer: Answer, stage_label: str) -> None:
    console.print(escape(answer.body))
    links: list[Text] = []
    for day in answer.cited_dates:
        rows = store.entries_in_range(conn, day, day)
        links.append(_source_link(f"[[{day.isoformat()}]]", rows[0]["file_path"] if rows else None))
    for title in answer.cited_notes:
        notes = store.find_notes(conn, title)
        links.append(_source_link(f"[[{title}]]", notes[0]["file_path"] if notes else None))
    if links:
        console.print()
        console.print(Text(" ").join([Text("Sources:"), *links]))
    console.print(f"[dim]({stage_label}: {answer.stage_used})[/dim]")


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
    if entries:
        console.print(_entry_table(conn, entries, title="Entries it read"))
    if notes:
        console.print(_note_table(conn, notes, title="Notes it read"))


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="The question, in plain words.")],
    config: ConfigOpt = None,
    sources: Annotated[
        bool,
        typer.Option("--sources", help="Also list the entries and notes the answer was built from."),
    ] = False,
    since: SinceOpt = None,
    until: UntilOpt = None,
    about: AboutOpt = None,
    tag: TagOpt = None,
) -> None:
    """Ask a question about your journal.

    --since, --until, --about and --tag replace whatever the question's wording
    suggests, so use them when you know the dates or the person.
    """
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        only = _entry_filter(conn, about, tag)
        with _working("Thinking...") as status:
            result = query.answer(
                question, cfg, conn, OllamaClient(cfg), on_stage=_show_stage(status),
                since=date_from, until=date_to, only=only,
            )
        _print_answer(conn, result, "answered by stage")
        if sources:
            _print_reading(conn, result)


@app.command()
def rollup(
    config: ConfigOpt = None,
    rebuild_all: Annotated[
        bool, typer.Option("--rebuild-all", help="Regenerate all digests, not just stale ones.")
    ] = False,
) -> None:
    """Write any missing or outdated week, month and year digests."""
    with _guard(), _open(config) as (cfg, conn):
        counts = rollups.generate_rollups(
            cfg, conn, OllamaClient(cfg), only_stale=not rebuild_all
        )
    console.print(
        f"Generated {counts['week']} week, {counts['month']} month, "
        f"{counts['year']} year and {counts['note']} relationship rollups."
    )


@app.command()
def stats(config: ConfigOpt = None) -> None:
    """Show counts, the date range, failed files and the cache size."""
    with _guard(), _open(config) as (cfg, conn):
        s = store.stats(conn)
        size_mb = cfg.db.path.stat().st_size / BYTES_PER_MB if cfg.db.path.exists() else 0
        kinds = ", ".join(f"{r['kind']} {r['n']}" for r in store.notes_by_kind(conn))
        console.print(f"Entries indexed : {s.entries} ({s.words:,} words)")
        console.print(f"Notes indexed   : {s.notes}" + (f" ({escape(kinds)})" if kinds else ""))
        console.print(f"Files tracked   : {s.files} ({s.errored_files} errored)")
        console.print(f"Date range      : {s.date_from or '-'} .. {s.date_to or '-'}")
        console.print(f"Tags / links    : {s.tags} / {s.links}")
        console.print(f"Rollup digests  : {s.rollups} (and {s.note_digests} for notes)")
        console.print(f"DB size         : {size_mb:.2f} MB ({escape(str(cfg.db.path))})")
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
) -> None:
    """Search entries and notes by keyword and meaning."""
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        only = _entry_filter(conn, about, tag)
        with _working("Searching..."):
            results = query.search(
                text, cfg, conn, OllamaClient(cfg),
                limit=limit, date_from=date_from, date_to=date_to, only=only,
            )
        if not results.entries and not results.notes:
            console.print("No matching entries or notes.")
            return
        if results.notes:
            console.print(_note_table(conn, results.notes, title="Related notes"))
        if results.entries:
            console.print(_entry_table(conn, results.entries))


@app.command()
def show(
    day: Annotated[str, typer.Argument(help="Entry date (YYYY-MM-DD).")],
    config: ConfigOpt = None,
    raw: Annotated[
        bool, typer.Option("--raw", help="Also print the stored entry text.")
    ] = False,
) -> None:
    """Show what's stored for one day."""
    d = _parse_date(day, "DATE")
    with _guard(), _open(config) as (cfg, conn):
        rows = store.entries_in_range(conn, d, d)
        if not rows:
            console.print(f"No indexed entry on {d}.")
            raise typer.Exit(1)
        for r in rows:
            own = fields.frontmatter_mood(store.file_frontmatter(conn, r["file_path"]), cfg)
            source = " (yours, from the frontmatter)" if own is not None else ""
            console.print(f"[bold cyan]{r['entry_date']}[/bold cyan]  "
                          f"mood {_mood_str(r['mood_score'])}{source}  "
                          f"({r['word_count']} words)  {escape(r['file_path'])}")
            tags = store.tags_for_entry(conn, r["id"])
            if tags:
                console.print(f"  tags: {escape(', '.join(tags))}")
            ents = store.entities_for_entry(conn, r["id"])
            if ents:
                console.print(
                    "  mentions: "
                    + escape(", ".join(f"{e['name']} ({e['type']})" for e in ents))
                )
            console.print(f"  {escape(r['summary'] or '(no summary)')}")
            if raw:
                console.print()
                console.print(escape(r["raw_text"]))


@app.command()
def note(
    name: Annotated[str, typer.Argument(help="Note title or alias.")],
    config: ConfigOpt = None,
    raw: Annotated[
        bool, typer.Option("--raw", help="Also print the note's own text.")
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="How many linking entries to list.")
    ] = 10,
) -> None:
    """Show a reference note and the entries that link to it."""
    with _guard(), _open(config) as (cfg, conn):
        matches = store.find_notes(conn, name)
        if not matches:
            console.print(
                f"No reference note called '{escape(name)}'. Notes come from the "
                "folders listed in notes.paths in config.toml."
            )
            raise typer.Exit(1)
        if len(matches) > 1:
            console.print(
                f"'{escape(name)}' matches {len(matches)} notes; use a full title:"
            )
            console.print(_note_table(conn, matches))
            raise typer.Exit(1)
        _print_note(cfg, conn, matches[0], raw=raw, limit=limit)


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
    outlinks = store.note_outlinks(conn, row["id"])
    if outlinks:
        console.print("  links to: " + escape(", ".join(r["title"] for r in outlinks)))
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
) -> None:
    """List the most used tags."""
    date_from, date_to = _parse_range(since, until)
    if similar:
        with _guard(), _open(config) as (_, conn):
            _print_similar_tags(fields.similar_tags(store.tag_stats(conn)))
        return
    with _guard(), _open(config) as (_, conn):
        rows = store.top_tags(conn, date_from, date_to, limit=limit)
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
    seen: set[str] = set()
    lines = []
    for rare, _, common, _ in pairs:
        if rare not in seen:
            seen.add(rare)
            lines.append(f'{_toml_key(rare)} = "{common}"')
    # plain print so the [tags] header isn't read as markup
    print("[tags]\nmerge = { " + ", ".join(lines) + " }")


def _toml_key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else f'"{name}"'


@app.command(name="unlinked")
def unlinked_cmd(
    name: Annotated[
        str | None,
        typer.Argument(help="A note's title or alias. Leave out to list every note with some."),
    ] = None,
    config: ConfigOpt = None,
    type_: Annotated[
        str | None,
        typer.Option("--type", help="Only notes of this kind, meaning their folder (e.g. people)."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many rows.")] = 20,
) -> None:
    """Entries that name a note without linking it, so its numbers miss them."""
    with _guard(), _open(config) as (_, conn):
        if name is None:
            with _working("Reading entries for names..."):
                found = unlinked.by_note(conn, type_)
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
        matches = store.find_notes(conn, name)
        if not matches:
            console.print(f"No reference note called '{escape(name)}'.")
            raise typer.Exit(1)
        if len(matches) > 1:
            console.print(f"'{escape(name)}' matches {len(matches)} notes; use a full title:")
            console.print(_note_table(conn, matches))
            raise typer.Exit(1)
        note = matches[0]
        found_entries = unlinked.for_note(conn, note["id"])
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


@app.command()
def entities(
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many notes.")] = 20,
    type_: Annotated[
        str | None,
        typer.Option("--type", help="Only notes of this kind, meaning their folder (e.g. people)."),
    ] = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
) -> None:
    """List the notes your entries link to most."""
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (_, conn):
        rows = store.top_entities(conn, date_from, date_to, limit=limit, entity_type=type_)
        kinds = ", ".join(r["kind"] for r in store.notes_by_kind(conn))
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


@app.command()
def mood(
    config: ConfigOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    about: AboutOpt = None,
    tag: TagOpt = None,
) -> None:
    """Average mood per month."""
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        only = _entry_filter(conn, about, tag)
        rows = store.monthly_mood(conn, date_from, date_to, only)
        label = _filter_label(conn, only)
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
def streaks(config: ConfigOpt = None) -> None:
    """Writing streaks and word counts."""
    with _guard(), _open(config) as (_, conn):
        s = store.writing_stats(conn)
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
        typer.Option("--date", help="Anchor date (YYYY-MM-DD); default today."),
    ] = None,
) -> None:
    """What you wrote on this date in earlier years."""
    anchor = _parse_date(day, "--date") if day is not None else date.today()
    with _guard(), _open(config) as (_, conn):
        rows = [
            r for r in store.entries_on_month_day(conn, anchor.month, anchor.day)
            if r["entry_date"] != anchor.isoformat()
        ]
        if not rows:
            console.print(
                f"Nothing written on {anchor.strftime('%B %d')} in other years."
            )
            return
        console.print(f"[bold]On {anchor.strftime('%B %d')} in other years:[/bold]")
        console.print(_entry_table(conn, rows))


@app.command()
def similar(
    day: Annotated[str, typer.Argument(help="Entry date (YYYY-MM-DD).")],
    config: ConfigOpt = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Max results.")] = 5,
) -> None:
    """Find entries similar to the one on a given day."""
    d = _parse_date(day, "DATE")
    with _guard(), _open(config) as (_, conn):
        day_rows = store.entries_in_range(conn, d, d)
        if not day_rows:
            console.print(f"No indexed entry on {d}.")
            raise typer.Exit(1)
        day_ids = {r["id"] for r in day_rows}
        seen: dict[int, None] = {}
        for r in day_rows:
            for eid in store.similar_entry_ids(conn, r["id"], limit + len(day_rows)):
                if eid not in day_ids:
                    seen.setdefault(eid, None)
        ids = list(seen)[:limit]
        if not ids:
            console.print(
                "No similar entries found. If the entry is new, run 'journal index' "
                "so it gets embedded."
            )
            return
        rows = store.entries_by_ids(conn, ids)
        order = {eid: i for i, eid in enumerate(ids)}
        rows.sort(key=lambda r: order[r["id"]])
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
    """Browse everything the cache knows in a local, read-only web page."""
    with _guard():
        cfg = load_config(config)

        def ready(url: str) -> None:
            console.print(f"Journal browser running at [bold cyan]{url}[/bold cyan]")
            console.print("[dim]Read-only. Press Ctrl-C to stop.[/dim]")

        browse.serve(cfg, port=port, open_browser=not no_open, on_ready=ready)


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
    include_raw: Annotated[
        bool, typer.Option("--include-raw", help="Include full entry text.")
    ] = False,
) -> None:
    """Export entries as JSON or Markdown."""
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        only = _entry_filter(conn, about, tag)
        entries = export.collect_entries(
            conn, date_from, date_to, include_raw=include_raw, only=only
        )
    if not entries:
        console.print("No entries in the requested range.")
        raise typer.Exit(1)
    text = (
        export.to_json(entries)
        if fmt is ExportFormat.json
        else export.to_markdown(entries)
    )
    if out is None:
        # plain print so rich doesn't read markup or rewrap the text
        print(text)
        return
    out = out.expanduser().resolve()
    if out.is_relative_to(cfg.vault.path):
        console.print(
            "[red]Not writing inside the vault. Pick a path outside it.[/red]"
        )
        raise typer.Exit(1)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        console.print(f"[red]Could not write {escape(str(out))}: {escape(str(exc))}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"Wrote {len(entries)} entries to {escape(str(out))}")


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


@app.command()
def chat(
    config: ConfigOpt = None,
    since: SinceOpt = None,
    until: UntilOpt = None,
    about: AboutOpt = None,
    tag: TagOpt = None,
) -> None:
    """Ask questions with follow-ups. The filters apply to every question."""
    date_from, date_to = _parse_range(since, until)
    with _guard(), _open(config) as (cfg, conn):
        only = _entry_filter(conn, about, tag)
        _chat_loop(cfg, conn, OllamaClient(cfg), date_from, date_to, only)


def _chat_loop(
    cfg: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    since: date | None = None,
    until: date | None = None,
    only: store.EntryFilter | None = None,
) -> None:
    console.print(
        "Follow-ups like 'what about the month after that?' work. Type 'sources' to "
        "see what the last answer read, and 'exit' or Ctrl-D to quit."
    )
    history: list[tuple[str, str]] = []
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
        try:
            with _working("Resolving your follow-up..." if history else "Thinking...") as status:
                standalone = query.contextualize_question(question, history, cfg, client)
                result = query.answer(
                    standalone, cfg, conn, client, on_stage=_show_stage(status),
                    since=since, until=until, only=only,
                )
        except JournalError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            continue
        except KeyboardInterrupt:
            # Ctrl-C drops this answer but keeps the chat
            console.print("[dim](cancelled)[/dim]")
            continue
        _print_answer(conn, result, "stage")
        last = result
        # keep the rewritten question so the next follow-up can build on it
        history.append((standalone, result.text))
        del history[: -query.CHAT_HISTORY_TURNS]


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
        if not all(checks):
            raise typer.Exit(1)


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
