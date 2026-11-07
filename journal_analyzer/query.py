"""Question answering.

The fast model routes the question. Aggregate questions are answered from SQL.
The rest go through hybrid retrieval, then the fast model tries to answer from
summaries, and the precise model re-reads the full text if it can't.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console

from . import context, db, embedder, parser, profile, quotes, store, themes
from .config import Config
from .models import Answer, AnswerStage, QueryPlan, Timing
from .ollama_client import REPLY_TOKENS, SYNTHESIS_TEMPERATURE, OllamaClient, prompt_tokens

console = Console(stderr=True)

TOP_K = 8
# characters in the reading bar
PROGRESS_WIDTH = 12
# shown when the model ends without writing anything
NO_ANSWER = (
    "The model stopped without writing an answer. Asking again usually works; if it "
    "keeps happening, try precise_model_think = false in config.toml."
)
# roughly 48-64k tokens; the client raises num_ctx to fit
MAX_RAW_CONTEXT_CHARS = 192_000
# ranges wider than this also get month digests
BROAD_RANGE_DAYS = 62
# ranges from this up to BROAD_RANGE_DAYS get week digests instead
WEEK_DIGEST_MIN_DAYS = 10
MAX_WEEK_DIGESTS = 9
# more months than this and we send year digests instead
MAX_MONTH_DIGESTS = 14
# digests of spans of days, like a trip, that share the most days with the question
MAX_RANGE_DIGESTS = 3
# themes with the most entries in a narrative question's dates, or a range this long
MAX_THEME_LINES = 5
THEME_MIN_DAYS = 28
# the reranker can only reorder what retrieval found, so fetch extra
RERANK_CANDIDATE_POOL = 25
# 60 is the k from the original RRF paper
RRF_DAMPING = 60
AGGREGATE_TOP_TAGS = 10
NOTE_SEARCH_K = 3
# notes the candidate entries link to, the most linked first
LINKED_NOTES_K = 8
MAX_CONTEXT_NOTES = 12
# comes out of MAX_RAW_CONTEXT_CHARS
MAX_NOTE_CONTEXT_CHARS = 32_000
# extra room for long entries' longer summaries when answering from summaries
ANSWER_DETAILS_CHARS = 6000
# and for the part of a long, many-headed entry that matched, each and in all
ANSWER_PART_CHARS = 700
ANSWER_PARTS_CHARS = 4000
# notes called "Go" or "Me" would match ordinary words all the time
MIN_NAME_CHARS = 3

# Without this the model sometimes takes the journal's "I" to mean itself.
_READER_VOICE = (
    "The journal material is written in the first person by the person asking "
    'you, so its "I" is them: answer them directly as "you", and never call '
    'them "the author" or "the user".'
)

_ROUTER_SYSTEM = """You are a query router for a personal journal search tool.
Today's date is {today}. Classify the question and extract constraints:
- intent: "aggregate" for statistics over time (mood trends, tag frequencies,
  counts); "factual" for specific facts/events; "narrative" for open-ended
  summaries or stories about a period.
- date_from / date_to: absolute ISO dates. Convert relative expressions
  ("last March", "this winter", "two weeks ago") to absolute dates using
  today's date. Use null when the question has no time constraint.
- entities: names of people, places, groups, topics or projects mentioned in
  the question.
- keywords: exact search terms (proper nouns, distinctive words) for
  full-text search. Exclude generic stop words.
- needs_raw_text: true only if answering will likely require reading full
  entries rather than one-line summaries (e.g. asking for exact details,
  quotes, or specifics unlikely to survive summarization)."""


CHAT_HISTORY_TURNS = 6
# the start of an old answer is enough to resolve "that" or "she"
_HISTORY_ANSWER_CHARS = 400

_CONTEXTUALIZE_SYSTEM = (
    "You rewrite a user's latest message into a self-contained journal "
    "question using the conversation so far. Resolve pronouns and references "
    '("that month", "she", "the same period", "what about after that") into '
    "explicit names, dates, and terms drawn from the history. If the latest "
    "message already stands on its own, return it unchanged. Output ONLY the "
    "rewritten question, with no preamble, quotes, or explanation."
)


def contextualize_question(
    question: str,
    history: list[tuple[str, str]],
    config: Config,
    client: OllamaClient,
    names: str = "",
) -> str:
    """`names` is profile.names: who the writer and the people they mention are."""
    if not history:
        return question
    lines = []
    for user_q, assistant_a in history[-CHAT_HISTORY_TURNS:]:
        lines.append(f"User: {user_q}")
        lines.append(f"Assistant: {assistant_a[:_HISTORY_ANSWER_CHARS]}")
    transcript = "\n".join(lines)
    rewritten = client.chat(
        model=config.ollama.fast_model,
        messages=[
            {"role": "system", "content": _CONTEXTUALIZE_SYSTEM + names},
            {
                "role": "user",
                "content": (
                    f"Conversation so far:\n{transcript}\n\n"
                    f"Latest message: {question}\n\nRewritten question:"
                ),
            },
        ],
    ).strip()
    # small models occasionally return nothing
    return rewritten or question


def _plan(
    question: str, config: Config, client: OllamaClient, today: date, names: str = ""
) -> QueryPlan:
    return client.chat_structured(
        model=config.ollama.fast_model,
        messages=[
            {"role": "system", "content": _ROUTER_SYSTEM.format(today=today.isoformat()) + names},
            {"role": "user", "content": question},
        ],
        schema=QueryPlan,
    )


_MONTHS = {
    name: i
    for i, name in enumerate(
        [
            "january", "february", "march", "april", "may", "june", "july",
            "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}
_MONTH_PATTERN = "|".join(_MONTHS)
# "since July 4th" is a range, not a single day
_RANGE_WORDS = re.compile(
    r"\b(since|after|before|until|till|through|between|from|during|over|throughout)\b"
)


def _explicit_day(question: str, today: date) -> date | None:
    # The router sometimes loses a date that's spelled out in the question,
    # and retrieval can miss that day's entry, so find it here as well.
    q = question.lower()
    if _RANGE_WORDS.search(q):
        return None
    if re.search(r"\byesterday\b", q):
        return today - timedelta(days=1)
    if re.search(r"\btoday\b", q):
        return today

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", question)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.search(
        rf"\b({_MONTH_PATTERN})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", q
    )
    if m:
        return _day_in_year(_MONTHS[m.group(1)], int(m.group(2)), m.group(3), today)

    m = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_PATTERN})(?:,?\s+(\d{{4}}))?\b", q
    )
    if m:
        return _day_in_year(_MONTHS[m.group(2)], int(m.group(1)), m.group(3), today)

    return None


def _day_in_year(
    month: int, day: int, year_text: str | None, today: date
) -> date | None:
    # No year means the most recent one: "December 20" asked in January is last December.
    try:
        if year_text:
            return date(int(year_text), month, day)
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    if candidate > today:
        try:
            return date(today.year - 1, month, day)
        except ValueError:  # Feb 29 in a non-leap previous year
            return None
    return candidate


def notes_named_in(question: str, conn: sqlite3.Connection) -> list[int]:
    """Notes whose title or alias appears in the question as whole words.

    Longer names go first, so "John Doe" doesn't also match a note called "John".
    """
    text = question.casefold()
    claimed: list[tuple[int, int, str]] = []
    found: dict[int, None] = {}
    for row in sorted(store.note_name_index(conn), key=lambda r: -len(r["key"])):
        key = row["key"]
        if len(key) < MIN_NAME_CHARS:
            continue
        for m in re.finditer(rf"(?<!\w){re.escape(key)}(?!\w)", text):
            overlaps_longer = any(
                start < m.end() and m.start() < end and other != key
                for start, end, other in claimed
            )
            if overlaps_longer:
                continue
            claimed.append((m.start(), m.end(), key))
            found.setdefault(row["note_id"], None)
            break
    return list(found)


def _named_notes(conn: sqlite3.Connection, plan: QueryPlan, named: list[int]) -> list[int]:
    return list(dict.fromkeys([*named, *store.note_ids_matching(conn, plan.entities)]))


def _words(text: str) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text.casefold()).split())


def _notes_used(
    answer_text: str,
    notes: list[sqlite3.Row],
    about: list[int],
    learned: dict[int, list[str]] | None = None,
) -> list[str]:
    # Background notes the answer never mentions would pad every Sources line.
    said = f" {_words(answer_text)} "
    cited: list[str] = []
    for row in notes:
        names = [row["title"], *store.note_from_row(row).aliases, *(learned or {}).get(row["id"], [])]
        mentioned = any(
            (words := _words(name)) and f" {words} " in said for name in names
        )
        if row["id"] in about or mentioned:
            cited.append(row["title"])
    return cited


def _finalize(
    text: str,
    stage: AnswerStage,
    cited_dates: Iterable[date] = (),
    cited_notes: Iterable[str] = (),
    read_entries: Iterable[int] = (),
    read_notes: Iterable[int] = (),
    cited_parts: dict[str, str] | None = None,
    quoted: dict[str, str] | None = None,
) -> Answer:
    """`cited_parts` is the heading of the part of a cited day that matched,
    and `quoted` the sentence of it the answer likely drew on, both by ISO date."""
    dates = sorted(set(cited_dates))
    notes = sorted(set(cited_notes), key=str.casefold)
    parts = {d: h for d, h in (cited_parts or {}).items() if h and date.fromisoformat(d) in dates}
    kept = {d: q for d, q in (quoted or {}).items() if date.fromisoformat(d) in dates}
    body = text.rstrip() or NO_ANSWER
    links = [
        f"[[{d.isoformat()}#{parts[d.isoformat()]}]]" if d.isoformat() in parts else f"[[{d.isoformat()}]]"
        for d in dates
    ] + [f"[[{n}]]" for n in notes]
    if links:
        text = f"{body}\n\nSources: {' '.join(links)}"
    return Answer(
        text=text,
        body=body,
        cited_dates=dates,
        cited_parts=parts,
        cited_notes=notes,
        quotes=kept,
        stage_used=stage,
        read_entry_ids=list(dict.fromkeys(read_entries)),
        read_note_ids=list(dict.fromkeys(read_notes)),
    )


def _mention_facts(
    conn: sqlite3.Connection, plan: QueryPlan, named: list[int]
) -> tuple[list[str], list[str], list[int]]:
    note_ids = _named_notes(conn, plan, named)[:MAX_CONTEXT_NOTES]
    if not note_ids:
        return [], [], []
    stats = store.mention_stats(conn, note_ids, plan.date_from, plan.date_to)
    facts: list[str] = []
    titles: list[str] = []
    for row in store.notes_by_ids(conn, note_ids):
        title, kind = row["title"], row["kind"]
        titles.append(title)
        mentions = stats.get(row["id"])
        if mentions is None:
            facts.append(f"[[{title}]] ({kind}): not linked from any entry in range.")
            continue
        years = store.mentions_by_year(conn, row["id"], plan.date_from, plan.date_to)
        by_year = ", ".join(f"{y['year']}: {y['n']}" for y in years)
        facts.append(
            f"[[{title}]] ({kind}): linked from {mentions.count} entries, first "
            f"{mentions.first}, last {mentions.last}; by year {by_year}."
        )
    return facts, titles, note_ids


def metrics_named_in(question: str, conn: sqlite3.Connection) -> list[str]:
    """Metric keys the question uses as words, like "sleep" in "how did I sleep"."""
    text = _words(question)
    named = []
    for row in store.metric_keys(conn):
        words = _words(row["key"])
        if words and f" {words} " in f" {text} ":
            named.append(row["key"])
    return named


def _metric_text(metrics: dict[str, float]) -> str:
    return ", ".join(f"{key} {value:g}" for key, value in metrics.items())


def _filter_facts(conn: sqlite3.Connection, only: store.EntryFilter | None) -> list[str]:
    facts = []
    if only and only.note_ids:
        titles = " or ".join(f"[[{r['title']}]]" for r in store.notes_by_ids(conn, list(only.note_ids)))
        facts.append(f"Only entries that link {titles} are counted.")
    if only and only.group:
        key, value = only.group
        facts.append(f"Only entries that link a note whose {key} is {value} are counted.")
    if only and only.tag:
        facts.append(f"Only entries tagged '{only.tag}' are counted.")
    return facts


def _no_progress(_stage: str) -> None:
    pass


def duration(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    whole = round(seconds)
    if whole < 3600:
        return f"{whole // 60}m {whole % 60:02d}s"
    return f"{whole // 3600}h {whole % 3600 // 60:02d}m"


def timing_line(timings: Sequence[Timing]) -> str:
    """"Took 4m 10s: searching 1.2s, precise model reading 3m 02s, ...", or empty."""
    if not timings:
        return ""
    total = sum(t.seconds for t in timings)
    # steps too short to notice would only crowd the line
    shown = [f"{t.step} {duration(t.seconds)}" for t in timings if t.seconds >= 0.5]
    return f"Took {duration(total)}" + (": " + ", ".join(shown) if shown else "") + "."


def with_step(answer: Answer, step: str, seconds: float) -> Answer:
    """The answer with a step that came before it, like rewriting a follow-up."""
    first = Timing(step=step, seconds=round(seconds, 2))
    return answer.model_copy(update={"timings": [first, *answer.timings]})


class _Clock:
    """How long each step of an answer takes, one step at a time."""

    def __init__(self) -> None:
        self.steps: list[Timing] = []
        self._step: str | None = None
        self._since = 0.0

    def start(self, step: str) -> None:
        if step == self._step:
            return
        now = time.monotonic()
        if self._step is not None:
            self.steps.append(Timing(step=self._step, seconds=round(now - self._since, 2)))
        self._step, self._since = step, now

    def stop(self) -> list[Timing]:
        if self._step is not None:
            self.steps.append(
                Timing(step=self._step, seconds=round(time.monotonic() - self._since, 2))
            )
            self._step = None
        return self.steps


Kept = list[tuple[sqlite3.Row, str]]


def _make_room(
    client: OllamaClient,
    model: str,
    prompt: Callable[[Kept, Kept], list[dict[str, str]]],
    notes: Kept,
    entries: Kept,
) -> tuple[Kept, Kept]:
    """Leaves out the least relevant notes, then the least relevant entries,
    until the prompt leaves the model room to think in the window it would
    get anyway. Thinking gets cut off when the window runs out, and a whole
    note it can do without costs the answer less than that. The most relevant
    note and entry always stay; when they alone don't leave room, nothing is
    left out and the window grows instead."""
    think = client.think_room(model)
    if not think:
        return notes, entries
    window = client.planned_window(model, prompt_tokens(prompt(notes, entries)) + REPLY_TOKENS)
    kept_notes, kept_entries = list(notes), list(entries)
    while prompt_tokens(prompt(kept_notes, kept_entries)) + REPLY_TOKENS + think > window:
        if len(kept_notes) > 1:
            kept_notes.pop()
        elif len(kept_entries) > 1:
            kept_entries.pop()
        else:
            return notes, entries
    return kept_notes, kept_entries


def _answer_aggregate(
    question: str,
    plan: QueryPlan,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    named: list[int],
    only: store.EntryFilter | None = None,
    on_text: TextSink | None = None,
    writer: str = "",
    on_stage: Callable[[str], None] = _no_progress,
    on_thinking: TextSink | None = None,
    clock: _Clock | None = None,
) -> Answer:
    mood_rows = store.monthly_mood(conn, plan.date_from, plan.date_to, only)
    tag_rows = store.top_tags(
        conn, plan.date_from, plan.date_to, limit=AGGREGATE_TOP_TAGS, only=only
    )
    lo, hi = store.date_bounds(conn, plan.date_from, plan.date_to, only)
    if not mood_rows:
        return Answer(
            text="No journal entries found in the requested range.",
            stage_used="aggregate",
        )
    facts = _filter_facts(conn, only)
    facts.append(f"Monthly average mood (1-{config.indexing.mood_scale_max}) and entry counts:")
    for r in mood_rows:
        mood = f"{r['avg_mood']:g}" if r["avg_mood"] is not None else "n/a"
        facts.append(f"- {r['month']}: mood {mood}, {r['entry_count']} entries")
    if tag_rows:
        facts.append("Top tags: " + ", ".join(f"{r['name']} ({r['n']})" for r in tag_rows))
    note_facts, cited_notes, note_ids = _mention_facts(conn, plan, named)
    facts.extend(note_facts)
    for key in metrics_named_in(question, conn):
        months = store.monthly_metric(conn, key, plan.date_from, plan.date_to, only)
        if months:
            facts.append(f"Monthly average of '{key}', a number recorded in the entries:")
            facts.extend(f"- {r['month']}: {r['avg']:g} over {r['n']} entries" for r in months)
    facts.append(f"Data covers {lo} to {hi}.")
    fact_block = "\n".join(facts)

    text = _chat(
        client,
        config.ollama.fast_model,
        [
            {
                "role": "system",
                "content": (
                    "Answer the question about the asker's own journal using "
                    "ONLY the statistics provided. Do not invent numbers. Be "
                    f"concise. {_READER_VOICE}{writer}"
                ),
            },
            {"role": "user", "content": f"Question: {question}\n\nStatistics:\n{fact_block}"},
        ],
        {"temperature": SYNTHESIS_TEMPERATURE},
        on_text,
        _phases(on_stage, "fast", "Putting the numbers into words...", clock),
        on_thinking,
        _reading_bar(on_stage, "Putting the numbers into words..."),
        clock,
        "fast",
    ).strip()
    cited = [date.fromisoformat(d) for d in (lo, hi) if d]
    return _finalize(
        text, "aggregate", cited_dates=cited, cited_notes=cited_notes, read_notes=note_ids
    )


def _rrf_merge(
    ranked_lists: list[list[int]], k: int, damping: int = RRF_DAMPING
) -> list[int]:
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (damping + rank + 1)
    return [i for i, _ in sorted(scores.items(), key=lambda kv: -kv[1])][:k]


_RANKER_CACHE: dict[str, Any] = {}
# flashrank downloads its model on first use, which fails offline. Warn once.
_RERANK_WARNED = False


def _get_ranker(model_name: str) -> Any:
    if model_name not in _RANKER_CACHE:
        from flashrank import Ranker
        # it calls logging.basicConfig at this level, and at INFO every
        # request to Ollama gets logged over the answer
        _RANKER_CACHE[model_name] = Ranker(model_name=model_name, log_level="WARNING")
    return _RANKER_CACHE[model_name]


def _passage(row: sqlite3.Row, part: sqlite3.Row | None) -> str:
    # the cross-encoder only reads the start, so the part that matched goes first
    pieces = [row["summary"], part["text"] if part is not None else None, row["raw_text"]]
    return "\n\n".join(p for p in pieces if p)


def _rerank(
    question: str,
    rows: list[sqlite3.Row],
    model_name: str,
    top_k: int,
    parts: dict[int, sqlite3.Row] | None = None,
) -> list[sqlite3.Row]:
    global _RERANK_WARNED
    passages = [
        {"id": idx, "text": _passage(r, (parts or {}).get(r["id"]))}
        for idx, r in enumerate(rows)
    ]
    try:
        from flashrank import RerankRequest

        ranker = _get_ranker(model_name)
        reranked = ranker.rerank(RerankRequest(query=question, passages=passages))
    except Exception as exc:  # noqa: BLE001 - keep the RRF order instead
        if not _RERANK_WARNED:
            _RERANK_WARNED = True
            console.print(
                f"[yellow]Reranking unavailable ({exc}); falling back to "
                "hybrid-search order. Set reranking.enabled = false in "
                "config.toml to silence this.[/yellow]"
            )
        return rows[:top_k]
    return [rows[item["id"]] for item in reranked[:top_k]]


def _embed_question(
    question: str, config: Config, conn: sqlite3.Connection, client: OllamaClient
) -> list[float]:
    vector = embedder.embed_query(question, config, client)
    db.verify_embed_model(conn, len(vector), config.ollama.embed_model)
    return vector


def _retrieve(
    question: str,
    plan: QueryPlan,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    top_k: int = TOP_K,
    *,
    query_vector: list[float] | None = None,
    boost_note_ids: list[int] | None = None,
    only: store.EntryFilter | None = None,
    parts: dict[int, sqlite3.Row] | None = None,
) -> list[sqlite3.Row]:
    """`parts`, when given, collects the part of each entry found that matched best."""
    allowed = store.filtered_entry_ids(conn, plan.date_from, plan.date_to, plan.entities, only)
    if not allowed:
        return []

    fetch_k = max(RERANK_CANDIDATE_POOL, top_k) if config.reranking.enabled else top_k

    if query_vector is None:
        query_vector = _embed_question(question, config, conn, client)
    ranked_lists = [
        store.vec_search(conn, query_vector, allowed, fetch_k),
        store.fts_search(conn, plan.keywords + plan.entities, allowed, fetch_k),
    ]
    best: dict[int, sqlite3.Row] = {}
    if config.indexing.sections:
        ranked_lists.extend(
            _part_rankings(conn, query_vector, plan.keywords + plan.entities, allowed, fetch_k, best)
        )
    # what a vision model saw in the photos, which the entry's own text may not say
    ranked_lists.append(store.photos_fts_search(conn, plan.keywords + plan.entities, allowed, fetch_k))
    if boost_note_ids:
        # a third ranking, so linked entries get a lift without crowding out the rest
        allowed_set = set(allowed)
        linked = [i for i in store.entry_ids_mentioning(conn, boost_note_ids) if i in allowed_set]
        ranked_lists.append(linked[:fetch_k])
    merged = _rrf_merge(ranked_lists, fetch_k)
    if not merged:
        # no hits, so take the latest entries in range (allowed is date ordered)
        merged = allowed[-fetch_k:]
    rows = store.entries_by_ids(conn, merged)
    order = {eid: i for i, eid in enumerate(merged)}
    ranked = sorted(rows, key=lambda r: order.get(r["id"], len(order)))

    if config.reranking.enabled and len(ranked) > 1:
        ranked = _rerank(question, ranked, config.reranking.model, top_k, best)
    ranked = ranked[:top_k]
    if parts is not None:
        parts.update({r["id"]: best[r["id"]] for r in ranked if r["id"] in best})
    return ranked


def _part_rankings(
    conn: sqlite3.Connection,
    query_vector: list[float] | None,
    terms: list[str],
    allowed: list[int] | None,
    k: int,
    best: dict[int, sqlite3.Row],
) -> list[list[int]]:
    """Entries ranked by their best part, by meaning and by keywords.

    `best` collects that part for each entry: the one scoring highest across
    both rankings, the same fusion the entries get.
    """
    searches = [store.sections_fts_search(conn, terms, allowed, k)]
    if query_vector is not None:
        searches.insert(0, store.section_vec_search(conn, query_vector, allowed, k))
    rankings = []
    scores: dict[int, float] = {}
    found: dict[int, sqlite3.Row] = {}
    for hits in searches:
        ranked: dict[int, None] = {}
        for rank, part in enumerate(hits):
            ranked.setdefault(part["entry_id"], None)
            scores[part["id"]] = scores.get(part["id"], 0.0) + 1.0 / (RRF_DAMPING + rank + 1)
            found[part["id"]] = part
        if ranked:
            rankings.append(list(ranked))
    for part_id in sorted(scores, key=lambda i: -scores[i]):
        best.setdefault(found[part_id]["entry_id"], found[part_id])
    return rankings


def _matching_notes(
    conn: sqlite3.Connection,
    query_vector: list[float],
    terms: list[str],
    named: list[int],
    k: int,
) -> list[int]:
    ordered = dict.fromkeys(named)
    by_meaning = store.notes_vec_search(conn, query_vector, k)
    by_keyword = store.notes_fts_search(conn, terms, k)
    for note_id in _rrf_merge([by_meaning, by_keyword], k):
        ordered.setdefault(note_id, None)
    return list(ordered)


def _retrieve_notes(
    plan: QueryPlan,
    conn: sqlite3.Connection,
    query_vector: list[float],
    about: list[int],
    candidates: list[sqlite3.Row],
) -> list[sqlite3.Row]:
    ordered = dict.fromkeys(
        _matching_notes(conn, query_vector, plan.keywords + plan.entities, about, NOTE_SEARCH_K)
    )
    for row in store.notes_linked_from_entries(conn, [r["id"] for r in candidates], LINKED_NOTES_K):
        ordered.setdefault(row["note_id"], None)
    return store.notes_by_ids(conn, list(ordered)[:MAX_CONTEXT_NOTES])


_SEARCH_STOPWORDS = frozenset(
    """a an and are but did for from had has have how the that this was were
    what when where which who why with you your about into over under""".split()
)
_SEARCH_WORD_RE = re.compile(r"(?<!\w)[^\W_][\w'-]{2,}")


@dataclass(frozen=True)
class SearchResults:
    entries: list[sqlite3.Row]
    notes: list[sqlite3.Row]
    # entry id -> the part of it that matched best, for entries cut into parts
    parts: dict[int, sqlite3.Row] = field(default_factory=dict)


def search(
    text: str,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    limit: int = 10,
    date_from: date | None = None,
    date_to: date | None = None,
    only: store.EntryFilter | None = None,
) -> SearchResults:
    """For `journal search`: no router and no answer, just the query embedding."""
    keywords = search_keywords(text)
    plan = QueryPlan(
        intent="factual", date_from=date_from, date_to=date_to, keywords=keywords
    )
    query_vector = _embed_question(text, config, conn, client)
    named = notes_named_in(text, conn)
    parts: dict[int, sqlite3.Row] = {}
    entries = _retrieve(
        text, plan, config, conn, client, top_k=limit,
        query_vector=query_vector, boost_note_ids=named, only=only, parts=parts,
    )
    note_ids = _matching_notes(conn, query_vector, keywords, named, NOTE_SEARCH_K)
    return SearchResults(entries=entries, notes=store.notes_by_ids(conn, note_ids), parts=parts)


def search_keywords(text: str) -> list[str]:
    return [
        w for w in _SEARCH_WORD_RE.findall(text.lower())
        if w not in _SEARCH_STOPWORDS
    ][:8]


def keyword_search(
    text: str,
    conn: sqlite3.Connection,
    limit: int = 10,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SearchResults:
    """`search` without the embedding, so it works while Ollama is down."""
    keywords = search_keywords(text)
    named = notes_named_in(text, conn)
    allowed = (
        store.filtered_entry_ids(conn, date_from, date_to) if date_from or date_to else None
    )
    ranked_lists = [
        store.fts_search(conn, keywords, allowed, limit),
        store.photos_fts_search(conn, keywords, allowed, limit),
    ]
    best: dict[int, sqlite3.Row] = {}
    ranked_lists.extend(_part_rankings(conn, None, keywords, allowed, limit, best))
    if named:
        allowed_set = set(allowed) if allowed is not None else None
        linked = [
            i for i in store.entry_ids_mentioning(conn, named)
            if allowed_set is None or i in allowed_set
        ]
        ranked_lists.append(linked[:limit])
    merged = _rrf_merge(ranked_lists, limit)
    order = {eid: i for i, eid in enumerate(merged)}
    entries = sorted(store.entries_by_ids(conn, merged), key=lambda r: order[r["id"]])
    parts = {eid: best[eid] for eid in merged if eid in best}
    note_ids = list(
        dict.fromkeys([*named, *store.notes_fts_search(conn, keywords, NOTE_SEARCH_K)])
    )
    return SearchResults(entries=entries, notes=store.notes_by_ids(conn, note_ids), parts=parts)


def _ensure_day_included(
    conn: sqlite3.Connection,
    day: date,
    candidates: list[sqlite3.Row],
    only: store.EntryFilter | None = None,
) -> list[sqlite3.Row]:
    have = {r["id"] for r in candidates}
    allowed = set(store.filtered_entry_ids(conn, day, day, only=only))
    day_rows = [r for r in store.entries_by_ids(conn, list(allowed)) if r["id"] not in have]
    # in front, so the raw-text budget can't drop it
    return day_rows + candidates


def _note_line(
    row: sqlite3.Row,
    mentions: store.Mentions | None,
    vault: Path,
    learned: Sequence[str] = (),
) -> str:
    """`learned` are the names its links are written with (nicknames)."""
    note = store.note_from_row(row, vault)
    facts = [
        note.kind,
        f"file: {note.rel_path}",
        *(f"{key}: {value}" for key, value in note.properties.items()),
    ]
    if note.aliases:
        facts.append("also called " + ", ".join(note.aliases))
    if learned:
        facts.append("the journal also writes it as " + ", ".join(learned))
    if mentions is not None:
        facts.append(
            f"linked from {mentions.count} entries, {mentions.first} to {mentions.last}"
        )
    body = row["summary"] or note.raw_text or "(the note itself has no text)"
    return f"- [[{note.title}]] ({'; '.join(facts)}): {' '.join(body.split())}"


def _learned_names(conn: sqlite3.Connection, notes: list[sqlite3.Row]) -> dict[int, list[str]]:
    found = store.learned_names_for(conn, [r["id"] for r in notes])
    return {note_id: [r["name"] for r in rows] for note_id, rows in found.items()}


def _range_digest_lines(plan: QueryPlan, conn: sqlite3.Connection) -> list[str]:
    if not (plan.date_from and plan.date_to):
        return []
    lo, hi = plan.date_from, plan.date_to

    def shared(row: sqlite3.Row) -> int:
        start, end = store.period_bounds("range", row["period_key"])
        return (min(end, hi) - max(start, lo)).days + 1

    rows = sorted(store.range_rollups_overlapping(conn, lo, hi), key=lambda r: (-shared(r), r["period_key"]))
    return [
        f"- [{store.period_title('range', r['period_key'])}] {r['summary']}"
        for r in rows[:MAX_RANGE_DIGESTS]
    ]


def _theme_lines(plan: QueryPlan, conn: sqlite3.Connection) -> list[str]:
    spans = bool(plan.date_from and plan.date_to and (plan.date_to - plan.date_from).days >= THEME_MIN_DAYS)
    if plan.intent != "narrative" and not spans:
        return []
    return [
        f"- [theme] {t.name}: {t.about or 'no description'} ({n} of its {t.entries} entries fall here)"
        for t, n in themes.in_range(conn, plan.date_from, plan.date_to, MAX_THEME_LINES)
    ]


def _digest_lines(plan: QueryPlan, conn: sqlite3.Connection) -> list[str]:
    return _range_digest_lines(plan, conn) + _period_digest_lines(plan, conn) + _theme_lines(plan, conn)


def _period_digest_lines(plan: QueryPlan, conn: sqlite3.Connection) -> list[str]:
    # An unbounded narrative question covers the whole journal.
    if plan.intent != "narrative" and not (plan.date_from and plan.date_to):
        return []
    lo, hi = store.date_bounds(conn, plan.date_from, plan.date_to)
    if lo is None or hi is None:
        return []
    start, end = date.fromisoformat(lo), date.fromisoformat(hi)
    days = (end - start).days
    if days < WEEK_DIGEST_MIN_DAYS:
        return []
    if days <= BROAD_RANGE_DAYS:
        weeks = store.get_rollups_in_range(conn, "week", start, end)[-MAX_WEEK_DIGESTS:]
        return [f"- [week {r['period_key']}] {r['summary']}" for r in weeks]
    months = store.get_rollups_in_range(conn, "month", start, end)
    if len(months) > MAX_MONTH_DIGESTS:
        years = store.get_rollups_in_range(conn, "year", start, end)
        if years:
            return [f"- [year {r['period_key']}] {r['summary']}" for r in years]
        months = months[-MAX_MONTH_DIGESTS:]
    return [f"- [month {r['period_key']}] {r['summary']}" for r in months]


def _relationship_digests(
    conn: sqlite3.Connection, config: Config, note_ids: list[int]
) -> dict[int, str]:
    """Relationship digests for these notes, by note id."""
    if config.rollups.note_min_mentions == 0 or not note_ids:
        return {}
    rows = store.notes_by_ids(conn, note_ids[:MAX_CONTEXT_NOTES])
    found = store.note_digests_for(conn, [r["link_key"] for r in rows])
    return {
        r["id"]: found[r["link_key"]]["summary"]
        for r in rows
        if r["link_key"] in found and found[r["link_key"]]["summary"]
    }


def _summary_context(
    candidates: list[sqlite3.Row],
    notes: list[sqlite3.Row],
    plan: QueryPlan,
    config: Config,
    conn: sqlite3.Connection,
    about: Sequence[int] = (),
    parts: dict[int, sqlite3.Row] | None = None,
) -> str:
    """`parts` is the part of each entry that matched the question best, when one did."""
    vault = config.vault.path
    sections: list[str] = []
    linked = store.entities_for_entries(conn, [r["id"] for r in candidates])
    metrics = store.metrics_for_entries(conn, [r["id"] for r in candidates])
    longer = context.longer_summaries(conn, candidates, ANSWER_DETAILS_CHARS)
    room = ANSWER_PARTS_CHARS
    lines = []
    for r in candidates:
        details = [f"file: {parser.relative_path(r['file_path'], vault)}"]
        names = [n["name"] for n in linked.get(r["id"], [])]
        if names:
            details.append(f"links: {', '.join(names)}")
        if r["id"] in metrics:
            details.append(f"recorded: {_metric_text(metrics[r['id']])}")
        summary = longer.get(r["id"]) or r["summary"] or "(no summary)"
        lines.append(f"- [{r['entry_date']}] {summary} ({'; '.join(details)})")
        part = (parts or {}).get(r["id"])
        if part is not None:
            text = context.clip(part["text"], ANSWER_PART_CHARS)
            if len(text) <= room:
                room -= len(text)
                where = f'under "{part["heading"]}"' if part["heading"] else "before its first heading"
                lines.append(f"  The part of that entry that matches, {where}: {text}")
    lines.extend(_digest_lines(plan, conn))
    if lines:
        sections.append("Journal entries:\n" + "\n".join(lines))
    if notes:
        mentions = store.mention_stats(conn, [r["id"] for r in notes])
        learned = _learned_names(conn, notes)
        sections.append(
            "Reference notes:\n"
            + "\n".join(
                _note_line(r, mentions.get(r["id"]), vault, learned.get(r["id"], [])) for r in notes
            )
        )
    digests = _relationship_digests(conn, config, list(about))
    if digests:
        titles = {r["id"]: r["title"] for r in store.notes_by_ids(conn, list(digests))}
        sections.append(
            "How they come up in the journal over the years:\n"
            + "\n".join(f"- [[{titles[i]}]]: {text}" for i, text in digests.items())
        )
    return "\n\n".join(sections)


# small models like to send "**INSUFFICIENT**" or "Insufficient."
_INSUFFICIENT_RE = re.compile(r"\W*insufficient\b", re.IGNORECASE)
_INSUFFICIENT = "insufficient"

TextSink = Callable[[str], None]


class _TrimStart:
    """Passes text on once the first non-space arrives, so a reply never opens on a blank."""

    def __init__(self, sink: TextSink):
        self._sink = sink
        self._started = False

    def __call__(self, text: str) -> None:
        if not self._started:
            text = text.lstrip()
            if not text:
                return
            self._started = True
        self._sink(text)


class _HoldUntilAnswer:
    """Holds a fast reply back until it can't turn out to be INSUFFICIENT."""

    def __init__(self, sink: TextSink):
        self._sink = sink
        self._held = ""
        self.passing: bool | None = None

    def __call__(self, text: str) -> None:
        if self.passing:
            self._sink(text)
            return
        if self.passing is False:
            return
        self._held += text
        word = re.sub(r"^\W+", "", self._held).lower()
        if not word or (_INSUFFICIENT.startswith(word) and len(word) <= len(_INSUFFICIENT)):
            return  # could still be the word, or the word with nothing after it yet
        if _INSUFFICIENT_RE.match(self._held):
            self.passing = False
            return
        self.passing = True
        self._sink(self._held)

    def finish(self, full: str) -> None:
        if self.passing is None and self._held.strip() and not _INSUFFICIENT_RE.match(full):
            self._sink(self._held)


def _chat(
    client: OllamaClient,
    model: str,
    messages: list[dict[str, str]],
    options: dict[str, Any],
    on_text: TextSink | None,
    on_phase: Callable[[str], None] | None = None,
    on_thinking: TextSink | None = None,
    on_reading: Callable[[float, float], None] | None = None,
    clock: _Clock | None = None,
    role: str = "",
) -> str:
    if on_text is None:
        # a streamed call's phases split it into steps; this one is a single step
        if clock is not None:
            clock.start(f"{role} model")
        return client.chat(model=model, messages=messages, options=options)
    sink = on_text

    def written(text: str) -> None:
        if clock is not None and text.strip():
            clock.start(f"{role} model writing")
        sink(text)

    return client.chat_stream(
        model, messages, _TrimStart(written), options=options,
        on_phase=on_phase, on_thinking=on_thinking, on_reading=on_reading,
    )


def _phases(
    on_stage: Callable[[str], None], role: str, reading: str, clock: _Clock | None = None
) -> Callable[[str], None]:
    """Stage lines for a streamed call, so a long wait says what it's waiting on,
    and the steps it times."""
    said = {
        "loading": f"Loading the {role} model into memory...",
        "reading": reading,
        "thinking": "Thinking before it answers...",
        "answering": "Out of room to think, so answering without it...",
    }
    steps = {
        "loading": f"loading the {role} model",
        "reading": f"{role} model reading",
        "thinking": f"{role} model thinking",
        "answering": f"{role} model answering without thinking",
    }

    def phase(name: str) -> None:
        if clock is not None and name in steps:
            clock.start(steps[name])
        if name in said:
            on_stage(said[name])

    return phase


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _time_left(seconds: float) -> str:
    if seconds < 60:
        return "under a minute left"
    return f"about {_count(round(seconds / 60), 'minute', 'minutes')} left"


def _reading_bar(on_stage: Callable[[str], None], reading: str) -> Callable[[float, float], None]:
    """A stage line with how far through the reading it likely is, from how
    long the same amount took before."""

    def show(elapsed: float, estimate: float) -> None:
        share = min(elapsed / estimate, 0.99) if estimate > 0 else 0.99
        filled = round(share * PROGRESS_WIDTH)
        bar = "\u2588" * filled + "\u2591" * (PROGRESS_WIDTH - filled)
        left = estimate - elapsed
        when = _time_left(left) if left > 0 else "taking longer than last time"
        on_stage(f"{reading} {bar} {share:.0%}, {when}")

    return show


def _fast_attempt(
    question: str,
    context: str,
    config: Config,
    client: OllamaClient,
    on_text: TextSink | None = None,
    writer: str = "",
    on_stage: Callable[[str], None] = _no_progress,
    on_thinking: TextSink | None = None,
    clock: _Clock | None = None,
) -> str | None:
    hold = _HoldUntilAnswer(on_text) if on_text else None
    response = _chat(
        client,
        config.ollama.fast_model,
        [
            {
                "role": "system",
                "content": (
                    "Answer the question using ONLY the journal entry "
                    "summaries and reference notes below. Reference notes "
                    "describe the people, places and topics the journal "
                    "links to, and carry no dates. Each item names the file "
                    "it comes from; folder and file names can say what it is "
                    "about. Mention the dates or [[note names]] your answer "
                    f"draws on. {_READER_VOICE} If the material does not "
                    "contain enough information to answer confidently, reply "
                    f"with exactly the single word INSUFFICIENT.{writer}"
                ),
            },
            {"role": "user", "content": f"Question: {question}\n\n{context}"},
        ],
        {"temperature": SYNTHESIS_TEMPERATURE},
        hold,
        _phases(on_stage, "fast", "Answering from summaries...", clock),
        on_thinking,
        _reading_bar(on_stage, "Answering from summaries..."),
        clock,
        "fast",
    ).strip()
    if hold is not None:
        hold.finish(response)
    # nothing at all is no answer either, so the precise model gets a go
    if not response or _INSUFFICIENT_RE.match(response):
        return None
    return response


def _precise_reanalysis(
    question: str,
    candidates: list[sqlite3.Row],
    notes: list[sqlite3.Row],
    config: Config,
    client: OllamaClient,
    digests: dict[int, str] | None = None,
    on_text: TextSink | None = None,
    conn: sqlite3.Connection | None = None,
    writer: str = "",
    on_stage: Callable[[str], None] = _no_progress,
    on_thinking: TextSink | None = None,
    clock: _Clock | None = None,
) -> tuple[str, list[sqlite3.Row], list[sqlite3.Row]]:
    ranked_notes: list[tuple[sqlite3.Row, str]] = []
    note_chars = 0
    vault = config.vault.path
    learned = _learned_names(conn, notes) if conn is not None else {}
    for row in notes:  # most relevant first
        note = store.note_from_row(row, vault)
        over_years = (digests or {}).get(row["id"])
        history = f"\nOver the years: {over_years}" if over_years else ""
        written = (
            "\nThe journal also writes it as: " + ", ".join(learned[row["id"]])
            if row["id"] in learned else ""
        )
        block = f"### Note: {note.header()}{written}{history}\n{note.raw_text}".rstrip()
        if ranked_notes and note_chars + len(block) > MAX_NOTE_CONTEXT_CHARS:
            continue
        block = block[:MAX_NOTE_CONTEXT_CHARS]
        ranked_notes.append((row, block))
        note_chars += len(block)

    budget = MAX_RAW_CONTEXT_CHARS - note_chars
    used: list[tuple[sqlite3.Row, str]] = []
    total = 0
    ids = [r["id"] for r in candidates]
    recorded = store.metrics_for_entries(conn, ids) if conn else {}
    seen = store.photos_for_entries(conn, ids) if conn else {}
    photo_text = {
        entry_id: "\n" + "\n".join(f"Photo {p['name']}: {p['description']}" for p in found)
        for entry_id, found in seen.items()
    }
    for row in candidates:  # best first
        heading = (
            f"### Entry {row['entry_date']}\n"
            f"File: {parser.relative_path(row['file_path'], vault)}\n"
        )
        if row["id"] in recorded:
            heading += f"Recorded: {_metric_text(recorded[row['id']])}\n"
        cost = len(heading) + len(row["raw_text"]) + len(photo_text.get(row["id"], ""))
        if used and total + cost > budget:
            continue
        used.append((row, heading))
        total += cost
    ranked_entries = [
        # the first entry always gets in, so it alone can be over budget
        (row, (heading + row["raw_text"] + photo_text.get(row["id"], ""))[:budget])
        for row, heading in used
    ]
    system = (
        "You answer questions about a personal journal from the "
        "full entry texts and reference notes provided. Reference "
        "notes describe the people, places and topics the journal "
        "links to, and carry no dates. Each one names the file it "
        "comes from; folder and file names can say what it is "
        "about. Base the answer strictly on this material; mention "
        f"the dates or [[note names]] it draws on. {_READER_VOICE} "
        f"If it does not answer the question, say so.{writer}"
    )

    def prompt(
        kept_notes: list[tuple[sqlite3.Row, str]], kept_entries: list[tuple[sqlite3.Row, str]]
    ) -> list[dict[str, str]]:
        by_date = sorted(kept_entries, key=lambda pair: pair[0]["entry_date"])
        blocks = [b for _, b in kept_notes] + [b for _, b in by_date]
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Question: {question}\n\n" + "\n\n".join(blocks)},
        ]

    kept_notes, kept_entries = _make_room(
        client, config.ollama.precise_model, prompt, ranked_notes, ranked_entries
    )
    used_notes = [row for row, _ in kept_notes]
    read_rows = [row for row, _ in kept_entries]
    messages = prompt(kept_notes, kept_entries)
    truncated = len(read_rows) < len(candidates) or len(used_notes) < len(notes)
    entry_blocks = [b for _, b in kept_entries]
    note_blocks = [b for _, b in kept_notes]
    omitted = (
        "\n\n(Note: some lower-ranked entries or notes were omitted to fit the "
        "context budget.)" if truncated else ""
    )
    words = sum(len(block.split()) for block in note_blocks + entry_blocks)
    read = _count(len(read_rows), "entry", "entries")
    if used_notes:
        read += f" and {_count(len(used_notes), 'note', 'notes')}"
    reading = f"Re-reading {read} in full, about {words:,} words..."
    on_stage(reading)
    text = _chat(
        client,
        config.ollama.precise_model,
        messages,
        {"temperature": SYNTHESIS_TEMPERATURE},
        on_text,
        _phases(on_stage, "precise", reading, clock),
        on_thinking,
        _reading_bar(on_stage, reading),
        clock,
        "precise",
    ).strip() + omitted
    if on_text and omitted:
        on_text(omitted)
    return text, read_rows, used_notes


def answer(
    question: str,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    today: date | None = None,
    on_stage: Callable[[str], None] = _no_progress,
    since: date | None = None,
    until: date | None = None,
    only: store.EntryFilter | None = None,
    on_text: TextSink | None = None,
    on_thinking: TextSink | None = None,
) -> Answer:
    """`since`, `until` and `only` are what the person asked for, so they
    override whatever the router reads into the question. With `on_text`, the
    answer is handed over piece by piece as the model writes it, and with
    `on_thinking` so is the model's reasoning, if [ollama] show_thinking is on.
    The answer's timings say how long each step took."""
    clock = _Clock()
    result = _answer(
        question, config, conn, client, today, on_stage, since, until, only, on_text,
        on_thinking, clock,
    )
    writer = {
        "fast_summary": config.ollama.fast_model,
        "aggregate": config.ollama.fast_model,
        "precise_raw": config.ollama.precise_model,
    }.get(result.stage_used, "")
    return result.model_copy(update={"timings": clock.stop(), "model": writer})


def _answer(
    question: str,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    today: date | None,
    on_stage: Callable[[str], None],
    since: date | None,
    until: date | None,
    only: store.EntryFilter | None,
    on_text: TextSink | None,
    on_thinking: TextSink | None,
    clock: _Clock,
) -> Answer:
    today = today or date.today()
    if not config.ollama.show_thinking:
        on_thinking = None
    writer = profile.context(conn, config)
    on_stage("Working out what you're asking...")
    clock.start("working out the question")
    plan = _plan(question, config, client, today, profile.names(conn, config))
    if since is not None or until is not None:
        plan = plan.model_copy(update={"date_from": since, "date_to": until})
    named = notes_named_in(question, conn)
    if only and only.note_ids:
        named = list(dict.fromkeys([*only.note_ids, *named]))

    if plan.intent == "aggregate":
        on_stage("Putting the numbers into words...")
        return _answer_aggregate(
            question, plan, config, conn, client, named, only, on_text, writer,
            on_stage, on_thinking, clock,
        )

    on_stage("Searching entries and notes...")
    clock.start("searching")
    about = _named_notes(conn, plan, named)
    query_vector = _embed_question(question, config, conn, client)
    found_parts: dict[int, sqlite3.Row] = {}
    candidates = _retrieve(
        question, plan, config, conn, client,
        query_vector=query_vector, boost_note_ids=named, only=only, parts=found_parts,
    )
    explicit_day = _explicit_day(question, today)
    if explicit_day is not None and (since is None or since <= explicit_day) and (
        until is None or explicit_day <= until
    ):
        candidates = _ensure_day_included(conn, explicit_day, candidates, only)
    notes = _retrieve_notes(plan, conn, query_vector, about, candidates)
    if not candidates and not notes:
        return Answer(
            text="No journal entries or reference notes matched the question.",
            stage_used="retrieval_empty",
        )
    wanted = quotes.terms(
        [*search_keywords(question), *search_keywords(" ".join(plan.keywords + plan.entities))]
    )

    if not plan.needs_raw_text:
        on_stage("Answering from summaries...")
        context = _summary_context(candidates, notes, plan, config, conn, about, found_parts)
        fast_answer = _fast_attempt(
            question, context, config, client, on_text, writer, on_stage, on_thinking, clock
        )
        if fast_answer is not None:
            return _finalize(
                fast_answer,
                "fast_summary",
                cited_dates=[date.fromisoformat(r["entry_date"]) for r in candidates],
                cited_notes=_notes_used(fast_answer, notes, about, _learned_names(conn, notes)),
                read_entries=[r["id"] for r in candidates],
                read_notes=[r["id"] for r in notes],
                cited_parts=_cited_parts(candidates, found_parts),
                quoted=quotes.for_days(candidates, wanted, found_parts),
            )

    text, used, used_notes = _precise_reanalysis(
        question, candidates, notes, config, client,
        digests=_relationship_digests(conn, config, about),
        on_text=on_text,
        conn=conn,
        writer=writer,
        on_stage=on_stage,
        on_thinking=on_thinking,
        clock=clock,
    )
    return _finalize(
        text,
        "precise_raw",
        cited_dates=[date.fromisoformat(r["entry_date"]) for r in used],
        cited_notes=_notes_used(text, used_notes, about, _learned_names(conn, used_notes)),
        read_entries=[r["id"] for r in used],
        read_notes=[r["id"] for r in used_notes],
        cited_parts=_cited_parts(used, found_parts),
        quoted=quotes.for_days(used, wanted, found_parts),
    )


def _cited_parts(rows: list[sqlite3.Row], parts: dict[int, sqlite3.Row]) -> dict[str, str]:
    """The heading of each cited day's best part; the first entry of a day wins."""
    cited: dict[str, str] = {}
    for r in rows:
        part = parts.get(r["id"])
        if part is not None and part["heading"]:
            cited.setdefault(r["entry_date"], part["heading"])
    return cited
