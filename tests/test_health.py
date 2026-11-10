"""The Health page's checks, and setting Commonplace up from the browser."""

import threading

import pytest
from conftest import copy_fixtures
from test_browse import Browser

from journal_analyzer import browse, db, health
from journal_analyzer.config import Config, load_config
from journal_analyzer.models import OllamaUnavailableError


def _serve(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return Browser(server.port, server.token)


@pytest.fixture
def served(config, conn, fake_client):
    server = browse.make_server(config, port=0, make_client=lambda cfg: fake_client)
    yield _serve(server), server
    server.shutdown()
    server.server_close()


def _by_name(checks):
    return {c["name"]: c for c in checks}


def test_health_checks_what_doctor_checks(served, config, fake_client):
    browser, _ = served
    checks = _by_name(browser.get("/api/health")["checks"])
    assert checks["Journal folder"]["state"] == "ok"
    assert checks["Ollama"]["state"] == "ok"
    assert (checks["Fast model"]["state"], checks["Fast model"]["said"]) == ("ok", "fake-fast is pulled.")
    assert checks["Search by meaning"]["state"] == "ok"
    assert "No .obsidian folder" in checks["Obsidian"]["said"]
    assert browser.get("/api/models")["models"] == ["fake-embed", "fake-fast", "fake-precise"]

    config.ollama.precise_model = "not-pulled:1b"
    config.notes.paths = ["Nowhere"]
    checks = _by_name(browser.get("/api/health")["checks"])
    assert checks["Precise model"] == {
        "name": "Precise model", "state": "fail", "said": "not-pulled:1b isn't pulled.",
        "command": "ollama pull not-pulled:1b",
    }
    assert checks["Note folders"]["state"] == "warn"

    def down():
        raise OllamaUnavailableError("Cannot reach Ollama at http://localhost:11434")

    fake_client.list_models = down
    checks = _by_name(browser.get("/api/health")["checks"])
    assert checks["Ollama"]["state"] == "fail" and "Cannot reach" in checks["Ollama"]["said"]
    assert "Fast model" not in checks
    assert browser.get("/api/models")["reachable"] is False


def test_what_obsidian_suggests_can_be_saved_as_a_setting():
    assert health._fix('daily_note_glob = "Daily/*.md"') == {
        "section": "vault", "key": "daily_note_glob", "value": "Daily/*.md",
        "line": 'daily_note_glob = "Daily/*.md"',
    }
    assert health._fix('ignore_patterns = [".obsidian", "Templates"]')["value"] == [".obsidian", "Templates"]
    assert health._fix("not a setting = 1") is None and health._fix("nothing_known = 1") is None


def test_looking_at_a_folder_says_what_is_in_it(tmp_path):
    vault = tmp_path / "Journal"
    vault.mkdir()
    assert health.look(str(vault))["said"] == "There are no Markdown files in that folder."
    copy_fixtures(vault)
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "skipped.md").write_text("x", encoding="utf-8")
    (vault / "Ideas.md").write_text("x", encoding="utf-8")
    found = health.look(f'"{vault}"')  # pasted with its quotes, as Explorer's Copy as path gives it
    assert found["exists"] and (found["files"], found["dated"], found["obsidian"]) == (3, 2, True)
    assert found["said"] == "3 Markdown files, 2 with a date in the name."
    assert health.look(str(tmp_path / "nope"))["exists"] is False
    assert "Type the folder" in health.look("  ")["said"]


def test_with_no_config_the_pages_only_do_setup(tmp_path, fake_client, _a_home_folder_of_its_own):
    # Setup leaves the cache where it goes by default, under the home folder,
    # which for a test is one of its own. Checked before anything is written.
    home = _a_home_folder_of_its_own
    assert Config.model_validate({"vault": {"path": "."}}).db.path == (home / ".journal_analyzer" / "journal.db").resolve()
    vault = tmp_path / "Journal"
    vault.mkdir()
    copy_fixtures(vault)
    path = tmp_path / "app" / "config.toml"
    started = {}

    def run():
        browse.serve(None, port=0, open_browser=False, on_ready=lambda url: started.setdefault("url", url), config_path=path)

    # serve() makes its own server, so reach it through make_server to get hold of it
    made = []
    real = browse.make_server

    def keep(*args, **kwargs):
        kwargs["make_client"] = lambda cfg: fake_client
        made.append(real(*args, **kwargs))
        return made[-1]

    browse.make_server = keep
    try:
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        while not made or "url" not in started:
            thread.join(0.01)
        server = made[0]
        browser = Browser(server.port, server.token)
        assert browser.get("/api/session") == {"token": server.token, "setup": True}
        # nothing else answers until it's set up
        assert browser.get("/api/overview", status=409)["setup"] is True
        assert "set up" in browser.post("/api/upkeep/start", {"kind": "index"}, status=409)["error"]
        assert browser.get("/api/models")["models"]
        assert browser.post("/api/setup/look", {"path": str(vault)})["files"] == 2

        assert "no folder" in browser.post("/api/setup/save", {"vault": str(tmp_path / "nope")}, status=400)["error"]
        assert not path.exists()
        done = browser.post("/api/setup/save", {
            "vault": str(vault), "fast_model": "fake-fast", "precise_model": "fake-precise", "embed_model": " fake-embed ",
        })
        assert done == {"path": str(path)}
        written = load_config(path)
        assert written.vault.path == vault.resolve() and written.ollama.embed_model == "fake-embed"
        assert written.db.path == (home / ".journal_analyzer" / "journal.db").resolve()
        assert path.read_text(encoding="utf-8").startswith("# Commonplace's settings.")
        # the pages work now, on an empty cache, with a file for Settings to save to
        assert browser.get("/api/session")["setup"] is False
        assert browser.get("/api/overview")["counts"]["entries"] == 0
        assert browser.get("/api/settings")["writable"] is True
        assert browser.get("/api/upkeep")["files"]["new"] == 2
        assert "set up already" in browser.post("/api/setup/save", {"vault": str(vault)}, status=409)["error"]
    finally:
        browse.make_server = real
        if made:
            made[0].shutdown()


def test_browse_makes_an_empty_cache_when_there_is_none(config):
    assert not config.db.path.exists()
    health.make_cache(config)
    db.connect_readonly(config).close()
