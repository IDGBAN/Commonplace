import io
import json
from datetime import date

from conftest import EMBED_DIM, add_entry, add_note, invoke_cli

from journal_analyzer import mcp_server, store
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


def _richer(conn):
    _journal(conn)
    add_note(conn, "Jun Park", properties={"relationship": "family"})
    add_entry(conn, date(2023, 2, 1), text="Lunch with John Doe today.", summary="Lunch", path="/vault/2023-02-01.md")
    for day in (1, 2, 3, 4, 5, 6):
        add_entry(conn, date(2023, 6, day), text=f"Called Jun Park, day {day}.", mood=6,
                  tags=["family"], links=["Jun Park"], summary=f"Called Jun {day}", path=f"/vault/2023-06-0{day}.md")
    conn.execute("INSERT INTO entry_metrics(entry_id, key, value) SELECT id, 'sleep', 7 FROM entries")
    conn.commit()


def test_the_new_read_tools_match_their_commands(tmp_path, config, conn, fake_client):
    _richer(conn)
    replies = _session(
        config, fake_client,
        _call(1, "compare", first="2024", second="2023"),
        _call(2, "compare", first="2024-01"),
        _call(3, "review", period="2023-06"),
        _call(4, "review"),
        _call(5, "metric"),
        _call(6, "metric", key="Sleep", tag="family"),
        _call(7, "tags", limit=1),
        _call(8, "lapsed", min=3, quiet_months=6),
        _call(9, "unlinked"),
        _call(10, "unlinked", name="john"),
        _call(11, "profile"),
        _call(12, "mood", group="relationship=family"),
    )[:12]
    both, one, month, latest, metrics, sleep, tags, lapsed, unlinked_all, john, prof, mood = map(_payload, replies)
    assert (both["a"]["label"], both["b"]["label"]) == ("2024", "2023")
    assert (one["a"]["label"], one["b"]["label"]) == ("2023-12", "2024-01")
    assert month["title"] == "June 2023" and month["writing"]["entries"] == 6
    assert latest["key"] == "2024"
    assert metrics["metrics"][0]["key"] == "sleep"
    assert sleep == {"metric": "sleep", "months": [{"month": "2023-06", "avg": 7.0, "n": 6}]}
    assert tags == {"tags": [{"name": "family", "n": 6}]}
    assert [n["title"] for n in lapsed["notes"]] == ["Jun Park"] and lapsed["notes"][0]["months_quiet"] == 7
    assert unlinked_all["total"] == 1 and unlinked_all["notes"][0]["title"] == "John Doe"
    assert john["note"]["title"] == "John Doe" and john["entries"][0]["entry_date"] == "2023-02-01"
    assert prof["profile"] is None and prof["recent_facts"] == []
    assert [m["month"] for m in mood["months"]] == ["2023-06"]

    # the same JSON as the commands print
    for args, payload in (
        (["compare", "2024", "2023"], both), (["review", "2023-06"], month), (["lapsed", "--min", "3"], lapsed),
    ):
        result = invoke_cli(tmp_path, config, *args, "--json")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == json.loads(json.dumps(payload))


def test_similar_returns_the_closest_days(config, conn, fake_client):
    _journal(conn)
    rows = conn.execute("SELECT id FROM entries ORDER BY entry_date").fetchall()
    for i, (entry_id,) in enumerate(rows):
        store.save_entry_vector(conn, entry_id, [float(i + 1)] * EMBED_DIM)
    similar, missing = _session(
        config, fake_client, _call(1, "similar", date="2024-01-15"), _call(2, "similar", date="2020-01-01"),
    )[:2]
    assert [e["date"] for e in _payload(similar)["entries"]] == ["2023-01-15"]
    assert missing["result"]["isError"] and "no entry on 2020-01-01" in missing["result"]["content"][0]["text"]


def test_bad_arguments_to_the_new_tools_are_tool_errors(config, conn, fake_client):
    _richer(conn)
    config.profile.enabled = False
    replies = _session(
        config, fake_client,
        _call(1, "compare", first="spring"),
        _call(2, "review", period="1999"),
        _call(3, "metric", key="steps"),
        _call(4, "tags", limit="lots"),
        _call(5, "mood", tag="nope"),
        _call(6, "mood", group="relationship"),
        _call(7, "profile"),
        _call(8, "unlinked", name="Nobody"),
    )[:8]
    texts = [r["result"]["content"][0]["text"] for r in replies]
    assert all(r["result"]["isError"] for r in replies), texts
    assert "isn't a period" in texts[0]
    assert "no entries in 1999" in texts[1]
    assert "No entry records 'steps'. Known: sleep." in texts[2]
    assert "must be a whole number" in texts[3]
    assert "No entry is tagged 'nope'" in texts[4]
    assert "takes a property and a value" in texts[5]
    assert "profile is turned off" in texts[6]
    assert "No note is called 'Nobody'" in texts[7]
