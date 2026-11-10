"""The Settings page: every setting described, and saved without losing the file's comments."""

import threading

import pytest
from test_browse import Browser

from journal_analyzer import browse, settings
from journal_analyzer.config import Config, load_config

FILE = """# my own notes about this file

[vault]
path = '{vault}'   # where I write
ignore_patterns = [".obsidian"]

[ollama]
# the big one
precise_model = "qwen3:14b"
precise_model_think = true

[db]
path = '{db}'

[tags]
merge = {{ friendship = "friends" }}
"""


@pytest.fixture
def on_disk(tmp_path, config, conn):
    path = tmp_path / "config.toml"
    path.write_text(FILE.format(vault=config.vault.path.as_posix(), db=config.db.path.as_posix()), encoding="utf-8")
    return path


@pytest.fixture
def served(on_disk, fake_client):
    server = browse.make_server(load_config(on_disk), port=0, make_client=lambda cfg: fake_client, config_path=on_disk)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield Browser(server.port, server.token), server
    server.shutdown()
    server.server_close()


def _field(data, section, key):
    fields = next(s for s in data["sections"] if s["name"] == section)["fields"]
    return next(f for f in fields if f["key"] == key)


def test_every_setting_is_described_and_says_what_changing_it_costs():
    known = {f"{section}.{key}" for section, f in Config.model_fields.items() for key in f.annotation.model_fields}
    assert known == set(settings.HELP), "a setting was added or removed without its line in settings.HELP"
    assert set(Config.model_fields) == set(settings.SECTIONS)
    for name, (effect, about) in settings.HELP.items():
        assert effect in settings.EFFECTS, name
        assert about.endswith(".") and "—" not in about, name


def test_the_page_gets_each_setting_with_its_kind_and_value(served, on_disk):
    browser, _ = served
    data = browser.get("/api/settings")
    assert data["path"] == str(on_disk) and data["writable"] is True
    assert [s["name"] for s in data["sections"]] == list(settings.SECTIONS)
    assert _field(data, "vault", "date_source")["choices"] == ["filename", "frontmatter", "heading"]
    assert _field(data, "ollama", "precise_model_think") | {"about": ""} == {
        "key": "precise_model_think", "kind": "maybe", "choices": [], "about": "", "effect": "now",
        "value": True, "default": None, "min": None, "max": None,
    }
    assert (_field(data, "ollama", "think_tokens")["kind"], _field(data, "ollama", "think_tokens")["min"]) == ("int", 256)
    assert _field(data, "tags", "merge")["value"] == {"friendship": "friends"}
    assert _field(data, "vault", "ignore_patterns")["kind"] == "list"
    assert _field(data, "themes", "stale_share")["max"] == 1
    assert _field(data, "ollama", "embed_model")["effect"] == "rebuild"


def test_saving_changes_only_what_changed_and_applies_it(served, on_disk):
    browser, server = served
    reply = browser.post("/api/settings/save", {"values": {
        "ollama": {"precise_model": "qwen3:30b-a3b", "precise_model_think": None, "think_tokens": 4096},
        "tags": {"merge": {"friendship": "friends", "pals": "friends"}, "ignore": ["daily", " "]},
        "history": {"enabled": True},
    }})
    assert sorted(reply["saved"]) == [
        "history.enabled", "ollama.precise_model", "ollama.precise_model_think", "ollama.think_tokens",
        "tags.ignore", "tags.merge",
    ]
    assert reply["costs"] == ["index", "now"]
    text = on_disk.read_text(encoding="utf-8")
    # the comments and what wasn't touched are as they were
    assert text.startswith("# my own notes about this file\n")
    assert "# where I write" in text and "# the big one" in text and 'ignore_patterns = [".obsidian"]' in text
    assert 'precise_model = "qwen3:30b-a3b"' in text and "precise_model_think" not in text
    saved = load_config(on_disk)
    assert saved.ollama.think_tokens == 4096 and saved.ollama.precise_model_think is None
    assert saved.tags.merge == {"friendship": "friends", "pals": "friends"} and saved.tags.ignore == ["daily"]
    assert saved.history.enabled is True
    # the file as it was is next to it
    assert 'precise_model = "qwen3:14b"' in on_disk.with_name("config.toml.bak").read_text(encoding="utf-8")
    # and the running server carries on with the new settings, no restart
    assert server.config.ollama.precise_model == "qwen3:30b-a3b"
    assert server.jobs.config is server.config and server.watcher.config is server.config
    assert _field(browser.get("/api/settings"), "ollama", "precise_model")["value"] == "qwen3:30b-a3b"
    assert browser.get("/api/history")["enabled"] is True


def test_a_value_that_is_not_valid_saves_nothing_and_says_which(served, on_disk):
    browser, server = served
    before = on_disk.read_text(encoding="utf-8")
    bad = browser.post("/api/settings/save", {"values": {
        "ollama": {"think_tokens": 12, "precise_model": "fine"},
        "vault": {"split_heading_regex": "("},
        "tags": {"merge": {"a": "b", "b": "a"}},
    }}, status=400)
    assert "nothing was saved" in bad["error"]
    assert set(bad["fields"]) == {"ollama.think_tokens", "vault.split_heading_regex", "tags.merge"}
    # a rule about two settings together is named by its section
    both = browser.post("/api/settings/save", {"values": {"vault": {"frontmatter_mood_max": 0}}}, status=400)
    assert "larger than frontmatter_mood_min" in both["fields"]["vault"]
    assert "256" in bad["fields"]["ollama.think_tokens"] and "circle" in bad["fields"]["tags.merge"]
    assert on_disk.read_text(encoding="utf-8") == before
    assert not on_disk.with_name("config.toml.bak").exists()
    assert server.config.ollama.precise_model == "qwen3:14b"
    for values in ({"ollama": {"think_tokens": "lots"}}, {"vault": {"date_source": "stars"}},
                   {"nope": {"x": 1}}, {"ollama": {"nope": 1}}, {"vault": {"ignore_patterns": "one"}}):
        browser.post("/api/settings/save", {"values": values}, status=400)
    assert "Nothing was changed" in browser.post("/api/settings/save", {"values": {}}, status=400)["error"]
    assert on_disk.read_text(encoding="utf-8") == before


def test_settings_wait_for_what_is_running_and_need_a_file(served, config, fake_client):
    browser, server = served
    with server.lane.working():
        busy = browser.post("/api/settings/save", {"values": {"history": {"enabled": True}}}, status=409)
    assert "Something is running" in busy["error"]

    bare = browse.make_server(config, port=0, make_client=lambda cfg: fake_client)
    threading.Thread(target=bare.serve_forever, daemon=True).start()
    try:
        page = Browser(bare.port, bare.token)
        shown = page.get("/api/settings")
        assert shown["path"] is None and shown["writable"] is False
        assert "without a config file" in page.post(
            "/api/settings/save", {"values": {"history": {"enabled": True}}}, status=409)["error"]
    finally:
        bare.shutdown()
        bare.server_close()
