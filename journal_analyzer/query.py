"""Question answering.

The fast model routes the question. Aggregate questions are answered from SQL.
The rest go through hybrid retrieval, then the fast model tries to answer from
summaries, and the precise model re-reads the full text if it can't.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from rich.console import Console

from . import db, embedder, parser, store
from .config import Config
from .models import Answer, AnswerStage, QueryPlan
from .ollama_client import SYNTHESIS_TEMPERATURE, OllamaClient

console = Console(stderr=True)

TOP_K = 8
# roughly 24-32k tokens; the client raises num_ctx to fit
MAX_RAW_CONTEXT_CHARS = 96_000
# ranges wider than this also get month digests
BROAD_RANGE_DAYS = 62
# more months than this and we send year digests instead
MAX_MONTH_DIGESTS = 14
# the reranker can only reorder what retrieval found, so fetch extra
RERANK_CANDIDATE_POOL = 25
# 60 is the k from the original RRF paper
RRF_DAMPING = 60
AGGREGATE_TOP_TAGS = 10
NOTE_SEARCH_K = 3
LINKED_NOTES_K = 3
MAX_CONTEXT_NOTES = 8
# comes out of MAX_RAW_CONTEXT_CHARS
MAX_NOTE_CONTEXT_CHARS = 16_000
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
) -> str:
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
            {"role": "system", "content": _CONTEXTUALIZE_SYSTEM},
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


def _plan(question: str, config: Config, client: OllamaClient, today: date) -> QueryPlan:
    return client.chat_structured(
        model=config.ollama.fast_model,
        messages=[
            {"role": "system", "content": _ROUTER_SYSTEM.format(today=today.isoformat())},
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

    Longer names go first, so "Sara Miller" doesn't also match a note called "Sara".
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
    answer_text: str, notes: list[sqlite3.Row], about: list[int]
) -> list[str]:
    # Background notes the answer never mentions would pad every Sources line.
    said = f" {_words(answer_text)} "
    cited: list[str] = []
    for row in notes:
        names = [row["title"], *store.note_from_row(row).aliases]
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
) -> Answer:
    dates = sorted(set(cited_dates))
    notes = sorted(set(cited_notes), key=str.casefold)
    links = [f"[[{d.isoformat()}]]" for d in dates] + [f"[[{n}]]" for n in notes]
    if links:
        text = f"{text.rstrip()}\n\nSources: {' '.join(links)}"
    return Answer(text=text, cited_dates=dates, cited_notes=notes, stage_used=stage)


def _mention_facts(
    conn: sqlite3.Connection, plan: QueryPlan, named: list[int]
) -> tuple[list[str], list[str]]:
    note_ids = _named_notes(conn, plan, named)[:MAX_CONTEXT_NOTES]
    if not note_ids:
        return [], []
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
    return facts, titles


def _answer_aggregate(
    question: str,
    plan: QueryPlan,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    named: list[int],
) -> Answer:
    mood_rows = store.monthly_mood(conn, plan.date_from, plan.date_to)
    tag_rows = store.top_tags(conn, plan.date_from, plan.date_to, limit=AGGREGATE_TOP_TAGS)
    lo, hi = store.date_bounds(conn, plan.date_from, plan.date_to)
    if not mood_rows:
        return Answer(
            text="No journal entries found in the requested range.",
            stage_used="aggregate",
        )
    facts = [f"Monthly average mood (1-{config.indexing.mood_scale_max}) and entry counts:"]
    for r in mood_rows:
        mood = f"{r['avg_mood']:g}" if r["avg_mood"] is not None else "n/a"
        facts.append(f"- {r['month']}: mood {mood}, {r['entry_count']} entries")
    if tag_rows:
        facts.append("Top tags: " + ", ".join(f"{r['name']} ({r['n']})" for r in tag_rows))
    note_facts, cited_notes = _mention_facts(conn, plan, named)
    facts.extend(note_facts)
    facts.append(f"Data covers {lo} to {hi}.")
    fact_block = "\n".join(facts)

    text = client.chat(
        model=config.ollama.fast_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Answer the question about the asker's own journal using "
                    "ONLY the statistics provided. Do not invent numbers. Be "
                    f"concise. {_READER_VOICE}"
                ),
            },
            {"role": "user", "content": f"Question: {question}\n\nStatistics:\n{fact_block}"},
        ],
        options={"temperature": SYNTHESIS_TEMPERATURE},
    ).strip()
    cited = [date.fromisoformat(d) for d in (lo, hi) if d]
    return _finalize(text, "aggregate", cited_dates=cited, cited_notes=cited_notes)


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
        _RANKER_CACHE[model_name] = Ranker(model_name=model_name)
    return _RANKER_CACHE[model_name]


def _rerank(
    question: str, rows: list[sqlite3.Row], model_name: str, top_k: int
) -> list[sqlite3.Row]:
    global _RERANK_WARNED
    passages = [
        {
            "id": idx,
            "text": f"{r['summary']}\n\n{r['raw_text']}" if r["summary"] else r["raw_text"],
        }
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
) -> list[sqlite3.Row]:
    allowed = store.filtered_entry_ids(conn, plan.date_from, plan.date_to, plan.entities)
    if not allowed:
        return []

    fetch_k = max(RERANK_CANDIDATE_POOL, top_k) if config.reranking.enabled else top_k

    if query_vector is None:
        query_vector = _embed_question(question, config, conn, client)
    ranked_lists = [
        store.vec_search(conn, query_vector, allowed, fetch_k),
        store.fts_search(conn, plan.keywords + plan.entities, allowed, fetch_k),
    ]
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

    if not config.reranking.enabled or len(ranked) <= 1:
        return ranked[:top_k]
    return _rerank(question, ranked, config.reranking.model, top_k)


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


def search(
    text: str,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    limit: int = 10,
    date_from: date | None = None,
    date_to: date | None = None,
) -> SearchResults:
    """For `journal search`: no router and no answer, just the query embedding."""
    keywords = [
        w for w in _SEARCH_WORD_RE.findall(text.lower())
        if w not in _SEARCH_STOPWORDS
    ][:8]
    plan = QueryPlan(
        intent="factual", date_from=date_from, date_to=date_to, keywords=keywords
    )
    query_vector = _embed_question(text, config, conn, client)
    named = notes_named_in(text, conn)
    entries = _retrieve(
        text, plan, config, conn, client, top_k=limit,
        query_vector=query_vector, boost_note_ids=named,
    )
    note_ids = _matching_notes(conn, query_vector, keywords, named, NOTE_SEARCH_K)
    return SearchResults(entries=entries, notes=store.notes_by_ids(conn, note_ids))


def _ensure_day_included(
    conn: sqlite3.Connection,
    day: date,
    candidates: list[sqlite3.Row],
) -> list[sqlite3.Row]:
    have = {r["id"] for r in candidates}
    day_rows = [r for r in store.entries_in_range(conn, day, day) if r["id"] not in have]
    # in front, so the raw-text budget can't drop it
    return day_rows + candidates


def _note_line(row: sqlite3.Row, mentions: store.Mentions | None, vault: Path) -> str:
    note = store.note_from_row(row, vault)
    facts = [
        note.kind,
        f"file: {note.rel_path}",
        *(f"{key}: {value}" for key, value in note.properties.items()),
    ]
    if note.aliases:
        facts.append("also called " + ", ".join(note.aliases))
    if mentions is not None:
        facts.append(
            f"linked from {mentions.count} entries, {mentions.first} to {mentions.last}"
        )
    body = row["summary"] or note.raw_text or "(the note itself has no text)"
    return f"- [[{note.title}]] ({'; '.join(facts)}): {' '.join(body.split())}"


def _digest_lines(plan: QueryPlan, conn: sqlite3.Connection) -> list[str]:
    # An unbounded narrative question covers the whole journal.
    if plan.intent != "narrative" and not (plan.date_from and plan.date_to):
        return []
    lo, hi = store.date_bounds(conn, plan.date_from, plan.date_to)
    if lo is None or hi is None:
        return []
    start, end = date.fromisoformat(lo), date.fromisoformat(hi)
    if (end - start).days <= BROAD_RANGE_DAYS:
        return []
    months = store.get_rollups_in_range(conn, "month", start, end)
    if len(months) > MAX_MONTH_DIGESTS:
        years = store.get_rollups_in_range(conn, "year", start, end)
        if years:
            return [f"- [year {r['period_key']}] {r['summary']}" for r in years]
        months = months[-MAX_MONTH_DIGESTS:]
    return [f"- [month {r['period_key']}] {r['summary']}" for r in months]


def _summary_context(
    candidates: list[sqlite3.Row],
    notes: list[sqlite3.Row],
    plan: QueryPlan,
    config: Config,
    conn: sqlite3.Connection,
) -> str:
    vault = config.vault.path
    sections: list[str] = []
    linked = store.entities_for_entries(conn, [r["id"] for r in candidates])
    lines = []
    for r in candidates:
        details = [f"file: {parser.relative_path(r['file_path'], vault)}"]
        names = [n["name"] for n in linked.get(r["id"], [])]
        if names:
            details.append(f"links: {', '.join(names)}")
        lines.append(
            f"- [{r['entry_date']}] {r['summary'] or '(no summary)'} ({'; '.join(details)})"
        )
    lines.extend(_digest_lines(plan, conn))
    if lines:
        sections.append("Journal entries:\n" + "\n".join(lines))
    if notes:
        mentions = store.mention_stats(conn, [r["id"] for r in notes])
        sections.append(
            "Reference notes:\n"
            + "\n".join(_note_line(r, mentions.get(r["id"]), vault) for r in notes)
        )
    return "\n\n".join(sections)


# small models like to send "**INSUFFICIENT**" or "Insufficient."
_INSUFFICIENT_RE = re.compile(r"\W*insufficient\b", re.IGNORECASE)


def _fast_attempt(
    question: str,
    context: str,
    config: Config,
    client: OllamaClient,
) -> str | None:
    response = client.chat(
        model=config.ollama.fast_model,
        messages=[
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
                    "with exactly the single word INSUFFICIENT."
                ),
            },
            {"role": "user", "content": f"Question: {question}\n\n{context}"},
        ],
        options={"temperature": SYNTHESIS_TEMPERATURE},
    ).strip()
    if _INSUFFICIENT_RE.match(response):
        return None
    return response


def _precise_reanalysis(
    question: str,
    candidates: list[sqlite3.Row],
    notes: list[sqlite3.Row],
    config: Config,
    client: OllamaClient,
) -> tuple[str, list[date], list[sqlite3.Row]]:
    note_blocks: list[str] = []
    used_notes: list[sqlite3.Row] = []
    note_chars = 0
    vault = config.vault.path
    for row in notes:  # most relevant first
        note = store.note_from_row(row, vault)
        block = f"### Note: {note.header()}\n{note.raw_text}".rstrip()
        if note_blocks and note_chars + len(block) > MAX_NOTE_CONTEXT_CHARS:
            continue
        block = block[:MAX_NOTE_CONTEXT_CHARS]
        note_blocks.append(block)
        used_notes.append(row)
        note_chars += len(block)

    budget = MAX_RAW_CONTEXT_CHARS - note_chars
    used: list[tuple[sqlite3.Row, str]] = []
    total = 0
    for row in candidates:  # best first
        heading = (
            f"### Entry {row['entry_date']}\n"
            f"File: {parser.relative_path(row['file_path'], vault)}\n"
        )
        cost = len(heading) + len(row["raw_text"])
        if used and total + cost > budget:
            continue
        used.append((row, heading))
        total += cost
    truncated = len(used) < len(candidates) or len(used_notes) < len(notes)
    entry_blocks = [
        # the first entry always gets in, so it alone can be over budget
        (heading + row["raw_text"])[:budget]
        for row, heading in sorted(used, key=lambda pair: pair[0]["entry_date"])
    ]
    omitted = (
        "\n\n(Note: some lower-ranked entries or notes were omitted to fit the "
        "context budget.)" if truncated else ""
    )
    text = client.chat(
        model=config.ollama.precise_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You answer questions about a personal journal from the "
                    "full entry texts and reference notes provided. Reference "
                    "notes describe the people, places and topics the journal "
                    "links to, and carry no dates. Each one names the file it "
                    "comes from; folder and file names can say what it is "
                    "about. Base the answer strictly on this material; mention "
                    f"the dates or [[note names]] it draws on. {_READER_VOICE} "
                    "If it does not answer the question, say so."
                ),
            },
            {
                "role": "user",
                "content": f"Question: {question}\n\n" + "\n\n".join(note_blocks + entry_blocks),
            },
        ],
        options={"temperature": SYNTHESIS_TEMPERATURE},
    ).strip() + omitted
    return text, [date.fromisoformat(row["entry_date"]) for row, _ in used], used_notes


def _no_progress(_stage: str) -> None:
    pass


def answer(
    question: str,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    today: date | None = None,
    on_stage: Callable[[str], None] = _no_progress,
) -> Answer:
    today = today or date.today()
    on_stage("Working out what you're asking...")
    plan = _plan(question, config, client, today)
    named = notes_named_in(question, conn)

    if plan.intent == "aggregate":
        on_stage("Putting the numbers into words...")
        return _answer_aggregate(question, plan, config, conn, client, named)

    on_stage("Searching entries and notes...")
    about = _named_notes(conn, plan, named)
    query_vector = _embed_question(question, config, conn, client)
    candidates = _retrieve(
        question, plan, config, conn, client,
        query_vector=query_vector, boost_note_ids=named,
    )
    explicit_day = _explicit_day(question, today)
    if explicit_day is not None:
        candidates = _ensure_day_included(conn, explicit_day, candidates)
    notes = _retrieve_notes(plan, conn, query_vector, about, candidates)
    if not candidates and not notes:
        return Answer(
            text="No journal entries or reference notes matched the question.",
            stage_used="retrieval_empty",
        )

    if not plan.needs_raw_text:
        on_stage("Answering from summaries...")
        context = _summary_context(candidates, notes, plan, config, conn)
        fast_answer = _fast_attempt(question, context, config, client)
        if fast_answer is not None:
            return _finalize(
                fast_answer,
                "fast_summary",
                cited_dates=[date.fromisoformat(r["entry_date"]) for r in candidates],
                cited_notes=_notes_used(fast_answer, notes, about),
            )

    on_stage("Re-reading the full entries...")
    text, cited_dates, used_notes = _precise_reanalysis(
        question, candidates, notes, config, client
    )
    return _finalize(
        text,
        "precise_raw",
        cited_dates=cited_dates,
        cited_notes=_notes_used(text, used_notes, about),
    )
