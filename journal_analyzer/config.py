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
    # print a thinking model's reasoning as it streams, before the answer
    show_thinking: bool = False
    # Tokens a thinking model may think for before an answer or a JSON reply.
    # Past it the model is stopped and asked again without thinking.
    think_tokens: int = Field(default=2048, ge=256)
    # A fixed window means Ollama loads the model once at that size instead
    # of reloading whenever a prompt needs more room. 0 means 4096, raised
    # only for prompts that need it.
    fast_num_ctx: int = Field(default=8192, ge=0)
    precise_num_ctx: int = Field(default=0, ge=0)
    embed_model: str = "qwen3-embedding:0.6b"
    # a model that reads images, to describe the photos entries link; empty turns it off
    vision_model: str = ""
    # capped at the model's own limit
    embed_num_ctx: int = Field(default=8192, ge=512)
    request_timeout_seconds: int = Field(default=120, ge=1)


class DbConfig(BaseModel):
    path: Path = Path("~/.journal_analyzer/journal.db")
    # keep model results so rebuilding the cache doesn't redo them
    memo: bool = True
    # empty means next to the cache, e.g. journal-memo.db
    memo_path: Path | None = None

    @field_validator("path")
    @classmethod
    def _abs_path(cls, v: Path) -> Path:
        return _expand(v)

    @field_validator("memo_path", mode="before")
    @classmethod
    def _memo_path(cls, v: object) -> Path | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return _expand(Path(str(v)))


class IndexingConfig(BaseModel):
    watch_debounce_seconds: float = Field(default=45, ge=0)
    mood_scale_max: int = Field(default=10, ge=2)
    # how much of each linked note goes along when an entry is read; 0 sends none
    linked_note_chars: int = Field(default=600, ge=0)
    # entries this long also get a longer summary; 0 turns it off
    detail_min_words: int = Field(default=400, ge=0)
    # and from this long, a longer one still
    detail_long_words: int = Field(default=1000, ge=1)
    # search entries with several top-level headings part by part, with a vector each
    sections: bool = True
    # parts shorter than this join the one before
    section_min_chars: int = Field(default=200, ge=0)
    # photos described for each entry, with [ollama] vision_model set
    photos_per_entry: int = Field(default=4, ge=1)

    @model_validator(mode="after")
    def _detail_tiers(self) -> IndexingConfig:
        if self.detail_min_words and self.detail_long_words < self.detail_min_words:
            raise ValueError("detail_long_words can't be below detail_min_words")
        return self


class RerankingConfig(BaseModel):
    enabled: bool = True
    model: str = "ms-marco-MiniLM-L-12-v2"


class RollupsConfig(BaseModel):
    # notes linked from at least this many entries get a relationship digest; 0 turns them off
    note_min_mentions: int = Field(default=8, ge=0)
    note_entries_per_year: int = Field(default=30, ge=1)
    # room for the notes a period's entries link to, in each digest's prompt; 0 sends none
    linked_notes_chars: int = Field(default=6000, ge=0)
    # room for long entries' longer summaries in each digest; the rest get one line
    entry_details_chars: int = Field(default=8000, ge=0)
    # `journal watch` writes the week and month digests of what it indexed, at
    # most this often; 0 leaves them to `journal rollup`
    watch_refresh_minutes: float = Field(default=0, ge=0)


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


class ThemesConfig(BaseModel):
    # how many themes `journal themes --refresh` looks for; 0 picks from how much you've written
    count: int = Field(default=0, ge=0, le=40)
    # a theme with fewer entries than this is folded into the others
    min_entries: int = Field(default=5, ge=2)
    # entries written since the last refresh, as a share of all, before themes say they're out of date
    stale_share: float = Field(default=0.2, ge=0, le=1)


class ProfileConfig(BaseModel):
    enabled: bool = True
    # a note about yourself, from the vault root; it overrides what's inferred
    note: str = ""
    scan_batch_entries: int = Field(default=8, ge=1)
    max_words: int = Field(default=350, ge=50)
    # `journal watch` rewrites the profile at most this often
    watch_update_minutes: float = Field(default=30, ge=0)

    @field_validator("note")
    @classmethod
    def _relative(cls, v: str) -> str:
        return v.strip().replace("\\", "/").strip("/")


class NotesConfig(BaseModel):
    paths: list[str] = Field(default_factory=list)
    # every Markdown file daily_note_glob doesn't pick is a note too
    other_files: bool = False
    # a note that links a day, like [[2024-03-05]], counts as mentioned that day
    day_links_count: bool = False
    # 0 summarizes every non-empty note
    summarize_over_chars: int = Field(default=280, ge=0)
    # link text used for one note in this many entries becomes a name for it; 0 turns it off
    learned_alias_min: int = Field(default=2, ge=0)
    learned_alias_ignore: list[str] = Field(default_factory=list)
    # properties whose values group notes for `journal insights`; empty means all of them
    group_keys: list[str] = Field(default_factory=list)

    @field_validator("paths")
    @classmethod
    def _normalize(cls, v: list[str]) -> list[str]:
        return [p.strip().replace("\\", "/").strip("/") for p in v if p.strip().strip("/\\")]

    @field_validator("learned_alias_ignore")
    @classmethod
    def _names(cls, v: list[str]) -> list[str]:
        return [name for name in (" ".join(n.casefold().split()) for n in v) if name]


class HistoryConfig(BaseModel):
    # keep your questions and their answers; off unless you turn it on
    enabled: bool = False
    # empty means next to the cache, e.g. journal-history.db
    path: Path | None = None
    # the newest questions kept; older ones are deleted
    keep: int = Field(default=500, ge=1)
    # questions older than this many days are deleted; 0 leaves it to `keep`
    forget_after_days: int = Field(default=0, ge=0)

    @field_validator("path", mode="before")
    @classmethod
    def _path(cls, v: object) -> Path | None:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return _expand(Path(str(v)))


class McpConfig(BaseModel):
    # entry and note text; false leaves summaries, digests and numbers
    raw_text: bool = True
    # entries tagged with any of these, and notes tagged in their file, are left out
    exclude_tags: list[str] = Field(default_factory=list)
    # notes of these kinds (their folder, like people) are left out
    exclude_kinds: list[str] = Field(default_factory=list)
    # only these tools; empty means all of them
    tools: list[str] = Field(default_factory=list)

    @field_validator("exclude_tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return [name for name in (_tag_name(t) for t in v) if name]

    @field_validator("exclude_kinds")
    @classmethod
    def _kinds(cls, v: list[str]) -> list[str]:
        return [k.strip().casefold() for k in v if k.strip()]

    @field_validator("tools")
    @classmethod
    def _tools(cls, v: list[str]) -> list[str]:
        return [t.strip() for t in v if t.strip()]


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
    profile: ProfileConfig = ProfileConfig()
    mcp: McpConfig = McpConfig()
    history: HistoryConfig = HistoryConfig()
    themes: ThemesConfig = ThemesConfig()

    @model_validator(mode="after")
    def _history_outside_vault(self) -> Config:
        # nothing is ever written to the vault
        if self.history.path and self.history.path.is_relative_to(self.vault.path):
            raise ValueError("[history] path has to be outside the vault")
        return self


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
