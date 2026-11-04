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

from . import __version__, db, links, parser, query, serialize, store
from .config import Config
from .models import JournalError
from .ollama_client import OllamaClient

# newest first; the client's version is used when this server knows it
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SEARCH_LIMIT = 10
NOTE_ENTRIES = 10

Json = dict[str, Any]
ClientFactory = Callable[[Config], OllamaClient]


class ToolError(Exception):
    """A problem with the call itself, reported to the client as a tool error."""


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


def _about(conn: sqlite3.Connection, args: Json) -> store.EntryFilter | None:
    name = str(args.get("about") or "").strip()
    if not name:
        return None
    found = store.find_notes(conn, name)
    if len(found) != 1:
        raise ToolError(
            f"No note is called '{name}'." if not found
            else f"'{name}' matches {len(found)} notes: " + ", ".join(r["title"] for r in found)
        )
    return store.EntryFilter(note_ids=(found[0]["id"],))


_SCHEMA_DATE = {"type": "string", "description": "An ISO date, like 2024-01-15."}

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
                "about": {"type": "string", "description": "Only entries that link this note."},
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
        "description": "Average mood by month, optionally for the days that link one note.",
        "inputSchema": {
            "type": "object",
            "properties": {"since": _SCHEMA_DATE, "until": _SCHEMA_DATE, "about": {"type": "string"}},
        },
    },
    {
        "name": "digest",
        "description": "The written digest of a year (2024), month (2024-03) or ISO week (2024-W10).",
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
]


class Server:
    def __init__(self, config: Config, make_client: ClientFactory = OllamaClient):
        self.config = config
        self.make_client = make_client

    @contextlib.contextmanager
    def _cache(self) -> Iterator[sqlite3.Connection]:
        with closing(db.connect_readonly(self.config)) as conn:
            yield conn

    def call(self, name: str, args: Json) -> object:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None or name not in {t["name"] for t in TOOLS}:
            raise ToolError(f"There's no tool called '{name}'.")
        with self._cache() as conn:
            return handler(conn, args)

    def _tool_search(self, conn: sqlite3.Connection, args: Json) -> object:
        text = _text_arg(args, "query")
        limit = min(max(int(args.get("limit") or SEARCH_LIMIT), 1), 50)
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
            since=_day(args, "since"), until=_day(args, "until"), only=_about(conn, args),
        )
        return {"question": question, **serialize.answer_json(conn, answer)}

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
                    "text": r["raw_text"],
                }
                for r in rows
            ],
        }

    def _tool_note(self, conn: sqlite3.Connection, args: Json) -> object:
        name = _text_arg(args, "name")
        found = store.find_notes(conn, name)
        if not found:
            raise ToolError(f"No note is called '{name}'. The 'notes' tool lists them.")
        if len(found) > 1:
            raise ToolError(f"'{name}' matches {len(found)} notes: " + ", ".join(r["title"] for r in found))
        row = found[0]
        parsed = store.note_from_row(row)
        return {
            **serialize.note_briefs(conn, [row])[0],
            "properties": parsed.properties,
            "text": parsed.raw_text,
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
        only = _about(conn, args)
        rows = store.monthly_mood(conn, _day(args, "since"), _day(args, "until"), only)
        return {"scale": self.config.indexing.mood_scale_max, "months": [dict(r) for r in rows]}

    def _tool_digest(self, conn: sqlite3.Connection, args: Json) -> object:
        period = _text_arg(args, "period")
        try:
            kind, key, start, end = store.parse_period(period)
        except ValueError:
            raise ToolError(
                f"'{period}' isn't a period. Use a year (2024), a month (2024-03) or a week (2024-W10)."
            ) from None
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
            return _result(msg_id, {"tools": TOOLS})
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
