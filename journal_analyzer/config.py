from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

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
    # entries and notes with this tag are never read; empty turns the tag off
    private_tag: str = "private"
    # leave out %% comments %%, which Obsidian hides in reading view
    strip_comments: bool = True
    # empty means the model's mood is used
    frontmatter_mood_key: str = ""
    frontmatter_mood_min: float = 1
    frontmatter_mood_max: float = 10

    @field_validator("path")
    @classmethod
    def _abs_path(cls, v: Path) -> Path:
        return _expand(v)

    @field_validator("private_tag")
    @classmethod
    def _tag(cls, v: str) -> str:
        return v.strip().lstrip("#").strip().lower()

    @model_validator(mode="after")
    def _mood_range(self) -> VaultConfig:
        if self.frontmatter_mood_max <= self.frontmatter_mood_min:
            raise ValueError(
                "frontmatter_mood_max must be larger than frontmatter_mood_min"
            )
        return self

    @field_validator("split_heading_regex")
    @classmethod
    def _compiles(cls, v: str) -> str:
        # otherwise every file fails to index with the same regex error
        try:
            re.compile(v, re.MULTILINE)
        except re.error as exc:
            raise ValueError(f"not a valid regular expression ({exc})") from exc
        return v


class OllamaConfig(BaseModel):
    host: str = "http://localhost:11434"
    fast_model: str = "llama3.2:3b"
    precise_model: str = "qwen3:14b"
    # None leaves it up to the model. Thinking makes extraction a lot slower for
    # not much benefit, so it's off for the fast model.
    fast_model_think: bool | None = False
    precise_model_think: bool | None = None
    # A fixed window means Ollama loads the model once at that size instead
    # of reloading whenever a prompt needs more room. 0 is Ollama's default,
    # raised only for prompts that need it.
    fast_num_ctx: int = Field(default=8192, ge=0)
    precise_num_ctx: int = Field(default=0, ge=0)
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
    # how much of each linked note goes along when an entry is read; 0 sends none
    linked_note_chars: int = Field(default=600, ge=0)


class RerankingConfig(BaseModel):
    enabled: bool = True
    model: str = "ms-marco-MiniLM-L-12-v2"


class RollupsConfig(BaseModel):
    # notes linked from at least this many entries get a relationship digest; 0 turns them off
    note_min_mentions: int = Field(default=8, ge=0)
    note_entries_per_year: int = Field(default=30, ge=1)
    # room for the notes a period's entries link to, in each digest's prompt; 0 sends none
    linked_notes_chars: int = Field(default=6000, ge=0)


class InsightsConfig(BaseModel):
    min_sample_size: int = Field(default=3, ge=1)
    mood_deviation_threshold: float = Field(default=0.8, ge=0)


def _tag_name(raw: str) -> str:
    return raw.strip().lstrip("#").strip().lower()


class TagsConfig(BaseModel):
    from_frontmatter: bool = True
    ignore: list[str] = Field(default_factory=list)
    # tag -> the tag it should count as
    merge: dict[str, str] = Field(default_factory=dict)
    known_in_prompt: int = Field(default=40, ge=0)

    @field_validator("ignore")
    @classmethod
    def _normalize(cls, v: list[str]) -> list[str]:
        return [name for name in (_tag_name(t) for t in v) if name]

    @field_validator("merge")
    @classmethod
    def _resolve(cls, v: dict[str, str]) -> dict[str, str]:
        raw = {_tag_name(k): _tag_name(t) for k, t in v.items() if _tag_name(k) and _tag_name(t)}
        resolved: dict[str, str] = {}
        for start in raw:
            # a -> b -> c becomes a -> c, so one lookup is enough later
            seen = [start]
            target = raw[start]
            while target in raw and target != start:
                if target in seen:
                    raise ValueError(f"merging goes round in a circle: {' -> '.join([*seen, target])}")
                seen.append(target)
                target = raw[target]
            if target == start:
                raise ValueError(f"merging goes round in a circle: {' -> '.join([*seen, target])}")
            resolved[start] = target
        return resolved


class MetricsConfig(BaseModel):
    # empty means every numeric frontmatter property
    keys: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)

    @field_validator("keys", "ignore")
    @classmethod
    def _casefold(cls, v: list[str]) -> list[str]:
        return [k.strip().casefold() for k in v if k.strip()]


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
    rollups: RollupsConfig = RollupsConfig()
    notes: NotesConfig = NotesConfig()
    tags: TagsConfig = TagsConfig()
    metrics: MetricsConfig = MetricsConfig()


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
