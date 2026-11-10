"""What `journal doctor` checks, for the browser's Health page, and the
questions its first-run Setup page asks.

Each check comes back as {name, state, said} with state ok, warn or fail.
One that can be put right by changing a setting carries the change as `fix`,
which the page saves through Settings.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from . import db, obsidian
from .config import Config
from .models import JournalError
from .ollama_client import OllamaClient, same_model

Json = dict[str, Any]

# a folder this big is counted as "more than" rather than walked to the end
LOOK_LIMIT = 20000
_DATED = re.compile(r"\d{4}-\d{2}-\d{2}")


def _one(name: str, state: str, said: str, **more: Any) -> Json:
    return {"name": name, "state": state, "said": said, **more}


def _fix(line: str) -> Json | None:
    """A line `journal doctor` would have you paste into config.toml, as a
    change to one setting."""
    try:
        found = tomlkit.parse(line).unwrap()
    except TOMLKitError:
        return None
    if len(found) != 1:
        return None
    key, value = next(iter(found.items()))
    for section, info in Config.model_fields.items():
        if key in info.annotation.model_fields:  # type: ignore[union-attr]
            return {"section": section, "key": key, "value": value, "line": line}
    return None


def models(config: Config, client: Callable[[], OllamaClient]) -> Json:
    """The models Ollama has, for picking from."""
    try:
        return {"reachable": True, "host": config.ollama.host, "models": sorted(client().list_models()), "error": None}
    except JournalError as exc:
        return {"reachable": False, "host": config.ollama.host, "models": [], "error": str(exc)}


def check(config: Config, client: Callable[[], OllamaClient]) -> list[Json]:
    out: list[Json] = []
    root = config.vault.path
    if root.is_dir():
        out.append(_one("Journal folder", "ok", f"Found at {root}."))
        for configured in config.notes.paths:
            if not (root / configured).exists():
                out.append(_one("Note folders", "warn", f"'{configured}' isn't in the journal folder."))
    else:
        out.append(_one("Journal folder", "fail", f"There is no folder at {root}."))

    found = models(config, client)
    if not found["reachable"]:
        out.append(_one("Ollama", "fail", found["error"]))
    else:
        out.append(_one("Ollama", "ok", f"Answering at {config.ollama.host}."))
        roles = [
            ("Fast model", config.ollama.fast_model), ("Precise model", config.ollama.precise_model),
            ("Embedding model", config.ollama.embed_model),
            *([("Vision model", config.ollama.vision_model)] if config.ollama.vision_model else []),
        ]
        for role, name in roles:
            if any(same_model(m, name) for m in found["models"]):
                out.append(_one(role, "ok", f"{name} is pulled."))
            else:
                out.append(_one(role, "fail", f"{name} isn't pulled.", command=f"ollama pull {name}"))

    try:
        # in memory, so checking doesn't create the cache file
        with closing(sqlite3.connect(":memory:")) as probe:
            db.load_vec(probe)
            probe.execute("SELECT vec_version()")
        out.append(_one("Search by meaning", "ok", "The sqlite-vec extension loads."))
    except (JournalError, sqlite3.Error) as exc:
        out.append(_one("Search by meaning", "fail", f"sqlite-vec: {exc}"))

    if root.is_dir():
        advice = obsidian.check(config)
        if advice is None:
            out.append(_one("Obsidian", "ok", "No .obsidian folder in the journal folder, so no Obsidian settings to compare."))
        for item in advice or []:
            out.append(_one(
                "Obsidian", "ok" if item.ok else "warn", item.message,
                fix=_fix(item.toml) if item.toml and not item.ok else None,
            ))
    return out


def look(text: str) -> Json:
    """What's in a folder someone typed as their journal folder."""
    raw = text.strip().strip('"').strip("'")
    if not raw:
        return {"path": "", "exists": False, "said": "Type the folder your journal is in."}
    folder = Path(raw).expanduser()
    try:
        if not folder.is_dir():
            return {"path": str(folder), "exists": False, "said": f"There is no folder at {folder}."}
        folder = folder.resolve()
        files = dated = 0
        sample: list[str] = []
        more = False
        for path in folder.rglob("*.md"):
            parts = path.relative_to(folder).parts
            if any(part.startswith(".") for part in parts):
                continue
            files += 1
            dated += bool(_DATED.search(path.stem))
            if len(sample) < 4:
                sample.append("/".join(parts))
            if files >= LOOK_LIMIT:
                more = True
                break
    except OSError as exc:
        return {"path": str(folder), "exists": False, "said": f"That folder couldn't be read: {exc}"}
    if not files:
        said = "There are no Markdown files in that folder."
    else:
        count = f"more than {files:,}" if more else f"{files:,}"
        said = f"{count} Markdown {'file' if files == 1 else 'files'}, {dated:,} with a date in the name."
    return {
        "path": str(folder), "exists": True, "files": files, "dated": dated, "sample": sample,
        "obsidian": (folder / ".obsidian").is_dir(), "said": said,
    }


def first_config(path: Path, values: Json) -> Config:
    """Writes a new config.toml from the Setup page's answers. JournalError
    when they don't make a valid config, or a file is there already."""
    if path.exists():
        raise JournalError(f"{path} is there already. Change it on the Settings page.")
    found = look(str(values.get("vault", "")))
    if not found["exists"]:
        raise JournalError(found["said"])
    doc = tomlkit.document()
    doc.add(tomlkit.comment("Commonplace's settings. The Settings page changes them and keeps your comments."))
    doc.add(tomlkit.comment("config.example.toml in the project lists every setting there is."))
    vault = tomlkit.table()
    vault.add(tomlkit.comment("Your journal folder. It's only ever read."))
    # forward slashes, so a Windows path needs no escaping
    vault["path"] = Path(found["path"]).as_posix()
    if found["obsidian"]:
        vault["ignore_patterns"] = [".obsidian", ".trash"]
    doc["vault"] = vault
    ollama = tomlkit.table()
    for key in ("fast_model", "precise_model", "embed_model"):
        chosen = values.get(key)
        if isinstance(chosen, str) and chosen.strip():
            ollama[key] = chosen.strip()
    if len(ollama):
        doc["ollama"] = ollama
    try:
        config = Config.model_validate(doc.unwrap())
    except ValueError as exc:
        raise JournalError(f"Those answers don't make a valid config: {exc}") from exc
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomlkit.dumps(doc), encoding="utf-8", newline="")
    except OSError as exc:
        raise JournalError(f"{path} couldn't be written: {exc}") from exc
    return config


def make_cache(config: Config) -> None:
    """An empty cache, so the pages have something to open before anything is read."""
    with closing(db.connect(config)) as conn:
        db.init_schema(conn)
