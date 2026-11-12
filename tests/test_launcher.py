"""Starting Commonplace without a terminal, and stopping it from the page."""

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from test_browse import Browser

from journal_analyzer import browse, launcher
from journal_analyzer.config import HOME_CONFIG, default_config_path, load_config
from journal_analyzer.models import ConfigError, JournalError

MINIMAL = "[vault]\npath = '{vault}'\n"

# Stands in for `ollama serve`: answers /api/version where OLLAMA_HOST says,
# with a child on another port like the runner Ollama starts for a model.
FAKE_OLLAMA = """
import http.server, os, subprocess, sys
class Version(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"version": "0.0.0"}')
    def log_message(self, *args):
        pass
if sys.argv[1:] == ["runner"]:
    port = int(os.environ["FAKE_RUNNER_PORT"])
else:
    subprocess.Popen([sys.executable, "-c", os.environ["FAKE_OLLAMA_SOURCE"], "runner"])
    port = int(os.environ["OLLAMA_HOST"].rsplit(":", 1)[1])
http.server.HTTPServer(("127.0.0.1", port), Version).serve_forever()
"""


@pytest.fixture(autouse=True)
def _ollama_is_already_running(monkeypatch):
    """No test reaches for the real Ollama, or starts one, unless it asks to."""
    monkeypatch.setattr(launcher, "ollama_answering", lambda host: True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


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


def test_the_launcher_stops_only_an_ollama_it_started(tmp_path, monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(launcher, "running", lambda port: False)
    monkeypatch.setattr(launcher.browse, "serve", lambda cfg, **kwargs: calls.append("serve"))
    monkeypatch.setattr(launcher, "stop_ollama", lambda process: calls.append(("stop", process)))
    path = tmp_path / "config.toml"
    path.write_text(
        MINIMAL.format(vault=tmp_path.as_posix()) + "[ollama]\nhost = 'http://127.0.0.1:12345'\n", encoding="utf-8"
    )

    started = object()
    monkeypatch.setattr(launcher, "start_ollama", lambda host: calls.append(host) or started)
    assert launcher.main(["--config", str(path)]) == 0
    assert calls == ["http://127.0.0.1:12345", "serve", ("stop", started)]

    # stopped even when the pages end in an error
    def fails(cfg, **kwargs):
        raise JournalError("no")
    monkeypatch.setattr(launcher.browse, "serve", fails)
    monkeypatch.setattr(launcher, "_tell", lambda *args: None)
    calls.clear()
    assert launcher.main(["--config", str(path)]) == 1
    assert calls == ["http://127.0.0.1:12345", ("stop", started)]

    # one that was already running isn't stopped
    monkeypatch.setattr(launcher, "start_ollama", lambda host: None)
    calls.clear()
    launcher.main(["--config", str(path)])
    assert calls == []


def test_ollama_is_started_only_when_nothing_answers_here(monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("nothing should be started")
    monkeypatch.setattr(launcher.subprocess, "Popen", never)
    assert launcher.start_ollama("http://localhost:11434") is None  # answering

    monkeypatch.setattr(launcher, "ollama_answering", lambda host: False)
    assert launcher.start_ollama("http://192.168.1.20:11434") is None  # another computer
    monkeypatch.setattr(launcher, "_ollama_command", lambda: None)
    assert launcher.start_ollama("http://localhost:11434") is None  # not installed


def test_a_started_ollama_and_its_runner_are_stopped(monkeypatch):
    port, runner_port = _free_port(), _free_port()
    monkeypatch.undo()  # the real ollama_answering, against the stand-in
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setenv("FAKE_OLLAMA_SOURCE", FAKE_OLLAMA)
    monkeypatch.setenv("FAKE_RUNNER_PORT", str(runner_port))
    monkeypatch.setattr(launcher, "_ollama_command", lambda: [sys.executable, "-c", FAKE_OLLAMA])
    process = launcher.start_ollama(f"http://127.0.0.1:{port}")
    assert process is not None
    try:
        assert launcher.ollama_answering(f"127.0.0.1:{port}")
        deadline = time.monotonic() + 10
        while not _listening(runner_port) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _listening(runner_port)
    finally:
        launcher.stop_ollama(process)
    assert process.poll() is not None
    assert not _listening(port) and not _listening(runner_port)


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
    # a virtual environment's layout, kept from finding a real install's Scripts folders
    monkeypatch.setattr(launcher.sysconfig, "get_path", lambda name, scheme=None: str(tmp_path / "Scripts"))
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


def test_the_shortcut_finds_the_program_in_a_scripts_folder(tmp_path, monkeypatch):
    # a plain or per-user install keeps commonplace.exe in a Scripts folder, not beside python.exe
    folders = {None: tmp_path / "Python" / "Scripts", "nt_user": tmp_path / "User" / "Scripts"}
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.sys, "executable", str(tmp_path / "Python" / "python.exe"))
    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setattr(launcher.sysconfig, "get_path", lambda name, scheme=None: str(folders[scheme]))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    ran: list[list[str]] = []
    monkeypatch.setattr(launcher.subprocess, "run", lambda args, **kwargs: ran.append(args))
    folders["nt_user"].mkdir(parents=True)
    (folders["nt_user"] / "commonplace.exe").write_bytes(b"")
    launcher.install_shortcut()
    assert str(folders["nt_user"] / "commonplace.exe") in ran[0][-1]
    assert "-m journal_analyzer.launcher" not in ran[0][-1]


def test_making_the_shortcut_says_so_in_a_window(tmp_path, monkeypatch):
    # run from a terminal, a windowed program's printing goes only to the log
    told: list[tuple[str, int]] = []
    monkeypatch.setattr(launcher, "_tell", lambda message, icon=0: told.append((message, icon)))
    monkeypatch.setattr(launcher, "install_shortcut", lambda: tmp_path / "Commonplace.lnk")
    assert launcher.main(["--shortcut"]) == 0
    assert told == [(f"Commonplace is in the Start Menu: {tmp_path / 'Commonplace.lnk'}", launcher._INFO_ICON)]


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
