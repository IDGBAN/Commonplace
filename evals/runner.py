"""Builds the made-up journal's cache and puts the scenarios to the models.

The models and their settings come from your config; the vault, cache and
memo are throwaway ones under the work folder, so your own journal is never
opened and nothing of it can leak into a result.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from journal_analyzer import db, embedder, query, store
from journal_analyzer.config import Config, load_config
from journal_analyzer.models import Extraction, ParsedEntry, ParsedNote
from journal_analyzer.ollama_client import OllamaClient

from . import journal
from .scenarios import BY_ID, SCENARIOS, Scenario

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".work"
# a mood for every entry, which no scenario turns on
_MOOD = 6.0


def make_config(path: Path | str | None = None, work: Path = WORK) -> Config:
    """Your config with the vault, cache and notes swapped for the made-up ones."""
    settings = load_config(path).model_dump(mode="json")
    return _in_work_folder(settings, work)


def _in_work_folder(settings: dict[str, Any], work: Path) -> Config:
    vault = work / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    settings["vault"] = {**settings.get("vault", {}), "path": str(vault)}
    # a cache per version of the journal, so an edited entry is never answered from the old one
    settings["db"] = {"path": str(work / f"journal-{_journal_hash()}.db")}
    settings["notes"] = {"paths": []}
    # the profile and the history are the real writer's
    settings["profile"] = {**settings.get("profile", {}), "enabled": False, "note": ""}
    settings["history"] = {**settings.get("history", {}), "enabled": False}
    return Config.model_validate(settings)


def _journal_hash() -> str:
    told = repr((journal.TODAY, journal.ENTRIES, journal.NOTES))
    return hashlib.sha256(told.encode("utf-8")).hexdigest()[:10]


def build(config: Config, client: Any) -> sqlite3.Connection:
    """The cache for the made-up journal, written once and reused after that.

    Entries go in with the summaries in journal.py, not ones a model wrote, so
    a run tests answering and not indexing.
    """
    fresh = not config.db.path.exists()
    conn = db.connect(config)
    if not fresh:
        return conn
    db.init_schema(conn)
    db.ensure_vec_table(conn, embedder.probe_dimension(config, client), config.ollama.embed_model)
    vault = config.vault.path
    for title, kind, text in journal.NOTES:
        path = str(vault / "Links" / kind / f"{title}.md")
        store.upsert_file(conn, path, "h", "indexed")
        note = ParsedNote(title=title, kind=kind, raw_text=text, source_path=path)
        note_id = store.save_note(conn, note, None, None)
        store.save_note_vector(conn, note_id, embedder.note_vector(note, None, config, client))
    for day, text, summary, links in journal.ENTRIES:
        path = str(vault / f"{day.isoformat()}.md")
        store.upsert_file(conn, path, "h", "indexed")
        entry = ParsedEntry(day, text, path, [target.casefold() for target in links])
        reading = Extraction(mood_score=_MOOD, summary=summary, tags=[])
        entry_id = store.save_entry(conn, entry, reading, "evals")
        vector = embedder.entry_vector(text, config, client, summary)
        if vector:
            store.save_entry_vector(conn, entry_id, vector)
    conn.commit()
    return conn


def tripped(scenario: Scenario, body: str) -> list[str]:
    """The tripwires an answer set off. None set off doesn't make it right."""
    found = [
        f"missing /{pattern}/" for pattern in scenario.must
        if not re.search(pattern, body, re.IGNORECASE)
    ]
    found += [
        f"has /{pattern}/" for pattern in scenario.must_not
        if re.search(pattern, body, re.IGNORECASE)
    ]
    return found


def ask(
    scenario: Scenario,
    config: Config,
    conn: sqlite3.Connection,
    client: Any,
    earlier: Sequence[tuple[str, str]] = (),
) -> dict[str, Any]:
    """One scenario's answer with what went into it, as a plain dict."""
    plans: list[dict[str, Any]] = []
    thinking: list[str] = []
    sent: list[int] = []
    real_settle, real_stream = query._settle, client.chat_stream

    def settle(*args: Any, **kwargs: Any) -> Any:
        # the router's plan once the question's own wording has been read
        found = real_settle(*args, **kwargs)
        plans.append(found.model_dump(mode="json"))
        return found

    def stream(model: str, messages: list[dict[str, str]], on_text: Callable[[str], None], **kwargs: Any) -> str:
        sent.append(sum(len(m["content"]) for m in messages))
        return real_stream(model, messages, on_text, **kwargs)

    query._settle = settle  # type: ignore[assignment]
    client.chat_stream = stream
    started = time.monotonic()
    try:
        asked = scenario.question
        searched = query.contextualize_question(asked, earlier, config, client) if earlier else asked
        # streamed, as the Ask page and journal ask do it
        result = query.answer(
            searched, config, conn, client, today=journal.TODAY,
            on_text=lambda _text: None, on_thinking=thinking.append,
            earlier=earlier, asked=asked,
        )
    finally:
        query._settle = real_settle  # type: ignore[assignment]
        client.chat_stream = real_stream
    if plans and earlier and "?" not in asked:
        plans[-1]["wants"] = "judgment"  # as query._answer reads a follow-up that tells
    return {
        "id": scenario.id,
        "question": asked,
        "after": scenario.after,
        "searched": searched,
        "want": scenario.want,
        "bad": scenario.bad,
        "tags": list(scenario.tags),
        "plan": plans[-1] if plans else None,
        "stage": result.stage_used,
        "answer": result.text,
        "body": result.body,
        "tripped": tripped(scenario, result.body),
        "read_entries": len(result.read_entry_ids),
        "prompt_chars": sent,
        "thinking": "".join(thinking),
        "seconds": round(time.monotonic() - started),
        "timings": query.timing_line(result.timings),
    }


def chosen(only: Sequence[str] = (), tags: Sequence[str] = ()) -> list[Scenario]:
    unknown = [name for name in only if name not in BY_ID]
    if unknown:
        raise ValueError(f"No scenario called {', '.join(unknown)}. Try: python -m evals list")
    return [
        s for s in SCENARIOS
        if (not only or s.id in only) and (not tags or set(tags) & set(s.tags))
    ]


def override(settings: Sequence[str]) -> dict[str, Any]:
    """Sets constants of journal_analyzer.query for this run, from NAME=VALUE."""
    applied: dict[str, Any] = {}
    for setting in settings:
        name, _, raw = setting.partition("=")
        name = name.strip()
        if not hasattr(query, name):
            raise ValueError(f"journal_analyzer.query has nothing called {name}.")
        try:
            value = ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError):
            value = raw.strip()
        setattr(query, name, value)
        applied[name] = value
    return applied


def _code_version() -> str:
    try:
        commit = subprocess.run(
            ["git", "-C", str(ROOT.parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(ROOT.parent), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit}{' with uncommitted changes' if dirty else ''}"


def run(
    label: str,
    config: Config,
    client: Any,
    scenarios: Sequence[Scenario],
    repeat: int = 1,
    overrides: dict[str, Any] | None = None,
    work: Path = WORK,
    say: Callable[[str], None] = print,
) -> Path:
    """Asks each scenario `repeat` times and writes a JSON file per answer
    under <work>/runs/<label>. Returns that folder."""
    conn = build(config, client)
    out = work / "runs" / label
    out.mkdir(parents=True, exist_ok=True)
    about = {
        "label": label,
        "code": _code_version(),
        "fast_model": config.ollama.fast_model,
        "precise_model": config.ollama.precise_model,
        "precise_model_think": config.ollama.precise_model_think,
        "think_tokens": config.ollama.think_tokens,
        "temperature": query.SYNTHESIS_TEMPERATURE,
        "overrides": overrides or {},
    }
    (out / "run.json").write_text(json.dumps(about, indent=1), encoding="utf-8")
    try:
        for take in range(repeat):
            answers: dict[str, dict[str, Any]] = {}
            for scenario in scenarios:
                earlier: list[tuple[str, str]] = []
                if scenario.after:
                    # a follow-up needs its first turn, asked in this take if it wasn't already
                    first = answers.get(scenario.after) or ask(BY_ID[scenario.after], config, conn, client)
                    answers[scenario.after] = first
                    earlier = [(first["question"], first["answer"])]
                got = ask(scenario, config, conn, client, earlier)
                answers[scenario.id] = got
                (out / f"{scenario.id}.{take}.json").write_text(
                    json.dumps(got, indent=1, ensure_ascii=False), encoding="utf-8"
                )
                flag = "; ".join(got["tripped"]) or "no tripwire"
                say(f"{scenario.id} [{take}] {got['stage']} {got['seconds']}s, {flag}")
    finally:
        conn.close()
    return out


def run_with_ollama(
    label: str,
    config_path: Path | str | None = None,
    only: Sequence[str] = (),
    tags: Sequence[str] = (),
    repeat: int = 1,
    settings: Sequence[str] = (),
    work: Path = WORK,
) -> Path:
    config = make_config(config_path, work)
    overrides = override(settings)
    return run(label, config, OllamaClient(config), chosen(only, tags), repeat, overrides, work)
