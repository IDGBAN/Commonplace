"""`commonplace`: starts the pages without a terminal.

Installed as a windowed program, so on Windows it opens no console. If
Commonplace is already running it just opens the pages in the browser; if
not it starts `journal browse` and opens them. With nowhere to print, what
would have gone to the terminal goes to a log file next to the cache, and a
start that fails says why in a message box.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import sysconfig
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import browse
from .config import default_config_path, load_config
from .models import JournalError

LOG_NAME = "commonplace.log"
SHORTCUT_NAME = "Commonplace.lnk"


def running(port: int) -> bool:
    """Whether Commonplace is already answering on this port."""
    try:
        with urllib.request.urlopen(f"http://{browse.HOST}:{port}/api/session", timeout=1.5) as reply:
            return "token" in json.loads(reply.read())
    except (OSError, ValueError, urllib.error.URLError):
        return False


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
        browse.serve(config, port=args.port, open_browser=True, on_ready=print, config_path=path)
    except JournalError as exc:
        _tell(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
