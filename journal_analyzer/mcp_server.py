"""`journal mcp`: the journal as read-only tools for a local MCP client.

MCP is JSON-RPC 2.0, one message per line on stdin and stdout, so this needs
nothing beyond the standard library, like `journal browse`. The tools return
the same JSON shapes as the --json commands. Every call opens the cache
read-only, so it can run next to `journal watch`. Anything else that would
print goes to stderr, since stdout carries the protocol.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import asdict
from datetime import date
from typing import IO, Any

from . import (
    __version__,
    compare,
    db,
    fields,
    links,
    parser,
    profile,
    query,
    review,
    serialize,
    store,
    unlinked,
    withhold,
)
from .config import Config
from .models import ConfigError, JournalError
from .ollama_client import OllamaClient

# newest first; the client's version is used when this server knows it
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SEARCH_LIMIT = 10
NOTE_ENTRIES = 10
# defaults the tools share with their commands
LIST_LIMIT = 20
SIMILAR_LIMIT = 5
LAPSED_MIN_LINKS = 5
LAPSED_QUIET_MONTHS = 6
PROFILE_FACTS = 50

Json = dict[str, Any]
ClientFactory = Callable[[Config], OllamaClient]


class ToolError(Exception):
    """A problem with the call itself, reported to the client as a tool error."""


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=serialize.json_default)


def _day(args: Json, key: str, required: bool = False) -> date | None:
    value = args.get(key)
    if value in (None, ""):
        if required:
            raise ToolError(f"'{key}' is required, as a date like 2024-01-15.")
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ToolError(f"'{key}' must be a date like 2024-01-15, not '{value}'.") from None


def _text_arg(args: Json, key: str) -> str:
    value = str(args.get(key) or "").strip()
    if not value:
        raise ToolError(f"'{key}' is required.")
    return value


def _number(args: Json, key: str, default: int, lo: int, hi: int) -> int:
    value = args.get(key)
    if value in (None, ""):
        return default
    try:
        return min(max(int(value), lo), hi)
    except (TypeError, ValueError):
        raise ToolError(f"'{key}' must be a whole number, not '{value}'.") from None


def _period(args: Json, key: str, ranges: bool = False) -> tuple[str, str, date, date]:
    period = _text_arg(args, key)
    try:
        return store.parse_period(period, ranges=ranges)
    except ValueError as exc:
        if ranges and ".." in period:
            raise ToolError(f"{exc}.") from None
        raise ToolError(
            f"'{period}' isn't a period. Use a year (2024), a month (2024-03) or a week (2024-W10)"
            + (", or a span of days (2024-03-02..2024-03-11)." if ranges else ".")
        ) from None


def _one_note(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    found = store.find_notes(conn, name)
    if not found:
        raise ToolError(f"No note is called '{name}'. The 'notes' tool lists them.")
    if len(found) > 1:
        raise ToolError(f"'{name}' matches {len(found)} notes: " + ", ".join(r["title"] for r in found))
    return found[0]


def _filter(conn: sqlite3.Connection, args: Json) -> store.EntryFilter | None:
    """`about` a note, a `tag` and a `group` like relationship=family, as the commands take them."""
    name = str(args.get("about") or "").strip()
    note_ids = (_one_note(conn, name)["id"],) if name else ()
    tag = str(args.get("tag") or "").strip().lstrip("#").strip().lower() or None
    if tag and not store.tag_exists(conn, tag):
        raise ToolError(f"No entry is tagged '{tag}'. The 'tags' tool lists them.")
    group: tuple[str, str] | None = None
    group_ids: tuple[int, ...] = ()
    raw = str(args.get("group") or "").strip()
    if raw:
        key, sep, value = raw.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key or not value:
            raise ToolError(f"'group' takes a property and a value, like relationship=family, not '{raw}'.")
        group_ids = tuple(store.group_note_ids(conn, key, value))
        if not group_ids:
            raise ToolError(f"No note's {key} is '{value}'.")
        group = (key, value)
    only = store.EntryFilter(tag, note_ids, group, group_ids)
    return only if only else None


_SCHEMA_DATE = {"type": "string", "description": "An ISO date, like 2024-01-15."}
_SCHEMA_PERIOD = {"type": "string", "description": "A year (2024), month (2024-03) or ISO week (2024-W10)."}
_SCHEMA_KIND = {"type": "string", "description": "A kind of note, meaning its folder, like people."}
_SCHEMA_LIMIT = {"type": "integer", "minimum": 1, "maximum": 500}
_SCHEMA_FILTER = {
    "about": {"type": "string", "description": "Only entries that link this note."},
    "tag": {"type": "string", "description": "Only entries with this tag."},
    "group": {
        "type": "string",
        "description": "Only entries that link a note with this property value, like relationship=family.",
    },
}

TOOLS: list[Json] = [
    {
        "name": "search",
        "description": "Search the journal's entries and reference notes by keywords and meaning. "
        "Falls back to keywords only when Ollama isn't running.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                "since": _SCHEMA_DATE,
                "until": _SCHEMA_DATE,
            },
            "required": ["query"],
        },
    },
    {
        "name": "ask",
        "description": "Answer a question about the journal the way `journal ask` does, with the "
        "days and notes it drew on. Needs Ollama, and can take minutes on a CPU.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "since": _SCHEMA_DATE,
                "until": _SCHEMA_DATE,
                **_SCHEMA_FILTER,
            },
            "required": ["question"],
        },
    },
    {
        "name": "day",
        "description": "What's stored for one day: mood, tags, summaries, linked notes and the text.",
        "inputSchema": {"type": "object", "properties": {"date": _SCHEMA_DATE}, "required": ["date"]},
    },
    {
        "name": "note",
        "description": "A reference note about a person, place or topic, by title or alias, with "
        "how often it's linked and the latest entries that link it.",
        "inputSchema": {
            "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
        },
    },
    {
        "name": "notes",
        "description": "Every reference note, optionally of one kind (its folder, like people).",
        "inputSchema": {"type": "object", "properties": {"kind": {"type": "string"}}},
    },
    {
        "name": "mood",
        "description": "Average mood by month, optionally for the days that link one note, have "
        "one tag or link a note in one group.",
        "inputSchema": {
            "type": "object",
            "properties": {"since": _SCHEMA_DATE, "until": _SCHEMA_DATE, **_SCHEMA_FILTER},
        },
    },
    {
        "name": "digest",
        "description": "The written digest of a year (2024), month (2024-03), ISO week (2024-W10) "
        "or span of days someone asked for (2024-03-02..2024-03-11).",
        "inputSchema": {
            "type": "object", "properties": {"period": {"type": "string"}}, "required": ["period"],
        },
    },
    {
        "name": "on_this_day",
        "description": "What was written on this date in other years; today by default.",
        "inputSchema": {"type": "object", "properties": {"date": _SCHEMA_DATE}},
    },
    {
        "name": "broken_links",
        "description": "Links in the journal that find no note or day, with why and what to do.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "compare",
        "description": "Two periods side by side: entries, words, mood, streaks, frontmatter numbers, "
        "and the tags and notes that rose, fell, appeared or went. Given one period, it's compared "
        "with the one just before.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "first": _SCHEMA_PERIOD,
                "second": {**_SCHEMA_PERIOD, "description": "Leave out for the period just before the first."},
            },
            "required": ["first"],
        },
    },
    {
        "name": "review",
        "description": "A year, month or week in review: its digest, mood by month or week, best "
        "and hardest days, the notes and tags that came up, and what was new. The latest year by default.",
        "inputSchema": {"type": "object", "properties": {"period": _SCHEMA_PERIOD}},
    },
    {
        "name": "metric",
        "description": "Numbers kept in entry frontmatter, like sleep: 7. Without a key, lists them "
        "with their range; with one, its average by month.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Which number, like sleep."},
                "since": _SCHEMA_DATE,
                "until": _SCHEMA_DATE,
                **_SCHEMA_FILTER,
            },
        },
    },
    {
        "name": "similar",
        "description": "The entries closest in meaning to the one on a day.",
        "inputSchema": {
            "type": "object",
            "properties": {"date": _SCHEMA_DATE, "limit": {"type": "integer", "minimum": 1, "maximum": 50}},
            "required": ["date"],
        },
    },
    {
        "name": "tags",
        "description": "The most used tags, or with similar set, tags that look like the same thing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": _SCHEMA_DATE, "until": _SCHEMA_DATE, "limit": _SCHEMA_LIMIT,
                "similar": {"type": "boolean"},
            },
        },
    },
    {
        "name": "lapsed",
        "description": "Notes linked often that haven't been linked lately, counted back from the "
        "latest entry rather than today.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": _SCHEMA_KIND,
                "min": {"type": "integer", "minimum": 1, "description": "Linked from at least this many entries."},
                "quiet_months": {"type": "integer", "minimum": 1, "description": "Months without a link."},
                "limit": _SCHEMA_LIMIT,
            },
        },
    },
    {
        "name": "unlinked",
        "description": "Entries that name a note without linking it. Without a name, lists the notes "
        "this happens to; with one, that note's entries and the sentence that names it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "A note's title or alias."},
                "kind": _SCHEMA_KIND,
                "limit": _SCHEMA_LIMIT,
            },
        },
    },
    {
        "name": "profile",
        "description": "The short profile of the writer that the journal keeps (names, likes, "
        "routines and so on), with the newest facts it was built from.",
        "inputSchema": {
            "type": "object",
            "properties": {"facts": {"type": "integer", "minimum": 0, "maximum": 500}},
        },
    },
]


class Server:
    def __init__(self, config: Config, make_client: ClientFactory = OllamaClient):
        """ConfigError if [mcp] tools names a tool there isn't."""
        self.config = config
        self.make_client = make_client
        names = [t["name"] for t in TOOLS]
        unknown = [t for t in config.mcp.tools if t not in names]
        if unknown:
            raise ConfigError(
                f"[mcp] tools in config.toml names {', '.join(repr(t) for t in unknown)}, which "
                f"{'isn' if len(unknown) == 1 else 'aren'}'t tools. The tools are {', '.join(names)}."
            )
        self.tools = [t for t in TOOLS if not config.mcp.tools or t["name"] in config.mcp.tools]
        self.filtered = withhold.FilteredCache(config) if withhold.active(config) else None

    @contextlib.contextmanager
    def _cache(self) -> Iterator[sqlite3.Connection]:
        if self.filtered is not None:
            yield self.filtered.connection()
            return
        with closing(db.connect_readonly(self.config)) as conn:
            yield conn

    def call(self, name: str, args: Json) -> object:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None or name not in {t["name"] for t in self.tools}:
            raise ToolError(f"There's no tool called '{name}'.")
        with self._cache() as conn:
            return handler(conn, args)

    def notice(self) -> str:
        """What [mcp] keeps from the client, for stderr when the server starts. Empty if nothing."""
        mcp = self.config.mcp
        kept = []
        if self.filtered is not None:
            self.filtered.connection()
            w = self.filtered.withheld
            parts = [
                _count(w.entries, "entry", "entries"),
                _count(w.notes, "note", "notes"),
                _count(w.digests, "digest", "digests"),
                _count(w.note_digests, "relationship digest", "relationship digests"),
            ]
            kept.append(", ".join(parts) + (" and your profile" if w.profile else ""))
        if not mcp.raw_text:
            kept.append("the text of entries and notes")
        if mcp.tools:
            kept.append("every tool but " + ", ".join(t["name"] for t in self.tools))
        return ("Leaving out " + "; ".join(kept) + " ([mcp] in config.toml).") if kept else ""

    def _tool_search(self, conn: sqlite3.Connection, args: Json) -> object:
        text = _text_arg(args, "query")
        limit = _number(args, "limit", SEARCH_LIMIT, 1, 50)
        since, until = _day(args, "since"), _day(args, "until")
        notice = None
        try:
            results = query.search(
                text, self.config, conn, self.make_client(self.config),
                limit=limit, date_from=since, date_to=until,
            )
        except JournalError as exc:
            notice = f"Keyword matches only: {exc}"
            results = query.keyword_search(text, conn, limit=limit, date_from=since, date_to=until)
        return {
            "query": text,
            "notice": notice,
            "entries": serialize.entry_briefs(conn, results.entries, results.parts),
            "notes": serialize.note_briefs(conn, results.notes),
        }

    def _tool_ask(self, conn: sqlite3.Connection, args: Json) -> object:
        question = _text_arg(args, "question")
        answer = query.answer(
            question, self.config, conn, self.make_client(self.config),
            since=_day(args, "since"), until=_day(args, "until"), only=_filter(conn, args),
        )
        found = serialize.answer_json(conn, answer)
        if not self.config.mcp.raw_text:
            # a quote is the entry's own text
            for day in found["sources"]["dates"]:
                day["quote"] = None
        return {"question": question, **found}

    def _tool_day(self, conn: sqlite3.Connection, args: Json) -> object:
        day = _day(args, "date", required=True)
        assert day is not None
        rows = store.entries_in_range(conn, day, day)
        if not rows:
            raise ToolError(f"There's no entry on {day}.")
        details = store.details_for_entries(conn, [r["id"] for r in rows])
        return {
            "date": day,
            "entries": [
                {
                    **serialize.entry_briefs(conn, [r])[0],
                    "details": details.get(r["id"]),
                    "notes": [
                        {"id": n["id"], "title": n["name"], "kind": n["type"]}
                        for n in store.entities_for_entry(conn, r["id"])
                    ],
                    "links_to_days": serialize.days_linked_from(conn, r["id"]),
                    "path": parser.relative_path(r["file_path"], self.config.vault.path),
                    **({"text": r["raw_text"]} if self.config.mcp.raw_text else {}),
                }
                for r in rows
            ],
        }

    def _tool_note(self, conn: sqlite3.Connection, args: Json) -> object:
        row = _one_note(conn, _text_arg(args, "name"))
        parsed = store.note_from_row(row)
        return {
            **serialize.note_briefs(conn, [row])[0],
            "properties": parsed.properties,
            **({"text": parsed.raw_text} if self.config.mcp.raw_text else {}),
            "by_year": [dict(r) for r in store.mentions_by_year(conn, row["id"])],
            "linked_with": [dict(r) for r in store.notes_linked_with(conn, row["id"], NOTE_ENTRIES)],
            "digest": serialize.note_digest_json(store.get_note_digest(conn, row["link_key"])),
            "entries": serialize.entry_briefs(conn, store.entries_mentioning(conn, row["id"], NOTE_ENTRIES)),
        }

    def _tool_notes(self, conn: sqlite3.Connection, args: Json) -> object:
        kind = str(args.get("kind") or "").strip().casefold()
        rows = [r for r in store.all_notes(conn) if not kind or r["kind"] == kind]
        return {"notes": serialize.note_briefs(conn, rows)}

    def _tool_mood(self, conn: sqlite3.Connection, args: Json) -> object:
        only = _filter(conn, args)
        rows = store.monthly_mood(conn, _day(args, "since"), _day(args, "until"), only)
        return {"scale": self.config.indexing.mood_scale_max, "months": [dict(r) for r in rows]}

    def _tool_digest(self, conn: sqlite3.Connection, args: Json) -> object:
        kind, key, start, end = _period(args, "period", ranges=True)
        row = store.get_rollup(conn, kind, key)
        members = store.entries_in_range(conn, start, end)
        return {
            "type": kind, "key": key, "start": start, "end": end, "entries": len(members),
            "digest": serialize.rollup_json(row) if row else None,
        }

    def _tool_on_this_day(self, conn: sqlite3.Connection, args: Json) -> object:
        anchor = _day(args, "date") or date.today()
        rows = [
            r for r in store.entries_on_month_day(conn, anchor.month, anchor.day)
            if r["entry_date"] != anchor.isoformat()
        ]
        return {"date": anchor, "entries": serialize.entry_briefs(conn, rows)}

    def _tool_broken_links(self, conn: sqlite3.Connection, args: Json) -> object:
        files = links.vault_markdown(self.config) if self.config.vault.path.is_dir() else None
        return {"links": [asdict(b) for b in links.diagnose(self.config, conn, files)]}

    def _tool_compare(self, conn: sqlite3.Connection, args: Json) -> object:
        kind, key, start, end = _period(args, "first")
        if args.get("second") in (None, ""):
            periods = (compare.previous(kind, start), (key, start, end))
        else:
            _, other, other_start, other_end = _period(args, "second")
            periods = ((key, start, end), (other, other_start, other_end))
        return asdict(compare.compare(conn, *periods))

    def _tool_review(self, conn: sqlite3.Connection, args: Json) -> object:
        if args.get("period") in (None, ""):
            _, last = store.date_bounds(conn, None, None)
            if last is None:
                raise ToolError("Nothing is indexed yet.")
            kind, key = "year", last[:4]
        else:
            kind, key, _, _ = _period(args, "period")
        built = review.build(self.config, conn, key)
        if built is None:
            raise ToolError(f"There are no entries in {store.period_title(kind, key)}.")
        return review.to_json(conn, built)

    def _tool_metric(self, conn: sqlite3.Connection, args: Json) -> object:
        known = store.metric_keys(conn)
        name = str(args.get("key") or "").strip().casefold()
        if not name:
            return {"metrics": [dict(r) for r in known]}
        if name not in {r["key"] for r in known}:
            raise ToolError(
                f"No entry records '{name}'. Known: {', '.join(r['key'] for r in known) or 'none yet'}."
            )
        rows = store.monthly_metric(conn, name, _day(args, "since"), _day(args, "until"), _filter(conn, args))
        return {"metric": name, "months": [dict(r) for r in rows]}

    def _tool_similar(self, conn: sqlite3.Connection, args: Json) -> object:
        day = _day(args, "date", required=True)
        assert day is not None
        rows = store.similar_to_day(conn, day, _number(args, "limit", SIMILAR_LIMIT, 1, 50))
        if rows is None:
            raise ToolError(f"There's no entry on {day}.")
        return {"date": day, "entries": serialize.entry_briefs(conn, rows)}

    def _tool_tags(self, conn: sqlite3.Connection, args: Json) -> object:
        if args.get("similar"):
            pairs = fields.similar_tags(store.tag_stats(conn))
            merge: dict[str, str] = {}
            for rare, _, common, _ in pairs:
                merge.setdefault(rare, common)
            return {
                "similar": [
                    {"tag": rare, "entries": n_rare, "like": common, "like_entries": n_common}
                    for rare, n_rare, common, n_common in pairs
                ],
                "merge": merge,
            }
        limit = _number(args, "limit", LIST_LIMIT, 1, 500)
        rows = store.top_tags(conn, _day(args, "since"), _day(args, "until"), limit=limit)
        return {"tags": [{"name": r["name"], "n": r["n"]} for r in rows]}

    def _tool_lapsed(self, conn: sqlite3.Connection, args: Json) -> object:
        min_links = _number(args, "min", LAPSED_MIN_LINKS, 1, 100_000)
        quiet = _number(args, "quiet_months", LAPSED_QUIET_MONTHS, 1, 1200)
        _, latest = store.date_bounds(conn, None, None)
        if latest is None:
            return {"anchor": None, "min": min_links, "quiet_months": quiet, "notes": []}
        anchor = date.fromisoformat(latest)
        kind = str(args.get("kind") or "").strip() or None
        rows = store.lapsed_notes(conn, min_links, store.months_before(anchor, quiet), kind)
        return {
            "anchor": anchor, "min": min_links, "quiet_months": quiet,
            "notes": [
                {**asdict(r), "months_quiet": store.months_between(date.fromisoformat(r.last), anchor)}
                for r in rows[:_number(args, "limit", LIST_LIMIT, 1, 500)]
            ],
        }

    def _tool_unlinked(self, conn: sqlite3.Connection, args: Json) -> object:
        limit = _number(args, "limit", LIST_LIMIT, 1, 500)
        name = str(args.get("name") or "").strip()
        if not name:
            found = unlinked.by_note(conn, str(args.get("kind") or "").strip() or None)
            return {"notes": [asdict(row) for row in found[:limit]], "total": len(found)}
        note = _one_note(conn, name)
        mentions = unlinked.for_note(conn, note["id"])
        shown = [asdict(m) for m in mentions[:limit]]
        if not self.config.mcp.raw_text:
            # the sentence that names it is the entry's own text
            for m in shown:
                del m["snippet"]
        return {
            "note": serialize.note_ref(note),
            "entries": shown,
            "total": len(mentions),
        }

    def _tool_profile(self, conn: sqlite3.Connection, args: Json) -> object:
        if not self.config.profile.enabled:
            raise ToolError("The profile is turned off in config.toml ([profile] enabled = false).")
        return profile.to_json(conn, self.config, _number(args, "facts", PROFILE_FACTS, 0, 500))

    def handle(self, message: Json) -> Json | None:
        """The reply to one JSON-RPC message, or None for a notification."""
        method = message.get("method")
        msg_id = message.get("id")
        if msg_id is None:
            return None  # notifications, like notifications/initialized, need no reply
        params = message.get("params") or {}
        if method == "initialize":
            wanted = params.get("protocolVersion")
            return _result(msg_id, {
                "protocolVersion": wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "journal", "version": __version__},
                "instructions": "Read-only tools over a personal journal kept in Obsidian. "
                "Days are cited as [[2024-01-15]] and notes as [[Note title]].",
            })
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": self.tools})
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(args, dict):
                return _error(msg_id, -32602, "tools/call takes a tool name and an arguments object.")
            try:
                value = self.call(name, args)
            except (ToolError, JournalError, sqlite3.Error, ValueError) as exc:
                return _result(msg_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
            return _result(msg_id, {"content": [{"type": "text", "text": _text(value)}], "isError": False})
        return _error(msg_id, -32601, f"Method '{method}' isn't supported.")


def _result(msg_id: object, result: Json) -> Json:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: object, code: int, message: str) -> Json:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(
    config: Config,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    make_client: ClientFactory = OllamaClient,
) -> None:
    """Answer messages until stdin closes."""
    reader = stdin or open(sys.stdin.fileno(), encoding="utf-8", closefd=False)  # noqa: SIM115
    writer = stdout or open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)  # noqa: SIM115
    server = Server(config, make_client)
    try:
        if notice := server.notice():
            print(notice, file=sys.stderr)
        _answer_lines(server, reader, writer)
    finally:
        if server.filtered is not None:
            server.filtered.close()


def _answer_lines(server: Server, reader: IO[str], writer: IO[str]) -> None:
    for line in reader:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: Json | None = _error(None, -32700, "That wasn't valid JSON.")
        else:
            if not isinstance(message, dict):
                reply = _error(None, -32600, "Batches aren't supported; send one message per line.")
            else:
                # stdout carries the protocol, so anything else that prints goes to stderr
                with contextlib.redirect_stdout(sys.stderr):
                    try:
                        reply = server.handle(message)
                    except Exception as exc:  # noqa: BLE001 - one bad call shouldn't end the session
                        reply = _error(
                            message.get("id"), -32603,
                            f"The journal server hit a bug: {type(exc).__name__}: {exc}",
                        )
        if reply is not None:
            writer.write(json.dumps(reply, ensure_ascii=False, default=serialize.json_default) + "\n")
            writer.flush()
