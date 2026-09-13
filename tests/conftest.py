from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import pytest
from click.testing import Result
from pydantic import BaseModel
from typer.testing import CliRunner

from journal_analyzer import cli, db, store
from journal_analyzer.config import Config
from journal_analyzer.models import (
    Extraction,
    NoteSummary,
    ParsedEntry,
    ParsedNote,
    QueryPlan,
)

FIXTURES = Path(__file__).parent / "fixtures"
EMBED_DIM = 8


class FakeOllamaClient:
    # chat_response and the structured[...] values can also be
    # callables taking (model, messages)
    def __init__(self):
        self.chat_response = "canned answer"
        self.structured: dict[str, object] = {
            "Extraction": Extraction(
                mood_score=7.0,
                summary="A pleasant day with friends outdoors.",
                tags=["Friends", "outdoors", "nature"],
            ),
            "NoteSummary": NoteSummary(summary="A canned note summary."),
        }
        self.chat_calls: list[tuple[str, list[dict]]] = []
        self.structured_calls: list[tuple[str, str]] = []
        self.embed_calls: list[str] = []
        self.embed_options: list[dict | None] = []
        self.context_lengths: dict[str, int] = {}

    def chat(self, model, messages, options=None):
        self.chat_calls.append((model, messages))
        if callable(self.chat_response):
            return self.chat_response(model, messages)
        return self.chat_response

    def chat_structured(self, model, messages, schema: type[BaseModel], options=None):
        self.structured_calls.append((model, schema.__name__))
        source = self.structured[schema.__name__]
        result = source(model, messages) if callable(source) else source
        assert isinstance(result, schema)
        return result.model_copy(deep=True)

    def embed(self, model, text, options=None):
        self.embed_calls.append(text)
        self.embed_options.append(options)
        digest = hashlib.sha256(text.encode()).digest()
        return [b / 255.0 for b in digest[:EMBED_DIM]]

    def list_models(self):
        return ["fake-fast", "fake-precise", "fake-embed"]

    def context_length(self, model):
        return self.context_lengths.get(model)


def make_config(
    tmp_path: Path, notes_paths: list[str] | None = None, **vault_overrides
) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return Config.model_validate(
        {
            "vault": {"path": str(vault), **vault_overrides},
            "ollama": {
                "fast_model": "fake-fast",
                "precise_model": "fake-precise",
                "embed_model": "fake-embed",
            },
            "db": {"path": str(tmp_path / "journal.db")},
            "indexing": {"watch_debounce_seconds": 0.1},
            "notes": {"paths": notes_paths or []},
        }
    )


@pytest.fixture
def config(tmp_path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def conn(config) -> sqlite3.Connection:
    connection = db.connect(config)
    db.init_schema(connection)
    db.ensure_vec_table(connection, EMBED_DIM, config.ollama.embed_model)
    yield connection
    connection.close()


@pytest.fixture
def fake_client() -> FakeOllamaClient:
    return FakeOllamaClient()


def copy_fixtures(vault: Path, names: list[str] | None = None) -> list[Path]:
    names = names or ["2024-01-15.md", "2024-01-16.md"]
    out = []
    for name in names:
        dest = vault / name
        shutil.copy(FIXTURES / name, dest)
        out.append(dest)
    return out


def copy_note_fixtures(vault: Path) -> Path:
    dest = vault / "Links"
    shutil.copytree(FIXTURES / "Links", dest)
    return dest


def add_note(
    conn: sqlite3.Connection,
    title: str,
    kind: str = "people",
    text: str = "",
    aliases: Iterable[str] = (),
    properties: dict[str, str] | None = None,
    links: Iterable[str] = (),
    summary: str | None = None,
) -> int:
    path = f"/vault/Links/{kind}/{title}.md"
    store.upsert_file(conn, path, "h", "indexed")
    note = ParsedNote(
        title=title,
        kind=kind,
        raw_text=text,
        source_path=path,
        aliases=list(aliases),
        properties=dict(properties or {}),
        links=[target.casefold() for target in links],
    )
    return store.save_note(conn, note, summary, None)


def add_entry(
    conn: sqlite3.Connection,
    day: date,
    text: str = "x",
    mood: float | None = 6.0,
    tags: Iterable[str] = (),
    links: Iterable[str] = (),
    summary: str = "s",
    path: str = "/vault/j.md",
) -> int:
    store.upsert_file(conn, path, "h", "indexed")
    entry = ParsedEntry(day, text, path, [target.casefold() for target in links])
    extraction = Extraction(mood_score=mood, summary=summary, tags=list(tags))
    return store.save_entry(conn, entry, extraction, "m")


def plan_for(intent: str, **kwargs) -> QueryPlan:
    return QueryPlan(intent=intent, **kwargs)


def invoke_cli(tmp_path: Path, config: Config, *args: str, input: str | None = None) -> Result:
    path = tmp_path / "config.toml"
    path.write_text(
        f"[vault]\npath = '{config.vault.path.as_posix()}'\n"
        f"[db]\npath = '{config.db.path.as_posix()}'\n"
        "[ollama]\n"
        f"fast_model = '{config.ollama.fast_model}'\n"
        f"precise_model = '{config.ollama.precise_model}'\n"
        f"embed_model = '{config.ollama.embed_model}'\n",
        encoding="utf-8",
    )
    return CliRunner().invoke(cli.app, [*args, "--config", str(path)], input=input)
