"""Starting Commonplace without a terminal, and stopping it from the page."""

import threading
import time
from pathlib import Path

import pytest
from test_browse import Browser

from journal_analyzer import browse, launcher
from journal_analyzer.config import HOME_CONFIG, default_config_path, load_config
from journal_analyzer.models import ConfigError, JournalError

MINIMAL = "[vault]\npath = '{vault}'\n"


def test_config_is_looked_for_here_then_in_the_home_folder(tmp_path, monkeypatch, _a_home_folder_of_its_own):
    home_config = _a_home_folder_of_its_own / ".journal_analyzer" / "config.toml"
    assert HOME_CONFIG.expanduser() == home_config
    work = tmp_path / "somewhere"
    work.mkdir()
    monkeypatch.chdir(work)
    # neither is there: the home one is where a new one would go, and the error names both
    assert default_config_path() == home_config
    with pytest.raises(ConfigError, match=r"\./config\.toml or .*journal browse"):
        load_config()

    home_config.parent.mkdir(parents=True)
    home_config.write_text(MINIMAL.format(vault=(tmp_path / "home-vault").as_posix()), encoding="utf-8")
    assert load_config().vault.path == (tmp_path / "home-vault").resolve()

    # one in the folder a command is run from still wins
    (work / "config.toml").write_text(MINIMAL.format(vault=(tmp_path / "here-vault").as_posix()), encoding="utf-8")
    assert default_config_path() == Path("config.toml")
    assert load_config().vault.path == (tmp_path / "here-vault").resolve()
    # and a path that's given is the only place looked
    with pytest.raises(ConfigError, match=r"nope\.toml"):
        load_config(tmp_path / "nope.toml")


def test_the_launcher_opens_what_is_running_or_starts_it(tmp_path, config, conn, monkeypatch):
    opened: list[str] = []
    served: list[dict] = []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(launcher.browse, "serve", lambda cfg, **kwargs: served.append({"config": cfg, **kwargs}))
    path = tmp_path / "config.toml"
    path.write_text(
        f"[vault]\npath = '{config.vault.path.as_posix()}'\n[db]\npath = '{config.db.path.as_posix()}'\n", encoding="utf-8"
    )

    # nothing is running on the port: it starts, and serve opens the browser itself
    monkeypatch.setattr(launcher, "running", lambda port: False)
    assert launcher.main(["--config", str(path), "--port", "9001"]) == 0
    assert opened == [] and len(served) == 1
    assert served[0]["config"].db.path == config.db.path and served[0]["port"] == 9001
    assert served[0]["open_browser"] is True and served[0]["config_path"] == path.resolve()

    # with no config file it still starts, for the Setup page to write one there
    assert launcher.main(["--config", str(tmp_path / "new" / "config.toml")]) == 0
    assert served[1]["config"] is None and served[1]["config_path"] == (tmp_path / "new" / "config.toml").resolve()

    # already running: only the browser is opened
    monkeypatch.setattr(launcher, "running", lambda port: True)
    assert launcher.main(["--port", "9001"]) == 0
    assert opened == ["http://127.0.0.1:9001/"] and len(served) == 2


def test_a_start_that_fails_says_why(tmp_path, monkeypatch):
    told: list[str] = []
    monkeypatch.setattr(launcher, "_tell", told.append)
    monkeypatch.setattr(launcher, "running", lambda port: False)
    bad = tmp_path / "config.toml"
    bad.write_text("[vault]\npath = 7\n", encoding="utf-8")
    assert launcher.main(["--config", str(bad)]) == 1
    assert "not a valid config" in told[0]


def test_running_means_commonplace_is_answering(config, conn, fake_client):
    server = browse.make_server(config, port=0, make_client=lambda cfg: fake_client)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert launcher.running(server.port)
    finally:
        server.shutdown()
        server.server_close()
    assert not launcher.running(server.port)


def test_the_shortcut_points_at_the_windowed_program(tmp_path, monkeypatch):
    command = launcher.shortcut_command(Path("C:/Start Menu/Commonplace.lnk"), Path("C:/it's here/commonplace.exe"))
    assert "CreateShortcut('C:\\Start Menu\\Commonplace.lnk')" in command.replace("/", "\\")
    assert "it''s here" in command and command.endswith("$s.Save()")

    ran: list[list[str]] = []
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "Scripts" / "python.exe"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.setattr(launcher.subprocess, "run", lambda args, **kwargs: ran.append(args))
    link = launcher.install_shortcut()
    assert link == tmp_path / "AppData" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Commonplace.lnk"
    # not installed as a program here, so Python runs the module
    assert ran[0][0] == "powershell" and "-m journal_analyzer.launcher" in ran[0][-1]
    (tmp_path / "Scripts").mkdir()
    (tmp_path / "Scripts" / "commonplace.exe").write_bytes(b"")
    launcher.install_shortcut()
    assert "commonplace.exe" in ran[1][-1] and "-m journal_analyzer.launcher" not in ran[1][-1]

    monkeypatch.setattr(launcher.sys, "platform", "linux")
    with pytest.raises(JournalError, match="only made on Windows"):
        launcher.install_shortcut()


def test_quit_stops_the_server_from_the_page(config, conn, fake_client):
    server = browse.make_server(config, port=0, make_client=lambda cfg: fake_client)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = Browser(server.port, server.token)
    try:
        # only the page can ask for it
        response = browser.request("/api/quit")
        response.read()
        assert response.status == 405 and thread.is_alive()
        assert browser.post("/api/quit", status=401, **{browse.TOKEN_HEADER: "nope"})
        assert thread.is_alive()
        assert browser.post("/api/quit") == {"quit": True, "working": False}
        thread.join(5)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
    time.sleep(0.05)
    assert not launcher.running(server.port)
