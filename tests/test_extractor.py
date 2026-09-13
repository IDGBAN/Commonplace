from datetime import date

from journal_analyzer import extractor
from journal_analyzer.models import Extraction, NoteSummary, ParsedEntry, ParsedNote


def _note(text: str, **extra) -> ParsedNote:
    return ParsedNote(title="Sara", kind="people", raw_text=text, source_path="x", **extra)


def test_short_notes_are_their_own_summary(config, fake_client):
    summary, model = extractor.summarize_note(
        _note("Friend from   school.\nClimbs."), config, fake_client
    )
    assert (summary, model) == ("Friend from school. Climbs.", None)
    assert fake_client.structured_calls == []


def test_long_notes_get_a_model_summary(config, fake_client):
    summary, model = extractor.summarize_note(_note("word " * 100), config, fake_client)
    assert (summary, model) == ("A canned note summary.", "fake-fast")
    assert fake_client.structured_calls == [("fake-fast", "NoteSummary")]


def test_empty_notes_get_no_summary_and_no_model_call(config, fake_client):
    assert extractor.summarize_note(_note("  \n "), config, fake_client) == (None, None)
    assert fake_client.structured_calls == []


def test_a_zero_threshold_summarizes_every_note(config, fake_client):
    every = config.model_copy(
        update={"notes": config.notes.model_copy(update={"summarize_over_chars": 0})}
    )
    _, model = extractor.summarize_note(_note("Short."), every, fake_client)
    assert model == "fake-fast"


def test_the_model_reads_the_note_header_before_its_text(config, fake_client):
    seen = {}

    def respond(model, messages):
        seen["content"] = messages[-1]["content"]
        return NoteSummary(summary="ok")

    fake_client.structured["NoteSummary"] = respond
    note = _note(
        "word " * 100, properties={"Relationship": "Friend"}, rel_path="Links/People/Sara.md"
    )
    extractor.summarize_note(note, config, fake_client)
    assert seen["content"].startswith(
        "Sara (people)\nFile: Links/People/Sara.md\nRelationship: Friend\n\nword"
    )


def test_entry_extraction_is_told_the_file_path(config, fake_client):
    seen = {}

    def respond(model, messages):
        seen["content"] = messages[-1]["content"]
        return Extraction(summary="ok")

    fake_client.structured["Extraction"] = respond
    entry = ParsedEntry(
        date(2024, 3, 5), "Trams and custard tarts.", "x", rel_path="Trips/Lisbon/2024-03-05.md"
    )
    extractor.extract(entry, config, fake_client)
    assert seen["content"] == (
        "Journal entry dated 2024-03-05\nFile: Trips/Lisbon/2024-03-05.md\n\n"
        "Trams and custard tarts."
    )


def _entry(text: str = "Long shift, then a walk by the river.") -> ParsedEntry:
    return ParsedEntry(date(2024, 3, 5), text, "x")


def test_a_summary_already_in_the_writers_voice_takes_one_call(config, fake_client):
    fake_client.structured["Extraction"] = Extraction(summary="Long shift; the walk helped.")
    assert extractor.extract(_entry(), config, fake_client).summary == (
        "Long shift; the walk helped."
    )
    assert len(fake_client.structured_calls) == 1


def test_a_summary_about_the_author_is_asked_for_again(config, fake_client):
    replies = [
        Extraction(mood_score=6, summary="The author expresses fatigue after work."),
        Extraction(mood_score=6, summary="Worn out after work."),
    ]
    sent = []

    def respond(model, messages):
        sent.append(messages)
        return replies[len(sent) - 1]

    fake_client.structured["Extraction"] = respond
    assert extractor.extract(_entry(), config, fake_client).summary == "Worn out after work."
    retry = sent[1]
    assert retry[:2] == sent[0]
    assert "The author expresses" in retry[2]["content"]
    assert retry[3]["role"] == "user" and "first-person" in retry[3]["content"]


def test_a_second_detached_summary_is_kept_rather_than_failing(config, fake_client):
    fake_client.structured["Extraction"] = Extraction(summary="This entry describes a walk.")
    assert extractor.extract(_entry(), config, fake_client).summary == (
        "This entry describes a walk."
    )
    assert len(fake_client.structured_calls) == 2


def test_note_summaries_get_the_same_second_chance(config, fake_client):
    replies = iter(
        [NoteSummary(summary="This note is about Sara."), NoteSummary(summary="Sara is my friend.")]
    )
    fake_client.structured["NoteSummary"] = lambda model, messages: next(replies)
    summary, _ = extractor.summarize_note(_note("word " * 100), config, fake_client)
    assert summary == "Sara is my friend."


def test_ordinary_openings_are_not_mistaken_for_a_detached_voice():
    for summary in (
        "Theatre night with Sara.",
        "The entryway flooded again.",
        "The new user interface at work finally shipped.",
        "Authored the first chapter.",
    ):
        assert not extractor._DETACHED_OPENING_RE.match(summary), summary
    for summary in (
        "The author feels lonely.",
        "In this entry, the writer reflects.",
        "this journal entry covers a hike.",
        "The user's sister visited.",
        "This file serves as a guide to my scans.",
    ):
        assert extractor._DETACHED_OPENING_RE.match(summary), summary


def test_long_summaries_are_cut_at_a_word_boundary(config, fake_client):
    long = "word " * 60
    fake_client.structured["Extraction"] = Extraction(summary=long)
    summary = extractor.extract(_entry(), config, fake_client).summary
    assert len(summary) <= extractor.SUMMARY_MAX_CHARS
    assert summary.endswith("word…")


def test_entry_extraction_no_longer_asks_the_model_for_entities(config, fake_client):
    assert "entities" not in Extraction.model_json_schema()["properties"]
    assert "entities" not in extractor._SYSTEM_PROMPT
    result = extractor.extract(
        ParsedEntry(date(2024, 1, 1), "Went out. #hiking", "x"), config, fake_client
    )
    assert "hiking" in result.tags
