import io
import json
from datetime import date

from conftest import add_entry, add_note

from journal_analyzer import mcp_server
from journal_analyzer.models import OllamaUnavailableError, QueryPlan


def _session(config, fake_client, *messages):
    out = io.StringIO()
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages) + "not json\n")
    mcp_server.serve(config, stdin, out, make_client=lambda cfg: fake_client)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _call(msg_id, tool, **arguments):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


def _payload(reply):
    result = reply["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _journal(conn):
    add_note(conn, "John Doe", aliases=["John"], text="Friend from school.")
    add_entry(conn, date(2024, 1, 15), text="Hiked Bear Mountain with John Doe.", mood=8,
              tags=["hiking"], links=["John Doe", "Nobody"], summary="Hiked with John", path="/vault/a.md")
    add_entry(conn, date(2023, 1, 15), text="Snow day.", summary="Snow", path="/vault/b.md")


def test_initialize_and_list_the_tools(config, conn, fake_client):
    replies = _session(
        config, fake_client,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
    )
    init, tools, ping, unknown, bad = replies
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert {t["name"] for t in tools["result"]["tools"]} >= {"search", "ask", "day", "note", "mood"}
    assert all(t["inputSchema"]["type"] == "object" for t in tools["result"]["tools"])
    assert ping["result"] == {}
    assert unknown["error"]["code"] == -32601
    assert bad["error"]["code"] == -32700


def test_an_unknown_protocol_version_gets_the_newest(config, conn, fake_client):
    [init] = _session(config, fake_client, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                            "params": {"protocolVersion": "1999-01-01"}})[:1]
    assert init["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSIONS[0]


def test_read_tools_return_the_json_shapes(config, conn, fake_client):
    _journal(conn)
    day, note, mood, on_this_day, broken = _session(
        config, fake_client,
        _call(1, "day", date="2024-01-15"),
        _call(2, "note", name="john"),
        _call(3, "mood", about="John Doe"),
        _call(4, "on_this_day", date="2025-01-15"),
        _call(5, "broken_links"),
    )[:5]
    [entry] = _payload(day)["entries"]
    assert (entry["summary"], entry["text"]) == ("Hiked with John", "Hiked Bear Mountain with John Doe.")
    assert [n["title"] for n in entry["notes"]] == ["John Doe"]
    assert _payload(note)["title"] == "John Doe" and _payload(note)["mentions"] == 1
    assert _payload(mood)["months"] == [{"month": "2024-01", "avg_mood": 8.0, "entry_count": 1}]
    assert [e["date"] for e in _payload(on_this_day)["entries"]] == ["2023-01-15", "2024-01-15"]
    assert [b["target"] for b in _payload(broken)["links"]] == ["nobody"]


def test_search_and_ask_use_the_model_and_fall_back(config, conn, fake_client):
    _journal(conn)
    config.reranking.enabled = False
    fake_client.structured["QueryPlan"] = QueryPlan(intent="factual")
    fake_client.chat_response = "You hiked with John on 2024-01-15."
    search, ask = _session(
        config, fake_client, _call(1, "search", query="Bear Mountain"), _call(2, "ask", question="When did I hike?"),
    )[:2]
    assert _payload(search)["notice"] is None
    assert "2024-01-15" in [e["date"] for e in _payload(search)["entries"]]
    assert _payload(ask)["body"] == "You hiked with John on 2024-01-15."

    def down(model, text, options=None):
        raise OllamaUnavailableError("Ollama isn't running.")

    fake_client.embed = down
    offline, failed = _session(
        config, fake_client, _call(1, "search", query="Bear Mountain"), _call(2, "ask", question="When?"),
    )[:2]
    assert _payload(offline)["notice"].startswith("Keyword matches only")
    assert _payload(offline)["entries"][0]["date"] == "2024-01-15"
    assert failed["result"]["isError"] is True and "Ollama isn't running" in failed["result"]["content"][0]["text"]


def test_bad_calls_are_tool_errors(config, conn, fake_client):
    _journal(conn)
    replies = _session(
        config, fake_client,
        _call(1, "day", date="tomorrow"),
        _call(2, "note", name="Nobody At All"),
        _call(3, "digest", period="spring"),
        _call(4, "delete_everything"),
        _call(5, "search"),
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": 3}},
    )
    texts = [r["result"]["content"][0]["text"] for r in replies[:5]]
    assert all(r["result"]["isError"] for r in replies[:5])
    assert "must be a date" in texts[0]
    assert "No note is called" in texts[1]
    assert "isn't a period" in texts[2]
    assert "no tool called" in texts[3]
    assert "'query' is required" in texts[4]
    assert replies[5]["error"]["code"] == -32602
