import http.client
import json
import re
import sqlite3
import threading
from datetime import date, timedelta
from urllib.parse import quote

import pytest
from conftest import add_entry, add_note

from journal_analyzer import browse, db, nicknames, parser, photos, query, store
from journal_analyzer.models import (
    DatabaseError,
    Extraction,
    OllamaUnavailableError,
    ParsedEntry,
    QueryPlan,
)


class Browser:
    def __init__(self, port: int):
        self.port = port

    def request(
        self,
        path: str,
        method: str = "GET",
        host: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> http.client.HTTPResponse:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Host": host} if host is not None else {}
        conn.request(method, path, headers={**sent, **(headers or {})})
        return conn.getresponse()

    def get(self, path: str, status: int = 200) -> dict:
        response = self.request(path)
        body = response.read()
        assert response.status == status, body
        assert response.getheader("Content-Type").startswith("application/json")
        return json.loads(body)


@pytest.fixture
def journal(conn):
    add_note(conn, "John Doe", kind="people", text="Friend from <b>school</b>.",
             aliases=["John"], properties={"Relationship": "Friend"}, links=["Bear Mountain"])
    add_note(conn, "Bear Mountain", kind="locations", summary="A hill we climb.")
    add_entry(conn, date(2024, 1, 15), text="Hiked Bear Mountain with John Doe.",
              mood=8.0, tags=["hiking", "friends"], links=["John Doe", "Bear Mountain"],
              summary="Hiked with John and felt calmer after", path="/vault/2024-01-15.md")
    add_entry(conn, date(2024, 1, 16), text="Rainy day inside with <script>x</script>.",
              mood=4.0, tags=["rain"], links=["Nobody Yet"],
              summary="Stayed in while it rained", path="/vault/2024-01-16.md")
    store.upsert_file(conn, "/vault/broken.md", "h", "error", "unreadable: bad bytes")
    store.upsert_rollup(conn, "month", "2024-01", "A calm January.", '["hiking"]', 6.0, 2)
    return conn


@pytest.fixture
def browser(config, journal, fake_client):
    config.reranking.enabled = False
    server = browse.make_server(config, port=0, make_client=lambda cfg: fake_client)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Browser(server.port)
    server.shutdown()
    server.server_close()


def entry_id(conn, day: str) -> int:
    return conn.execute("SELECT id FROM entries WHERE entry_date = ?", (day,)).fetchone()["id"]


def note_id(conn, title: str) -> int:
    return conn.execute("SELECT id FROM notes WHERE title = ?", (title,)).fetchone()["id"]


def test_readonly_connection_cannot_write(config, journal):
    ro = db.connect_readonly(config)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM entries")
        assert ro.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
    finally:
        ro.close()


def test_readonly_connection_explains_a_missing_cache(config):
    with pytest.raises(DatabaseError, match="journal init && journal index"):
        db.connect_readonly(config)
    assert not config.db.path.exists()


def test_readonly_connection_refuses_other_cache_formats(config, conn):
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.commit()
    with pytest.raises(DatabaseError, match="newer version"):
        db.connect_readonly(config)


def test_period_bounds():
    assert store.period_bounds("week", "2024-W03") == (date(2024, 1, 15), date(2024, 1, 21))
    assert store.period_bounds("month", "2024-02") == (date(2024, 2, 1), date(2024, 2, 29))
    assert store.period_bounds("month", "2024-12") == (date(2024, 12, 1), date(2024, 12, 31))
    assert store.period_bounds("year", "2023") == (date(2023, 1, 1), date(2023, 12, 31))
    with pytest.raises(ValueError):
        store.period_bounds("month", "March")


@pytest.mark.parametrize(
    ("typed", "parsed"),
    [
        ("2024", ("year", "2024", date(2024, 1, 1), date(2024, 12, 31))),
        (" 2024-02 ", ("month", "2024-02", date(2024, 2, 1), date(2024, 2, 29))),
        ("2024-2", ("month", "2024-02", date(2024, 2, 1), date(2024, 2, 29))),
        ("2024-w3", ("week", "2024-W03", date(2024, 1, 15), date(2024, 1, 21))),
    ],
)
def test_parse_period_writes_keys_the_way_rollups_are_stored(typed, parsed):
    assert store.parse_period(typed) == parsed


@pytest.mark.parametrize("typed", ["March", "2024-13", "2023-W53", "2024-03-01", "24"])
def test_parse_period_refuses_anything_else(typed):
    with pytest.raises(ValueError):
        store.parse_period(typed)


def test_serves_the_page_with_a_strict_content_policy(browser):
    response = browser.request("/")
    body = response.read()
    assert response.status == 200
    assert b'<script src="/app.js"' in body
    assert "default-src 'self'" in response.getheader("Content-Security-Policy")
    assert response.getheader("X-Content-Type-Options") == "nosniff"
    for asset in ("/app.js", "/app.css", "/favicon.svg"):
        assert browser.request(asset).status == 200


def test_only_listed_static_files_are_served(browser):
    for path in ("/../pyproject.toml", "/browse.py", "/browse_static/app.js", "/nope"):
        response = browser.request(path)
        response.read()
        assert response.status == 404


def test_refuses_requests_for_another_host_name(browser):
    response = browser.request("/api/overview", host="attacker.example:80")
    assert response.status == 403
    assert "John" not in response.read().decode()


def test_refuses_api_calls_from_other_sites(browser, fake_client):
    for site in ("cross-site", "same-site"):
        response = browser.request("/api/ask?q=hello", headers={"Sec-Fetch-Site": site})
        assert response.status == 403
        assert "John" not in response.read().decode()
    assert fake_client.chat_calls == fake_client.structured_calls == []
    response = browser.request("/api/overview", headers={"Sec-Fetch-Site": "same-origin"})
    response.read()
    assert response.status == 200
    # a link from elsewhere can still open the page itself
    response = browser.request("/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
    response.read()
    assert response.status == 200


def test_is_read_only(browser):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = browser.request("/api/entries", method=method)
        response.read()
        assert response.status == 405
        assert response.getheader("Allow") == "GET"


def test_overview_reports_counts_problems_and_setup(browser):
    data = browser.get("/api/overview")
    assert data["counts"]["entries"] == 2
    assert data["counts"]["notes"] == 2
    assert data["counts"]["errored_files"] == 1
    assert data["errored_files"][0]["error"] == "unreadable: bad bytes"
    assert data["baseline_mood"] == 6.0
    assert data["setup"]["embed_model"] == "fake-embed"
    assert {k["kind"] for k in data["note_kinds"]} == {"people", "locations"}


def test_catalog_lists_everything_the_jump_box_needs(browser):
    data = browser.get("/api/catalog")
    assert [e["date"] for e in data["entries"]] == ["2024-01-15", "2024-01-16"]
    john = next(n for n in data["notes"] if n["title"] == "John Doe")
    assert john["aliases"] == ["John"]
    assert {t["name"] for t in data["tags"]} == {"hiking", "friends", "rain"}


def test_entry_shows_what_was_extracted_and_what_it_links(browser, journal):
    first = entry_id(journal, "2024-01-15")
    data = browser.get(f"/api/entries/{first}")
    assert data["mood"] == 8.0
    assert data["summary"] == "Hiked with John and felt calmer after"
    assert data["tags"] == ["friends", "hiking"]
    assert {n["title"] for n in data["notes"]} == {"John Doe", "Bear Mountain"}
    assert data["unresolved_links"] == []
    assert data["model_used"] == "m"
    assert data["prev"] is None
    assert data["next"]["date"] == "2024-01-16"
    assert data["obsidian_url"].startswith("obsidian://open?path=")
    month = next(d for d in data["digests"] if d["type"] == "month")
    assert month == {"type": "month", "key": "2024-01", "exists": True, "stale": False,
                     "summary": "A calm January."}
    assert next(d for d in data["digests"] if d["type"] == "year")["exists"] is False

    second = browser.get(f"/api/entries/{data['next']['id']}")
    assert second["unresolved_links"] == ["nobody yet"]
    # markup in journal text is data, handed over untouched for textContent
    assert "<script>x</script>" in second["raw_text"]


def test_entry_says_when_the_mood_is_your_own(browser, config, journal):
    config.vault.frontmatter_mood_key = "mood"
    first = entry_id(journal, "2024-01-15")
    store.save_file_frontmatter(journal, "/vault/2024-01-15.md", {"mood": "8", "tags": "hiking"})
    store.save_entry_reading(journal, first, Extraction(mood_score=5.0, tags=["friends"]))
    data = browser.get(f"/api/entries/{first}")
    assert data["mood_from_frontmatter"] is True
    assert data["model_reading"] == {"mood": 5.0, "tags": ["friends"]}
    assert data["frontmatter_tags"] == ["hiking"]

    second = browser.get(f"/api/entries/{entry_id(journal, '2024-01-16')}")
    assert second["mood_from_frontmatter"] is False
    assert second["model_reading"] is None


def test_links_to_other_days_point_at_those_entries(browser, journal):
    add_entry(journal, date(2024, 1, 20), links=["2024-01-15", "Someone"], path="/vault/20.md")
    later = browser.get(f"/api/entries/{entry_id(journal, '2024-01-20')}")
    assert [d["date"] for d in later["links_to_days"]] == ["2024-01-15"]
    assert later["unresolved_links"] == ["someone"]

    earlier = browser.get(f"/api/entries/{entry_id(journal, '2024-01-15')}")
    assert [d["date"] for d in earlier["linked_from_days"]] == ["2024-01-20"]
    unresolved = {link["target"] for link in browser.get("/api/notes")["unresolved_links"]}
    assert unresolved == {"nobody yet", "someone"}


def test_links_to_part_of_a_day_or_note_say_which_part(browser, journal):
    add_note(journal, "Garden", kind="topics", text="# Beds\nThree raised beds.\n# Pests\nSlugs.")
    add_entry(journal, date(2024, 1, 14), text="# Work\nBoxes.\n# Evening\nRest.", path="/vault/14.md")
    entry = ParsedEntry(date(2024, 1, 20), "x", "/vault/20.md", links=["2024-01-14", "garden"],
                        link_anchors={"2024-01-14": ["Work"], "garden": ["Pests"]})
    store.upsert_file(journal, entry.source_path, "h", "indexed")
    later = store.save_entry(journal, entry, Extraction(summary="s"), "m")
    data = browser.get(f"/api/entries/{later}")
    assert data["links_to_days"][0]["sections"] == [{"anchor": "Work", "label": "Work"}]
    garden = next(n for n in data["notes"] if n["title"] == "Garden")
    assert garden["sections"] == [{"anchor": "Pests", "label": "Pests"}]

    earlier = entry_id(journal, "2024-01-14")
    day = browser.get(f"/api/entries/{earlier}?at=Work")
    assert day["focus"] == {"anchor": "Work", "label": "Work", "lines": [0, 2]}
    assert day["linked_from_days"][0]["sections"][0]["label"] == "Work"
    assert browser.get(f"/api/entries/{earlier}?at=Holiday")["focus"]["lines"] is None
    assert browser.get(f"/api/entries/{earlier}")["focus"] is None
    note = browser.get(f"/api/notes/{note_id(journal, 'Garden')}?at=Pests")
    assert note["focus"]["lines"] == [2, 4]


def test_notes_and_the_days_they_link_show_on_both_pages(browser, journal):
    add_note(journal, "Lisbon Trip", kind="events", links=["2024-01-15"])
    trip = browser.get(f"/api/notes/{note_id(journal, 'Lisbon Trip')}")
    assert [d["date"] for d in trip["linked_days"]] == ["2024-01-15"]
    assert trip["day_links_count"] is False
    day = browser.get(f"/api/entries/{entry_id(journal, '2024-01-15')}")
    assert [n["title"] for n in day["linked_from_notes"]] == ["Lisbon Trip"]


def test_property_values_group_notes_and_filter_entries(browser, journal):
    add_note(journal, "Jane Doe", properties={"Relationship": "Friend, Family"})
    add_entry(journal, date(2024, 1, 22), links=["Jane Doe"], path="/vault/22.md")
    [group] = browser.get("/api/notes")["groups"]
    assert group["name"] == "Relationship"
    assert [(v["value"], v["notes"]) for v in group["values"]] == [("Friend", 2), ("Family", 1)]

    data = browser.get("/api/entries?group=relationship%3Dfriend")
    assert data["group"] == {"key": "relationship", "value": "friend", "notes": 2}
    assert [e["date"] for e in data["items"]] == ["2024-01-22", "2024-01-15"]
    assert "property and a value" in browser.get("/api/entries?group=friend", status=400)["error"]
    assert browser.get("/api/entries?group=relationship%3Denemy", status=404)

    jane = browser.get(f"/api/notes/{note_id(journal, 'Jane Doe')}")
    assert [(p["value"], p["notes"]) for p in jane["property_groups"]] == [("Friend", 2), ("Family", 1)]


def test_entries_cut_into_parts_show_them_and_search_names_the_part(browser, journal):
    day = add_entry(journal, date(2024, 1, 25), text="# Morning\nTea.\n# Work\nThe quarterly review.",
                    path="/vault/25.md")
    store.replace_entry_sections(journal, day, parser.split_sections("# Morning\nTea.\n# Work\nThe quarterly review.", 0))
    data = browser.get(f"/api/entries/{day}")
    assert [(p["heading"], p["lines"], p["embedded"]) for p in data["parts"]] == [
        ("Morning", [0, 2], False), ("Work", [2, 4], False),
    ]
    found = browser.get(f"/api/search?q={quote('quarterly review')}")
    assert found["entries"][0]["id"] == day and found["entries"][0]["section"] == "Work"


def test_entry_pages_show_what_the_vision_model_saw(browser, journal):
    day = entry_id(journal, "2024-01-15")
    store.save_entry_photos(journal, day, [photos.Photo("top.jpg", "Links/!Files/top.jpg", "A summit cairn.")])
    data = browser.get(f"/api/entries/{day}")
    assert data["photos"] == [{"name": "top.jpg", "path": "Links/!Files/top.jpg", "description": "A summit cairn."}]
    assert browser.get(f"/api/entries/{entry_id(journal, '2024-01-16')}")["photos"] == []


def test_notes_page_says_why_links_find_nothing(browser, journal):
    add_entry(journal, date(2024, 1, 21), links=["Jon Doe", "2024-01-30"], path="/vault/21.md")
    data = browser.get("/api/notes")
    by = {link["target"]: link for link in data["unresolved_links"]}
    assert (by["jon doe"]["reason"], by["jon doe"]["like"]) == ("typo", "John Doe")
    assert by["jon doe"]["like_id"] == note_id(journal, "John Doe")
    assert by["2024-01-30"]["reason"] == "no_entry"
    assert by["nobody yet"]["reason"] == "missing"
    assert data["unresolved_total"] == 3


def test_note_pages_list_the_names_learned_from_link_text(browser, config, journal):
    for day in (21, 22):
        entry = ParsedEntry(date(2024, 1, day), "x", f"/vault/{day}.md", links=["john doe"],
                            link_labels={"john doe": ["Johnny"]})
        store.upsert_file(journal, entry.source_path, "h", "indexed")
        store.save_entry(journal, entry, Extraction(summary="s"), "m")
    nicknames.refresh(config, journal)
    john = note_id(journal, "John Doe")
    detail = browser.get(f"/api/notes/{john}")
    assert detail["learned_names"] == [{"name": "Johnny", "entries": 2}]
    catalog = next(n for n in browser.get("/api/catalog")["notes"] if n["id"] == john)
    assert catalog["learned"] == ["Johnny"]
    listed = next(n for n in browser.get("/api/notes")["notes"] if n["id"] == john)
    assert listed["learned"] == ["Johnny"]


def test_similar_entries_use_stored_vectors(browser, journal):
    first, second = entry_id(journal, "2024-01-15"), entry_id(journal, "2024-01-16")
    store.save_entry_vector(journal, first, [1.0] * 8)
    store.save_entry_vector(journal, second, [0.9] * 8)
    data = browser.get(f"/api/entries/{first}")
    assert data["embedded"] is True
    assert [e["id"] for e in data["similar"]] == [second]


def test_missing_entry_is_a_404(browser):
    assert "no indexed entry" in browser.get("/api/entries/9999", status=404)["error"]
    assert browser.get("/api/nope", status=404)["error"] == "No such API route."


def test_ids_past_the_sqlite_range_are_a_404(browser):
    assert "no indexed entry" in browser.get(f"/api/entries/{2**63}", status=404)["error"]
    assert "no indexed note" in browser.get(f"/api/notes/{'9' * 5000}", status=404)["error"]


def test_a_bug_in_a_route_still_answers_in_json(browser, monkeypatch):
    def broken(req):
        raise KeyError("month")

    monkeypatch.setattr(browse, "_ROUTES", [(re.compile(r"/api/overview"), broken)])
    assert "KeyError" in browser.get("/api/overview", status=500)["error"]


def test_entries_filter_by_tag_note_text_and_dates(browser, journal):
    assert browser.get("/api/entries")["total"] == 2
    assert [e["date"] for e in browser.get("/api/entries?order=oldest")["items"]] == [
        "2024-01-15", "2024-01-16"]
    assert [e["date"] for e in browser.get("/api/entries?tag=rain")["items"]] == ["2024-01-16"]
    john = note_id(journal, "John Doe")
    by_note = browser.get(f"/api/entries?note={john}")
    assert by_note["total"] == 1
    assert by_note["note"]["title"] == "John Doe"
    assert browser.get("/api/entries?q=rained")["total"] == 1
    assert browser.get("/api/entries?q=100%25")["total"] == 0
    assert browser.get("/api/entries?from=2024-01-16")["total"] == 1
    page = browser.get("/api/entries?limit=1&offset=1")
    assert page["total"] == 2 and len(page["items"]) == 1
    assert "date like" in browser.get("/api/entries?from=soon", status=400)["error"]


def test_notes_list_and_detail(browser, journal):
    data = browser.get("/api/notes")
    john = next(n for n in data["notes"] if n["title"] == "John Doe")
    assert john["mentions"] == 1
    assert data["unresolved_links"][0]["target"] == "nobody yet"

    detail = browser.get(f"/api/notes/{john['id']}")
    assert detail["properties"] == {"Relationship": "Friend"}
    assert detail["raw_text"] == "Friend from <b>school</b>."
    assert detail["summary_source"] is None
    assert [o["title"] for o in detail["outlinks"]] == ["Bear Mountain"]
    assert [e["date"] for e in detail["entries"]] == ["2024-01-15"]
    assert detail["avg_mood"] == 8.0
    assert detail["by_year"] == [{"year": "2024", "n": 1}]

    mountain = browser.get(f"/api/notes/{note_id(journal, 'Bear Mountain')}")
    assert mountain["summary_source"] == "own_text"
    assert [b["title"] for b in mountain["backlinks"]] == ["John Doe"]
    assert [(o["title"], o["n"]) for o in mountain["linked_with"]] == [("John Doe", 1)]


def test_tags_and_tag_detail(browser):
    data = browser.get("/api/tags")
    hiking = next(t for t in data["tags"] if t["name"] == "hiking")
    assert hiking["n"] == 1 and hiking["avg_mood"] == 8.0

    detail = browser.get("/api/tags/hiking")
    assert detail["total"] == 1
    assert detail["entries_capped"] is False
    assert [c["name"] for c in detail["cooccurring"]] == ["friends"]
    assert browser.get("/api/tags/unknown", status=404)["error"]


def test_mood_averages_cover_entries_past_the_listed_ones(browser, journal, monkeypatch):
    monkeypatch.setattr(browse, "TAG_ENTRIES_LIMIT", 1)
    monkeypatch.setattr(browse, "NOTE_ENTRIES_LIMIT", 1)
    add_entry(journal, date(2024, 2, 1), mood=2.0, tags=["hiking"],
              links=["John Doe"], path="/vault/2024-02-01.md")

    tag = browser.get("/api/tags/hiking")
    assert (tag["total"], len(tag["entries"]), tag["entries_capped"]) == (2, 1, True)
    assert tag["avg_mood"] == 5.0

    note = browser.get(f"/api/notes/{note_id(journal, 'John Doe')}")
    assert (len(note["entries"]), note["entries_capped"]) == (1, True)
    assert note["avg_mood"] == 5.0


def test_tag_names_with_slashes_and_spaces(browser, journal):
    add_entry(journal, date(2024, 2, 1), tags=["work/side project"], path="/vault/w.md")
    assert browser.get(f"/api/tags/{quote('work/side project', safe='')}")["total"] == 1


def test_digests_and_digest_detail(browser):
    data = browser.get("/api/digests")
    assert [d["key"] for d in data["digests"]["month"]] == ["2024-01"]
    month = data["digests"]["month"][0]
    assert month["tags"] == ["hiking"]
    assert (month["start"], month["end"]) == ("2024-01-01", "2024-01-31")

    detail = browser.get("/api/digests/month/2024-01")
    assert detail["digest"]["summary"] == "A calm January."
    assert len(detail["entries"]) == 2

    week = browser.get("/api/digests/week/2024-W03")
    assert week["digest"] is None and len(week["entries"]) == 2
    assert browser.get("/api/digests/month/1999-01", status=404)["error"]
    assert browser.get("/api/digests/month/March", status=400)["error"]


def test_files_show_status_and_what_each_became(browser, journal):
    files = {f["path"]: f for f in browser.get("/api/files")["files"]}
    assert files["/vault/broken.md"]["status"] == "error"
    assert files["/vault/2024-01-15.md"]["entries"] == 1
    assert files["/vault/2024-01-15.md"]["entry_id"] == entry_id(journal, "2024-01-15")
    john = next(f for f in files.values() if f["note_id"] == note_id(journal, "John Doe"))
    assert john["entries"] == 0
    # problems first
    assert next(iter(files)) == "/vault/broken.md"


def test_keyword_search_needs_no_model(browser, fake_client):
    data = browser.get("/api/search?q=rainy&mode=keyword")
    assert data["mode"] == "keyword"
    assert fake_client.embed_calls == []
    data = browser.get(f"/api/search?q={quote('John Doe')}&mode=keyword")
    assert [e["date"] for e in data["entries"]] == ["2024-01-15"]
    assert data["notes"][0]["title"] == "John Doe"
    assert browser.get("/api/search?q=%20", status=400)["error"]


def test_meaning_search_uses_the_embedding(browser, journal, fake_client):
    for day in ("2024-01-15", "2024-01-16"):
        store.save_entry_vector(journal, entry_id(journal, day), [0.5] * 8)
    data = browser.get("/api/search?q=calm+walk&mode=meaning")
    assert data["mode"] == "meaning"
    assert data["notice"] is None
    assert fake_client.embed_calls
    assert len(data["entries"]) == 2


def test_meaning_search_falls_back_to_keywords_when_ollama_is_down(browser, fake_client):
    def down(model, text, options=None):
        raise OllamaUnavailableError("Cannot reach Ollama at http://localhost:11434")

    fake_client.embed = down
    data = browser.get("/api/search?q=rainy&mode=meaning")
    assert data["mode"] == "keyword"
    assert "Cannot reach Ollama" in data["notice"]
    assert [e["date"] for e in data["entries"]] == ["2024-01-16"]


def test_sees_changes_written_while_it_runs(browser, journal):
    assert browser.get("/api/entries")["total"] == 2
    add_entry(journal, date.today() + timedelta(days=400), path="/vault/typo.md")
    assert browser.get("/api/entries")["total"] == 3
    assert len(browser.get("/api/overview")["future_entries"]) == 1


def test_cache_errors_come_back_as_json(browser, config, journal):
    journal.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    journal.commit()
    assert "newer version" in browser.get("/api/overview", status=503)["error"]


def test_port_in_use_is_explained(config, journal):
    first = browse.make_server(config, port=0)
    try:
        with pytest.raises(Exception, match="--port"):
            browse.make_server(config, port=first.port)
    finally:
        first.server_close()


def _events(response) -> list[tuple[str, dict]]:
    assert response.getheader("Content-Type").startswith("text/event-stream")
    out = []
    for block in response.read().decode().split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_ask_streams_its_steps_then_the_answer(browser, journal, fake_client):
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked Bear Mountain with John on 2024-01-15."
    response = browser.request(f"/api/ask?q={quote('When did I hike?')}")
    assert response.status == 200
    events = _events(response)
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "answer" and "stage" in kinds
    answer = events[-1][1]
    assert answer["body"] == "You hiked Bear Mountain with John on 2024-01-15."
    assert answer["stage"] == "fast_summary"
    first = entry_id(journal, "2024-01-15")
    assert {"date": "2024-01-15", "id": first, "section": None} in answer["sources"]["dates"]
    assert {e["id"] for e in answer["entries"]} == {first, entry_id(journal, "2024-01-16")}
    assert "Sources:" in answer["text"]


def test_ask_follow_ups_are_rewritten_first(browser, fake_client):
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")

    def chat(model, messages):
        if messages[0]["content"] == query._CONTEXTUALIZE_SYSTEM:
            assert "User: How was January?" in messages[-1]["content"]
            return "How was February 2024?"
        return "Quiet."

    fake_client.chat_response = chat
    history = json.dumps([["How was January?", "Calm."]])
    events = _events(browser.request(f"/api/ask?q=and+Feb%3F&history={quote(history)}"))
    assert ("rewritten", {"question": "How was February 2024?"}) in events
    assert events[-1][1]["question"] == "How was February 2024?"


def test_ask_reports_problems_inside_the_stream(browser, fake_client):
    def down(model, messages, schema, options=None):
        raise OllamaUnavailableError("Cannot reach Ollama at http://localhost:11434")

    fake_client.chat_structured = down
    events = _events(browser.request("/api/ask?q=anything"))
    assert events[-1][0] == "problem"
    assert "Cannot reach Ollama" in events[-1][1]["error"]


def test_ask_rejects_bad_input_before_streaming(browser):
    assert "Type a question" in browser.get("/api/ask?q=%20", status=400)["error"]
    long = "x" * (browse.MAX_QUESTION_CHARS + 1)
    assert "shorter" in browser.get(f"/api/ask?q={long}", status=400)["error"]
    for history in ("not json", json.dumps({"a": 1}), json.dumps([["only one"]])):
        error = browser.get(f"/api/ask?q=hi&history={quote(history)}", status=400)["error"]
        assert "conversation" in error


def test_ask_keeps_only_the_last_turns_of_history():
    turns = [[f"q{i}", f"a{i}"] for i in range(10)]
    _, history = browse._ask_params({"q": "next", "history": json.dumps(turns)})
    assert history == [(f"q{i}", f"a{i}") for i in range(4, 10)]


def test_note_pages_carry_the_relationship_digest(browser, journal):
    john = note_id(journal, "John Doe")
    assert browser.get(f"/api/notes/{john}")["digest"] is None
    store.upsert_note_digest(
        journal, "john doe", "John keeps turning up.", store.Mentions(1, "2024-01-15", "2024-01-15"), 8.0
    )
    detail = browser.get(f"/api/notes/{john}")
    assert detail["digest"]["summary"] == "John keeps turning up."
    assert detail["digest"]["stale"] is False
    assert detail["digest_min_mentions"] == 8
    listed = browser.get("/api/digests")["notes"]
    assert [(n["id"], n["title"], n["entries"]) for n in listed] == [(john, "John Doe", 1)]


def test_note_pages_list_entries_that_name_it_without_a_link(browser, journal):
    add_entry(journal, date(2024, 1, 20), text="Called John about the hike.", path="/vault/20.md")
    detail = browser.get(f"/api/notes/{note_id(journal, 'John Doe')}")
    assert detail["unlinked"] == [{
        "id": entry_id(journal, "2024-01-20"), "date": "2024-01-20", "name": "John",
        "snippet": "Called John about the hike.",
    }]


def test_ask_streams_the_answer_text_as_it_is_written(browser, fake_client):
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked with John."
    events = _events(browser.request("/api/ask?q=when"))
    written = "".join(data["text"] for kind, data in events if kind == "text")
    assert written == "You hiked with John."
    assert events[-1][0] == "answer"


def test_entry_pages_show_the_numbers_from_the_frontmatter(browser, journal):
    first = entry_id(journal, "2024-01-15")
    assert browser.get(f"/api/entries/{first}")["metrics"] == {}
    store.replace_entry_metrics(journal, first, {"sleep": 7.5})
    assert browser.get(f"/api/entries/{first}")["metrics"] == {"sleep": 7.5}


def test_notes_page_lists_notes_that_went_quiet(browser, journal):
    add_note(journal, "Jun Park")
    for i in range(5):
        add_entry(journal, date(2022, 1, i + 1), links=["Jun Park"], path=f"/vault/jun{i}.md")
    data = browser.get("/api/notes")
    assert [(n["title"], n["n"], n["last"]) for n in data["gone_quiet"]] == [("Jun Park", 5, "2022-01-05")]
    assert data["gone_quiet_rule"] == {"min": 5, "months": 6}


def test_the_you_page_shows_the_profile_and_its_facts(browser, journal):
    empty = browser.get("/api/profile")
    assert empty["profile"] is None and empty["enabled"] is True and empty["unread"] == 2
    db.set_meta(journal, "profile_text", "I like hills.")
    db.set_meta(journal, "profile_names", "Al")
    store.save_profile_scan(journal, ["h"], [("h", "2024-01-15", "hobbies", "I hike.")])
    data = browser.get("/api/profile")
    assert data["profile"]["text"] == "I like hills." and data["facts_count"] == 1
    assert data["facts"] == [{"date": "2024-01-15", "category": "hobbies", "fact": "I hike.",
                              "folded": False, "entry_id": entry_id(journal, "2024-01-15")}]


def test_entry_pages_carry_the_longer_summary(browser, journal):
    first = entry_id(journal, "2024-01-15")
    assert browser.get(f"/api/entries/{first}")["details"] is None
    store.save_entry_details(journal, first, "A longer account of the hike.")
    assert browser.get(f"/api/entries/{first}")["details"] == "A longer account of the hike."


def test_ask_can_be_kept_to_a_note_tag_group_or_dates(browser, journal, fake_client):
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "Only the hike."
    john = note_id(journal, "John Doe")
    events = _events(browser.request(f"/api/ask?q=what+happened&note={john}"))
    answer = events[-1][1]
    assert events[-1][0] == "answer"
    assert [e["date"] for e in answer["entries"]] == ["2024-01-15"]

    events = _events(browser.request("/api/ask?q=what+happened&tag=rain"))
    assert [e["date"] for e in events[-1][1]["entries"]] == ["2024-01-16"]

    events = _events(browser.request("/api/ask?q=what+happened&group=relationship%3Dfriend"))
    assert [e["date"] for e in events[-1][1]["entries"]] == ["2024-01-15"]

    events = _events(browser.request("/api/ask?q=what+happened&from=2024-01-16&to=2024-01-31"))
    assert [e["date"] for e in events[-1][1]["entries"]] == ["2024-01-16"]


def test_a_scope_that_finds_nothing_is_refused_before_streaming(browser, journal):
    assert "no indexed note" in browser.get("/api/ask?q=hi&note=99999", status=404)["error"]
    assert "must be a note's id" in browser.get("/api/ask?q=hi&note=john", status=400)["error"]
    assert "tagged 'nope'" in browser.get("/api/ask?q=hi&tag=nope", status=404)["error"]
    assert "property and a value" in browser.get("/api/ask?q=hi&group=relationship", status=400)["error"]
    assert "No note's relationship" in browser.get("/api/ask?q=hi&group=relationship%3Denemy", status=404)["error"]
    assert "is after" in browser.get("/api/ask?q=hi&from=2024-02-01&to=2024-01-01", status=400)["error"]
    assert "a date like" in browser.get("/api/ask?q=hi&from=yesterday", status=400)["error"]
