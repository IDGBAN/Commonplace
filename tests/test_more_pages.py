"""Insights, writing prompts, downloads, tag merges and the unlinked list, from the browser."""

import csv
import io
import json
import threading
from datetime import date, timedelta

import pytest
from conftest import add_entry, add_note
from test_browse import Browser

from journal_analyzer import browse
from journal_analyzer.models import OllamaUnavailableError


@pytest.fixture
def served(config, conn, fake_client):
    add_note(conn, "John Doe", aliases=["John"])
    add_entry(conn, date(2024, 1, 15), text="Hiked with John Doe.", mood=8.0, tags=["hiking", "friends"],
              links=["John Doe"], summary="Hiked with John", path="/vault/2024-01-15.md")
    add_entry(conn, date(2024, 1, 16), text="Called John about the hike.", mood=4.0, tags=["friend", "rain"],
              summary="Stayed in", path="/vault/2024-01-16.md")
    add_entry(conn, date.today() - timedelta(days=2), text="Started the garden.", mood=7.0, tags=["friends"],
              summary="Dug the first bed", path="/vault/recent.md")
    config.reranking.enabled = False
    server = browse.make_server(config, port=0, make_client=lambda cfg: fake_client)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Browser(server.port, server.token), server
    server.shutdown()
    server.server_close()


def _events(response) -> list[tuple[str, dict]]:
    assert response.getheader("Content-Type").startswith("text/event-stream")
    out = []
    for block in response.read().decode().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if line and not line.startswith(":"))
        if lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_insights_are_written_in_the_background_and_can_be_followed_again(served, fake_client):
    browser, _ = served
    fake_client.chat_response = "You write happier on days with friends."
    events = _events(browser.send("/api/insights", {"from": "2024-01-01", "to": ""}))
    assert [kind for kind, _ in events] == ["run", "stage", "result"]
    result = events[-1][1]
    # three entries are too few for anything to stand out, which it says without asking a model
    assert result["text"].startswith("No patterns cleared") and fake_client.chat_calls == []
    assert result["entries"] == 3 and result["from"] == "2024-01-01" and result["to"] is None
    assert isinstance(result["signals"], list)
    # a page that reloaded gets the same result from the run's id
    again = _events(browser.request(f"/api/ask/{events[0][1]['id']}/events"))
    assert again[-1] == events[-1]
    assert "a date like" in browser.post("/api/insights", {"from": "soon"}, status=400)["error"]


def test_writing_prompts_come_from_recent_entries(served, fake_client):
    browser, _ = served
    fake_client.chat_response = "1. What will you plant first?\n2. Who would you show the garden to?"
    events = _events(browser.send("/api/prompts", {"days": 7, "count": 2}))
    assert events[-1] == ("result", {
        "prompts": ["What will you plant first?", "Who would you show the garden to?"], "days": 7,
    })
    assert "must be numbers" in browser.post("/api/prompts", {"days": "a week"}, status=400)["error"]

    def down(model, messages):
        raise OllamaUnavailableError("Cannot reach Ollama at http://localhost:11434")

    fake_client.chat_response = down
    events = _events(browser.send("/api/prompts", {}))
    assert events[-1][0] == "problem" and "Cannot reach Ollama" in events[-1][1]["error"]
    # reading is GET, and these start a model
    for path in ("/api/insights", "/api/prompts"):
        response = browser.request(path)
        response.read()
        assert (response.status, response.getheader("Allow")) == (405, "POST")


def test_entries_download_as_a_file(served):
    browser, _ = served
    response = browser.request("/api/export?format=csv&tag=hiking")
    body = response.read()
    assert response.status == 200 and response.getheader("Content-Type").startswith("text/csv")
    assert response.getheader("Content-Disposition") == f'attachment; filename="journal-{date.today().isoformat()}.csv"'
    assert body.startswith(b"\xef\xbb\xbf")  # so Excel reads it as UTF-8
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    assert [r["date"] for r in rows] == ["2024-01-15"]

    everything = browser.request("/api/export?format=json&raw=1")
    data = json.loads(everything.read())
    assert len(data["entries"]) == 3 and "Hiked with John Doe." in json.dumps(data)
    markdown = browser.request("/api/export?format=markdown&from=2024-01-16&to=2024-01-31")
    text = markdown.read().decode()
    assert markdown.getheader("Content-Disposition").endswith('.md"') and "2024-01-16" in text and "2024-01-15" not in text

    assert "json, markdown or csv" in browser.get("/api/export?format=docx", status=400)["error"]
    assert "No entries to export" in browser.get("/api/export?from=1999-01-01&to=1999-01-02", status=404)["error"]
    # a page on another site can't pull the journal out as a file
    blocked = browser.request("/api/export?format=json", headers={"Sec-Fetch-Site": "cross-site"})
    blocked.read()
    assert blocked.status == 403


def test_a_review_downloads_as_markdown(served):
    browser, _ = served
    response = browser.request("/api/review/2024-01/download")
    text = response.read().decode()
    assert response.status == 200
    assert response.getheader("Content-Disposition") == 'attachment; filename="review-2024-01.md"'
    assert "January 2024" in text and "[[2024-01-15]]" in text
    assert "isn't a period" in browser.get("/api/review/sometime/download", status=400)["error"]
    assert "No entries" in browser.get("/api/review/1999/download", status=404)["error"]


def test_tags_that_look_alike_are_listed_until_they_are_merged(served, config):
    browser, _ = served
    found = browser.get("/api/similar-tags")
    assert found == {"pairs": [{"tag": "friend", "n": 1, "like": "friends", "like_n": 2}], "merge": {}}
    config.tags.merge = {"friend": "friends"}
    assert browser.get("/api/similar-tags")["pairs"] == []


def test_notes_named_without_a_link_are_listed(served, conn):
    browser, _ = served
    found = browser.get("/api/unlinked")
    assert found["total"] == 1
    assert found["notes"][0] | {"note_id": 0} == {
        "note_id": 0, "title": "John Doe", "kind": "people", "count": 1, "latest": "2024-01-16",
    }
