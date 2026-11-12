"""`commonplace`: starts the pages without a terminal.

Installed as a windowed program, so on Windows it opens no console. If
Commonplace is already running it just opens the pages in the browser; if
not it starts `journal browse` and opens them. When Ollama isn't running
it starts `ollama serve` too, and stops it on quitting; an Ollama that was
already running is left alone. With nowhere to print, what
would have gone to the terminal goes to a log file next to the cache, and a
start that fails says why in a message box.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from . import browse
from .config import OllamaConfig, default_config_path, load_config
from .models import JournalError

LOG_NAME = "commonplace.log"
SHORTCUT_NAME = "Commonplace.lnk"
OLLAMA_DEFAULT_PORT = 11434
OLLAMA_WAIT_SECONDS = 15
_THIS_COMPUTER = frozenset({"localhost", "127.0.0.1", "::1"})
# keeps a console program from flashing a window when started from a windowed one
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def running(port: int) -> bool:
    """Whether Commonplace is already answering on this port."""
    try:
        with urllib.request.urlopen(f"http://{browse.HOST}:{port}/api/session", timeout=1.5) as reply:
            return "token" in json.loads(reply.read())
    except (OSError, ValueError, urllib.error.URLError):
        return False


def ollama_answering(host: str) -> bool:
    """Whether Ollama answers at `host`, like http://localhost:11434."""
    try:
        with urllib.request.urlopen(f"{_with_scheme(host).rstrip('/')}/api/version", timeout=1.5) as reply:
            return "version" in json.loads(reply.read())
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _with_scheme(host: str) -> str:
    return host if "://" in host else f"http://{host}"


def _ollama_command() -> list[str] | None:
    found = shutil.which("ollama")
    if found is None and sys.platform == "win32":
        # where Ollama's installer puts it, in case the Start Menu's PATH lacks it
        default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
        found = str(default) if default.is_file() else None
    return [found, "serve"] if found else None


def start_ollama(host: str) -> subprocess.Popen[bytes] | None:
    """Starts `ollama serve` when nothing answers at `host` on this computer.

    Returns the process only if this started it, so whatever was already
    running is left alone, now and when Commonplace quits.
    """
    if ollama_answering(host):
        return None
    address = urllib.parse.urlsplit(_with_scheme(host))
    if address.hostname not in _THIS_COMPUTER:
        print(f"Ollama isn't answering at {host}, which is another computer, so it wasn't started.", file=sys.stderr)
        return None
    command = _ollama_command()
    if command is None:
        print("Ollama isn't running, and the ollama program wasn't found to start it.", file=sys.stderr)
        return None
    env = dict(os.environ)
    # listen where config.toml looks, unless Ollama has been told where already
    env.setdefault("OLLAMA_HOST", f"{address.hostname}:{address.port or OLLAMA_DEFAULT_PORT}")
    try:
        process = subprocess.Popen(
            command, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW, start_new_session=sys.platform != "win32",
        )
    except OSError as exc:
        print(f"Ollama couldn't be started: {exc}", file=sys.stderr)
        return None
    deadline = time.monotonic() + OLLAMA_WAIT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"ollama serve stopped as it started (exit code {process.returncode}).", file=sys.stderr)
            return None
        if ollama_answering(host):
            break
        time.sleep(0.25)
    else:
        print(f"Ollama was started but isn't answering at {host} yet.", file=sys.stderr)
    return process


def stop_ollama(process: subprocess.Popen[bytes]) -> None:
    """Stops an Ollama that start_ollama started, with the model runners it started."""
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # Windows has no signal Ollama stops on, and ending only the server
            # would leave a runner holding the model in memory
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, creationflags=_NO_WINDOW, timeout=30,
            )
        else:
            process.terminate()  # it stops its runners on the way out
        process.wait(timeout=30)
    except (OSError, subprocess.SubprocessError):
        process.kill()


def _log_output() -> None:
    """A windowed program has no stdout, and printing to nothing fails."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    folder = Path("~/.journal_analyzer").expanduser()
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        log = (folder / LOG_NAME).open("a", encoding="utf-8", buffering=1)
    except OSError:
        log = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - kept open for the life of the program
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


_ERROR_ICON = 0x10
_INFO_ICON = 0x40


def _tell(message: str, icon: int = _ERROR_ICON) -> None:
    print(message, file=sys.stderr)
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Commonplace", icon)


def shortcut_command(link: Path, target: Path, arguments: str = "") -> str:
    """The PowerShell that makes a Start Menu shortcut to the launcher."""

    def quoted(value: object) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    return (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({quoted(link)}); "
        f"$s.TargetPath = {quoted(target)}; $s.Arguments = {quoted(arguments)}; "
        f"$s.WorkingDirectory = {quoted(Path.home())}; "
        "$s.Description = 'Ask questions about your journal'; $s.Save()"
    )


def _installed_program() -> Path | None:
    """commonplace.exe where pip puts it: beside python.exe in a virtual
    environment, otherwise in Python's Scripts folder or the user's."""
    folders = [Path(sys.executable).parent, Path(sysconfig.get_path("scripts"))]
    with contextlib.suppress(KeyError):
        folders.append(Path(sysconfig.get_path("scripts", f"{os.name}_user")))
    return next((f / "commonplace.exe" for f in folders if (f / "commonplace.exe").is_file()), None)


def install_shortcut() -> Path:
    """Puts Commonplace in the Start Menu. Returns the shortcut's path."""
    if sys.platform != "win32":
        raise JournalError("Shortcuts are only made on Windows. Elsewhere, run 'commonplace' from your launcher of choice.")
    program = _installed_program()
    if program is not None:
        target, arguments = program, ""
    else:
        # not installed as a program, so the windowed Python runs the module
        windowed = Path(sys.executable).parent / "pythonw.exe"
        target = windowed if windowed.is_file() else Path(sys.executable)
        arguments = "-m journal_analyzer.launcher"
    link = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_NAME
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", shortcut_command(link, target, arguments)],
            check=True, capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise JournalError(f"The shortcut couldn't be made: {exc}") from exc
    return link


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="commonplace", description="Open Commonplace, starting it if it isn't running.")
    parser.add_argument("--config", type=Path, help="Path to config.toml.")
    parser.add_argument("--port", type=int, default=browse.DEFAULT_PORT, help="Port on 127.0.0.1.")
    parser.add_argument("--shortcut", action="store_true", help="Add Commonplace to the Start Menu and exit.")
    args = parser.parse_args(argv)
    _log_output()
    try:
        if args.shortcut:
            # a windowed program run from a terminal can't print there
            _tell(f"Commonplace is in the Start Menu: {install_shortcut()}", _INFO_ICON)
            return 0
        if running(args.port):
            webbrowser.open(f"http://{browse.HOST}:{args.port}/")
            return 0
        path = (args.config or default_config_path()).resolve()
        config = load_config(path) if path.is_file() else None
        ollama = start_ollama((config.ollama if config else OllamaConfig()).host)
        try:
            browse.serve(config, port=args.port, open_browser=True, on_ready=print, config_path=path)
        finally:
            if ollama is not None:
                stop_ollama(ollama)
    except JournalError as exc:
        _tell(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
