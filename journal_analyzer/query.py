"""Question answering.

The fast model routes the question. Aggregate questions are answered from SQL.
The rest go through hybrid retrieval, then the fast model tries to answer from
summaries, and the precise model re-reads the full text if it can't.

Two kinds of question leave that path. One that wants a judgment (is this
right, why does it keep happening, what should I do) goes straight to the
precise model, which is told to reason from the facts in the journal rather
than report what the writer said about it. One about how things stand now or
what to do next reads the latest days and the notes about goals and plans,
since a search for its words would find nothing to the point.

The precise model thinks before a judgment, a count or sum, and a follow-up,
and answers a plain lookup without it. A judgment reads more days than a
lookup, as many as fit in [ollama] read_tokens, and a count over a stretch
of days reads all of them.
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
from .models import (
    Answer,
    AnswerStage,
    FollowUp,
    NotePicks,
    QueryPlan,
    StructuredOutputError,
    Timing,
)
from .ollama_client import (
    CHARS_PER_TOKEN_ESTIMATE,
    DEFAULT_CHARS_PER_TOKEN,
    REPLY_TOKENS,
    SYNTHESIS_TEMPERATURE,
    OllamaClient,
    prompt_tokens,
)

console = Console(stderr=True)

TOP_K = 8
# A verdict or a "why does this keep happening" rests on days spread across
# the journal, and the eight nearest the question's words left most of them
# out. A count or a sum over a stretch of days needs every one of them.
JUDGMENT_TOP_K = 16
# A count of entries says nothing about how much there is to read: sixteen
# short ones are a page, and a subject written about on a hundred days has
# barely been opened. So a judgment keeps taking the next most relevant entry
# until [ollama] read_tokens of text is reached, from at most this many.
READ_POOL = 80
# What the asker can say about an answer before it's written: how much work
# it gets, and what kind of answer it is. "balanced" and "auto" are how a
# question is answered when nothing is said.
EFFORTS = ("quick", "balanced", "thorough")
STYLES = ("auto", "record", "verdict", "advice")
# how much of [ollama] read_tokens each effort reads; quick goes by count
_READ_SCALE = {"quick": 0.0, "balanced": 1.0, "thorough": 2.0}
COUNTED_RANGE_DAYS = 45
COUNTED_TOP_K = 45
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
# A question about now or what's next reads the past week whatever a search
# would find, and never fewer than a few entries: after a quiet week the
# latest ones still say where things stand.
RECENT_DAYS = 7
MAX_RECENT_ENTRIES = 10
MIN_RECENT_ENTRIES = 3
# the newest entries that link a note it names, on top of those
CURRENT_TOPIC_ENTRIES = 4
# Notes about goals, plans and the like. The nearest notes by meaning are
# only candidates: something is always nearest, and a note about bread handed
# over as background ends up in the advice. The fast model picks from this
# many which ones bear on the question.
BACKGROUND_NOTE_POOL = 12
BACKGROUND_NOTES_K = 5
BACKGROUND_NOTE_CHARS = 200
# fewer of the people and places the week's entries link, which are only
# background here
CURRENT_LINKED_NOTES_K = 4
# What led up to the days it reads: a line for each entry of the two weeks
# before them, then digests of the weeks before that. Without these, something
# settled eight days ago is news to the model.
CURRENT_LEAD_DAYS = 14
MAX_LEAD_ENTRIES = 14
CURRENT_WEEK_DIGESTS = 3
# what's looked for when the router names no subjects of its own
_CURRENT_BACKGROUND = ["goals", "plans", "priorities", "habits", "things to improve", "the future"]

# Without this the model sometimes takes the journal's "I" to mean itself.
_READER_VOICE = (
    "The journal material is written in the first person by the person asking "
    'you, so its "I" is them: answer them directly as "you", and never call '
    'them "the author" or "the user".'
)

# Answers are shown as Markdown in the browser, and light Markdown still reads
# well as plain text in a terminal. The fast model only gets lists and bold:
# asked for more, a small model puts headings on two sentences.
_ANSWER_FORMAT = (
    "Write the answer in Markdown where that makes it easier to read: a short "
    "bulleted list when it names several days, people or things, and **bold** "
    "for the fact the question asked for. An answer of a sentence or two stays "
    "plain sentences."
)
_FAST_ANSWER_FORMAT = f"{_ANSWER_FORMAT} No headings or tables."
_PRECISE_ANSWER_FORMAT = (
    f"{_ANSWER_FORMAT} Only a long answer with several distinct parts gets "
    "headings, and only a comparison of the same things across periods gets a "
    "table. Stop when the question is answered: no summary at the end that "
    "says it again."
)
# Left to itself it wrote [[Entry 2026-10-01]] and [[Luke (people)]], which
# link to nothing.
_CITING = (
    "Where the answer rests on a day, give its date in the sentence, as a "
    "plain date, and name a note by its title alone in double brackets, "
    "like [[Ana]]."
)

# The journal is good evidence of what happened and of what its writer
# thought. It isn't evidence that what they thought is so, and neither is the
# way a question is put. Told only that, a model takes disagreeing for the
# job: it piles on, and brings up troubles from entries that have nothing to
# do with the question. Told it may agree, it agrees with whichever way the
# question leans. So the verdict comes from the facts before the writer's
# view is looked at, and only how it's delivered is kept in proportion.
_STRAIGHT = (
    "Be straight and even-handed. The journal is a reliable record of what "
    "happened and of what its writer thought at the time. Settle what is "
    "true or right from the facts and what you know, as you would for a "
    "stranger, before you look at which way the writer or the question "
    "leans. A view isn't true because it's written there or because the "
    'question expects a yes ("..., didn\'t I?" is still a question), and it '
    "isn't wrong because it's theirs. What they say about themselves in "
    'general ("I\'ve been consistent", "I\'m just a bad sleeper") is a view '
    "too: check it against what the days show before you repeat it. Then "
    "say what you found. If it matches their view, say it holds and leave "
    "it there: the reason in a few sentences, no list of what to do next. "
    "If it doesn't, say so plainly and say why, once, as firmly as it "
    "matters and no more. If they were partly right, say where they are "
    "right and where they aren't rather than picking a side. If reasonable "
    "people would differ, say that. Where they got something wrong that "
    "can still be put right, or are dealing with a problem, finish with "
    "what would fix it: the one or two things most worth doing, about this "
    "matter only, and where someone was wronged, undoing the harm itself "
    "(setting the record straight with whoever was misled) as well as "
    "saying sorry. Don't flatter or agree to be agreeable, and don't hunt "
    "for faults, pile on, or disagree to show independence. Answer the "
    "question that was asked. The material covers more than it does: use "
    "the parts that are about it, don't explain a feeling or an event by a "
    "cause the material doesn't show, keep who did and said what, and "
    "every number, time and date, exactly as written, work out any count, "
    "sum or rate step by step and check it before you state it, and leave out other "
    "troubles, and advice and next steps about anything else, unless they "
    "change the answer."
)

# For questions that want the journal's own words. The router gets some
# judgment questions wrong, so these prompts still say what to do with one.
_RECORD = (
    "What the journal records (what happened, when, with whom, and what you "
    "thought or felt when that is what's asked) comes only from this "
    "material. If the question instead asks whether something is true, right "
    "or wise, or what to do, it wants your own reasoned answer: take the "
    "facts from the material and work out the verdict yourself rather than "
    "quoting what the entries say about it."
)

# When the asker has said they want the record and nothing else. _RECORD
# still judges a question that sounds like one, which is right when the
# router was only guessing and wrong when they've picked.
_RECORD_ONLY = (
    "The asker wants only what the journal records: what happened, when, "
    "with whom, and what they wrote about it at the time, in order. It "
    "comes only from this material. Give no verdict, opinion or advice of "
    "your own, even if the question is put as one: say what the entries "
    "say that bears on it, including what they thought then, as theirs."
)

# For questions that want a conclusion. Without this the model looks for the
# entry where the writer gave their own view and hands that back.
_JUDGMENT = (
    "The asker wants this thought through, not looked up. Use the material "
    "for the facts of their situation: what happened, when, who was "
    "involved, what was done and what came of it. Their own opinions, "
    "feelings and conclusions in it are what they thought, not evidence "
    "either way: don't hand one back as the answer, and don't set out to "
    "overturn it. Work it out: pick the facts that matter, connect facts "
    "from different days and notes where they bear on each other (causes, "
    "patterns, contradictions, consequences), bring in what you know about "
    "the world, and reach your own conclusion. When the question is who "
    "was right or whose fault something was, first work through, to "
    "yourself, what each person did and failed to do, the asker included: "
    "what they knew, what they said, and what they could have said or done "
    "and didn't. A thing left unsaid or undone that would have changed the "
    "outcome is part of the cause. Then check the verdict by asking "
    "whether you would give the same one had the other person written "
    "these entries. None of that working goes in the answer, only the "
    "verdict and the facts it turns on. Open with the verdict itself in a "
    "few words (yes, no, partly, or what it is), as sure as the facts "
    "allow and no surer, then give the reasoning with the facts and dates "
    "it rests on. If the material lacks a fact the answer turns on, say "
    "what's missing and answer as far as general knowledge goes rather "
    "than guessing about their life. When they ask how to fix or get better at something, look "
    "in their own days for the cause: what was different on the days it "
    "went well and the days it didn't, what they did or left undone. If "
    "the days show one, say it and build the advice on it. If they don't, "
    "don't supply one: say in a line that the entries don't show why, and "
    "go to the plan. Either way give a few concrete steps that fit their "
    "situation, the one that matters most first, rather than a list anyone "
    "could be given, and use only what the material says about that "
    "situation. No invented particulars (who holds what, what a task "
    "needs, what must have got in the way); a step that depends on "
    "something the material doesn't say is put as an if."
)

# Goes after the material, where it's the last thing read. A week of entries
# holds a sore leg, a bad night and a row with a colleague, and told only in
# the instructions above to leave them out, the model still made each one a
# cause of whatever was asked about.
_LAST_WORD = (
    "Now answer what was asked, with what would fix it if it's a problem "
    "they can do something about, and only that. Something else going on in "
    "the same days is a cause of it only when the days show that (the one "
    "comes with the other time after time, or an entry says so), not "
    "because it happened in the same week. Being tired, hurt, busy or on "
    "bad terms with someone is not why a thing went wrong unless an entry "
    "says it was. Otherwise leave it out. Give "
    "nobody a part in it that the entries don't give them."
)

# for a quick answer
_BRIEF = (
    "Keep it short: the answer and the reason for it in a few sentences, and "
    "what would fix it in one line when that's called for."
)

# on top of _JUDGMENT, when the question is what to do next
_ADVICE = (
    "If the question is what to do, be specific. An open one (what to do "
    "tomorrow, what to focus on) gets concrete things for the day or period "
    "asked about, drawn from what the latest entries show is unfinished, "
    "coming up, slipping or going well, and from the goals and plans in the "
    "notes: a handful at most, each said once in a line or two with its "
    "reason, the one that matters most first, and a line on what can wait. "
    "It should read in under a minute. One about a single matter (a sore "
    "leg, a loan) gets advice on that matter only, not on the rest of the "
    "week."
)

_ROUTER_SYSTEM = """You are a query router for a personal journal search tool.
Today's date is {today}. The journal has nothing written after today.
Classify the question and extract constraints:
- intent: "aggregate" only for statistics the index already holds: mood over
  time, how often a tag, person or place comes up, averages of numbers the
  entries record. "factual" for specific facts/events, and for counting or
  adding up things that happened ("how many times did I run in May", "how far
  in total", "how much did I spend"): no statistic holds those, the entries
  have to be read. "narrative" for open-ended summaries or stories about a
  period.
- date_from / date_to: absolute ISO dates of the entries to read. Convert
  relative expressions ("last March", "this winter", "two weeks ago") to
  absolute dates using today's date. Never give a date after today. Use null
  when the question has no time constraint, and for a question about the
  future or what to do next.
- entities: names of people, places, groups, topics or projects mentioned in
  the question.
- keywords: exact search terms (proper nouns, distinctive words) for
  full-text search. Exclude generic stop words.
- needs_raw_text: true only if answering will likely require reading full
  entries rather than one-line summaries (e.g. asking for exact details,
  quotes, counts or sums of things that happened, or specifics unlikely to
  survive summarization).
- wants: "record" when the journal's own words are the answer: what happened,
  when, with whom, or what the writer themselves thought, felt or said ("how
  do I feel about X", "what did I write about Y"). "judgment" when the writer
  wants an answer worked out rather than looked up: whether something is
  true, right, ethical, fair, wise or healthy, why something keeps happening,
  what a pattern means, what you think, or what they should do.
- current: true when the question is about how things stand now or what to do
  next ("what should I do tomorrow", "what should I focus on this week", "how
  am I doing lately", "what am I neglecting"), and not about a subject or a
  past period to search for. The latest entries are read for these, and
  nothing older, so a question about why something keeps happening, or about
  a habit or pattern ("why do I keep sleeping badly", "do I always argue with
  Sam"), is not current: its evidence is spread over the whole journal. Nor
  is a question about one subject, even when it asks what to do ("what can I
  do to sleep better", "how do I fix things with Sam", "should I worry about
  my knee", "was I right to skip the run"): that is searched for by its
  subject. Current is for the open ones, about everything at once.
- background: for a judgment or current question, up to five subjects whose
  notes would help beyond the question's own words, such as goals, plans,
  habits and things to improve for "what should I do tomorrow". Empty
  otherwise."""


CHAT_HISTORY_TURNS = 6
# the start of an old answer is enough to resolve "that" or "she"
_HISTORY_ANSWER_CHARS = 400
# and most of the answer being followed up, to tell what the message is about
LAST_ANSWER_CHARS = 4000
# The model that answers a follow-up gets the conversation itself: the
# messages as they were typed and its own answers, the latest whole and older
# ones for as long as this lasts. A summary of the follow-up, however careful,
# drops most of a long message, and it only ever saw the start of the answer
# being replied to.
CONVERSATION_CHARS = 24_000
_SOURCES_LINE_RE = re.compile(r"\n+Sources: .*\Z", re.DOTALL)

# The rewrite is only what the journal is searched with, and what the router
# reads. It isn't shown to the model that answers in place of the message.
_CONTEXTUALIZE_SYSTEM = (
    "You read the latest message in a conversation about someone's journal "
    "and write the question to search the journal with, so the right entries "
    "and notes are found for answering it. Whoever answers has the whole "
    "conversation and the message as it was typed, so nothing has to be "
    "carried over for them: this is only for finding material.\n\n"
    "First reason it through in `reasoning`: what the conversation is about "
    "so far, and what in it the latest message responds to or asks next (a "
    "claim in the last answer they doubt, a detail they want more of, a "
    "correction, a new angle, or a new subject altogether).\n\n"
    "Then write `question`, in their own voice (I, my): one self-contained "
    "question that names the people, dates, events and subjects that words "
    'like "that", "she", "the same period" or "the month after" stand for, '
    "and that asks for the same kind of answer the message does (what "
    "happened, a judgment on it, or what to do next). Keep every name, date "
    "and particular the latest message itself gives. Don't answer it, and "
    "don't add facts that aren't in the conversation. If the latest message "
    "starts a new subject that owes nothing to the conversation, give it "
    "back unchanged."
)

# added to an answer's instructions when it follows earlier turns
_FOLLOW_UP = (
    "This is a conversation. Your earlier answers are above, written from "
    "journal material you no longer have in front of you; the material with "
    "their latest message was looked up for that message. Answer the "
    "message as they wrote it, every point of it. Don't say again what you "
    "already said: build on it. Where they push back, take their point on "
    "its merits, and change your answer if they're right and only then. "
    "Something they tell you in a message that the material doesn't show is "
    "their account: take it as given unless the material contradicts it, "
    "and work out what it changes and what it leaves as it was. Don't wave "
    "it away for not being written down, and don't say the journal confirms "
    "it."
)

# (the message as typed, the answer), then how it was searched for if it was rewritten
Turn = Sequence[str]


def _turn(turn: Turn) -> tuple[str, str, str]:
    asked, answer, *rest = turn
    return asked, answer, rest[0] if rest else ""


def contextualize_question(
    question: str,
    history: Sequence[Turn],
    config: Config,
    client: OllamaClient,
    names: str = "",
) -> str:
    """The question to search the journal with for a follow-up. `names` is
    profile.names: who the writer and the people they mention are."""
    if not history:
        return question
    turns = [_turn(t) for t in history[-CHAT_HISTORY_TURNS:]]
    lines = []
    for i, (asked, answer, searched) in enumerate(turns):
        room = LAST_ANSWER_CHARS if i == len(turns) - 1 else _HISTORY_ANSWER_CHARS
        lines.append(f"User: {asked}")
        if searched and searched != asked:
            # "and April?" two turns back is easier to follow with this
            lines.append(f"(searched for as: {searched})")
        lines.append(f"Assistant: {answer[:room]}")
    transcript = "\n".join(lines)
    try:
        read = client.chat_structured(
            model=config.ollama.fast_model,
            messages=[
                {"role": "system", "content": _CONTEXTUALIZE_SYSTEM + names},
                {
                    "role": "user",
                    "content": f"Conversation so far:\n{transcript}\n\nLatest message: {question}",
                },
            ],
            schema=FollowUp,
        )
    except StructuredOutputError:
        # the question as typed still gets an answer
        return question
    # small models occasionally return nothing
    return " ".join(read.question.split()) or question


def _conversation(earlier: Sequence[Turn]) -> list[dict[str, str]]:
    """Earlier turns as chat messages for the model that answers, oldest
    first. The latest turn always goes in; older ones while there's room."""
    kept: list[dict[str, str]] = []
    room = CONVERSATION_CHARS
    for turn in reversed(earlier[-CHAT_HISTORY_TURNS:]):
        asked, answer, _searched = _turn(turn)
        # it would start writing Sources lines of its own
        answer = _SOURCES_LINE_RE.sub("", answer).strip()
        if not asked.strip() or not answer:
            continue
        cost = len(asked) + len(answer)
        if cost > room:
            if kept:
                break
            asked, answer = asked[:room], answer[: max(room - len(asked), 0)]
        room -= cost
        kept[:0] = [
            {"role": "user", "content": asked},
            {"role": "assistant", "content": answer},
        ]
    return kept


# the days the last answer's Sources line links, most of which a search for
# the follow-up's own words won't find again
CARRIED_DAYS = 8
_SOURCE_DAY_RE = re.compile(r"\[\[(\d{4}-\d{2}-\d{2})(?:#[^\]]*)?\]\]")


def _days_cited(earlier: Sequence[Turn]) -> list[date]:
    """The days the latest answer cited, so a follow-up is answered with
    them in front of the model again."""
    if not earlier:
        return []
    _asked_then, answer, _searched = _turn(earlier[-1])
    line = _SOURCES_LINE_RE.search(answer)
    if line is None:
        return []
    days: dict[date, None] = {}
    for found in _SOURCE_DAY_RE.findall(line.group(0)):
        try:
            days.setdefault(date.fromisoformat(found), None)
        except ValueError:
            continue
    return list(days)[:CARRIED_DAYS]


def _asked(question: str, asked: str, material: str, following: bool) -> str:
    """The last message an answering model gets. A follow-up goes in as it
    was typed, with what the journal was searched for beside the material."""
    if not following:
        return f"Question: {question}\n\n{material}"
    searched = f" (searched for: {question})" if question != asked else ""
    return f"{asked}\n\nFrom the journal, looked up for this message{searched}:\n\n{material}"


def _dated(entry_date: str, today: date) -> str:
    """An entry's date with its weekday, so "Friday" in it can be placed."""
    day = date.fromisoformat(entry_date)
    when = {0: ", today", 1: ", yesterday"}.get((today - day).days, "")
    return f"{entry_date} ({day:%A}{when})"


def _today_line(today: date) -> str:
    # an entry's "tomorrow" is the day after it was written, which a model
    # otherwise reads as the day after today
    # spelled out, since a model asked about "tomorrow" on a Monday has
    # answered for the Monday
    ahead = today + timedelta(days=1)
    # and the days after it, since "due Wednesday the 18th" was read as tomorrow
    after = ", ".join(
        f"{day:%A} {day.day}" for day in (today + timedelta(days=n) for n in range(2, 8))
    )
    # and the days just gone, for "he paid me on Saturday" in a message
    gone = ", ".join(
        f"{day:%A} {day.day}" for day in (today - timedelta(days=n) for n in range(1, 7))
    )
    return (
        f"Today is {today:%A}, {today.isoformat()}, so tomorrow is {ahead:%A}, "
        f"{ahead.isoformat()}, and then come {after}. The days just gone, latest "
        f"first, were {gone}. Each entry was written on "
        'its own date, so words in it like "today" and "tomorrow" count from '
        "that date, not from today."
    )


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


# days that haven't happened, which no entry can be about
_AHEAD_RE = re.compile(
    r"\b(?:tomorrow|tonight|from\s+now\s+on|going\s+forward"
    r"|next\s+(?:few\s+days|day|week|weekend|month|year|semester|term"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|this\s+(?:coming\s+)?weekend|the\s+(?:coming|upcoming)\s+\w+"
    r"|the\s+rest\s+of\s+(?:the|this|my)\s+(?:day|week|month|year))\b",
    re.IGNORECASE,
)
# "how do I feel about it" asks for the writer's own words
_OWN_VIEW_RE = re.compile(
    r"\b(?:how|what)\s+(?:do|did|have)\s+i\s+(?:feel|felt|think|thought|believe|say|said|write|written)\b"
    r"|\bdo\s+i\s+(?:think|feel|believe)\b"
    r"|\bmy\s+(?:own\s+)?(?:views?|opinions?|take|feelings?|thoughts?)\s+(?:on|about|of)\b",
    re.IGNORECASE,
)
# and these ask for the reader's
_JUDGMENT_RE = re.compile(
    r"\b(?:what|how)\s+do\s+you\s+(?:think|reckon|make\s+of)\b|\bdo\s+you\s+think\b"
    r"|\bin\s+your\s+(?:opinion|view)\b|\bwhat\s+would\s+you\b"
    r"|\b(?:should|shouldn't|ought)\s+i\b"
    # how to fix something is worked out, not looked up
    r"|\b(?:how|what)\s+(?:do|can|could|should|would)\s+i\b(?!\s+(?:feel|think|say|write)\b)"
    r"|\b(?:is|was|were|are|am)\s+(?:it|that|this|i|we|he|she|they)\s+(?:\w+\s+)?"
    r"(?:ethical|unethical|moral|immoral|right|wrong|fair|unfair|okay|ok|justified|reasonable|fine|rude)\b"
    # whose fault it was, or whether anyone at all was right, is a verdict too
    r"|\b(?:fault|to\s+blame|blamed?|responsible|selfish|overreact\w*)\b"
    r"|\b(?:is|was|were|are|am)\s+[^?.,]{1,60}?\s(?:right(?!\s+(?:by|next|beside|behind|there|here|now|after|before|away|at|on|in)\b)"
    r"|wrong|fair|unfair|justified|reasonable|rude)\b"
    # and so is checking something they claimed
    r"|\b(?:like|as)\s+i\s+(?:said|thought|think|told|claimed|wrote)\b"
    # "..., wasn't I?" and "so it's fine, right?"
    r"|,\s*(?:right|no|is(?:n't)?\s+it|(?:was|did|do|am|are|have)(?:n't)?\s+i)\s*\?"
    r"|\bso\s+(?:it's|it\s+is|that's|i'm|i\s+was)\s+(?:fine|ok|okay|alright|all\s+right)\b",
    re.IGNORECASE,
)
# Counting and adding up are worked out too. Without thinking, ten runs
# scattered through a month of entries came back as eight.
_COUNTED_RE = re.compile(
    r"\bhow\s+(?:many|much|far|long|often)\b|\b(?:in\s+total|total|altogether|add\s+up|sum|average)\b",
    re.IGNORECASE,
)
# what the advice instructions are for
_WHAT_TO_DO_RE = re.compile(
    r"\b(?:should|shall|ought)\s+i\b|\bi\s+(?:should|ought)\b"
    r"|\bwhat\s+(?:do|can|could|would)\s+i\s+do\b"
    r"|\bwhat\s+to\s+(?:do|focus|work)\b|\b(?:focus|prioriti[sz]e|priorit(?:y|ies)|plan)\b"
    r"|\bwhat(?:'s|\s+is)\s+next\b|\bnext\s+steps?\b",
    re.IGNORECASE,
)


def _settle(plan: QueryPlan, question: str, today: date) -> QueryPlan:
    """Fits the router's plan to what the journal can hold and to what the
    question plainly says. Small routers get both wrong now and then."""
    wants = plan.wants
    own, yours = _OWN_VIEW_RE.search(question), _JUDGMENT_RE.search(question)
    if own and not yours:
        wants = "record"
    elif yours and not own:
        wants = "judgment"
    lo, hi = plan.date_from, plan.date_to
    current = plan.current
    if lo is not None and lo > today:
        # only days to come, so it's asking about what's next
        lo, hi, current = None, None, True
    elif hi is not None and hi > today:
        hi = today
    # "what did John say about next year?" is still a search for John
    searched = wants == "record" and bool(plan.entities)
    if lo is None and hi is None and not searched and _AHEAD_RE.search(question):
        current = True
    if current and hi is not None and hi < today - timedelta(days=RECENT_DAYS):
        current = False  # a period that's over is being asked about, not now
    if current:
        lo, hi = None, None  # the latest days are read, whatever "this week" came to
    return plan.model_copy(
        update={"date_from": lo, "date_to": hi, "current": current, "wants": wants}
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
    role: str = "",
    keep: int = 0,
) -> tuple[Kept, Kept]:
    """Leaves out the least relevant notes, then the least relevant entries,
    until the prompt leaves the model room to think in the window it would
    get anyway. `keep` is how many tokens of prompt were asked for with
    [ollama] read_tokens: the window grows to think beside that much, since
    cutting it back here would undo the setting. Thinking gets cut off when the window runs out, and a whole
    note it can do without costs the answer less than that. The most relevant
    note and entry always stay; when they alone don't leave room, nothing is
    left out and the window grows instead."""
    think = client.think_room(model, role)
    if not think:
        return notes, entries
    whole = prompt_tokens(prompt(notes, entries))
    window = client.planned_window(
        model, max(whole + REPLY_TOKENS, min(whole, keep) + REPLY_TOKENS + think)
    )
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
    convo: Sequence[dict[str, str]] = (),
    asked: str = "",
    brief: bool = False,
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
                    "concise and straight: if the question assumes a trend or a "
                    "cause the numbers don't show, say so plainly, and if "
                    "they do show it, just answer. "
                    f"{_FOLLOW_UP + ' ' if convo else ''}"
                    f"{_READER_VOICE} {_FAST_ANSWER_FORMAT}"
                    f"{' ' + _BRIEF if brief else ''}{writer}"
                ),
            },
            *convo,
            {
                "role": "user",
                "content": _asked(question, asked, f"Statistics:\n{fact_block}", bool(convo)),
            },
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


def _counted(plan: QueryPlan) -> bool:
    lo, hi = plan.date_from, plan.date_to
    return bool(plan.needs_raw_text and lo and hi and (hi - lo).days <= COUNTED_RANGE_DAYS)


def _how_many(plan: QueryPlan, effort: str = "balanced") -> int:
    """How many entries a search hands on for this question, at the least."""
    if _counted(plan):
        return COUNTED_TOP_K
    if effort == "quick":
        return TOP_K
    return JUDGMENT_TOP_K if plan.wants == "judgment" or effort == "thorough" else TOP_K


def _read_room(plan: QueryPlan, config: Config, effort: str = "balanced") -> int:
    """The characters of entries a judgment reads up to, 0 when it goes by count."""
    if plan.wants != "judgment" or _counted(plan):
        return 0
    return int(config.ollama.read_tokens * DEFAULT_CHARS_PER_TOKEN * _READ_SCALE[effort])


def _within(rows: list[sqlite3.Row], least: int, room: int) -> list[sqlite3.Row]:
    """The first `least` rows, then each later one that still fits in `room`
    characters of full text. One too long to fit is passed over for the
    shorter ones after it."""
    kept = rows[:least]
    used = sum(len(r["raw_text"]) for r in kept)
    for row in rows[least:]:
        size = len(row["raw_text"])
        if used + size <= room:
            kept.append(row)
            used += size
    return kept


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
    room: int = 0,
) -> list[sqlite3.Row]:
    """`parts`, when given, collects the part of each entry found that matched
    best. With `room`, `top_k` is the fewest entries handed on, and more follow
    in order of relevance until that many characters of them are read."""
    allowed = store.filtered_entry_ids(conn, plan.date_from, plan.date_to, plan.entities, only)
    if not allowed:
        return []

    fetch_k = max(RERANK_CANDIDATE_POOL, top_k) if config.reranking.enabled else top_k
    if room:
        fetch_k = max(fetch_k, READ_POOL)

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
        ranked = _rerank(
            question, ranked, config.reranking.model, len(ranked) if room else top_k, best
        )
    ranked = _within(ranked, top_k, room) if room else ranked[:top_k]
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


def _last_day(today: date, until: date | None) -> date:
    return min(until, today) if until is not None else today


def _recent_entries(
    conn: sqlite3.Connection,
    today: date,
    since: date | None,
    until: date | None,
    only: store.EntryFilter | None,
) -> list[sqlite3.Row]:
    """The past week's entries, newest first, or the latest few after a quiet week."""
    end = _last_day(today, until)
    start = end - timedelta(days=RECENT_DAYS)
    if since is not None and since > start:
        start = since
    ids = store.filtered_entry_ids(conn, start, end, only=only)[-MAX_RECENT_ENTRIES:]
    if len(ids) < MIN_RECENT_ENTRIES:
        ids = store.filtered_entry_ids(conn, since, end, only=only)[-MIN_RECENT_ENTRIES:]
    return store.entries_by_ids(conn, ids)[::-1]


def _current_entries(
    conn: sqlite3.Connection,
    today: date,
    since: date | None,
    until: date | None,
    only: store.EntryFilter | None,
    about: list[int],
) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """What a question about now reads: the latest days, and the newest
    entries from before them that link a note it names. Nothing is searched
    for by meaning, since "what should I do tomorrow" is close to nothing in
    particular."""
    recent = _recent_entries(conn, today, since, until, only)
    have = {r["id"] for r in recent}
    allowed = set(store.filtered_entry_ids(conn, since, _last_day(today, until), only=only))
    linked = [i for i in store.entry_ids_mentioning(conn, about) if i in allowed and i not in have]
    return recent, store.entries_by_ids(conn, linked[:CURRENT_TOPIC_ENTRIES])[::-1]


def _word_forms(words: list[str]) -> list[str]:
    # the index doesn't stem, so "goals" wouldn't find a note called "Goal"
    forms: dict[str, None] = {}
    for word in words:
        forms.setdefault(word, None)
        forms.setdefault(word[:-1] if word.endswith("s") and len(word) > 3 else word + "s", None)
    return list(forms)


_PICK_SYSTEM = (
    "You choose background reading for a question someone asks about their "
    "own life. Below are numbered notes from their files, each with its "
    "file, title and how it starts. Give the numbers of the notes that would "
    "help answer the question: ones about the subjects listed, and ones about "
    "anything the question itself names. Leave out a note that is only about "
    "a person, place, hobby or project the question doesn't touch, however "
    "close it sits in the list. An empty list is right when none would help."
)


def _pick_notes(
    question: str,
    subjects: list[str],
    offered: list[sqlite3.Row],
    by_keyword: list[int],
    config: Config,
    client: OllamaClient,
) -> list[int]:
    """The offered notes worth reading for the question, best first."""
    if not offered:
        return []
    lines = []
    for number, row in enumerate(offered, start=1):
        note = store.note_from_row(row, config.vault.path)
        start = context.clip(row["summary"] or note.raw_text or "", BACKGROUND_NOTE_CHARS)
        lines.append(f"{number}. {note.rel_path or note.kind}, {note.title}: {start or '(empty)'}")
    try:
        picks = client.chat_structured(
            model=config.ollama.fast_model,
            messages=[
                {"role": "system", "content": _PICK_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Question: {question}\nSubjects: {', '.join(subjects)}\n\n"
                        "Notes:\n" + "\n".join(lines)
                    ),
                },
            ],
            schema=NotePicks,
        )
    except StructuredOutputError:
        # the ones with the subjects' own words in them, at least
        words = set(by_keyword)
        return [r["id"] for r in offered if r["id"] in words][:BACKGROUND_NOTES_K]
    chosen = dict.fromkeys(
        offered[n - 1]["id"] for n in picks.numbers if 1 <= n <= len(offered)
    )
    return list(chosen)[:BACKGROUND_NOTES_K]


def _current_notes(
    question: str,
    plan: QueryPlan,
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    about: list[int],
    candidates: list[sqlite3.Row],
) -> list[sqlite3.Row]:
    """The notes a question about now reads: the ones it names, the ones about
    the router's subjects (goals and plans, when it gave none), then the
    people and places the latest entries link most."""
    subjects = [s for s in plan.background if s.strip()] or _CURRENT_BACKGROUND
    vector = _embed_question(", ".join(subjects), config, conn, client)
    terms = _word_forms(search_keywords(" ".join(subjects)))
    ordered = dict.fromkeys(about)
    by_meaning = store.notes_vec_search(conn, vector, BACKGROUND_NOTE_POOL)
    by_keyword = store.notes_fts_search(conn, terms, BACKGROUND_NOTE_POOL)
    pool = [
        i for i in _rrf_merge([by_keyword, by_meaning], BACKGROUND_NOTE_POOL) if i not in ordered
    ]
    offered = store.notes_by_ids(conn, pool)
    for note_id in _pick_notes(question, subjects, offered, by_keyword, config, client):
        ordered.setdefault(note_id, None)
    linked = store.notes_linked_from_entries(conn, [r["id"] for r in candidates], CURRENT_LINKED_NOTES_K)
    for row in linked:
        ordered.setdefault(row["note_id"], None)
    return store.notes_by_ids(conn, list(ordered)[:MAX_CONTEXT_NOTES])


def _lead_up(
    conn: sqlite3.Connection,
    recent: list[sqlite3.Row],
    since: date | None,
    only: store.EntryFilter | None,
) -> tuple[list[str], list[int]]:
    """What came before the latest entries, as titled blocks for a prompt: a
    line for each entry of the two weeks before them, then digests of the
    weeks that ended before those. Also the ids of the entries the lines are
    for."""
    if not recent:
        return [], []
    first = date.fromisoformat(min(r["entry_date"] for r in recent))
    start = first - timedelta(days=CURRENT_LEAD_DAYS)
    if since is not None and since > start:
        start = since
    ids = store.filtered_entry_ids(conn, start, first - timedelta(days=1), only=only)
    rows = store.entries_by_ids(conn, ids[-MAX_LEAD_ENTRIES:])
    blocks = []
    if rows:
        lines = [f"- [{r['entry_date']}] {r['summary'] or '(no summary)'}" for r in rows]
        blocks.append("The two weeks before these entries, a line each\n" + "\n".join(lines))
    weeks = [
        r for r in store.get_rollups_in_range(
            conn, "week", start - timedelta(weeks=CURRENT_WEEK_DIGESTS + 1), start
        )
        if r["summary"] and store.period_bounds("week", r["period_key"])[1] < start
    ]
    if weeks:
        lines = [f"- [week {r['period_key']}] {r['summary']}" for r in weeks[-CURRENT_WEEK_DIGESTS:]]
        title = "The weeks before that, in brief" if rows else "The weeks before these entries, in brief"
        blocks.append(f"{title}\n" + "\n".join(lines))
    return blocks, [r["id"] for r in rows]


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
    before: Sequence[str] = (),
) -> str:
    """`parts` is the part of each entry that matched the question best, when
    one did. `before` is what _lead_up gives a question about now."""
    vault = config.vault.path
    sections: list[str] = []
    if plan.current:
        # they were found by date, not by how well they match, so they read in order
        candidates = sorted(candidates, key=lambda r: (r["entry_date"], r["id"]))
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
        heading = "The latest journal entries, oldest first:" if plan.current else "Journal entries:"
        sections.append(f"{heading}\n" + "\n".join(lines))
    sections.extend(block.replace("\n", ":\n", 1) for block in before)
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
    think: bool | None = None,
) -> str:
    """`think` False answers without thinking whatever the config says."""
    if on_text is None:
        # a streamed call's phases split it into steps; this one is a single step
        if clock is not None:
            clock.start(f"{role} model")
        return client.chat(model=model, messages=messages, options=options, role=role, think=think)
    sink = on_text

    def written(text: str) -> None:
        if clock is not None and text.strip():
            clock.start(f"{role} model writing")
        sink(text)

    return client.chat_stream(
        model, messages, _TrimStart(written), options=options,
        on_phase=on_phase, on_thinking=on_thinking, on_reading=on_reading, role=role,
        think=think,
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
    today: date | None = None,
    convo: Sequence[dict[str, str]] = (),
    asked: str = "",
    brief: bool = False,
    record_only: bool = False,
) -> str | None:
    """`convo` is _conversation for a follow-up, and `asked` the message as
    typed. `brief` asks for a short answer, and `record_only` for what the
    journal says with no verdict."""
    hold = _HoldUntilAnswer(on_text) if on_text else None
    response = _chat(
        client,
        config.ollama.fast_model,
        [
            {
                "role": "system",
                "content": (
                    "Answer the question from the journal entry "
                    "summaries and reference notes below. Reference notes "
                    "describe the people, places and topics the journal "
                    "links to, and carry no dates. Each item names the file "
                    "it comes from; folder and file names can say what it is "
                    f"about. {_RECORD_ONLY if record_only else _RECORD + ' ' + _STRAIGHT} "
                    f"{_FOLLOW_UP + ' ' if convo else ''}{_CITING} "
                    f"{_today_line(today or date.today())} "
                    f"{_READER_VOICE} {_FAST_ANSWER_FORMAT} "
                    f"{_BRIEF + ' ' if brief else ''}"
                    "If the material does not "
                    "contain enough information to answer confidently, reply "
                    f"with exactly the single word INSUFFICIENT.{writer}"
                ),
            },
            *convo,
            {"role": "user", "content": _asked(question, asked, context, bool(convo))},
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
    plan: QueryPlan | None = None,
    today: date | None = None,
    convo: Sequence[dict[str, str]] = (),
    asked: str = "",
    before: Sequence[str] = (),
    effort: str = "balanced",
    advise: bool = False,
    record_only: bool = False,
) -> tuple[str, list[sqlite3.Row], list[sqlite3.Row]]:
    """`plan` says whether the question wants the record or a judgment, and
    whether it's about now. `convo` is _conversation for a follow-up and
    `asked` the message as typed, and `before` what _lead_up gives a question
    about now. `effort` is one of EFFORTS, and `advise` says the asker wants
    to be told what to do whatever the wording, `record_only` that they want
    what the journal says and no verdict."""
    judging = plan is not None and plan.wants == "judgment"
    current = plan is not None and plan.current
    day = today or date.today()
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
            f"### Entry {_dated(row['entry_date'], day)}\n"
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
    if judging:
        # only when it's asked for: given it anyway, "why do I sleep badly"
        # came back with a to-do list for the week
        advising = advise or (current and bool(_WHAT_TO_DO_RE.search(asked or question)))
        approach = f"{_JUDGMENT} {_ADVICE if advising else ''}".rstrip()
        missing = ""
    else:
        approach = _RECORD_ONLY if record_only else _RECORD
        missing = " If the material does not answer the question, say so."
    system = (
        "You answer questions about a personal journal from the "
        "full entry texts and reference notes provided. Reference "
        "notes describe the people, places and topics the journal "
        "links to, and carry no dates. Each one names the file it "
        "comes from; folder and file names can say what it is "
        f"about. {approach} {'' if record_only and not judging else _STRAIGHT + ' '}"
        f"{_FOLLOW_UP + ' ' if convo else ''}{_CITING} "
        f"{_today_line(day)} {_READER_VOICE} "
        f"{_PRECISE_ANSWER_FORMAT}{' ' + _BRIEF if effort == 'quick' else ''}{missing}{writer}"
    )
    lead_up = [f"### {block}" for block in before]

    def prompt(
        kept_notes: list[tuple[sqlite3.Row, str]], kept_entries: list[tuple[sqlite3.Row, str]]
    ) -> list[dict[str, str]]:
        by_date = sorted(kept_entries, key=lambda pair: pair[0]["entry_date"])
        blocks = [b for _, b in kept_notes] + lead_up + [b for _, b in by_date]
        if judging:
            # the date again, which by here is a long way back
            blocks.append(
                f"{_LAST_WORD} Today is {day:%A}, {day.isoformat()}.\n\nAsked: {asked or question}"
            )
        return [
            {"role": "system", "content": system},
            *convo,
            {
                "role": "user",
                "content": _asked(question, asked, "\n\n".join(blocks), bool(convo)),
            },
        ]

    # Thinking is for what has to be worked out. Looking something up in the
    # full text came out the same without it, in a fraction of the time.
    kept_notes, kept_entries = ranked_notes, ranked_entries
    # A follow-up thinks too: it has an earlier answer to square with.
    thinking = judging or bool(convo) or bool(_COUNTED_RE.search(asked or question))
    # a quick answer never waits on it, and a thorough one always does
    thinking = effort == "thorough" or (thinking and effort != "quick")
    if thinking:
        kept_notes, kept_entries = _make_room(
            client, config.ollama.precise_model, prompt, ranked_notes, ranked_entries, "precise",
            # in the units prompt_tokens counts in
            keep=int(
                config.ollama.read_tokens * _READ_SCALE[effort]
                * DEFAULT_CHARS_PER_TOKEN / CHARS_PER_TOKEN_ESTIMATE
            ) if judging else 0,
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
        None if thinking else False,
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
    earlier: Sequence[Turn] = (),
    asked: str | None = None,
    effort: str | None = None,
    style: str | None = None,
) -> Answer:
    """`since`, `until` and `only` are what the person asked for, so they
    override whatever the router reads into the question. With `on_text`, the
    answer is handed over piece by piece as the model writes it, and with
    `on_thinking` so is the model's reasoning.
    For a follow-up, `question` is what the journal is searched for, `asked`
    the message as it was typed, and `earlier` the turns before it as
    (message, answer) or (message, answer, what was searched for). The model
    that answers gets those turns and the message itself, not the rewrite.
    `effort` (one of EFFORTS) says how much is read and whether the model
    thinks first, and `style` (one of STYLES) what kind of answer is wanted
    whatever the router makes of the wording; left out, they come from
    [answers] in the config.
    The answer's timings say how long each step took."""
    effort = effort or config.answers.effort
    style = style or config.answers.style
    if effort not in EFFORTS or style not in STYLES:
        raise ValueError(f"effort is one of {EFFORTS} and style one of {STYLES}")
    clock = _Clock()
    result = _answer(
        question, config, conn, client, today, on_stage, since, until, only, on_text,
        on_thinking, clock, _conversation(earlier), asked or question, _days_cited(earlier),
        effort, style,
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
    convo: list[dict[str, str]],
    asked: str,
    carried: Sequence[date] = (),
    effort: str = "balanced",
    style: str = "auto",
) -> Answer:
    today = today or date.today()
    writer = profile.context(conn, config)
    on_stage("Working out what you're asking...")
    clock.start("working out the question")
    # A follow-up is rewritten for searching, and the rewrite can turn "so
    # it's fine, right?" into a lookup, so the message as typed is read too.
    wording = question if asked == question else f"{asked}\n{question}"
    plan = _settle(
        _plan(question, config, client, today, profile.names(conn, config)), wording, today
    )
    if convo and "?" not in asked:
        # A follow-up that tells rather than asks (a correction, something the
        # journal doesn't have) is a point to reason with. Read as a lookup,
        # it was answered "the journal doesn't record that".
        plan = plan.model_copy(update={"wants": "judgment"})
    if style != "auto":
        # the asker has said what kind of answer this is
        wants = "record" if style == "record" else "judgment"
        plan = plan.model_copy(update={"wants": wants})
    if since is not None or until is not None:
        plan = plan.model_copy(update={"date_from": since, "date_to": until})
    named = notes_named_in(question, conn)
    if only and only.note_ids:
        named = list(dict.fromkeys([*only.note_ids, *named]))

    # what to do next is never a matter of counting, whatever the router made of it
    reasoned = plan.wants == "judgment" and (plan.current or style in ("verdict", "advice"))
    if plan.intent == "aggregate" and not reasoned:
        on_stage("Putting the numbers into words...")
        return _answer_aggregate(
            question, plan, config, conn, client, named, only, on_text, writer,
            on_stage, on_thinking, clock, convo, asked, effort == "quick",
        )

    about = _named_notes(conn, plan, named)
    found_parts: dict[int, sqlite3.Row] = {}
    before: list[str] = []
    lead_ids: list[int] = []

    def with_named_day(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        day = _explicit_day(question, today)
        if day is None or (since is not None and day < since) or (until is not None and day > until):
            return rows
        return _ensure_day_included(conn, day, rows, only)

    if plan.current:
        on_stage("Gathering your latest entries and notes...")
        clock.start("searching")
        recent, linked = _current_entries(conn, today, since, until, only, about)
        before, lead_ids = _lead_up(conn, recent, since, only)
        candidates = with_named_day(recent + linked)
        notes = _current_notes(question, plan, config, conn, client, about, candidates)
    else:
        on_stage("Searching entries and notes...")
        clock.start("searching")
        query_vector = _embed_question(question, config, conn, client)
        candidates = with_named_day(_retrieve(
            question, plan, config, conn, client, top_k=_how_many(plan, effort),
            query_vector=query_vector, boost_note_ids=named, only=only, parts=found_parts,
            room=_read_room(plan, config, effort),
        ))
        notes = _retrieve_notes(plan, conn, query_vector, about, candidates)
    for day in carried:
        if (since is None or day >= since) and (until is None or day <= until):
            have = {r["id"] for r in candidates}
            ids = [i for i in store.filtered_entry_ids(conn, day, day, only=only) if i not in have]
            # after what was found for this message, which still comes first
            candidates = candidates + store.entries_by_ids(conn, ids)
    if not candidates and not notes:
        return Answer(
            text="No journal entries or reference notes matched the question.",
            stage_used="retrieval_empty",
        )
    wanted = quotes.terms(
        [*search_keywords(question), *search_keywords(" ".join(plan.keywords + plan.entities))]
    )

    # a judgment needs the facts themselves, and a model that thinks
    # and a thorough answer is never given from summaries
    if not plan.needs_raw_text and plan.wants != "judgment" and effort != "thorough":
        on_stage("Answering from summaries...")
        context = _summary_context(
            candidates, notes, plan, config, conn, about, found_parts, before
        )
        fast_answer = _fast_attempt(
            question, context, config, client, on_text, writer, on_stage, on_thinking, clock,
            today, convo, asked, effort == "quick", style == "record",
        )
        if fast_answer is not None:
            return _finalize(
                fast_answer,
                "fast_summary",
                cited_dates=[date.fromisoformat(r["entry_date"]) for r in candidates],
                cited_notes=_notes_used(fast_answer, notes, about, _learned_names(conn, notes)),
                read_entries=[*(r["id"] for r in candidates), *lead_ids],
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
        plan=plan,
        today=today,
        convo=convo,
        asked=asked,
        before=before,
        effort=effort,
        advise=style == "advice",
        record_only=style == "record",
    )
    drawn = _days_drawn_on(text, used)
    return _finalize(
        text,
        "precise_raw",
        cited_dates=[date.fromisoformat(r["entry_date"]) for r in drawn],
        cited_notes=_notes_used(text, used_notes, about, _learned_names(conn, used_notes)),
        read_entries=[*(r["id"] for r in used), *lead_ids],
        read_notes=[r["id"] for r in used_notes],
        cited_parts=_cited_parts(used, found_parts),
        quoted=quotes.for_days(used, wanted, found_parts),
    )


_MONTH_NAMES = "|".join(f"{name[:3]}(?:{name[3:]})?" for name in _MONTHS)
_DAY_MENTION_RE = re.compile(
    rf"\b(\d{{4}})-(\d{{2}})-(\d{{2}})\b"
    rf"|\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_NAMES})\b"
    rf"|\b({_MONTH_NAMES})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_MONTH_NUMBERS = {name[:3]: number for name, number in _MONTHS.items()}


def _days_drawn_on(text: str, rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """The entries whose dates the answer gives. A verdict reads sixteen days
    and rests on three, and citing all sixteen buried those. An answer that
    names no day cites what it read, as before."""
    said: set[tuple[int, int]] = set()
    for iso_year, iso_month, iso_day, day_a, month_a, month_b, day_b in _DAY_MENTION_RE.findall(text):
        if iso_year:
            said.add((int(iso_month), int(iso_day)))
        elif (month_a or month_b) != "may":  # "I may 1 day"
            month = _MONTH_NUMBERS[(month_a or month_b)[:3].lower()]
            said.add((month, int(day_a or day_b)))
    named = [
        r for r in rows
        if (int(r["entry_date"][5:7]), int(r["entry_date"][8:10])) in said
    ]
    return named or rows


def _cited_parts(rows: list[sqlite3.Row], parts: dict[int, sqlite3.Row]) -> dict[str, str]:
    """The heading of each cited day's best part; the first entry of a day wins."""
    cited: dict[str, str] = {}
    for r in rows:
        part = parts.get(r["id"])
        if part is not None and part["heading"]:
            cited.setdefault(r["entry_date"], part["heading"])
    return cited
