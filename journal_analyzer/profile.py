"""A short profile of the person writing the journal, kept up to date from it.

Two steps. The fast model reads entries a few at a time and pulls out lasting
facts about the writer (names, likes, habits, traits and so on), each kept
with the date of its entry. The precise model then folds new facts into the
profile, later facts winning when they disagree. A note the writer keeps
about themselves, if config names one, overrides anything inferred.

The profile goes to the steps that write for the person: answers, digests,
insights and writing prompts. The question router and follow-up rewriting
only get the names line. It's never given to entry extraction or to the scan
itself, so a wrong guess can't feed on itself.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from . import db, parser, store
from .config import Config
from .ollama_client import (
    CHARS_PER_TOKEN_ESTIMATE,
    REPLY_TOKENS,
    OllamaClient,
    context_window,
)

# a scan reply lists facts, which can run longer than an extraction
SCAN_REPLY_TOKENS = 1500
# one entry's share of a scan prompt, so a long entry doesn't crowd out the rest
SCAN_ENTRY_CHARS = 4000
FACT_CHARS = 200
# facts per rewrite of the profile; a first build goes through them in order
FOLD_FACTS_PER_CALL = 120
ME_NOTE_CHARS = 6000

Category = Literal[
    "names", "preferences", "personality", "hobbies", "routines", "work",
    "relationships", "values", "health", "other",
]

_META_TEXT = "profile_text"
_META_NAMES = "profile_names"
_META_UPDATED = "profile_updated_at"
_META_NOTE_HASH = "profile_note_hash"

_SCAN_PROMPT = """You read entries from one person's journal and pull out lasting \
facts about that person: names or nicknames they go by or use for people \
close to them, likes and dislikes, personality traits, hobbies and interests, \
routines, work or studies, values and goals, health. Write each fact in the \
writer's own first person, short and specific ("I take my coffee black", "My \
friends call me Ban"). Only take what an entry says or clearly shows about the \
writer. One-off events, passing moods and plans aren't lasting facts, and \
facts about other people only count for what they say about the writer. Most \
entries have none, so an empty list is a normal answer. Give each fact the \
date of the entry it came from."""

_FOLD_PROMPT = """You keep a short profile of the person who writes a journal, \
written in their own first person (I, me, my). You get the profile so far, \
any note they wrote about themselves, and new facts taken from their journal \
in date order. Rewrite the profile to take in the new facts. When facts \
disagree, the later one wins; mention a change only when it says something \
("I used to run, now I swim"). Keep what lasts and drop one-offs. Their own \
note is right whenever it disagrees with the facts. Use short sections with \
plain headings: Names, Personality, Likes and dislikes, Hobbies and \
interests, Routines, Work and studies, People, Values and goals, Health, \
leaving out any with nothing in them. Keep it under {max_words} words. \
Separately, give one or two sentences of just the names: what the writer is \
called, and nicknames they use for people, so a reader can tell who's meant."""


class ScannedFact(BaseModel):
    date: str
    category: Category
    fact: str


class ScannedFacts(BaseModel):
    facts: list[ScannedFact] = Field(default_factory=list)


class ProfileDigest(BaseModel):
    names: str = ""
    profile: str = ""


@dataclass(frozen=True)
class Profile:
    text: str
    names: str
    updated_at: str | None


def text_hash(raw_text: str) -> str:
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def current(conn: sqlite3.Connection) -> Profile | None:
    text = db.get_meta(conn, _META_TEXT)
    if not text:
        return None
    return Profile(text, db.get_meta(conn, _META_NAMES) or "", db.get_meta(conn, _META_UPDATED))


def context(conn: sqlite3.Connection, config: Config) -> str:
    """The profile as background for a prompt, or empty."""
    found = current(conn) if config.profile.enabled else None
    if found is None:
        return ""
    return (
        "\n\nAbout the person who writes this journal, from what they've written "
        "(background to read their words by; don't repeat it unless it's asked "
        f"about):\n{found.text}"
    )


def names(conn: sqlite3.Connection, config: Config) -> str:
    """Just the names line, for the router and follow-up rewriting."""
    found = current(conn) if config.profile.enabled else None
    if found is None or not found.names:
        return ""
    return f"\n\nNames the writer uses: {found.names}"


def _me_note(config: Config) -> str:
    """The note the writer keeps about themselves, unless it's missing or private."""
    if not config.profile.note:
        return ""
    path = config.vault.path / config.profile.note
    try:
        text = parser.read_bytes(Path(path)).decode("utf-8", errors="replace")
    except OSError:
        return ""
    fields, body = parser._parse_frontmatter(text)
    if config.vault.strip_comments:
        body = parser.strip_comments(body)
    if parser.file_is_private(fields, config) or parser.text_is_private(body, config):
        return ""
    lines = [f"{key}: {value}" for key, value in fields.items() if key.casefold() != "tags"]
    return ("\n".join(lines) + "\n\n" + parser.clean_text(body)).strip()[:ME_NOTE_CHARS]


def pending_entries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Entries whose current text hasn't been read for the profile, oldest first."""
    scanned = store.profile_scanned_hashes(conn)
    return [
        r for r in conn.execute(
            "SELECT id, entry_date, raw_text, file_path FROM entries ORDER BY entry_date, id"
        )
        if r["raw_text"].strip() and text_hash(r["raw_text"]) not in scanned
    ]


def scan_calls(config: Config, conn: sqlite3.Connection) -> int:
    """How many fast-model calls reading the unread entries would take."""
    return len(_batches(config, pending_entries(conn)))


def _batches(config: Config, rows: list[sqlite3.Row]) -> list[list[sqlite3.Row]]:
    window = context_window(config, config.ollama.fast_model)
    room = (window - SCAN_REPLY_TOKENS - REPLY_TOKENS // 2) * CHARS_PER_TOKEN_ESTIMATE
    room -= len(_SCAN_PROMPT)
    batches: list[list[sqlite3.Row]] = []
    batch: list[sqlite3.Row] = []
    used = 0
    for row in rows:
        cost = min(len(row["raw_text"]), SCAN_ENTRY_CHARS) + 40
        if batch and (len(batch) >= config.profile.scan_batch_entries or used + cost > room):
            batches.append(batch)
            batch, used = [], 0
        batch.append(row)
        used += cost
    if batch:
        batches.append(batch)
    return batches


def scan(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    rows: list[sqlite3.Row],
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Reads these entries for facts about the writer. Returns how many it found."""
    found = 0
    batches = _batches(config, rows)
    for done, batch in enumerate(batches, start=1):
        text = "\n\n".join(
            f"### Entry {r['entry_date']}\n{r['raw_text'].strip()[:SCAN_ENTRY_CHARS]}" for r in batch
        )
        reply = client.chat_structured(
            model=config.ollama.fast_model,
            messages=[
                {"role": "system", "content": _SCAN_PROMPT},
                {"role": "user", "content": text},
            ],
            schema=ScannedFacts,
        )
        by_date: dict[str, sqlite3.Row] = {}
        for r in batch:
            by_date.setdefault(r["entry_date"], r)
        seen: set[str] = set()
        facts: list[tuple[str, str, str, str]] = []
        for item in reply.facts:
            fact = " ".join(item.fact.split())[:FACT_CHARS]
            source = by_date.get(item.date.strip())
            if not fact or source is None or fact.casefold() in seen:
                continue
            seen.add(fact.casefold())
            facts.append((text_hash(source["raw_text"]), source["entry_date"], item.category, fact))
        store.save_profile_scan(conn, [text_hash(r["raw_text"]) for r in batch], facts)
        found += len(facts)
        if on_progress:
            on_progress(done, len(batches))
    return found


def needs_update(conn: sqlite3.Connection, config: Config) -> bool:
    """New facts to fold in, or the note about yourself changed."""
    if not config.profile.enabled:
        return False
    if store.unfolded_profile_facts(conn, limit=1):
        return True
    me = _me_note(config)
    return db.get_meta(conn, _META_NOTE_HASH) != text_hash(me) and bool(me or current(conn))


def update(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    rebuild: bool = False,
) -> bool:
    """Folds new facts into the profile. Returns whether it was rewritten."""
    if not config.profile.enabled:
        return False
    me = _me_note(config)
    note_hash = text_hash(me)
    if rebuild:
        store.unfold_profile_facts(conn)
    note_changed = db.get_meta(conn, _META_NOTE_HASH) != note_hash
    facts = store.unfolded_profile_facts(conn)
    if not facts and not (note_changed and (me or current(conn))):
        return False
    profile = None if rebuild else current(conn)
    so_far = profile.text if profile else ""
    names_so_far = profile.names if profile else ""
    chunks = [facts[i:i + FOLD_FACTS_PER_CALL] for i in range(0, len(facts), FOLD_FACTS_PER_CALL)] or [[]]
    for chunk in chunks:
        lines = [f"- {f['entry_date']} [{f['category']}] {f['fact']}" for f in chunk]
        parts = [
            f"Profile so far:\n{so_far or '(none yet)'}",
            f"Names so far: {names_so_far or '(none yet)'}",
        ]
        if me:
            parts.append(f"The note I keep about myself:\n{me}")
        parts.append("New facts, oldest first:\n" + ("\n".join(lines) or "(none)"))
        digest = client.chat_structured(
            model=config.ollama.precise_model,
            messages=[
                {"role": "system", "content": _FOLD_PROMPT.format(max_words=config.profile.max_words)},
                {"role": "user", "content": "\n\n".join(parts)},
            ],
            schema=ProfileDigest,
        )
        so_far = digest.profile.strip() or so_far
        names_so_far = digest.names.strip() or names_so_far
        # saved with each chunk, so a later chunk timing out doesn't lose
        # facts already marked as folded
        db.set_meta(conn, _META_TEXT, so_far)
        db.set_meta(conn, _META_NAMES, names_so_far)
        db.set_meta(conn, _META_UPDATED, _now())
        db.set_meta(conn, _META_NOTE_HASH, note_hash)
        store.mark_profile_facts_folded(conn, [f["id"] for f in chunk])
    return True


def forget_gone_entries(conn: sqlite3.Connection) -> int:
    """Drops facts and scans for entry text that's no longer in the cache."""
    live = {text_hash(r["raw_text"]) for r in conn.execute("SELECT raw_text FROM entries")}
    return store.prune_profile(conn, live)


def summary_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "facts": store.profile_fact_count(conn),
        "unfolded": len(store.unfolded_profile_facts(conn)),
        "unread": len(pending_entries(conn)),
    }


def facts_json(conn: sqlite3.Connection, limit: int = 500) -> list[dict[str, object]]:
    return [
        {"date": r["entry_date"], "category": r["category"], "fact": r["fact"],
         "folded": bool(r["folded"])}
        for r in store.profile_facts(conn, limit)
    ]

