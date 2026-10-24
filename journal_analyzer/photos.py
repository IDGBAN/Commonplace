"""Photos entries link or embed, described by a local vision model.

With [ollama] vision_model set, each image an entry links, like
![[beach.jpg]] or ![](beach.jpg), is described once, and the description
goes along with the entry to the fast model and the embedding model, so a
day that's two lines and a photo can still be found. Images are found the
way Obsidian finds attachments: by the path in the link, or else by file
name anywhere in the vault. The memo keeps each description by the image's
bytes, so a photo is described once however many entries show it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from . import parser
from .config import Config
from .models import ModelRequestError, ParsedEntry
from .ollama_client import OllamaClient

console = Console()

PROMPT = (
    "This photo is from someone's personal journal. Describe it in one or two "
    "plain sentences: what is in it, who or how many people, where it seems to "
    "be, and any text you can read. Don't guess anyone's name."
)
DESCRIPTION_CHARS = 300
# images bigger than this are skipped rather than sent
MAX_IMAGE_BYTES = 20_000_000
# a vault walked for images is walked again for a name it lacks after this long
INDEX_MAX_AGE_SECONDS = 30.0


@dataclass(frozen=True)
class Photo:
    name: str  # as the entry links it
    path: str  # the file, from the vault root
    description: str


class _Images:
    """Image files in the vault by name, for links that give only a name."""

    def __init__(self) -> None:
        self._by_vault: dict[Path, tuple[float, dict[str, list[Path]]]] = {}

    def _walk(self, vault: Path) -> dict[str, list[Path]]:
        found: dict[str, list[Path]] = {}
        for path in sorted(vault.rglob("*")):
            rel = path.relative_to(vault).parts
            if any(part.startswith(".") for part in rel) or not path.is_file():
                continue
            if parser.image_name(path.name) is not None:
                found.setdefault(path.name.casefold(), []).append(path)
        return found

    def find(self, vault: Path, name: str) -> Path | None:
        direct = vault / name
        if "/" in name and direct.is_file():
            return direct
        key = name.rsplit("/", 1)[-1].casefold()
        stamp, index = self._by_vault.get(vault, (0.0, {}))
        if key not in index and time.monotonic() - stamp > INDEX_MAX_AGE_SECONDS:
            index = self._walk(vault)
            self._by_vault[vault] = (time.monotonic(), index)
        # like Obsidian, the one closest to the vault root wins
        paths = sorted((p for p in index.get(key, []) if p.is_file()), key=lambda p: (len(p.parts), str(p)))
        return paths[0] if paths else None


_IMAGES = _Images()
_warned: set[str] = set()


def find(config: Config, name: str) -> Path | None:
    return _IMAGES.find(config.vault.path, name)


def to_describe(entry: ParsedEntry, config: Config) -> list[tuple[str, Path]]:
    """The images of an entry that would be described, as (name, file)."""
    if not config.ollama.vision_model:
        return []
    found: list[tuple[str, Path]] = []
    for name in entry.images:
        path = find(config, name)
        if path is not None and not any(p == path for _, p in found):
            found.append((name, path))
        if len(found) >= config.indexing.photos_per_entry:
            break
    return found


def describe(entry: ParsedEntry, config: Config, client: OllamaClient) -> list[Photo]:
    """The entry's photos with their descriptions. One that can't be read is left out."""
    model = config.ollama.vision_model
    photos = []
    for name, path in to_describe(entry, config):
        try:
            data = parser.read_bytes(path)
        except OSError:
            continue
        if len(data) > MAX_IMAGE_BYTES:
            continue
        try:
            text = client.describe_image(model, PROMPT, data)
        except ModelRequestError as exc:
            # a model that can't read images says so for every photo; once is enough
            if model not in _warned:
                _warned.add(model)
                console.print(f"[yellow]Couldn't describe a photo with {model}: {exc}[/yellow]")
            continue
        text = " ".join(text.split())
        if len(text) > DESCRIPTION_CHARS:
            text = text[: DESCRIPTION_CHARS - 1].rsplit(" ", 1)[0] + "…"
        if text:
            photos.append(Photo(name, parser.relative_path(path, config.vault.path), text))
    return photos


def block(photos: list[Photo]) -> str:
    """The photos as a few lines for a prompt or an embedding, or "" when there are none."""
    if not photos:
        return ""
    return "Photos in this entry, as a vision model describes them:\n" + "\n".join(
        f"- {p.name}: {p.description}" for p in photos
    )
