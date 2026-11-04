"""The sentence from each cited day that an answer most likely drew on.

It's picked by the question's words, so checking an answer means reading what
you wrote instead of opening every day it cites. No model is called.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable

QUOTE_CHARS = 280
# words that don't say which sentence a question is after, since most
# sentences could have them
_QUESTION_WORDS = frozenset(
    """ever last time times day days week weeks month months year years often
    much many tell write wrote written journal entry entries mention mentioned
    does doing done they them their there then than been being something
    anything feel felt think thought the and but for from had has have how
    that this was were what when where which who why with you your about into
    over under near after before during again also still just really very
    some more most other onto around through while because since until our
    out off one two all any each every not only own same too can will would
    could should may might must its it's i'm i've she her hers his him he
    we us me my mine myself are did get got make made went going come came""".split()
)
_SUFFIXES = ("ing", "ed", "es", "s", "ly")
_WORD_RE = re.compile(r"[^\W_][\w'-]*")
_LIST_MARK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[.\]\s+)?")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
# a part that matched the search counts for this much more
_PART_BONUS = 1.5


def _stem(word: str) -> str:
    """Rough, but hike, hiked and hiking come out the same, and so do run and running."""
    w = word.casefold().strip("'-")
    for suffix in _SUFFIXES:
        # pass, focus and analysis aren't plurals
        if suffix == "s" and w.endswith(("ss", "us", "is")):
            continue
        if len(w) - len(suffix) >= 3 and w.endswith(suffix):
            w = w[: -len(suffix)]
            # stopped, running; but not called or passed
            if w[-1] == w[-2] and w[-1] not in "aeioulsfz":
                w = w[:-1]
            break
    return w[:-1] if len(w) > 3 and w.endswith("e") else w


def terms(words: Iterable[str]) -> set[str]:
    """What to look for, from the question's keywords and the names in it."""
    return {
        _stem(w)
        for raw in words
        for w in _WORD_RE.findall(raw)
        if len(w) >= 3 and w.casefold() not in _QUESTION_WORDS
    }


def sentences(text: str) -> list[str]:
    """Sentences and list items, leaving out headings and list and checkbox marks."""
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "```", "|")):
            continue
        line = _LIST_MARK_RE.sub("", line)
        out.extend(s.strip() for s in _SENTENCE_END_RE.split(line) if s.strip())
    return out


def _clip(sentence: str, wanted: set[str]) -> str:
    if len(sentence) <= QUOTE_CHARS:
        return sentence
    first = next((m.start() for m in _WORD_RE.finditer(sentence) if _stem(m.group()) in wanted), 0)
    lo = max(0, min(first - QUOTE_CHARS // 3, len(sentence) - QUOTE_CHARS))
    cut = sentence[lo:lo + QUOTE_CHARS].strip()
    return ("…" if lo > 0 else "") + cut + ("…" if lo + QUOTE_CHARS < len(sentence) else "")


def best(text: str, wanted: set[str], part: str = "") -> tuple[float, str] | None:
    """The sentence with the most of `wanted`, as (score, quote), or None if
    no sentence has any. A word counts for less the more sentences have it,
    so a name beats "today". Sentences in `part` count for more."""
    found = sentences(text)
    stems = [{_stem(w) for w in _WORD_RE.findall(s)} for s in found]
    hits = {t: sum(1 for st in stems if t in st) for t in wanted}
    preferred = set(sentences(part)) if part else set()
    best_score, best_at = 0.0, -1
    for i, (sentence, st) in enumerate(zip(found, stems, strict=True)):
        score = sum(1 / hits[t] for t in wanted & st)
        if score and sentence in preferred:
            score *= _PART_BONUS
        if score > best_score:
            best_score, best_at = score, i
    if best_at < 0:
        return None
    return best_score, _clip(found[best_at], wanted)


def for_days(
    rows: Iterable[sqlite3.Row], wanted: set[str], parts: dict[int, sqlite3.Row] | None = None
) -> dict[str, str]:
    """The best sentence for each day among these entries, by ISO date."""
    if not wanted:
        return {}
    chosen: dict[str, tuple[float, str]] = {}
    for row in rows:
        part = (parts or {}).get(row["id"])
        found = best(row["raw_text"], wanted, part["text"] if part is not None else "")
        if found and found[0] > chosen.get(row["entry_date"], (0.0, ""))[0]:
            chosen[row["entry_date"]] = found
    return {day: quote for day, (_, quote) in chosen.items()}
