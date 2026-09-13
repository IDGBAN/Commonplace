from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .models import ConfigError


def _expand(p: Path) -> Path:
    return p.expanduser().resolve()


class VaultConfig(BaseModel):
    path: Path
    daily_note_glob: str = "**/*.md"
    date_source: Literal["filename", "frontmatter", "heading"] = "filename"
    date_format: str = "%Y-%m-%d"
    frontmatter_date_key: str = "date"
    split_heading_regex: str = ""
    ignore_patterns: list[str] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _abs_path(cls, v: Path) -> Path:
        return _expand(v)


class OllamaConfig(BaseModel):
    host: str = "http://localhost:11434"
    fast_model: str = "llama3.2:3b"
    precise_model: str = "qwen3:14b"
    # None leaves it up to the model. Thinking makes extraction a lot slower for
    # not much benefit, so it's off for the fast model.
    fast_model_think: bool | None = False
    precise_model_think: bool | None = None
    embed_model: str = "qwen3-embedding:0.6b"
    # capped at the model's own limit
    embed_num_ctx: int = Field(default=8192, ge=512)
    request_timeout_seconds: int = Field(default=120, ge=1)


class DbConfig(BaseModel):
    path: Path = Path("~/.journal_analyzer/journal.db")

    @field_validator("path")
    @classmethod
    def _abs_path(cls, v: Path) -> Path:
        return _expand(v)


class IndexingConfig(BaseModel):
    watch_debounce_seconds: float = Field(default=45, ge=0)
    mood_scale_max: int = Field(default=10, ge=2)


class RerankingConfig(BaseModel):
    enabled: bool = True
    model: str = "ms-marco-MiniLM-L-12-v2"


class InsightsConfig(BaseModel):
    min_sample_size: int = Field(default=3, ge=1)
    mood_deviation_threshold: float = Field(default=0.8, ge=0)


class NotesConfig(BaseModel):
    paths: list[str] = Field(default_factory=list)
    # 0 summarizes every non-empty note
    summarize_over_chars: int = Field(default=280, ge=0)

    @field_validator("paths")
    @classmethod
    def _normalize(cls, v: list[str]) -> list[str]:
        return [p.strip().replace("\\", "/").strip("/") for p in v if p.strip().strip("/\\")]


class Config(BaseModel):
    vault: VaultConfig
    ollama: OllamaConfig = OllamaConfig()
    db: DbConfig = DbConfig()
    indexing: IndexingConfig = IndexingConfig()
    reranking: RerankingConfig = RerankingConfig()
    insights: InsightsConfig = InsightsConfig()
    notes: NotesConfig = NotesConfig()


def load_config(path: Path | str | None = None) -> Config:
    cfg_path = Path(path) if path else Path("config.toml")
    if not cfg_path.exists():
        raise ConfigError(
            f"Config file not found: {cfg_path}. "
            "Copy config.example.toml to config.toml and edit it, "
            "or pass --config PATH."
        )
    # Windows editors like to add a BOM, and tomllib won't accept one
    text = cfg_path.read_bytes().decode("utf-8-sig")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Could not parse {cfg_path}: {exc}. If this points at a "
            'backslash escape (e.g. "Invalid hex value"), it\'s likely a '
            r'Windows path in a double-quoted string, e.g. "C:\Users\...".'
            " TOML treats \\ as an escape character in double quotes. Fix "
            r"it by using forward slashes (C:/Users/...) or wrapping the "
            r"path in single quotes ('C:\Users\...') instead."
        ) from exc
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(x) for x in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(
            f"{cfg_path} is not a valid config:\n{problems}\n"
            "See config.example.toml for the expected shape."
        ) from exc
