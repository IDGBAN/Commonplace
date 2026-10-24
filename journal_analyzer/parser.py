from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path, PurePath, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote

from .config import Config
from .models import DateParseError, FrontmatterValue, ParsedEntry, ParsedNote

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
# what a vision model can read; svg and the rest are left alone
_IMAGE_EXTENSIONS = frozenset("png jpg jpeg gif webp bmp avif heic tif tiff".split())
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(<?([^)>\s]+)>?(?:\s+\"[^\"]*\")?\)")
# Obsidian's own keys, not facts about the note
_NON_PROPERTY_KEYS = frozenset(
    {"aliases", "alias", "tags", "tag", "cssclasses", "cssclass", "publish", "position",
     "private"}
)
_TAG_KEYS = frozenset({"tags", "tag"})
# `private: true` in the frontmatter keeps a whole file out of the cache
PRIVATE_KEY = "private"
# Obsidian hides %% comments %% in reading view; an unclosed one runs to the end
_COMMENT_RE = re.compile(r"%%.*?(?:%%|\Z)", re.DOTALL)


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


def obsidian_url(path: Path | str) -> str:
    return "obsidian://open?path=" + quote(str(path), safe="")


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


def read_frontmatter(data: bytes) -> dict[str, FrontmatterValue]:
    return _parse_frontmatter(data.decode("utf-8", errors="replace"))[0]


def frontmatter_value(
    fields: dict[str, FrontmatterValue], key: str
) -> FrontmatterValue | None:
    """Obsidian treats property names case-insensitively."""
    wanted = key.strip().casefold()
    for name, value in fields.items():
        if name.casefold() == wanted:
            return value
    return None


def frontmatter_tags(fields: dict[str, FrontmatterValue]) -> list[str]:
    # tags: [a, b], a "- item" list, or one string that may hold several
    seen: dict[str, None] = {}
    for name, value in fields.items():
        if name.casefold() not in _TAG_KEYS:
            continue
        for item in _as_list(value):
            for word in re.split(r"[,\s]+", item):
                tag = word.strip().lstrip("#").lower()
                if tag:
                    seen.setdefault(tag, None)
    return list(seen)


def strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


def _truthy(value: FrontmatterValue | None) -> bool:
    item = value[0] if isinstance(value, list) and value else value
    return isinstance(item, str) and item.strip().casefold() in {"true", "yes", "1"}


def file_is_private(fields: dict[str, FrontmatterValue], config: Config) -> bool:
    tag = config.vault.private_tag
    return _truthy(frontmatter_value(fields, PRIVATE_KEY)) or bool(
        tag and tag in frontmatter_tags(fields)
    )


def text_is_private(text: str, config: Config) -> bool:
    tag = config.vault.private_tag
    return bool(tag) and tag in extract_literal_tags(text)


def _body(text: str, config: Config) -> tuple[dict[str, FrontmatterValue], str]:
    frontmatter, body = _parse_frontmatter(text)
    return frontmatter, strip_comments(body) if config.vault.strip_comments else body


def date_from_text(text: str, date_format: str) -> date | None:
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
    if not name.strip():
        return anchor.lstrip("^").strip()
    # like Obsidian's reading view: [[Note#Heading]] is "Note > Heading", a ^block shows nothing
    headings = [p.strip() for p in anchor.split("#") if p.strip() and not p.strip().startswith("^")]
    return " > ".join([name.strip(), *headings])


def _names_attachment(target: str) -> bool:
    name = target.split("|", 1)[0].split("#", 1)[0].strip()
    return "." in name and name.rpartition(".")[2].casefold() in _ATTACHMENT_EXTENSIONS


def image_name(target: str) -> str | None:
    """The file an [[image.jpg]] link or ![](image.jpg) points at, or None for anything else."""
    name = target.split("|", 1)[0].split("#", 1)[0].strip()
    ext = name.rpartition(".")[2].casefold() if "." in name else ""
    if ext not in _IMAGE_EXTENSIONS or "://" in name:
        return None
    return unquote(name).replace("\\", "/")


def has_images(text: str) -> bool:
    return bool(_clean(text)[2])


def clean_with_links(text: str) -> tuple[str, list[tuple[int, str]]]:
    """Cleaned text plus (offset, link key) pairs.

    Offsets are into the cleaned text, so split sections can pick out their own links.
    """
    cleaned, links, _ = _clean(text)
    return cleaned, links


def _clean(text: str) -> tuple[str, list[tuple[int, str]], list[tuple[int, str]]]:
    """clean_with_links, plus (offset, file) for each image it links or embeds."""
    text = _TASK_RE.sub(r"\1 ", text)
    pieces: list[str] = []
    links: list[tuple[int, str]] = []
    images: list[tuple[int, str]] = []
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
        elif (image := image_name(target)) is not None:
            images.append((length, image))
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
    # ![alt](photo.jpg) stays in the text as written, so it's found there
    images.extend(
        (m.start() + lead, image)
        for m in _MD_IMAGE_RE.finditer(joined.strip())
        if (image := image_name(m.group(1))) is not None
    )
    images.sort()
    cleaned = joined.strip()
    return (
        cleaned,
        [(max(pos - lead, 0), key) for pos, key in links],
        # an image dropped from the very end sits at the end of the cleaned text
        [(min(max(pos - lead, 0), len(cleaned)), name) for pos, name in images],
    )


def link_labels(text: str) -> dict[str, list[str]]:
    """For each link key, the display texts that differ from the target's name."""
    labels: dict[str, dict[str, None]] = {}
    for m in _WIKILINK_RE.finditer(text):
        target = m.group(2)
        key = link_key(target)
        if key is None:
            continue
        # a heading after the name isn't a different name for the note
        shown = _shown_text(target if "|" in target else target.split("#", 1)[0])
        if shown and shown.casefold() != key:
            labels.setdefault(key, {}).setdefault(shown, None)
    return {key: list(names) for key, names in labels.items()}


def link_anchor(target: str) -> str:
    """The #heading or #^block a link points at inside its note, or "" for all of it."""
    name = target.split("|", 1)[0]
    _, hashed, anchor = name.partition("#")
    return anchor.strip() if hashed else ""


def link_anchors(text: str) -> dict[str, list[str]]:
    """For each link key that points at a heading or block, the anchors it's linked with.

    "" among them means the key is also linked as a whole.
    """
    found: dict[str, dict[str, None]] = {}
    for m in _WIKILINK_RE.finditer(text):
        key = link_key(m.group(2))
        if key is not None:
            found.setdefault(key, {}).setdefault(link_anchor(m.group(2)), None)
    return {key: list(anchors) for key, anchors in found.items() if any(anchors)}


def anchor_label(anchor: str) -> str:
    """How an anchor reads to a person: the innermost heading, or "passage" for a block."""
    if anchor.startswith("^"):
        return "passage"
    parts = [p.strip() for p in anchor.split("#") if p.strip()]
    return parts[-1] if parts else anchor


# up to three spaces of indent, then a closing run of #s is optional, as in CommonMark
_HEADING_LINE_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def _anchor_words(text: str) -> str:
    # Obsidian leaves these characters out of heading links, and formatting doesn't count
    return " ".join(re.sub(r"[#|^:%\[\]*_`~=\\]", " ", text).casefold().split())


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line, level, words) for each heading, skipping fenced code."""
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            if fence is None:
                fence = stripped[:3]
            elif stripped.startswith(fence):
                fence = None
            continue
        m = _HEADING_LINE_RE.match(line.rstrip("\r")) if fence is None else None
        if m:
            found.append((i, len(m.group(1)), _anchor_words(m.group(2))))
    return found


def _heading_span(
    headings: list[tuple[int, int, str]], lo: int, hi: int, words: str
) -> tuple[int, int] | None:
    for n, (line, level, text) in enumerate(headings):
        if not lo <= line < hi or text != words:
            continue
        end = next((h for h, lvl, _ in headings[n + 1:] if lvl <= level and h < hi), hi)
        return line, end
    return None


def _block_span(lines: list[str], block_id: str) -> tuple[int, int] | None:
    marker = re.compile(rf"(?:^|\s)\^{re.escape(block_id)}\s*$", re.IGNORECASE)
    for i, line in enumerate(lines):
        if not marker.search(line):
            continue
        last = i
        if not marker.sub("", line).strip():
            # an id on a line of its own names the block above it
            last = i - 1
            while last >= 0 and not lines[last].strip():
                last -= 1
            if last < 0:
                return None
        # back to the start of the paragraph, or of the list item it continues
        first = last
        while (
            not _LIST_ITEM_RE.match(lines[first])
            and first > 0
            and lines[first - 1].strip()
            and not _HEADING_LINE_RE.match(lines[first - 1])
        ):
            first -= 1
        return first, i + 1
    return None


def section_span(text: str, anchor: str) -> tuple[int, int] | None:
    """Lines [start, end) of `text` that an anchor points at, splitting on "\\n".

    A heading's section starts at the heading and runs to the next heading of
    the same or a higher level. [[Note#A#B]] is heading B inside A, or any B
    when A has been renamed since.
    """
    anchor = anchor.strip()
    lines = text.split("\n")
    if anchor.startswith("^"):
        block_id = anchor[1:].strip()
        return _block_span(lines, block_id) if block_id else None
    parts = [w for w in (_anchor_words(p) for p in anchor.split("#")) if w]
    if not parts:
        return None
    headings = _headings(lines)
    lo, hi = 0, len(lines)
    span: tuple[int, int] | None = None
    for words in parts:
        span = _heading_span(headings, lo, hi, words)
        if span is None:
            break
        lo, hi = span[0] + 1, span[1]
    if span is None and len(parts) > 1:
        span = _heading_span(headings, 0, len(lines), parts[-1])
    if span is None:
        return None
    start, end = span
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def find_section(text: str, anchor: str) -> str | None:
    """The text an anchor points at, without its heading or ^block id."""
    span = section_span(text, anchor)
    if span is None:
        return None
    lines = text.split("\n")[span[0]:span[1]]
    if anchor.strip().startswith("^"):
        block_id = re.escape(anchor.strip()[1:].strip())
        lines = [re.sub(rf"(?:^|\s)\^{block_id}\s*$", "", line, flags=re.IGNORECASE) for line in lines]
    else:
        lines = lines[1:]
    body = "\n".join(line.rstrip("\r") for line in lines).strip()
    return body or None


@dataclass(frozen=True)
class Section:
    """One part of an entry, cut at a top-level heading."""

    heading: str  # as written; "" for text before the first heading
    start: int  # lines [start, end) of the entry's text, split on "\n"
    end: int
    text: str  # with its heading line


def _heading_text(line: str) -> str:
    m = _HEADING_LINE_RE.match(line.rstrip("\r"))
    return m.group(2).strip() if m else ""


def split_sections(text: str, min_chars: int) -> list[Section]:
    """An entry cut at its top-level headings, or [] when that leaves fewer than two parts.

    Top level is the biggest heading the entry uses. A part shorter than
    `min_chars` joins the one before it, or the one after when it comes first.
    """
    lines = text.split("\n")
    headings = _headings(lines)
    if not headings:
        return []
    top = min(level for _, level, _ in headings)
    cuts = [line for line, level, _ in headings if level == top]
    bounds = ([0] if cuts[0] > 0 else []) + cuts + [len(lines)]
    merged: list[list[Any]] = []
    carry: int | None = None
    for start, end in pairwise(bounds):
        heading = _heading_text(lines[start]) if start in cuts else ""
        if carry is not None:
            start, carry = carry, None
        if len("\n".join(lines[start:end]).strip()) < min_chars:
            if merged:
                merged[-1][2] = end
            else:
                carry = start
            continue
        merged.append([heading, start, end])
    if len(merged) < 2:
        return []
    return [
        Section(heading, start, end, "\n".join(line.rstrip("\r") for line in lines[start:end]).strip())
        for heading, start, end in merged
    ]


def file_link_key(path: Path | str) -> str:
    """The key a link uses for this file, the way link_key reads [[its name]]."""
    return link_key(PurePath(path).name) or ""


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
        return date_from_text(path.stem, vault.date_format)
    if vault.date_source == "frontmatter":
        values = _as_list(frontmatter.get(vault.frontmatter_date_key))
        return date_from_text(values[0], vault.date_format) if values else None
    for line in body.splitlines():
        if line.lstrip().startswith("#") and not line.lstrip().startswith("#["):
            heading_text = line.lstrip().lstrip("#").strip()
            d = date_from_text(heading_text, vault.date_format)
            if d:
                return d
    return None


def parse_file(path: Path | str, config: Config) -> list[ParsedEntry]:
    """The file's entries, leaving out private ones. Empty when all of it is private."""
    path = Path(path)
    text = read_bytes(path).decode("utf-8", errors="replace")
    frontmatter, body = _body(text, config)
    if file_is_private(frontmatter, config):
        return []
    file_date = _file_date(path, frontmatter, body, config)
    cleaned, positions, images = _clean(body)
    source = str(path)
    rel_path = relative_path(path, config.vault.path)
    file_links = _frontmatter_links(frontmatter)
    labels = link_labels(body)
    anchors = link_anchors(body)

    def labels_for(keys: list[str]) -> dict[str, list[str]]:
        return {k: labels[k] for k in keys if k in labels}

    def anchors_for(keys: list[str]) -> dict[str, list[str]]:
        return {k: anchors[k] for k in keys if k in anchors}

    def whole_file() -> list[ParsedEntry]:
        if text_is_private(cleaned, config):
            return []
        if file_date is None:
            raise DateParseError(
                f"Could not determine entry date for {path} "
                f"(date_source={config.vault.date_source})"
            )
        links = _unique(file_links + [key for _, key in positions])
        return [
            ParsedEntry(
                file_date, cleaned, source, links, rel_path, frontmatter, labels_for(links),
                anchors_for(links), _unique([name for _, name in images]),
            )
        ]

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
    private_sections = 0
    for i, m in enumerate(matches):
        start = 0 if i == 0 else m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(cleaned)
        section = cleaned[m.start():end].strip()
        heading_line = section.splitlines()[0] if section else ""
        if i == 0 and preamble:
            section = f"{preamble}\n\n{section}" if section else preamble
        if text_is_private(section, config):
            private_sections += 1
            continue
        section_date = date_from_text(
            heading_line.lstrip("#").strip(), config.vault.date_format
        ) or file_date
        if section_date is None:
            raise DateParseError(
                f"Section {i + 1} of {path} has no parsable date and the file "
                "itself has none to fall back on."
            )
        if section:
            section_links = _unique(file_links + [key for pos, key in positions if start <= pos < end])
            entries.append(
                ParsedEntry(
                    section_date,
                    section,
                    source,
                    section_links,
                    rel_path,
                    frontmatter,
                    labels_for(section_links),
                    anchors_for(section_links),
                    _unique([
                        name for pos, name in images
                        if start <= pos < end or (pos == end == len(cleaned))
                    ]),
                )
            )
    if not entries and not private_sections:
        raise DateParseError(f"No non-empty sections found in {path}")
    return entries


def matches_entry_glob(path: Path | str, config: Config) -> bool:
    """Whether daily_note_glob picks this file, the way the indexer walks the vault.

    PurePath.match treats "**" like "*" and misses nested files, so this uses
    fnmatch, where "*" crosses slashes. The copy without "**/" catches files at
    the top level.
    """
    try:
        rel = PurePath(path).relative_to(config.vault.path)
    except ValueError:
        return False
    rel_str = "/".join(rel.parts)
    glob = config.vault.daily_note_glob
    return any(fnmatch.fnmatch(rel_str, pattern) for pattern in {glob, glob.replace("**/", "")})


def _kind_of(folder: str) -> str:
    return re.sub(r"^\W+|\W+$", "", folder).casefold() or "note"


def note_kind(path: Path | str, config: Config) -> str | None:
    """None for journal entries.

    Otherwise the folder right under the notes path, lowercased with punctuation
    trimmed, so Links/!Files/x.md is "files". Files with no such folder are "note".
    With [notes] other_files, a Markdown file daily_note_glob doesn't pick is a
    note too, its kind the folder it's in under the vault root.
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
        return _kind_of(inner[0])
    if (
        config.notes.other_files
        and rel.suffix.casefold() == ".md"
        # Obsidian leaves out hidden folders like .trash, so they aren't notes
        and not any(part.startswith(".") for part in rel.parts)
        and not matches_entry_glob(path, config)
    ):
        return _kind_of(rel.parts[0]) if len(rel.parts) > 1 else "note"
    return None


def parse_note(path: Path | str, kind: str, config: Config) -> ParsedNote:
    path = Path(path)
    text = read_bytes(path).decode("utf-8", errors="replace")
    frontmatter, body = _body(text, config)
    cleaned, positions = clean_with_links(body)
    title = path.stem

    aliases: dict[str, str] = {}
    for alias in _as_list(frontmatter.get("aliases")) + _as_list(frontmatter.get("alias")):
        if alias.casefold() != title.casefold():
            aliases.setdefault(alias.casefold(), alias)

    properties: dict[str, str] = {}
    values: dict[str, list[str]] = {}
    for key, value in frontmatter.items():
        if key.casefold() in _NON_PROPERTY_KEYS:
            continue
        items = [v for v in (clean_text(item) for item in _as_list(value)) if v]
        if items:
            properties[key] = ", ".join(items)
            values[key] = items

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
        property_values=values,
        links=links,
        rel_path=relative_path(path, config.vault.path),
        private=file_is_private(frontmatter, config) or text_is_private(cleaned, config),
    )
