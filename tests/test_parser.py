from datetime import date

import pytest
from conftest import FIXTURES, make_config

from journal_analyzer import parser
from journal_analyzer.models import DateParseError


def test_filename_date_and_cleaning(tmp_path):
    cfg = make_config(tmp_path)
    entries = parser.parse_file(FIXTURES / "2024-01-15.md", cfg)
    assert len(entries) == 1
    e = entries[0]
    assert e.entry_date == date(2024, 1, 15)
    assert "Sara" in e.raw_text and "[[" not in e.raw_text
    assert "Bear Mountain" in e.raw_text
    assert "- [x]" not in e.raw_text and "- [ ]" not in e.raw_text
    assert "- pack lunch" in e.raw_text
    assert "#hiking" in e.raw_text


def test_frontmatter_date_and_stripping(tmp_path):
    cfg = make_config(tmp_path, date_source="frontmatter")
    entries = parser.parse_file(FIXTURES / "2024-01-16.md", cfg)
    assert len(entries) == 1
    e = entries[0]
    assert e.entry_date == date(2024, 1, 16)
    assert "---" not in e.raw_text
    assert "mood: tired" not in e.raw_text
    assert e.raw_text.startswith("Long day at the office.")


@pytest.mark.parametrize(
    ("text", "fields", "body"),
    [
        ("---\n---\nBody text", {}, "Body text"),
        ("---\r\ndate: 2024-01-01\r\n---\r\nBody", {"date": "2024-01-01"}, "Body"),
        ("---\ndate: 2024-01-01\n---", {"date": "2024-01-01"}, ""),
        ("---\n\nkey: v\n---   \nBody", {"key": "v"}, "Body"),
        ("---\ntitle: a\n----\n---\nBody", {"title": "a"}, "Body"),
        ("--- not a fence\nBody", {}, "--- not a fence\nBody"),
    ],
)
def test_frontmatter_fences(text, fields, body):
    assert parser._parse_frontmatter(text) == (fields, body)


def test_heading_date_source(tmp_path):
    cfg = make_config(tmp_path, date_source="heading")
    note = tmp_path / "vault" / "some-note.md"
    note.write_text("# 2024-03-05\n\nA short entry about nothing much.\n", encoding="utf-8")
    entries = parser.parse_file(note, cfg)
    assert entries[0].entry_date == date(2024, 3, 5)


def test_multi_entry_split(tmp_path):
    cfg = make_config(tmp_path, split_heading_regex=r"^## \d{4}-\d{2}-\d{2}")
    entries = parser.parse_file(FIXTURES / "multi.md", cfg)
    assert [e.entry_date for e in entries] == [date(2024, 2, 1), date(2024, 2, 2)]
    assert "urban planning" in entries[0].raw_text
    assert "Lisbon" in entries[1].raw_text
    assert "Lisbon" not in entries[0].raw_text


def test_unparsable_date_raises_typed_error(tmp_path):
    cfg = make_config(tmp_path)
    bad = tmp_path / "vault" / "random-notes.md"
    bad.write_text("No date anywhere in here.", encoding="utf-8")
    with pytest.raises(DateParseError):
        parser.parse_file(bad, cfg)


def test_extract_literal_tags():
    tags = parser.extract_literal_tags("Went out #Hiking with friends #hiking #work/deep")
    assert tags == ["hiking", "work/deep"]


def test_compute_hash_stable():
    assert parser.compute_hash(b"abc") == parser.compute_hash(b"abc")
    assert parser.compute_hash(b"abc") != parser.compute_hash(b"abd")


def test_task_markers_stripped_for_every_bullet_style():
    text = "- [ ] one\n* [x] two\n+ [/] three\n  - [>] nested"
    assert parser.clean_text(text) == "- one\n* two\n+ three\n  - nested"


def test_task_marker_without_trailing_text_loses_the_checkbox():
    assert parser.clean_text("- [x]") == "-"


def test_wikilink_aliases_keep_the_display_text():
    cleaned = parser.clean_text("Met [[people/Sara Miller|Sara]] at [[Bear Mountain]].")
    assert cleaned == "Met Sara at Bear Mountain."


def test_text_before_the_first_split_heading_is_not_dropped(tmp_path):
    cfg = make_config(tmp_path, split_heading_regex=r"^## \d{4}-\d{2}-\d{2}")
    note = tmp_path / "vault" / "2024-02-01.md"
    note.write_text(
        "Journal for the week.\n\n## 2024-02-01\n\nFirst.\n\n## 2024-02-02\n\nSecond.\n",
        encoding="utf-8",
    )
    entries = parser.parse_file(note, cfg)
    assert [e.entry_date for e in entries] == [date(2024, 2, 1), date(2024, 2, 2)]
    assert "Journal for the week." in entries[0].raw_text
    assert "Journal for the week." not in entries[1].raw_text


def test_sections_fall_back_to_the_file_date(tmp_path):
    cfg = make_config(tmp_path, split_heading_regex=r"^## ")
    note = tmp_path / "vault" / "2024-03-09.md"
    note.write_text("## Morning\n\nCoffee.\n\n## Evening\n\nRain.\n", encoding="utf-8")
    entries = parser.parse_file(note, cfg)
    assert [e.entry_date for e in entries] == [date(2024, 3, 9), date(2024, 3, 9)]


def test_split_regex_with_no_matches_yields_one_entry(tmp_path):
    cfg = make_config(tmp_path, split_heading_regex=r"^## \d{4}-\d{2}-\d{2}")
    note = tmp_path / "vault" / "2024-03-10.md"
    note.write_text("No headings at all here.\n", encoding="utf-8")
    entries = parser.parse_file(note, cfg)
    assert len(entries) == 1
    assert entries[0].entry_date == date(2024, 3, 10)


def test_invalid_utf8_is_replaced_rather_than_raising(tmp_path):
    cfg = make_config(tmp_path)
    note = tmp_path / "vault" / "2024-04-01.md"
    note.write_bytes(b"caf\xe9 and a good day")
    entries = parser.parse_file(note, cfg)
    assert entries[0].raw_text.endswith("and a good day")


def test_frontmatter_date_falls_back_to_an_embedded_iso_date(tmp_path):
    cfg = make_config(tmp_path, date_source="frontmatter")
    note = tmp_path / "vault" / "whatever.md"
    note.write_text(
        "---\ndate: 2024-05-06T08:30:00\n---\n\nA morning entry.\n", encoding="utf-8"
    )
    assert parser.parse_file(note, cfg)[0].entry_date == date(2024, 5, 6)


def test_entry_links_come_from_its_wikilinks(tmp_path):
    cfg = make_config(tmp_path)
    [entry] = parser.parse_file(FIXTURES / "2024-01-15.md", cfg)
    assert entry.links == ["sara miller", "bear mountain", "garden-project"]


def test_link_keys_resolve_the_way_obsidian_does():
    assert parser.link_key("Sara Miller") == "sara miller"
    assert parser.link_key("Links/People/Sara Miller|Sara") == "sara miller"
    assert parser.link_key("Sara Miller#Childhood") == "sara miller"
    assert parser.link_key("Sara Miller^block-1") == "sara miller"
    assert parser.link_key("Sara Miller.md") == "sara miller"
    assert parser.link_key("Dr. Smith") == "dr. smith"
    assert parser.link_key("summit.JPG") is None
    assert parser.link_key("#Only a heading") is None


def test_attachments_are_dropped_unless_a_plain_link_gives_them_words():
    cleaned, links = parser.clean_with_links(
        "Top ![[summit.jpg|300]] view. ![[Bear Mountain]] and [[scan.pdf]] "
        "plus [[lease.pdf|the lease]]."
    )
    assert cleaned == "Top  view. Bear Mountain and  plus the lease."
    assert [key for _, key in links] == ["bear mountain"]


def test_link_offsets_point_into_the_cleaned_text():
    cleaned, links = parser.clean_with_links(
        "  Met [[Sara Miller|Sara]] at [[Bear Mountain#Summit]]."
    )
    assert cleaned == "Met Sara at Bear Mountain."
    assert [(cleaned[pos:pos + 4], key) for pos, key in links] == [
        ("Sara", "sara miller"),
        ("Bear", "bear mountain"),
    ]


def test_split_sections_keep_their_own_links(tmp_path):
    cfg = make_config(tmp_path, split_heading_regex=r"^## \d{4}-\d{2}-\d{2}")
    note = tmp_path / "vault" / "2024-02.md"
    note.write_text(
        "Week with [[Mum]].\n\n## 2024-02-01\n\nSaw [[Sara]].\n\n"
        "## 2024-02-02\n\nSaw [[Tomas]] and [[Sara]].\n",
        encoding="utf-8",
    )
    first, second = parser.parse_file(note, cfg)
    assert first.links == ["mum", "sara"]
    assert second.links == ["tomas", "sara"]


def test_frontmatter_links_count_for_the_entry(tmp_path):
    cfg = make_config(tmp_path)
    note = tmp_path / "vault" / "2024-05-01.md"
    note.write_text('---\nwith: "[[Sara Miller]]"\n---\nA day out.\n', encoding="utf-8")
    [entry] = parser.parse_file(note, cfg)
    assert entry.links == ["sara miller"]


def test_frontmatter_block_lists_inline_lists_and_quoted_links():
    fields, body = parser._parse_frontmatter(
        '---\naliases:\n  - Sara\n  - "S. M."\ntags: [a, b]\n'
        'Relationship: "[[Friend]]"\nempty:\n---\nBody\n'
    )
    assert fields == {
        "aliases": ["Sara", "S. M."],
        "tags": ["a", "b"],
        "Relationship": "[[Friend]]",
        "empty": [],
    }
    assert body == "Body\n"


def test_note_kind_is_the_folder_below_a_notes_path(tmp_path):
    cfg = make_config(tmp_path, notes_paths=["Links", "Lyrics.md"])
    vault = cfg.vault.path
    assert parser.note_kind(vault / "Links" / "People" / "Sara.md", cfg) == "people"
    assert parser.note_kind(vault / "links" / "Topics-Things" / "Go.md", cfg) == "topics-things"
    assert parser.note_kind(vault / "Links" / "!Files" / "deep" / "x.md", cfg) == "files"
    assert parser.note_kind(vault / "Links" / "Loose.md", cfg) == "note"
    assert parser.note_kind(vault / "Lyrics.md", cfg) == "note"
    assert parser.note_kind(vault / "2024" / "2024-01-01.md", cfg) is None
    assert parser.note_kind(vault / "LinksArchive" / "x.md", cfg) is None
    assert parser.note_kind(tmp_path / "outside.md", cfg) is None


def test_without_notes_paths_every_file_is_an_entry(tmp_path):
    cfg = make_config(tmp_path)
    assert parser.note_kind(cfg.vault.path / "Links" / "People" / "Sara.md", cfg) is None


def test_parse_note_reads_aliases_properties_and_links(tmp_path):
    cfg = make_config(tmp_path, notes_paths=["Links"])
    path = cfg.vault.path / "Links" / "People" / "Sara Miller.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\naliases:\n  - Sara\n  - sara miller\nRelationship: \"[[Friend]]\"\n"
        "tags: [people]\n---\n"
        "Climbs with me. Runs the [[garden-project]]; see [[Sara Miller#Notes]]. "
        "![[face.jpg]]\n",
        encoding="utf-8",
    )
    note = parser.parse_note(path, "people", cfg)
    assert (note.title, note.kind) == ("Sara Miller", "people")
    assert note.rel_path == "Links/People/Sara Miller.md"
    assert note.aliases == ["Sara"]
    assert note.properties == {"Relationship": "Friend"}
    assert note.links == ["friend", "garden-project"]
    assert note.raw_text == "Climbs with me. Runs the garden-project; see Sara Miller."
    assert note.header() == (
        "Sara Miller (people)\nFile: Links/People/Sara Miller.md\n"
        "Also called: Sara\nRelationship: Friend"
    )


def test_an_empty_note_still_parses(tmp_path):
    path = tmp_path / "Chess.md"
    path.write_text("", encoding="utf-8")
    note = parser.parse_note(path, "games", make_config(tmp_path))
    assert (note.title, note.raw_text, note.aliases, note.links) == ("Chess", "", [], [])


def test_entries_know_where_their_file_sits_in_the_vault(tmp_path):
    cfg = make_config(tmp_path, split_heading_regex=r"^## \d{4}-\d{2}-\d{2}")
    path = cfg.vault.path / "Trips" / "Lisbon 2024" / "2024-02.md"
    path.parent.mkdir(parents=True)
    path.write_text("## 2024-02-01\n\nLanded.\n\n## 2024-02-02\n\nTrams.\n", encoding="utf-8")
    entries = parser.parse_file(path, cfg)
    assert [e.rel_path for e in entries] == ["Trips/Lisbon 2024/2024-02.md"] * 2


def test_a_path_outside_the_vault_is_shown_by_its_file_name(tmp_path):
    cfg = make_config(tmp_path)
    assert parser.relative_path(tmp_path / "elsewhere" / "x.md", cfg.vault.path) == "x.md"


def test_digits_alone_are_not_tags_but_letters_in_any_script_are():
    text = "Day #3 of #2024, #3rd try, #café and #日記 #year-1"
    assert parser.extract_literal_tags(text) == ["3rd", "café", "日記", "year-1"]
