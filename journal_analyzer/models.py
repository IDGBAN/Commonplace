from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


@dataclass
class ParsedEntry:
    entry_date: date
    raw_text: str
    source_path: str
    links: list[str] = field(default_factory=list)  # casefolded note names
    rel_path: str = ""  # vault-relative, forward slashes


@dataclass
class ParsedNote:
    title: str
    kind: str
    raw_text: str
    source_path: str
    aliases: list[str] = field(default_factory=list)
    # other frontmatter, e.g. Relationship
    properties: dict[str, str] = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    rel_path: str = ""

    def header(self) -> str:
        lines = [f"{self.title} ({self.kind})"]
        if self.rel_path:
            lines.append(f"File: {self.rel_path}")
        if self.aliases:
            lines.append("Also called: " + ", ".join(self.aliases))
        lines.extend(f"{key}: {value}" for key, value in self.properties.items())
        return "\n".join(lines)


QueryIntent = Literal["aggregate", "factual", "narrative"]
AnswerStage = Literal["aggregate", "fast_summary", "precise_raw", "retrieval_empty"]


class Extraction(BaseModel):
    mood_score: float | None = None
    summary: str = ""
    tags: list[str] = Field(default_factory=list)


class NoteSummary(BaseModel):
    summary: str = ""


class QueryPlan(BaseModel):
    intent: QueryIntent
    date_from: date | None = None
    date_to: date | None = None
    entities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    needs_raw_text: bool = False

    @model_validator(mode="after")
    def _ordered_range(self) -> QueryPlan:
        # small routers sometimes give them back to front
        if self.date_from and self.date_to and self.date_from > self.date_to:
            self.date_from, self.date_to = self.date_to, self.date_from
        return self


class Answer(BaseModel):
    text: str
    cited_dates: list[date] = Field(default_factory=list)
    cited_notes: list[str] = Field(default_factory=list)
    stage_used: AnswerStage


class JournalError(Exception):
    pass


class DateParseError(JournalError):
    pass


class OllamaUnavailableError(JournalError):
    pass


class ModelRequestError(JournalError):
    pass


class StructuredOutputError(JournalError):
    pass


class EmbeddingModelMismatchError(JournalError):
    pass


class DatabaseError(JournalError):
    pass


class ConfigError(JournalError):
    pass
