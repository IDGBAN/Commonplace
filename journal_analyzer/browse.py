"""`journal browse`: a local, read-only web view of everything in the cache.

The server binds to 127.0.0.1, only answers GET, and opens the cache with
db.connect_readonly, so it can run next to `journal watch` without touching it.
Every value comes from store/query, the same functions the CLI and models use.
Asking a question streams server-sent events from GET /api/ask, since an
answer can take minutes and the page shows each step as it happens.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import webbrowser
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import db, fields, parser, query, serialize, store, unlinked
from .config import Config
from .models import JournalError
from .ollama_client import OllamaClient
from .parser import relative_path

DEFAULT_PORT = 8765
HOST = "127.0.0.1"
MAX_PAGE_SIZE = 200
MAX_QUESTION_CHARS = 2000
SIMILAR_K = 6
NOTE_ENTRIES_LIMIT = 500
TAG_ENTRIES_LIMIT = 1000
UNRESOLVED_LINKS_LIMIT = 60
# the "gone quiet" list on the notes page, the same as `journal lapsed` defaults
LAPSED_MIN_LINKS = 5
LAPSED_QUIET_MONTHS = 6
COOCCURRING_LIMIT = 12
_MAX_ROWID = 2**63 - 1
_NO_ENTRY = "There is no indexed entry with that id."
_NO_NOTE = "There is no indexed note with that id."

Json = dict[str, Any]
ClientFactory = Callable[[Config], OllamaClient]

_STATIC: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

# Journal text is rendered with textContent, but the policy is a second line
# of defence if a note ever contains markup.
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Request:
    config: Config
    conn: sqlite3.Connection
    params: dict[str, str]
    make_client: ClientFactory

    def day(self, key: str) -> date | None:
        value = self.params.get(key, "").strip()
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ApiError(
                HTTPStatus.BAD_REQUEST, f"'{key}' must be a date like 2024-01-15."
            ) from None

    def number(self, key: str, default: int, lo: int, hi: int) -> int:
        value = self.params.get(key, "").strip()
        if not value:
            return default
        try:
            return min(max(int(value), lo), hi)
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"'{key}' must be a number.") from None


# Serializers


def _row_id(digits: str, missing: str) -> int:
    # the route only lets digits through, but they can still be past SQLite's range
    if len(digits) > len(str(_MAX_ROWID)) or int(digits) > _MAX_ROWID:
        raise ApiError(HTTPStatus.NOT_FOUND, missing)
    return int(digits)


def _vault_path(req: Request, path: str) -> str:
    return relative_path(path, req.config.vault.path)


def _has_vector(conn: sqlite3.Connection, table: str, id_column: str, row_id: int) -> bool:
    try:
        return conn.execute(
            f"SELECT 1 FROM {table} WHERE {id_column} = ?", (row_id,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False  # the vec tables are created by the first index run


# Routes


def overview(req: Request) -> Json:
    conn, cfg = req.conn, req.config
    today = date.today()
    baseline, scored = store.overall_mood(conn, None, None)
    on_this_day = [
        r for r in store.entries_on_month_day(conn, today.month, today.day)
        if r["entry_date"] != today.isoformat()
    ]
    db_bytes = cfg.db.path.stat().st_size if cfg.db.path.exists() else 0
    return {
        "today": today.isoformat(),
        "counts": asdict(store.stats(conn)),
        "writing": asdict(store.writing_stats(conn)),
        "note_kinds": [dict(r) for r in store.notes_by_kind(conn)],
        "monthly_mood": [dict(r) for r in store.monthly_mood(conn, None, None)],
        "baseline_mood": baseline,
        "scored_entries": scored,
        "mood_scale": cfg.indexing.mood_scale_max,
        "errored_files": [
            {"path": r["path"], "vault_path": _vault_path(req, r["path"]),
             "error": r["error_message"]}
            for r in store.errored_files(conn)
        ],
        "future_entries": [
            {"date": r["entry_date"], "vault_path": _vault_path(req, r["file_path"])}
            for r in store.entries_dated_after(conn, today)
        ],
        "on_this_day": serialize.entry_briefs(conn, on_this_day),
        "setup": {
            "vault": str(cfg.vault.path),
            "notes_paths": cfg.notes.paths,
            "db_path": str(cfg.db.path),
            "db_bytes": db_bytes,
            "schema_version": db.SCHEMA_VERSION,
            "fast_model": cfg.ollama.fast_model,
            "precise_model": cfg.ollama.precise_model,
            "embed_model": db.get_meta(conn, "embed_model") or cfg.ollama.embed_model,
            "embed_dim": db.get_meta(conn, "embed_dim"),
            "reranking": cfg.reranking.model if cfg.reranking.enabled else None,
        },
    }


def catalog(req: Request) -> Json:
    """Everything the jump box and calendar need, in one small payload."""
    conn = req.conn
    return {
        "mood_scale": req.config.indexing.mood_scale_max,
        "entries": [
            {"id": r["id"], "date": r["entry_date"], "mood": r["mood_score"],
             "words": r["word_count"], "summary": r["summary"]}
            for r in store.entry_days(conn)
        ],
        "notes": [
            {**serialize.note_ref(r), "aliases": json.loads(r["aliases"])}
            for r in store.all_notes(conn)
        ],
        "tags": [{"name": r["name"], "n": r["n"]} for r in store.tag_stats(conn)],
    }


def entries(req: Request) -> Json:
    note_id = req.number("note", -1, -1, sys.maxsize)
    limit = req.number("limit", 50, 1, MAX_PAGE_SIZE)
    offset = req.number("offset", 0, 0, sys.maxsize)
    date_from, date_to = req.day("from"), req.day("to")
    rows, total = store.browse_entries(
        req.conn,
        date_from=date_from,
        date_to=date_to,
        tag=req.params.get("tag") or None,
        note_id=note_id if note_id >= 0 else None,
        text=req.params.get("q") or None,
        newest_first=req.params.get("order", "newest") != "oldest",
        limit=limit,
        offset=offset,
    )
    note = store.notes_by_ids(req.conn, [note_id]) if note_id >= 0 else []
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "note": serialize.note_ref(note[0]) if note else None,
        "items": serialize.entry_briefs(req.conn, rows),
    }


def entry(req: Request, entry_id: str) -> Json:
    conn = req.conn
    row = store.entry_by_id(conn, _row_id(entry_id, _NO_ENTRY))
    if row is None:
        raise ApiError(HTTPStatus.NOT_FOUND, _NO_ENTRY)
    before, after = store.adjacent_entries(conn, row)
    file = store.get_file_record(conn, row["file_path"])
    similar_ids = store.similar_entry_ids(conn, row["id"], SIMILAR_K)
    order = {eid: i for i, eid in enumerate(similar_ids)}
    similar = sorted(store.entries_by_ids(conn, similar_ids), key=lambda r: order[r["id"]])
    digests = []
    day = date.fromisoformat(row["entry_date"])
    for period_type, key in store.period_keys_for_date(day).items():
        rollup = store.get_rollup(conn, period_type, key)
        digests.append(
            {"type": period_type, "key": key, "exists": rollup is not None,
             "stale": bool(rollup["stale"]) if rollup else False,
             "summary": rollup["summary"] if rollup else None}
        )
    frontmatter = store.file_frontmatter(conn, row["file_path"])
    reading = store.entry_reading(conn, row["id"])
    own_mood = fields.frontmatter_mood(frontmatter, req.config)
    return {
        "id": row["id"],
        "date": row["entry_date"],
        "mood": row["mood_score"],
        "mood_from_frontmatter": own_mood is not None and own_mood == row["mood_score"],
        "frontmatter_tags": parser.frontmatter_tags(frontmatter)
        if req.config.tags.from_frontmatter else [],
        "model_reading": {"mood": reading.mood_score, "tags": reading.tags} if reading else None,
        "mood_scale": req.config.indexing.mood_scale_max,
        "words": row["word_count"],
        "summary": row["summary"],
        "model_used": row["model_used"],
        "extracted_at": row["extracted_at"],
        "raw_text": row["raw_text"],
        "path": row["file_path"],
        "vault_path": _vault_path(req, row["file_path"]),
        "obsidian_url": parser.obsidian_url(row["file_path"]),
        "file": {
            "status": file["status"], "last_indexed": file["last_indexed"],
            "error": file["error_message"],
        } if file else None,
        "tags": store.tags_for_entry(conn, row["id"]),
        "metrics": store.metrics_for_entries(conn, [row["id"]]).get(row["id"], {}),
        "notes": [
            {"id": r["id"], "title": r["name"], "kind": r["type"]}
            for r in store.entities_for_entry(conn, row["id"])
        ],
        "unresolved_links": store.unresolved_links_for_entry(conn, row["id"]),
        "links_to_days": [
            {"id": r["id"], "date": r["entry_date"]}
            for r in store.days_linked_from_entry(conn, row["id"])
        ],
        "linked_from_days": [
            {"id": r["id"], "date": r["entry_date"]}
            for r in store.entries_linking_to_day(conn, row)
        ],
        "embedded": _has_vector(conn, "entries_vec", "entry_id", row["id"]),
        "similar": serialize.entry_briefs(conn, similar),
        "digests": digests,
        "prev": {"id": before["id"], "date": before["entry_date"]} if before else None,
        "next": {"id": after["id"], "date": after["entry_date"]} if after else None,
    }


def notes(req: Request) -> Json:
    conn = req.conn
    _, latest = store.date_bounds(conn, None, None)
    quiet = (
        store.lapsed_notes(
            conn, LAPSED_MIN_LINKS, store.months_before(date.fromisoformat(latest), LAPSED_QUIET_MONTHS)
        )
        if latest else []
    )
    return {
        "kinds": [dict(r) for r in store.notes_by_kind(conn)],
        "notes": serialize.note_briefs(conn, store.all_notes(conn)),
        "gone_quiet": [
            {"id": r.note_id, "title": r.title, "kind": r.kind, "n": r.count, "last": r.last}
            for r in quiet
        ],
        "gone_quiet_rule": {"min": LAPSED_MIN_LINKS, "months": LAPSED_QUIET_MONTHS},
        "unresolved_links": [
            dict(r) for r in store.unresolved_links(conn, UNRESOLVED_LINKS_LIMIT)
        ],
    }


def note(req: Request, note_id: str) -> Json:
    conn = req.conn
    rows = store.notes_by_ids(conn, [_row_id(note_id, _NO_NOTE)])
    if not rows:
        raise ApiError(HTTPStatus.NOT_FOUND, _NO_NOTE)
    row = rows[0]
    parsed = store.note_from_row(row, req.config.vault.path)
    linking = serialize.entry_briefs(conn, store.entries_mentioning(conn, row["id"], NOTE_ENTRIES_LIMIT))
    mentions = store.mention_stats(conn, [row["id"]]).get(row["id"])
    if row["model_used"]:
        summary_source = "model"
    elif row["summary"]:
        summary_source = "own_text"
    else:
        summary_source = None
    return {
        **serialize.note_ref(row),
        "aliases": parsed.aliases,
        "properties": parsed.properties,
        "summary": row["summary"],
        "summary_source": summary_source,
        "model_used": row["model_used"],
        "extracted_at": row["extracted_at"],
        "raw_text": parsed.raw_text,
        "words": row["word_count"],
        "path": row["file_path"],
        "vault_path": parsed.rel_path,
        "obsidian_url": parser.obsidian_url(row["file_path"]),
        "embedded": _has_vector(conn, "notes_vec", "note_id", row["id"]),
        "mentions": asdict(mentions) if mentions else None,
        "by_year": [dict(r) for r in store.mentions_by_year(conn, row["id"])],
        "avg_mood": store.browse_mood(conn, note_id=row["id"]),
        "baseline_mood": store.overall_mood(conn, None, None)[0],
        "mood_scale": req.config.indexing.mood_scale_max,
        "digest": serialize.note_digest_json(store.get_note_digest(conn, row["link_key"])),
        "digest_min_mentions": req.config.rollups.note_min_mentions,
        "outlinks": [serialize.note_ref(r) for r in store.note_outlinks(conn, row["id"])],
        "backlinks": [serialize.note_ref(r) for r in store.note_backlinks(conn, row["id"])],
        "entries": linking,
        "entries_capped": bool(mentions and mentions.count > len(linking)),
        "unlinked": [
            {"id": m.entry_id, "date": m.entry_date, "name": m.name, "snippet": m.snippet}
            for m in unlinked.for_note(conn, row["id"])
        ],
    }


def tags(req: Request) -> Json:
    return {
        "baseline_mood": store.overall_mood(req.conn, None, None)[0],
        "mood_scale": req.config.indexing.mood_scale_max,
        "tags": [dict(r) for r in store.tag_stats(req.conn)],
    }


def tag(req: Request, name: str) -> Json:
    conn = req.conn
    rows, total = store.browse_entries(conn, tag=name, limit=TAG_ENTRIES_LIMIT)
    if not total:
        raise ApiError(HTTPStatus.NOT_FOUND, f"No indexed entry is tagged '{name}'.")
    return {
        "name": name,
        "total": total,
        "avg_mood": store.browse_mood(conn, tag=name),
        "baseline_mood": store.overall_mood(conn, None, None)[0],
        "mood_scale": req.config.indexing.mood_scale_max,
        "cooccurring": [
            dict(r) for r in store.tags_cooccurring_with(conn, name, COOCCURRING_LIMIT)
        ],
        "entries": serialize.entry_briefs(conn, rows),
        "entries_capped": total > len(rows),
    }


def digests(req: Request) -> Json:
    grouped: dict[str, list[Json]] = {"year": [], "month": [], "week": []}
    for row in store.all_rollups(req.conn):
        grouped.setdefault(row["period_type"], []).append(serialize.rollup_json(row))
    notes = [
        {**serialize.note_ref({"id": r["note_id"], "title": r["title"], "kind": r["kind"]}),
         **(serialize.note_digest_json(r) or {})}
        for r in store.all_note_digests(req.conn)
    ]
    return {
        "mood_scale": req.config.indexing.mood_scale_max,
        "digests": grouped,
        "notes": notes,
        "note_min_mentions": req.config.rollups.note_min_mentions,
    }


def digest(req: Request, period_type: str, period_key: str) -> Json:
    conn = req.conn
    try:
        start, end = store.period_bounds(period_type, period_key)
    except ValueError:
        raise ApiError(
            HTTPStatus.BAD_REQUEST, f"'{period_type} {period_key}' isn't a digest period."
        ) from None
    row = store.get_rollup(conn, period_type, period_key)
    members = store.entries_in_range(conn, start, end)
    if row is None and not members:
        raise ApiError(HTTPStatus.NOT_FOUND, "There is no digest or entry for that period.")
    return {
        "type": period_type,
        "key": period_key,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "mood_scale": req.config.indexing.mood_scale_max,
        "digest": serialize.rollup_json(row) if row else None,
        "entries": serialize.entry_briefs(conn, members),
    }


def files(req: Request) -> Json:
    return {
        "files": [
            {
                "path": r["path"],
                "vault_path": _vault_path(req, r["path"]),
                "status": r["status"],
                "last_indexed": r["last_indexed"],
                "error": r["error_message"],
                "entries": r["entries"],
                "entry_id": r["entry_id"],
                "note_id": r["note_id"],
            }
            for r in store.files_overview(req.conn)
        ]
    }


def search(req: Request) -> Json:
    text = req.params.get("q", "").strip()
    if not text:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Type something to search for.")
    limit = req.number("limit", 20, 1, MAX_PAGE_SIZE)
    date_from, date_to = req.day("from"), req.day("to")
    notice = None
    results: query.SearchResults | None = None
    if req.params.get("mode") == "meaning":
        try:
            results = query.search(
                text, req.config, req.conn, req.make_client(req.config),
                limit=limit, date_from=date_from, date_to=date_to,
            )
        except JournalError as exc:
            # Ollama down or a different embed model: keyword hits still help
            notice = str(exc)
    mode = "meaning" if results is not None else "keyword"
    if results is None:
        results = query.keyword_search(
            text, req.conn, limit=limit, date_from=date_from, date_to=date_to
        )
    return {
        "q": text,
        "mode": mode,
        "notice": notice,
        "entries": serialize.entry_briefs(req.conn, results.entries),
        "notes": serialize.note_briefs(req.conn, results.notes),
    }


def _ask_params(params: dict[str, str]) -> tuple[str, list[tuple[str, str]]]:
    question = params.get("q", "").strip()
    if not question:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Type a question to ask.")
    if len(question) > MAX_QUESTION_CHARS:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"That question is over {MAX_QUESTION_CHARS} characters. Try a shorter one.",
        )
    try:
        raw = json.loads(params.get("history") or "[]")
    except json.JSONDecodeError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "The conversation so far couldn't be read.") from None
    if not isinstance(raw, list) or not all(
        isinstance(turn, list) and len(turn) == 2 and all(isinstance(x, str) for x in turn)
        for turn in raw
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "The conversation so far couldn't be read.")
    history = [(q, a) for q, a in raw][-query.CHAT_HISTORY_TURNS:]
    return question, history


Handler = Callable[..., Json]

_ROUTES: list[tuple[re.Pattern[str], Handler]] = [
    (re.compile(r"/api/overview"), overview),
    (re.compile(r"/api/catalog"), catalog),
    (re.compile(r"/api/entries"), entries),
    (re.compile(r"/api/entries/(\d+)"), entry),
    (re.compile(r"/api/notes"), notes),
    (re.compile(r"/api/notes/(\d+)"), note),
    (re.compile(r"/api/tags"), tags),
    (re.compile(r"/api/tags/(.+)"), tag),
    (re.compile(r"/api/digests"), digests),
    (re.compile(r"/api/digests/(week|month|year)/([^/]+)"), digest),
    (re.compile(r"/api/files"), files),
    (re.compile(r"/api/search"), search),
]


# Server


class _Handler(BaseHTTPRequestHandler):
    server: BrowseServer
    server_version = "journal-browse"
    sys_version = ""

    def do_GET(self) -> None:
        # A page on another site can point a DNS name at 127.0.0.1; its
        # requests still carry that name in Host, so they're refused.
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Unexpected Host header."})
            return
        url = urlsplit(self.path)
        if url.path == "/api/ask":
            self._ask(url.query)
        elif url.path.startswith("/api/"):
            self._api(url.path, url.query)
        else:
            self._static(url.path)

    def _read_only(self) -> None:
        self._send_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"error": "The journal browser is read-only."},
            {"Allow": "GET"},
        )

    do_POST = do_PUT = do_PATCH = do_DELETE = _read_only

    def log_message(self, format: str, *args: Any) -> None:
        # Search text is in the URL. Keep it out of terminal scrollback.
        return

    def _api(self, path: str, query_string: str) -> None:
        for pattern, handler in _ROUTES:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            params = {k: v[-1] for k, v in parse_qs(query_string).items()}
            args = [unquote(g) for g in match.groups()]
            try:
                with closing(db.connect_readonly(self.server.config)) as conn:
                    request = Request(self.server.config, conn, params, self.server.make_client)
                    payload = handler(request, *args)
            except ApiError as exc:
                self._send_json(exc.status, {"error": str(exc)})
            except JournalError as exc:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            except sqlite3.Error as exc:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"The cache couldn't be read: {exc}"},
                )
            except Exception as exc:  # noqa: BLE001 - otherwise the page just sees a dropped connection
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": f"The journal browser hit a bug: {type(exc).__name__}: {exc}"},
                )
            else:
                self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "No such API route."})

    def _ask(self, query_string: str) -> None:
        params = {k: v[-1] for k, v in parse_qs(query_string).items()}
        try:
            question, history = _ask_params(params)
        except ApiError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()

        def send(event: str, data: Json) -> None:
            body = json.dumps(data, default=serialize.json_default, ensure_ascii=False)
            self.wfile.write(f"event: {event}\ndata: {body}\n\n".encode())
            self.wfile.flush()

        config = self.server.config
        try:
            try:
                with closing(db.connect_readonly(config)) as conn:
                    client = self.server.make_client(config)
                    standalone = question
                    if history:
                        send("stage", {"text": "Resolving your follow-up..."})
                        standalone = query.contextualize_question(question, history, config, client)
                        if standalone != question:
                            send("rewritten", {"question": standalone})
                    result = query.answer(
                        standalone, config, conn, client,
                        on_stage=lambda stage: send("stage", {"text": stage}),
                        on_text=lambda text: send("text", {"text": text}),
                    )
                    send("answer", {"question": standalone, **serialize.answer_json(conn, result)})
            except JournalError as exc:
                send("problem", {"error": str(exc)})
            except sqlite3.Error as exc:
                send("problem", {"error": f"The cache couldn't be read: {exc}"})
            except (ConnectionError, OSError):
                raise
            except Exception as exc:  # noqa: BLE001 - otherwise the page waits on a dead stream
                send("problem", {"error": f"The journal browser hit a bug: {type(exc).__name__}: {exc}"})
        except (ConnectionError, OSError):
            # the page closed or cancelled; stop here instead of running more model calls
            return

    def _static(self, path: str) -> None:
        asset = _STATIC.get(path)
        if asset is None:
            self._send(HTTPStatus.NOT_FOUND, b"Not found", "text/plain; charset=utf-8")
            return
        name, content_type = asset
        body = resources.files(__package__).joinpath("browse_static", name).read_bytes()
        self._send(HTTPStatus.OK, body, content_type)

    def _send_json(
        self, status: HTTPStatus, payload: Json, extra: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload, default=serialize.json_default, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in {**_SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class BrowseServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that's in use
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, config: Config, port: int, make_client: ClientFactory):
        self.config = config
        self.make_client = make_client
        super().__init__((HOST, port), _Handler)
        self.port: int = self.socket.getsockname()[1]
        self.allowed_hosts = {f"{HOST}:{self.port}", f"localhost:{self.port}"}

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/"


def make_server(
    config: Config, port: int = DEFAULT_PORT, make_client: ClientFactory = OllamaClient
) -> BrowseServer:
    try:
        return BrowseServer(config, port, make_client)
    except OSError as exc:
        raise JournalError(
            f"Couldn't listen on {HOST}:{port} ({exc}). If 'journal browse' is "
            "already running, use that window, or pass --port to pick another port."
        ) from exc


def serve(
    config: Config,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    on_ready: Callable[[str], None] = print,
) -> None:
    # fail here, in the terminal, rather than as an error page
    db.connect_readonly(config).close()
    server = make_server(config, port)
    on_ready(server.url)
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
