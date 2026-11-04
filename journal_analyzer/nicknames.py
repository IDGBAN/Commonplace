"""Names you give notes in link text, like Johnny in [[John Doe|Johnny]].

Link text you use for one note in at least [notes] learned_alias_min entries
counts as another name for it wherever names are matched: the names in a
question, `journal note` and --about, and unlinked mentions. Links still
resolve only by file name and aliases, the way Obsidian does it.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict

from . import store
from .config import Config
from .query import MIN_NAME_CHARS

# words that stand for someone without naming them
_NOT_NAMES = frozenset(
    """i me my mine myself you your yours he him his she her hers it its we us
    our ours they them their theirs this that these those there here the a an
    and or of to""".split()
)
# straight or curly apostrophe
_POSSESSIVE_RE = re.compile(r"['\u2019]s$", re.IGNORECASE)


def name_key(text: str) -> str:
    """How a name is compared: case and spacing ignored, and "Mom's" is Mom."""
    return _POSSESSIVE_RE.sub("", " ".join(text.casefold().split())).strip()


def _shown(label: str) -> str:
    return _POSSESSIVE_RE.sub("", " ".join(label.split())).strip()


def learn(config: Config, conn: sqlite3.Connection) -> list[tuple[int, str, str, int]]:
    """(note_id, name, key, entries) for each name the link text gives a note."""
    minimum = config.notes.learned_alias_min
    if not minimum:
        return []
    ignored = {name_key(n) for n in config.notes.learned_alias_ignore}
    known = store.note_name_keys(conn)
    uses: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    spellings: dict[tuple[str, int], Counter[str]] = defaultdict(Counter)
    for row in store.link_label_uses(conn):
        key = name_key(row["label"])
        if (
            len(key) < MIN_NAME_CHARS
            or key in known
            or key in ignored
            or all(word in _NOT_NAMES for word in key.split())
        ):
            continue
        uses[key][row["note_id"]].add(row["entry_id"])
        spellings[(key, row["note_id"])][_shown(row["label"])] += 1
    learned = []
    for key, notes in sorted(uses.items()):
        # the same words for two notes don't name either of them
        if len(notes) != 1:
            continue
        [(note_id, entries)] = notes.items()
        if len(entries) >= minimum:
            name = spellings[(key, note_id)].most_common(1)[0][0]
            learned.append((note_id, name, key, len(entries)))
    return learned


def refresh(config: Config, conn: sqlite3.Connection) -> int:
    """Rebuilds the learned names from the links in the cache. Returns how many there are."""
    learned = learn(config, conn)
    store.replace_learned_names(conn, learned)
    return len(learned)
