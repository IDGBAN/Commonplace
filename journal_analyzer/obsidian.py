"""What Obsidian's own settings say about the vault, for `journal doctor`.

Obsidian keeps them as JSON under .obsidian/: where daily notes go and how
they're named, the templates folder, and the files you excluded. When
config.toml disagrees, the check says what to set. It only reads.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from . import ingester, parser
from .config import Config

# moment.js tokens, longest first so MMMM isn't read as MM twice
_TOKEN_RE = re.compile(
    r"\[[^\]]*\]|YYYY|YY|MMMM|MMM|MM|M|Do|DDDD|DDD|DD|D|dddd|ddd|dd|d|.", re.DOTALL
)
# letters moment reads as a token; any other character is written as it is
_MOMENT_LETTERS = frozenset("YMDdwWgGQHhkmsSaAXxZEeo")
_GLOBS = {
    "YYYY": "[0-9][0-9][0-9][0-9]", "YY": "[0-9][0-9]", "MM": "[0-9][0-9]", "DD": "[0-9][0-9]",
}
_STRPTIME = {
    "YYYY": "%Y", "YY": "%y", "MMMM": "%B", "MMM": "%b", "MM": "%m", "M": "%m",
    "DD": "%d", "D": "%d", "dddd": "%A", "ddd": "%a",
}
_DEFAULT_FORMAT = "YYYY-MM-DD"


@dataclass(frozen=True)
class Advice:
    ok: bool
    message: str
    toml: str | None = None  # a line to paste into config.toml


def _tokens(fmt: str) -> list[str]:
    return _TOKEN_RE.findall(fmt)


def _literal(token: str) -> str | None:
    """The text a token stands for as it is, or None for a date token."""
    if len(token) >= 2 and token.startswith("[") and token.endswith("]"):
        return token[1:-1]
    if len(token) == 1 and token not in _MOMENT_LETTERS:
        return token
    return None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def supported(fmt: str) -> bool:
    """Whether a format uses only the date tokens these checks understand."""
    return all(_literal(t) is not None or t in _STRPTIME or t == "Do" for t in _tokens(fmt))


def format_date(fmt: str, day: date) -> str:
    """A date written the way a moment.js format writes it."""
    out = []
    for token in _tokens(fmt):
        literal = _literal(token)
        if literal is not None:
            out.append(literal)
        elif token == "Do":
            out.append(_ordinal(day.day))
        elif token == "M":
            out.append(str(day.month))
        elif token == "D":
            out.append(str(day.day))
        else:
            out.append(day.strftime(_STRPTIME[token]))
    return "".join(out)


def _glob_literal(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def to_glob(fmt: str) -> str:
    """A glob picking every file name the format writes, as daily_note_glob takes it."""
    out = []
    for token in _tokens(fmt):
        literal = _literal(token)
        out.append(_glob_literal(literal) if literal is not None else _GLOBS.get(token, "*"))
    return "".join(out)


def to_strptime(fmt: str) -> str | None:
    """The date_format reading the format's file names, or None if it can't be read back."""
    out = []
    for token in _tokens(fmt):
        literal = _literal(token)
        if literal is not None:
            out.append(literal.replace("%", "%%"))
        elif token in _STRPTIME:
            out.append(_STRPTIME[token])
        else:
            return None
    return "".join(out)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _toml_list(items: list[str]) -> str:
    return "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in items) + "]"


def _daily_notes(config: Config, settings: Path, today: date) -> list[Advice]:
    data = _read_json(settings / "daily-notes.json")
    if not isinstance(data, dict):
        return []
    fmt = str(data.get("format") or _DEFAULT_FORMAT)
    folder = str(data.get("folder") or "").strip().strip("/")
    if not supported(fmt):
        return [Advice(True, f"Obsidian's daily note format '{fmt}' uses parts these checks can't read, so it's skipped.")]
    written = format_date(fmt, today)
    rel = PurePosixPath(folder, f"{written}.md") if folder else PurePosixPath(f"{written}.md")
    sample = config.vault.path.joinpath(*rel.parts)
    advice: list[Advice] = []
    if parser.matches_entry_glob(sample, config) and not ingester.is_ignored(sample, config):
        advice.append(Advice(True, f"daily_note_glob picks Obsidian's daily notes, like {rel}"))
    else:
        glob = (f"{_glob_literal(folder)}/" if folder else "") + to_glob(fmt) + ".md"
        advice.append(Advice(
            False,
            f"Obsidian writes daily notes like {rel}, but daily_note_glob "
            f"('{config.vault.daily_note_glob}') doesn't pick that path"
            + (" or ignore_patterns leaves it out" if ingester.is_ignored(sample, config) else ""),
            f"daily_note_glob = '{glob}'",
        ))
    if config.vault.date_source == "filename":
        stem = PurePosixPath(written).name
        if parser.date_from_text(stem, config.vault.date_format) == today:
            advice.append(Advice(True, f"date_format reads the date in names like '{stem}'"))
        else:
            wanted = to_strptime(PurePosixPath(fmt).name)
            advice.append(Advice(
                False,
                f"date_format ('{config.vault.date_format}') can't read the date in a daily note named '{stem}'",
                f"date_format = '{wanted}'" if wanted else None,
            ))
    return advice


def _templates_folders(settings: Path) -> list[str]:
    folders = []
    core = _read_json(settings / "templates.json")
    if isinstance(core, dict) and core.get("folder"):
        folders.append(str(core["folder"]))
    templater = _read_json(settings / "plugins" / "templater-obsidian" / "data.json")
    if isinstance(templater, dict) and templater.get("templates_folder"):
        folders.append(str(templater["templates_folder"]))
    return list(dict.fromkeys(f.strip().strip("/") for f in folders if f.strip().strip("/")))


def _indexed_under(config: Config, files: list[Path], folder: str) -> list[Path]:
    prefix = PurePosixPath(folder).parts
    return [
        p for p in files
        if PurePosixPath(parser.relative_path(p, config.vault.path)).parts[: len(prefix)] == prefix
    ]


def _templates(config: Config, settings: Path, files: list[Path]) -> list[Advice]:
    advice = []
    for folder in _templates_folders(settings):
        caught = _indexed_under(config, files, folder)
        if caught:
            advice.append(Advice(
                False,
                f"{len(caught)} template files in {folder} would be indexed, and template "
                "placeholders like {{date}} don't parse as entries",
                f"ignore_patterns = {_toml_list([*config.vault.ignore_patterns, folder])}",
            ))
        else:
            advice.append(Advice(True, f"the templates folder {folder} isn't indexed"))
    return advice


def _excluded(config: Config, settings: Path, files: list[Path]) -> list[Advice]:
    data = _read_json(settings / "app.json")
    filters = data.get("userIgnoreFilters") if isinstance(data, dict) else None
    if not isinstance(filters, list):
        return []
    # a /.../ filter is a regex in Obsidian; the rest are paths from the vault root
    paths = [str(f).strip().strip("/") for f in filters if not (str(f).startswith("/") and str(f).endswith("/"))]
    caught = [f for f in paths if f and _indexed_under(config, files, f)]
    if not caught:
        return [Advice(True, "files excluded in Obsidian aren't indexed")] if paths else []
    return [Advice(
        False,
        f"Obsidian excludes {', '.join(caught)}, but files there are indexed",
        f"ignore_patterns = {_toml_list([*config.vault.ignore_patterns, *caught])}",
    )]


def _hidden(config: Config, files: list[Path]) -> list[Advice]:
    folders: dict[str, int] = {}
    for path in files:
        for part in PurePosixPath(parser.relative_path(path, config.vault.path)).parts[:-1]:
            if part.startswith("."):
                folders[part] = folders.get(part, 0) + 1
                break
    if not folders:
        return []
    shown = ", ".join(f"{n} in {name}" for name, n in sorted(folders.items()))
    return [Advice(
        False,
        f"Files in hidden folders are indexed ({shown}). Obsidian leaves hidden folders "
        "out of the vault, and .trash holds notes you deleted",
        f"ignore_patterns = {_toml_list([*config.vault.ignore_patterns, *sorted(folders)])}",
    )]


def check(config: Config, today: date | None = None) -> list[Advice] | None:
    """What Obsidian's settings say, or None when the vault has no .obsidian folder."""
    settings = config.vault.path / ".obsidian"
    if not settings.is_dir():
        return None
    files = ingester.vault_files(config)
    return [
        *_daily_notes(config, settings, today or date.today()),
        *_templates(config, settings, files),
        *_excluded(config, settings, files),
        *_hidden(config, files),
    ]
