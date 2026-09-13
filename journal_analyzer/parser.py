from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path, PurePath, PurePosixPath

from .config import Config
from .models import DateParseError, ParsedEntry, ParsedNote

# The closing --- has to be on its own line, so "----" inside the block doesn't end it.
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$\n?", re.DOTALL | re.MULTILINE
)
# [[target]], [[target|shown]] and ![[embeds]]; a target may carry #heading or ^block.
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\[\]]+?)\]\]")
# Any one-char checkbox state, including custom ones like [/] and [?]. The space
# after it is optional so an empty task still loses its marker.
_TASK_RE = re.compile(r"^(\s*[-*+]) \[.\][ \t]?", re.MULTILINE)
# Same rules as Obsidian: any script, digits, _ - /, but not digits alone ("#2024").
_TAG_RE = re.compile(r"(?<!\S)#(?!\d+(?![\w/-]))(\w[\w/-]*)")
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# links to these are attachments, not notes
_ATTACHMENT_EXTENSIONS = frozenset(
    "png jpg jpeg gif webp svg bmp avif heic tif tiff "
    "mp4 mov webm mkv avi m4v mp3 wav m4a ogg flac opus pdf canvas".split()
)
# Obsidian's own keys, not facts about the note
_NON_PROPERTY_KEYS = frozenset(
    {"aliases", "alias", "tags", "tag", "cssclasses", "cssclass", "publish", "position"}
)

FrontmatterValue = str | list[str]


def compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path) -> bytes:
    # the only way vault files get opened
    with open(path, "rb") as f:
        return f.read()


def relative_path(path: Path | str, vault: Path) -> str:
    pure = PurePath(path)
    try:
        return pure.relative_to(vault).as_posix()
    except ValueError:
        return pure.name  # outside the vault


def _unquote(value: str) -> str:
    return value.strip().strip("'\"").strip()


def _as_list(value: FrontmatterValue | None) -> list[str]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _parse_frontmatter(text: str) -> tuple[dict[str, FrontmatterValue], str]:
    # Not real YAML, just what Obsidian writes: key: value, [a, b] and
    # "- item" lists. Anything nested deeper is skipped.
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fields: dict[str, FrontmatterValue] = {}
    list_key: str | None = None
    for line in m.group(1).splitlines():
        item = line.strip()
        if not item:
            continue
        if list_key is not None and item.startswith("-"):
            value = _unquote(item[1:])
            target = fields[list_key]
            if value and isinstance(target, list):
                target.append(value)
            continue
        list_key = None
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        key, _, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if raw.startswith("[") and raw.endswith("]") and not raw.startswith("[["):
            fields[key] = [v for v in (_unquote(x) for x in raw[1:-1].split(",")) if v]
        elif raw:
            fields[key] = _unquote(raw)
        else:
            fields[key] = []
            list_key = key
    return fields, text[m.end():]


def _try_parse_date(text: str, date_format: str) -> date | None:
    text = text.strip()
    try:
        return datetime.strptime(text, date_format).date()
    except ValueError:
        pass
    # fall back to an ISO date anywhere in it, e.g. "Tuesday 2024-02-01"
    m = _ISO_DATE_RE.search(text)
    if m:
        try:
            return date.fromisoformat(m.group(0))
        except ValueError:
            return None
    return None


def link_key(target: str) -> str | None:
    # Obsidian resolves [[Folder/Name]], [[Name.md]] and [[name]] the same way,
    # by file name ignoring case. None for attachments and [[#heading]] links.
    name = target.split("|", 1)[0].split("#", 1)[0].split("^", 1)[0]
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem, dot, ext = name.rpartition(".")
    if dot and ext.casefold() == "md":
        name = stem.strip()
    elif dot and ext.casefold() in _ATTACHMENT_EXTENSIONS:
        return None
    return name.casefold() or None


def _shown_text(target: str) -> str:
    if "|" in target:
        return target.split("|", 1)[1].strip()
    name, _, anchor = target.partition("#")
    return name.strip() or anchor.lstrip("^").strip()


def _names_attachment(target: str) -> bool:
    name = target.split("|", 1)[0].split("#", 1)[0].strip()
    return "." in name and name.rpartition(".")[2].casefold() in _ATTACHMENT_EXTENSIONS


def clean_with_links(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Cleaned text plus (offset, link key) pairs.

    Offsets are into the cleaned text, so split sections can pick out their own links.
    """
    text = _TASK_RE.sub(r"\1 ", text)
    pieces: list[str] = []
    links: list[tuple[int, str]] = []
    length = 0
    last = 0
    for m in _WIKILINK_RE.finditer(text):
        before = text[last:m.start()]
        pieces.append(before)
        length += len(before)
        target = m.group(2)
        key = link_key(target)
        if key is not None:
            links.append((length, key))
        # in ![[img.png|300]] the part after | is a size, not text
        drop = (
            key is None
            and _names_attachment(target)
            and (bool(m.group(1)) or "|" not in target)
        )
        shown = "" if drop else _shown_text(target)
        pieces.append(shown)
        length += len(shown)
        last = m.end()
    pieces.append(text[last:])
    joined = "".join(pieces)
    lead = len(joined) - len(joined.lstrip())
    return joined.strip(), [(max(pos - lead, 0), key) for pos, key in links]


def clean_text(text: str) -> str:
    return clean_with_links(text)[0]


def extract_literal_tags(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in _TAG_RE.finditer(text):
        seen.setdefault(m.group(1).lower(), None)
    return list(seen)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _frontmatter_links(fields: dict[str, FrontmatterValue]) -> list[str]:
    # e.g. Relationship: "[[Family]]"
    keys: list[str] = []
    for value in fields.values():
        for item in _as_list(value):
            for m in _WIKILINK_RE.finditer(item):
                key = link_key(m.group(2))
                if key is not None:
                    keys.append(key)
    return keys


def _file_date(
    path: Path, frontmatter: dict[str, FrontmatterValue], body: str, config: Config
) -> date | None:
    vault = config.vault
    if vault.date_source == "filename":
        return _try_parse_date(path.stem, vault.date_format)
    if vault.date_source == "frontmatter":
        values = _as_list(frontmatter.get(vault.frontmatter_date_key))
        return _try_parse_date(values[0], vault.date_format) if values else None
    for line in body.splitlines():
        if line.lstrip().startswith("#") and not line.lstrip().startswith("#["):
            heading_text = line.lstrip().lstrip("#").strip()
            d = _try_parse_date(heading_text, vault.date_format)
            if d:
                return d
    return None


def parse_file(path: Path | str, config: Config) -> list[ParsedEntry]:
    path = Path(path)
    text = read_bytes(path).decode("utf-8", errors="replace")
    frontmatter, body = _parse_frontmatter(text)
    file_date = _file_date(path, frontmatter, body, config)
    cleaned, positions = clean_with_links(body)
    source = str(path)
    rel_path = relative_path(path, config.vault.path)
    file_links = _frontmatter_links(frontmatter)

    def whole_file() -> list[ParsedEntry]:
        if file_date is None:
            raise DateParseError(
                f"Could not determine entry date for {path} "
                f"(date_source={config.vault.date_source})"
            )
        links = _unique(file_links + [key for _, key in positions])
        return [ParsedEntry(file_date, cleaned, source, links, rel_path)]

    split_re = config.vault.split_heading_regex
    if not split_re:
        return whole_file()

    # each section gets its date from its heading, or the file's date if that fails
    pattern = re.compile(split_re, re.MULTILINE)
    matches = list(pattern.finditer(cleaned))
    if not matches:
        return whole_file()

    # anything above the first heading goes with the first section
    preamble = cleaned[: matches[0].start()].strip()

    entries: list[ParsedEntry] = []
    for i, m in enumerate(matches):
        start = 0 if i == 0 else m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
        section = cleaned[m.start():end].strip()
        heading_line = section.splitlines()[0] if section else ""
        if i == 0 and preamble:
            section = f"{preamble}\n\n{section}" if section else preamble
        section_date = _try_parse_date(
            heading_line.lstrip("#").strip(), config.vault.date_format
        ) or file_date
        if section_date is None:
            raise DateParseError(
                f"Section {i + 1} of {path} has no parsable date and the file "
                "itself has none to fall back on."
            )
        if section:
            section_links = [key for pos, key in positions if start <= pos < end]
            entries.append(
                ParsedEntry(
                    section_date,
                    section,
                    source,
                    _unique(file_links + section_links),
                    rel_path,
                )
            )
    if not entries:
        raise DateParseError(f"No non-empty sections found in {path}")
    return entries


def note_kind(path: Path | str, config: Config) -> str | None:
    """None for journal entries.

    Otherwise the folder right under the notes path, lowercased with punctuation
    trimmed, so Links/!Files/x.md is "files". Files with no such folder are "note".
    """
    try:
        rel = PurePath(path).relative_to(config.vault.path)
    except ValueError:
        return None
    parts = [p.casefold() for p in rel.parts]
    for configured in config.notes.paths:
        prefix = [p.casefold() for p in PurePosixPath(configured).parts]
        if parts[: len(prefix)] != prefix:
            continue
        inner = rel.parts[len(prefix):]
        if len(inner) <= 1:
            return "note"
        return re.sub(r"^\W+|\W+$", "", inner[0]).casefold() or "note"
    return None


def parse_note(path: Path | str, kind: str, config: Config) -> ParsedNote:
    path = Path(path)
    text = read_bytes(path).decode("utf-8", errors="replace")
    frontmatter, body = _parse_frontmatter(text)
    cleaned, positions = clean_with_links(body)
    title = path.stem

    aliases: dict[str, str] = {}
    for alias in _as_list(frontmatter.get("aliases")) + _as_list(frontmatter.get("alias")):
        if alias.casefold() != title.casefold():
            aliases.setdefault(alias.casefold(), alias)

    properties: dict[str, str] = {}
    for key, value in frontmatter.items():
        if key.casefold() in _NON_PROPERTY_KEYS:
            continue
        shown = ", ".join(v for v in (clean_text(item) for item in _as_list(value)) if v)
        if shown:
            properties[key] = shown

    own_key = title.casefold()
    links = [
        key
        for key in _unique(_frontmatter_links(frontmatter) + [key for _, key in positions])
        if key != own_key
    ]
    return ParsedNote(
        title=title,
        kind=kind,
        raw_text=cleaned,
        source_path=str(path),
        aliases=list(aliases.values()),
        properties=properties,
        links=links,
        rel_path=relative_path(path, config.vault.path),
    )
