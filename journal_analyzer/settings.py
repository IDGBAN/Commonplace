"""The browser's Settings page: every setting in config.toml as a form.

The form is built from the Config model, so a new setting shows up without
anything being added here except its line in HELP, which a test insists on.
Saving goes through tomlkit, which keeps the file's comments and layout and
only touches the keys that changed. The file as it was is kept next to it.
"""

from __future__ import annotations

import os
import types
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

import tomlkit
from pydantic import ValidationError
from tomlkit.exceptions import TOMLKitError

from .config import Config

Json = dict[str, Any]

# What a change costs, said next to each setting and again after saving.
EFFECTS = {
    "now": "Takes effect straight away.",
    "index": "Takes effect the next time your files are read. No model is called for it.",
    "reread": "Only applies to files read after the change. Read every file again, on Upkeep, to apply it to the rest.",
    "rebuild": "Needs the cache rebuilt: what's stored was made with the old value and can't be mixed with the new.",
    "restart": "Points Commonplace at a different file. Pages show what's there from now on.",
}

SECTIONS = {
    "vault": "Your journal",
    "notes": "Reference notes",
    "ollama": "Models",
    "indexing": "Reading entries",
    "tags": "Tags",
    "metrics": "Numbers",
    "rollups": "Digests",
    "profile": "Your profile",
    "themes": "Themes",
    "insights": "Insights",
    "reranking": "Search",
    "history": "Question history",
    "mcp": "MCP clients",
    "db": "The cache",
}

# section.key -> (what a change costs, what the setting does)
HELP: dict[str, tuple[str, str]] = {
    "vault.path": ("reread", "Your journal folder. It's only ever read."),
    "vault.daily_note_glob": ("index", "Which files are journal entries, as a pattern from the journal folder."),
    "vault.date_source": ("reread", "Where an entry's date comes from: the file name, a frontmatter key, or its first heading that reads as a date."),
    "vault.date_format": ("reread", "How dates are written in file names and headings, like %Y-%m-%d."),
    "vault.frontmatter_date_key": ("reread", "The frontmatter key that holds the date, when dates come from frontmatter."),
    "vault.split_heading_regex": ("reread", "Splits one file into several entries at headings that match. Empty keeps one entry a file."),
    "vault.ignore_patterns": ("index", "Folders and files to skip. A name without a / matches anywhere; * and ? work."),
    "vault.private_tag": ("index", "Entries and notes with this tag are never read by a model or stored. Empty turns it off."),
    "vault.strip_comments": ("index", "Leave out %% comments %%, which Obsidian hides in reading view."),
    "vault.frontmatter_mood_key": ("index", "Use the mood you write in frontmatter under this key. Empty leaves mood to the model."),
    "vault.frontmatter_mood_min": ("index", "The lowest value on the scale you write your mood on."),
    "vault.frontmatter_mood_max": ("index", "The highest value on the scale you write your mood on."),
    "notes.paths": ("index", "Folders, or single files, that hold your undated notes about people, places and topics."),
    "notes.other_files": ("index", "Count every Markdown file that isn't a journal entry as a note."),
    "notes.day_links_count": ("index", "A note that links a day counts as mentioned on that day."),
    "notes.summarize_over_chars": ("reread", "Notes longer than this get a summary from the fast model. 0 summarizes every note."),
    "notes.learned_alias_min": ("index", "Link text used for one note in this many entries becomes a name for it. 0 turns it off."),
    "notes.learned_alias_ignore": ("index", "Link text that should never count as a name."),
    "notes.group_keys": ("now", "The note properties whose values group notes for insights. Empty means all of them."),
    "ollama.host": ("now", "Where Ollama is listening."),
    "ollama.fast_model": ("now", "Reads entries, summarizes notes and answers from summaries. Entries already read keep what the old model said."),
    "ollama.precise_model": ("now", "Writes digests, the profile and answers from full text."),
    "ollama.fast_model_think": ("now", "Whether the fast model thinks first. It makes reading entries a lot slower."),
    "ollama.precise_model_think": ("now", "Whether the precise model thinks before answers and digests."),
    "ollama.show_thinking": ("now", "Show a model's thinking as it comes, before the answer."),
    "ollama.think_tokens": ("now", "How long a model may think, in tokens, before it's stopped and asked again without thinking."),
    "ollama.fast_num_ctx": ("now", "The fast model's context window. 0 means 4096, raised only when a prompt needs more."),
    "ollama.precise_num_ctx": ("now", "The precise model's context window. 0 means 4096, raised only when a prompt needs more."),
    "ollama.embed_model": ("rebuild", "Makes the vectors that search by meaning uses."),
    "ollama.vision_model": ("index", "A model that reads images, to describe the photos entries link. Empty turns it off."),
    "ollama.embed_num_ctx": ("now", "The embedding model's context window, capped at the model's own limit."),
    "ollama.request_timeout_seconds": ("now", "How long to wait for Ollama before giving up on a request."),
    "indexing.watch_debounce_seconds": ("now", "How long a saved file is left alone before it's read."),
    "indexing.mood_scale_max": ("reread", "The top of the mood scale. Moods run from 1 to this."),
    "indexing.linked_note_chars": ("reread", "How much of each linked note goes along when an entry is read. 0 sends none."),
    "indexing.detail_min_words": ("reread", "Entries this long also get a longer summary. 0 turns it off."),
    "indexing.detail_long_words": ("reread", "Entries this long get a longer summary still."),
    "indexing.sections": ("index", "Search entries with several top-level headings part by part."),
    "indexing.section_min_chars": ("index", "Parts shorter than this join the one before."),
    "indexing.photos_per_entry": ("index", "How many photos are described for each entry."),
    "tags.from_frontmatter": ("index", "Count tags in an entry's frontmatter the same as inline #tags."),
    "tags.ignore": ("index", "Tags dropped from every entry, like one a template adds to every note."),
    "tags.merge": ("index", "Tags that count as another tag, one a line, like friendship = friends."),
    "tags.known_in_prompt": ("reread", "How many of your most used tags the model is shown and asked to reuse."),
    "metrics.keys": ("index", "The frontmatter numbers to keep. Empty means every numeric property."),
    "metrics.ignore": ("index", "Frontmatter numbers to leave out."),
    "rollups.note_min_mentions": ("now", "Notes linked from at least this many entries get a relationship digest. 0 turns them off."),
    "rollups.note_entries_per_year": ("now", "How many entries from each year go into a relationship digest."),
    "rollups.linked_notes_chars": ("now", "Room for linked notes in each digest's prompt. 0 sends none."),
    "rollups.entry_details_chars": ("now", "Room for long entries' longer summaries in each digest."),
    "rollups.watch_refresh_minutes": ("now", "How often journal watch writes the digests of what it read. 0 leaves them to you. The browser's Keep up as you write doesn't use it."),
    "profile.enabled": ("now", "Keep a short profile of you that answers and digests read as background."),
    "profile.note": ("now", "A note about yourself, from the journal folder. It wins over what's inferred."),
    "profile.scan_batch_entries": ("now", "How many entries the fast model reads for the profile in one call."),
    "profile.max_words": ("now", "About how long the profile may run."),
    "profile.watch_update_minutes": ("now", "The profile is rewritten at most this often while keeping up as you write."),
    "themes.count": ("now", "How many themes to look for. 0 picks from how much you've written."),
    "themes.min_entries": ("now", "A theme with fewer entries than this is folded into the others."),
    "themes.stale_share": ("now", "The share of entries written since the last refresh before themes say they're out of date."),
    "insights.min_sample_size": ("now", "How many entries a pattern needs before it's reported."),
    "insights.mood_deviation_threshold": ("now", "How far from your usual mood a pattern has to be."),
    "reranking.enabled": ("now", "Reorder search results with a small model before answering. It's downloaded the first time."),
    "reranking.model": ("now", "The reranking model."),
    "history.enabled": ("now", "Keep your questions and their answers so you can read them again."),
    "history.path": ("restart", "Where the history file goes. Empty puts it next to the cache. Never inside the journal folder."),
    "history.keep": ("now", "The newest questions kept. Older ones are deleted."),
    "history.forget_after_days": ("now", "Questions older than this many days are deleted. 0 leaves it to the count above."),
    "mcp.raw_text": ("now", "Let MCP clients read the text of entries and notes, not only summaries."),
    "mcp.exclude_tags": ("now", "Entries with any of these tags are left out of every MCP tool."),
    "mcp.exclude_kinds": ("now", "Notes of these kinds are left out of every MCP tool."),
    "mcp.tools": ("now", "Offer only these MCP tools. Empty means all of them."),
    "db.path": ("restart", "Where the cache file goes."),
    "db.memo": ("now", "Keep what the models returned, so reading everything again reuses it."),
    "db.memo_path": ("restart", "Where the kept results go. Empty puts them next to the cache."),
}

BACKUP_SUFFIX = ".bak"


class SettingsError(Exception):
    """A save that was refused. `fields` says which settings, by section.key."""

    def __init__(self, message: str, fields: dict[str, str] | None = None):
        super().__init__(message)
        self.fields = fields or {}


def _kind(annotation: Any) -> tuple[str, list[str]]:
    """How a setting is edited, and its choices when it has a fixed set."""
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        inner = [a for a in get_args(annotation) if a is not type(None)]
        return ("maybe" if inner == [bool] else _kind(inner[0])[0]), []
    if origin is Literal:
        return "choice", [str(a) for a in get_args(annotation)]
    if origin is list:
        return "list", []
    if origin is dict:
        return "pairs", []
    if annotation is bool:
        return "bool", []
    if annotation is int:
        return "int", []
    if annotation is float:
        return "float", []
    return "text", []


def _shown(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    return value


def describe(config: Config, path: Path | None) -> Json:
    """The whole form: sections, their settings, and what each is set to."""
    sections = []
    for name in SECTIONS:
        model = getattr(config, name)
        fields = []
        for key, info in type(model).model_fields.items():
            effect, about = HELP[f"{name}.{key}"]
            kind, choices = _kind(info.annotation)
            limits = {
                bound: getattr(m, bound)
                for m in info.metadata for bound in ("ge", "le") if getattr(m, bound, None) is not None
            }
            fields.append({
                "key": key, "kind": kind, "choices": choices, "about": about, "effect": effect,
                "value": _shown(getattr(model, key)),
                "default": None if info.is_required() else _shown(info.get_default(call_default_factory=True)),
                "min": limits.get("ge"), "max": limits.get("le"),
            })
        sections.append({"name": name, "title": SECTIONS[name], "fields": fields})
    return {
        "path": str(path) if path else None,
        "writable": bool(path and path.is_file() and os.access(path, os.W_OK)),
        "backup": str(_backup(path)) if path else None,
        "effects": EFFECTS,
        "sections": sections,
    }


def _backup(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def _clean(section: str, key: str, value: Any) -> Any:
    """A value from the page as the type its setting takes. None means unset."""
    model = Config.model_fields[section].annotation
    kind, choices = _kind(model.model_fields[key].annotation)  # type: ignore[union-attr]
    wrong = SettingsError("That value couldn't be read.", {f"{section}.{key}": "Not a value this setting takes."})
    if kind == "maybe":
        if value is None or isinstance(value, bool):
            return value
    elif kind == "bool":
        if isinstance(value, bool):
            return value
    elif kind in ("int", "float"):
        if isinstance(value, int | float) and not isinstance(value, bool):
            return int(value) if kind == "int" and float(value).is_integer() else value
    elif kind == "list":
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return [v.strip() for v in value if v.strip()]
    elif kind == "pairs":
        if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            return {k.strip(): v.strip() for k, v in value.items() if k.strip()}
    elif isinstance(value, str) and (kind != "choice" or value in choices):
        return value
    raise wrong


def save(path: Path, changes: Json) -> tuple[Config, list[str]]:
    """Writes the changed settings to config.toml and returns the config it
    now holds with the settings that changed, as section.key.

    Nothing is written unless the whole file is still a valid config.
    """
    try:
        text = path.read_bytes().decode("utf-8-sig")
        doc = tomlkit.parse(text)
    except (OSError, UnicodeDecodeError, TOMLKitError) as exc:
        raise SettingsError(f"{path} couldn't be read: {exc}") from exc
    changed: list[str] = []
    for section, values in changes.items():
        if section not in SECTIONS or not isinstance(values, dict):
            raise SettingsError(f"There's no [{section}] in config.toml.")
        for key, raw in values.items():
            if f"{section}.{key}" not in HELP:
                raise SettingsError(f"There's no setting called {section}.{key}.")
            value = _clean(section, key, raw)
            if section not in doc:
                doc[section] = tomlkit.table()
            table = doc[section]
            if value is None:
                # TOML can't say "unset", so the key goes and the default applies
                if key in table:
                    del table[key]
            else:
                table[key] = value
            changed.append(f"{section}.{key}")
    try:
        config = Config.model_validate(doc.unwrap())
    except ValidationError as exc:
        fields = {
            ".".join(str(x) for x in err["loc"][:2]) or "config": err["msg"].removeprefix("Value error, ")
            for err in exc.errors()
        }
        raise SettingsError("Some of that isn't valid, so nothing was saved.", fields) from exc
    try:
        _backup(path).write_bytes(path.read_bytes())
        swap = path.with_name(path.name + ".saving")
        swap.write_text(tomlkit.dumps(doc), encoding="utf-8", newline="")
        os.replace(swap, path)
    except OSError as exc:
        raise SettingsError(f"{path} couldn't be written: {exc}") from exc
    return config, changed

