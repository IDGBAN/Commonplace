import hashlib
import json
from datetime import date
from types import SimpleNamespace

import ollama
from conftest import copy_fixtures, copy_note_fixtures, invoke_cli, make_config

from journal_analyzer import cli, extractor, ingester, memo, ollama_client
from journal_analyzer import db as db_mod
from journal_analyzer.models import Extraction, NoteSummary, ParsedEntry
from journal_analyzer.profile import ProfileDigest, ScannedFacts


class FakeOllama:
    """Stands in for ollama.Client and counts what reaches it."""

    def __init__(self):
        self.chats = 0
        self.embeds = 0
        self.weights = "w1"

    def chat(self, model, messages, options=None, think=None, format=None, stream=False):
        self.chats += 1
        if format is not None:
            title = format.get("title")
            reply = {
                "Extraction": Extraction(mood_score=6, summary="Hiked.", tags=["hiking"]),
                "NoteSummary": NoteSummary(summary="A friend."),
                "ScannedFacts": ScannedFacts(),
                "ProfileDigest": ProfileDigest(profile="p"),
            }[title]
            return {"message": {"content": reply.model_dump_json()}}
        return {"message": {"content": f"reply {self.chats}"}}

    def embed(self, model, input, options=None):
        self.embeds += 1
        digest = hashlib.sha256(input.encode()).digest()
        return {"embeddings": [[b / 255.0 for b in digest[:8]]]}

    def list(self):
        return SimpleNamespace(models=[SimpleNamespace(model=m, digest=self.weights)
                                       for m in ("fake-fast", "fake-precise", "fake-embed")])

    def show(self, model):
        return SimpleNamespace(modelinfo={})


def _client(cfg, monkeypatch, inner):
    monkeypatch.setattr(ollama, "Client", lambda **kwargs: inner)
    return ollama_client.OllamaClient(cfg)


def test_the_memo_sits_next_to_the_cache_unless_moved(tmp_path):
    cfg = make_config(tmp_path)
    assert memo.memo_path(cfg) == tmp_path / "journal-memo.db"
    cfg.db.memo_path = tmp_path / "elsewhere.db"
    assert memo.memo_path(cfg) == tmp_path / "elsewhere.db"


def test_text_and_vectors_round_trip(tmp_path):
    kept = memo.Memo(tmp_path / "m.db")
    assert kept.get_text("k") is None
    kept.put_text("k", "chat", "m", "hello")
    kept.put_vector("v", "e", [0.5, 0.25])
    assert kept.get_text("k") == "hello"
    assert kept.get_vector("v") == [0.5, 0.25]


def test_a_memo_that_cannot_open_is_skipped(tmp_path):
    (tmp_path / "m.db").mkdir()
    kept = memo.Memo(tmp_path / "m.db")
    assert kept.get_text("k") is None
    kept.put_text("k", "chat", "m", "x")  # no error


def test_settled_chat_replies_are_reused_and_others_are_not(tmp_path, monkeypatch):
    inner = FakeOllama()
    client = _client(make_config(tmp_path), monkeypatch, inner)
    messages = [{"role": "user", "content": "q"}]
    assert client.chat("fake-precise", messages) == client.chat("fake-precise", messages) == "reply 1"
    assert inner.chats == 1
    client.chat("fake-precise", messages, options={"temperature": 0.3})
    client.chat("fake-precise", messages, options={"temperature": 0.3})
    assert inner.chats == 3


def test_structured_replies_and_embeddings_are_reused(tmp_path, monkeypatch):
    inner = FakeOllama()
    cfg = make_config(tmp_path)
    client = _client(cfg, monkeypatch, inner)
    messages = [{"role": "user", "content": "entry"}]
    first = client.chat_structured("fake-fast", messages, Extraction)
    again = ollama_client.OllamaClient(cfg).chat_structured("fake-fast", messages, Extraction)
    assert first == again and inner.chats == 1
    assert client.embed("fake-embed", "text") == client.embed("fake-embed", "text")
    assert inner.embeds == 1


def test_new_weights_under_the_same_name_miss(tmp_path, monkeypatch):
    inner = FakeOllama()
    cfg = make_config(tmp_path)
    _client(cfg, monkeypatch, inner).chat("fake-precise", [{"role": "user", "content": "q"}])
    inner.weights = "w2"
    ollama_client.OllamaClient(cfg).chat("fake-precise", [{"role": "user", "content": "q"}])
    assert inner.chats == 2


def test_turned_off_it_always_asks(tmp_path, monkeypatch):
    inner = FakeOllama()
    cfg = make_config(tmp_path)
    cfg.db.memo = False
    client = _client(cfg, monkeypatch, inner)
    client.chat("fake-precise", [{"role": "user", "content": "q"}])
    client.chat("fake-precise", [{"role": "user", "content": "q"}])
    assert inner.chats == 2 and not memo.memo_path(cfg).exists()


def test_a_different_tag_list_reuses_the_extraction(tmp_path, monkeypatch):
    inner = FakeOllama()
    cfg = make_config(tmp_path)
    client = _client(cfg, monkeypatch, inner)
    entry = ParsedEntry(date(2024, 1, 1), "Hiked.", "x")
    extractor.extract(entry, cfg, client, known_tags=["work"])
    extractor.extract(entry, cfg, client, known_tags=["work", "hiking", "family"])
    assert inner.chats == 1
    extractor.extract(ParsedEntry(date(2024, 1, 1), "Swam.", "x"), cfg, client)
    assert inner.chats == 2


def test_rebuilding_the_cache_reuses_every_model_result(tmp_path, monkeypatch):
    inner = FakeOllama()
    cfg = make_config(tmp_path, notes_paths=["Links"])
    copy_fixtures(cfg.vault.path)
    copy_note_fixtures(cfg.vault.path)
    monkeypatch.setattr(ollama, "Client", lambda **kwargs: inner)

    def build():
        conn = db_mod.connect(cfg)
        db_mod.init_schema(conn)
        ingester.run_backfill(cfg, conn, client=ollama_client.OllamaClient(cfg))
        rows = conn.execute("SELECT entry_date, summary, mood_score FROM entries ORDER BY entry_date").fetchall()
        conn.close()
        return [tuple(r) for r in rows]

    first = build()
    calls = (inner.chats, inner.embeds)
    assert calls[0] > 0 and calls[1] > 0
    for suffix in ("", "-wal", "-shm"):
        (tmp_path / f"journal.db{suffix}").unlink(missing_ok=True)
    assert build() == first
    assert (inner.chats, inner.embeds) == calls


def test_no_memo_and_stats(tmp_path, config, conn):
    assert not cli._memo(config, True).db.memo and cli._memo(config, False) is config
    kept = memo.Memo(memo.memo_path(config))
    kept.put_text("k", "chat", "m", "x")
    kept.close()
    output = " ".join(invoke_cli(tmp_path, config, "stats").output.split())
    assert "Model memo : 1 results" in output
    data = json.loads(invoke_cli(tmp_path, config, "stats", "--json").stdout)
    assert data["memo"]["results"] == 1
