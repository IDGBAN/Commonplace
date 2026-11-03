"""`journal themes`: groups of entries close in meaning, found from their vectors.

`journal themes --refresh` groups the entries by k-means on their stored
vectors, seeded so the same entries always give the same themes, then has the
fast model name each group from the entries nearest its middle. Grouping
needs no model; naming is one call per theme, kept in the memo. Distances are
worked out by sqlite-vec, so only the averaging runs in Python.

Entries indexed since the last refresh go with the nearest theme whenever the
themes are read, without writing anything, until there are enough of them
that the grouping should be done again.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from array import array
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import sqlite_vec
from pydantic import BaseModel

from . import store
from .config import Config
from .models import JournalError
from .ollama_client import OllamaClient

# the same entries always give the same themes
SEED = 7
MAX_ROUNDS = 30
# fewer entries with vectors than this and there's nothing worth grouping
MIN_GROUPED = 20
# what `count = 0` picks between, from how many entries there are
AUTO_FEWEST = 3
AUTO_MOST = 15
# entries nearest a theme's middle that the model reads to name it
NAMED_FROM = 8
NAME_CHARS = 60
TOP_SHOWN = 8

_NAME_PROMPT = """You name a thread that runs through one person's journal, from \
some of the entries it groups together and the tags and notes they share.

Give it a short name, two to five words, the way the writer might put it \
(like "Training for the half marathon", "Dad's recovery" or "Weekends on the \
allotment"). Then write one sentence on what these entries have in common, in \
the writer's own first person ("Long runs on Sundays and how my knee held \
up"), never "the author", "the user" or "these entries"."""


class ThemeName(BaseModel):
    name: str = ""
    about: str = ""


class ThemesError(Exception):
    """Too few entries, or no vectors, to group."""


def _unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _blob(values: list[float]) -> bytes:
    return sqlite_vec.serialize_float32(values)


def _from_chunks(conn: sqlite3.Connection) -> dict[int, bytes] | None:
    """Every vector in entries_vec, read from sqlite-vec's chunk tables.

    Selecting the embedding column reads a whole chunk for every row, about
    2 ms each on disk, so a few thousand entries took seconds a query. The
    chunks hold the same floats in bulk. This is how sqlite-vec 0.1 lays
    them out (pyproject keeps it below 0.2), and None means they aren't
    laid out that way, so the caller reads them row by row instead.
    """
    try:
        chunks = conn.execute(
            "SELECT chunk_id, size, validity, rowids FROM entries_vec_chunks ORDER BY chunk_id"
        ).fetchall()
        out: dict[int, bytes] = {}
        for chunk_id, size, validity, rowids in chunks:
            row = conn.execute(
                "SELECT vectors FROM entries_vec_vector_chunks00 WHERE rowid = ?", (chunk_id,)
            ).fetchone()
            if row is None or len(rowids) != size * 8 or len(validity) * 8 != size or len(row[0]) % (size * 4):
                return None
            width = len(row[0]) // size
            ids = array("q", rowids)
            for i in range(size):
                if validity[i // 8] >> (i % 8) & 1:
                    out[ids[i]] = row[0][i * width:(i + 1) * width]
    except (sqlite3.Error, ValueError):
        return None
    if out:
        # one row read the usual way has to match, or the layout isn't what this expects
        sample = min(out)
        usual = conn.execute("SELECT embedding FROM entries_vec WHERE entry_id = ?", (sample,)).fetchone()
        if usual is None or usual[0] != out[sample]:
            return None
    return out


def _raw_vectors(conn: sqlite3.Connection, only: set[int] | None = None) -> list[tuple[int, bytes]]:
    """(entry id, vector) for the entries that have one, or just `only`, by id."""
    try:
        found = _from_chunks(conn)
        if found is None:
            found = dict(conn.execute("SELECT entry_id, embedding FROM entries_vec").fetchall())
    except sqlite3.OperationalError:
        return []  # no vector table until the first index run
    known = {r[0] for r in conn.execute("SELECT id FROM entries")}
    keep = known if only is None else known & only
    return sorted((i, v) for i, v in found.items() if i in keep)


# Plain temp tables, since scanning the vec0 table once per centroid each
# round was most of the time. One query then finds every entry's nearest.
_VECTORS = "temp.theme_vectors"
_CENTROIDS = "temp.theme_centroids"


def _load(conn: sqlite3.Connection, raw: list[tuple[int, bytes]]) -> tuple[list[int], list[list[float]]]:
    """Puts the vectors, scaled to length 1, in a temp table, and returns them."""
    conn.execute(f"DROP TABLE IF EXISTS {_VECTORS}")
    conn.execute(f"DROP TABLE IF EXISTS {_CENTROIDS}")
    conn.execute(f"CREATE TABLE {_VECTORS}(id INTEGER PRIMARY KEY, embedding BLOB NOT NULL)")
    conn.execute(f"CREATE TABLE {_CENTROIDS}(idx INTEGER PRIMARY KEY, embedding BLOB NOT NULL)")
    conn.executemany(f"INSERT INTO {_VECTORS}(id, embedding) VALUES (?, ?)", raw)
    conn.execute(f"UPDATE {_VECTORS} SET embedding = vec_normalize(embedding)")
    rows = conn.execute(f"SELECT id, embedding FROM {_VECTORS} ORDER BY id").fetchall()
    return [r[0] for r in rows], [array("f", r[1]).tolist() for r in rows]


def _unload(conn: sqlite3.Connection) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {_VECTORS}")
    conn.execute(f"DROP TABLE IF EXISTS {_CENTROIDS}")


def _distances(conn: sqlite3.Connection, centroid: list[float]) -> dict[int, float]:
    """Cosine distance from every loaded vector to this one, by entry id."""
    return dict(conn.execute(
        f"SELECT id, vec_distance_cosine(embedding, ?) FROM {_VECTORS}", (_blob(centroid),)
    ).fetchall())


def theme_count(config: Config, entries: int) -> int:
    """How many themes to look for: the setting, or about sqrt(n / 4)."""
    if config.themes.count:
        wanted = config.themes.count
    else:
        wanted = max(AUTO_FEWEST, min(AUTO_MOST, round(math.sqrt(entries / 4))))
    # each theme needs room for min_entries of them
    return max(2, min(wanted, entries // config.themes.min_entries))


def _mean(members: list[list[float]]) -> list[float]:
    return _unit([sum(col) for col in zip(*members, strict=True)])


@dataclass
class Grouping:
    centroids: list[list[float]]
    # entry id -> (index into centroids, distance)
    assigned: dict[int, tuple[int, float]]


def _assign(conn: sqlite3.Connection, centroids: list[list[float]]) -> dict[int, tuple[int, float]]:
    conn.execute(f"DELETE FROM {_CENTROIDS}")
    conn.executemany(
        f"INSERT INTO {_CENTROIDS}(idx, embedding) VALUES (?, ?)",
        [(c, _blob(v)) for c, v in enumerate(centroids)],
    )
    # SQLite takes idx from the row MIN() picked
    return {
        entry_id: (c, d)
        for entry_id, c, d in conn.execute(
            f"""SELECT id, idx, MIN(d) FROM (
                    SELECT v.id, c.idx, vec_distance_cosine(v.embedding, c.embedding) AS d
                    FROM {_VECTORS} v CROSS JOIN {_CENTROIDS} c ORDER BY c.idx)
                GROUP BY id"""
        )
    }


def _seed_centroids(ids: list[int], vectors: list[list[float]], k: int, conn: sqlite3.Connection) -> list[list[float]]:
    """k-means++: each next centroid is picked with odds by its distance from the nearest so far."""
    rng = random.Random(SEED)
    at = {entry_id: i for i, entry_id in enumerate(ids)}
    centroids = [vectors[rng.randrange(len(ids))]]
    nearest = _distances(conn, centroids[0])
    while len(centroids) < k:
        weights = [nearest[entry_id] ** 2 for entry_id in ids]
        if not any(weights):
            break  # every entry sits on a centroid already
        pick = rng.choices(ids, weights=weights)[0]
        centroids.append(vectors[at[pick]])
        for entry_id, d in _distances(conn, centroids[-1]).items():
            nearest[entry_id] = min(nearest[entry_id], d)
    return centroids


def _rounds(
    conn: sqlite3.Connection, ids: list[int], vectors: list[list[float]], centroids: list[list[float]]
) -> dict[int, tuple[int, float]]:
    """Moves the centroids until next to no entry changes theme. Returns where each entry went."""
    at = {entry_id: i for i, entry_id in enumerate(ids)}
    # a handful of entries flipping between two near-equal themes needn't keep it going
    settled_at = max(1, len(ids) // 500)
    assigned = _assign(conn, centroids)
    for _ in range(MAX_ROUNDS):
        members: dict[int, list[list[float]]] = defaultdict(list)
        for entry_id, (c, _) in assigned.items():
            members[c].append(vectors[at[entry_id]])
        for c in range(len(centroids)):
            if members.get(c):
                centroids[c] = _mean(members[c])
            else:
                # an empty theme takes the entry furthest from its own
                far = max(assigned, key=lambda e: (assigned[e][1], -e))
                centroids[c] = vectors[at[far]]
        fresh = _assign(conn, centroids)
        moved = sum(1 for e, (c, _) in fresh.items() if assigned[e][0] != c)
        assigned = fresh
        if moved < settled_at:
            break
    return assigned


def group(conn: sqlite3.Connection, config: Config, count: int | None = None) -> Grouping:
    """k-means on the unit sphere over every entry's vector. Themes smaller
    than [themes] min_entries are folded into the others. ThemesError if
    there are too few entries."""
    raw = _raw_vectors(conn)
    if len(raw) < MIN_GROUPED:
        raise ThemesError(
            f"There are {len(raw)} entries with vectors, and grouping needs at least {MIN_GROUPED}. "
            "Run 'journal index' first, or write a few more."
        )
    try:
        ids, vectors = _load(conn, raw)
        centroids = _seed_centroids(ids, vectors, count or theme_count(config, len(ids)), conn)
        while True:
            assigned = _rounds(conn, ids, vectors, centroids)
            sizes = Counter(c for c, _ in assigned.values())
            small = [c for c in range(len(centroids)) if sizes.get(c, 0) < config.themes.min_entries]
            if not small or len(centroids) <= 2:
                return Grouping(centroids, assigned)
            # drop the smallest and let its entries find another
            del centroids[min(small, key=lambda c: (sizes.get(c, 0), c))]
    finally:
        _unload(conn)


def _top(counter: Counter[str], n: int) -> list[str]:
    return [name for name, _ in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def _name(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient,
    central: list[int],
    members: list[int],
) -> ThemeName:
    rows = store.entries_by_ids(conn, central)
    rows.sort(key=lambda r: r["entry_date"])
    tags, notes = _shared(conn, members)
    lines = [f"- {r['entry_date']}: {r['summary'] or '(no summary)'}" for r in rows]
    extra = []
    if tags:
        extra.append("Tags these entries have most: " + ", ".join(_top(tags, TOP_SHOWN)))
    if notes:
        extra.append("Notes they link most: " + ", ".join(_top(notes, TOP_SHOWN)))
    reply = client.chat_structured(
        model=config.ollama.fast_model,
        messages=[
            {"role": "system", "content": _NAME_PROMPT},
            {"role": "user", "content": "\n".join(["Entries:", *lines, "", *extra]).strip()},
        ],
        schema=ThemeName,
    )
    return ThemeName(
        name=" ".join(reply.name.split())[:NAME_CHARS].strip(" ."), about=" ".join(reply.about.split())
    )


@dataclass
class Refreshed:
    themes: int
    entries: int
    named: int
    naming_error: str | None = None


def refresh(
    config: Config,
    conn: sqlite3.Connection,
    client: OllamaClient | None,
    count: int | None = None,
    on_step: Callable[[str], None] = lambda _: None,
) -> Refreshed:
    """Groups the entries again and names the groups, replacing the themes
    there were. With no client, or when naming fails, the themes are kept
    without names; `journal themes --refresh` names them later from the memo."""
    on_step("Grouping your entries...")
    found = group(conn, config, count)
    members: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for entry_id, (c, d) in found.assigned.items():
        members[c].append((d, entry_id))
    # biggest first, which is also the order they're numbered in
    order = sorted(members, key=lambda c: (-len(members[c]), c))
    names: dict[int, ThemeName] = {}
    error = None
    if client is not None:
        for i, c in enumerate(order, start=1):
            on_step(f"Naming theme {i} of {len(order)}...")
            central = [e for _, e in sorted(members[c])[:NAMED_FROM]]
            try:
                names[c] = _name(config, conn, client, central, [e for _, e in members[c]])
            except JournalError as exc:
                # the grouping is still worth keeping
                error = str(exc)
                break
    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute("DELETE FROM entry_themes")
        conn.execute("DELETE FROM themes")
        for theme_id, c in enumerate(order, start=1):
            name = names.get(c)
            conn.execute(
                "INSERT INTO themes(id, name, about, centroid, named_from, made_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    theme_id, (name.name or None) if name else None, (name.about or None) if name else None,
                    _blob(found.centroids[c]),
                    json.dumps([e for _, e in sorted(members[c])[:NAMED_FROM]]), now,
                ),
            )
            conn.executemany(
                "INSERT INTO entry_themes(entry_id, theme_id, distance) VALUES (?, ?, ?)",
                [(e, theme_id, d) for d, e in members[c]],
            )
    return Refreshed(len(order), len(found.assigned), sum(1 for n in names.values() if n.name), error)


# Reading


@dataclass
class Status:
    made_at: str | None
    themes: int
    grouped: int  # entries in the last refresh, still there
    placed: int  # entries since, put with the nearest theme
    stale_share: float

    @property
    def stale(self) -> bool:
        total = self.grouped + self.placed
        return bool(total) and self.placed / total > self.stale_share


@dataclass
class Theme:
    id: int
    name: str | None
    about: str | None
    label: str  # the name, or what its entries share when it has none
    entries: int
    first: str | None
    last: str | None
    mood: float | None
    months: dict[str, int] = field(default_factory=dict)


def _has_themes(conn: sqlite3.Connection) -> bool:
    try:
        return conn.execute("SELECT 1 FROM themes LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        return False  # a read-only connection to a cache from before themes


def membership(conn: sqlite3.Connection) -> dict[int, tuple[int, float, bool]]:
    """entry id -> (theme id, distance, placed since the last refresh)."""
    if not _has_themes(conn):
        return {}
    out = {
        r["entry_id"]: (r["theme_id"], r["distance"], False)
        for r in conn.execute("SELECT entry_id, theme_id, distance FROM entry_themes")
    }
    try:
        newer = {
            r[0] for r in conn.execute(
                "SELECT id FROM entries WHERE id NOT IN (SELECT entry_id FROM entry_themes)"
            )
        }
        centroids = conn.execute("SELECT id, centroid FROM themes ORDER BY id").fetchall()
        for entry_id, vector in _raw_vectors(conn, newer) if newer else []:
            for theme in centroids:
                d = conn.execute("SELECT vec_distance_cosine(?, ?)", (vector, theme["centroid"])).fetchone()[0]
                if entry_id not in out or d < out[entry_id][1]:
                    out[entry_id] = (theme["id"], d, True)
    except sqlite3.OperationalError:
        pass  # vectors from another embedding model can't be compared; refresh sorts it out
    return out


def status(conn: sqlite3.Connection, config: Config) -> Status:
    placed = membership(conn)
    if not placed:
        return Status(None, 0, 0, 0, config.themes.stale_share)
    row = conn.execute("SELECT COUNT(*) AS n, MAX(made_at) AS made FROM themes").fetchone()
    new = sum(1 for _, _, p in placed.values() if p)
    return Status(row["made"], row["n"], len(placed) - new, new, config.themes.stale_share)


def _light(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """Just the dates and moods, since a theme can hold most of the journal."""
    return list(conn.execute(
        "SELECT id, entry_date, mood_score FROM entries WHERE id IN (SELECT value FROM json_each(?))",
        (json.dumps(ids),),
    ))


def _shared(conn: sqlite3.Connection, ids: list[int]) -> tuple[Counter[str], Counter[str]]:
    tags = Counter(t for ts in store.tags_for_entries(conn, ids).values() for t in ts)
    notes = Counter(n["name"] for ns in store.entities_for_entries(conn, ids).values() for n in ns)
    return tags, notes


def _label(name: str | None, tags: Counter[str], notes: Counter[str]) -> str:
    if name:
        return name
    shared = _top(notes, 2) + _top(tags, 2)
    return "Unnamed: " + ", ".join(shared[:3]) if shared else "Unnamed"


def overview(conn: sqlite3.Connection, placed: dict[int, tuple[int, float, bool]] | None = None) -> list[Theme]:
    """Every theme, biggest first, with how many entries it has each month."""
    placed = membership(conn) if placed is None else placed
    if not placed:
        return []
    by_theme: dict[int, list[int]] = defaultdict(list)
    for entry_id, (theme_id, _, _) in placed.items():
        by_theme[theme_id].append(entry_id)
    out = []
    for theme in conn.execute("SELECT id, name, about FROM themes ORDER BY id").fetchall():
        ids = by_theme.get(theme["id"], [])
        rows = _light(conn, ids)
        # an unnamed theme is labelled by what its entries share
        tags, notes = _shared(conn, ids) if not theme["name"] else (Counter(), Counter())
        moods = [r["mood_score"] for r in rows if r["mood_score"] is not None]
        dates = sorted(r["entry_date"] for r in rows)
        out.append(Theme(
            id=theme["id"], name=theme["name"], about=theme["about"],
            label=_label(theme["name"], tags, notes),
            entries=len(rows), first=dates[0] if dates else None, last=dates[-1] if dates else None,
            mood=round(sum(moods) / len(moods), 2) if moods else None,
            months=dict(sorted(Counter(d[:7] for d in dates).items())),
        ))
    return sorted(out, key=lambda t: (-t.entries, t.id))


@dataclass
class Detail:
    theme: Theme
    central: list[sqlite3.Row]
    newest: list[sqlite3.Row]
    tags: list[tuple[str, int]]
    notes: list[tuple[int, str, str, int]]  # id, title, kind, entries


def detail(conn: sqlite3.Connection, theme_id: int, newest: int = 30) -> Detail | None:
    placed = membership(conn)
    found = next((t for t in overview(conn, placed) if t.id == theme_id), None)
    if found is None:
        return None
    mine = sorted((d, e) for e, (t, d, _) in placed.items() if t == theme_id)
    ids = [e for _, e in mine]
    central = store.entries_by_ids(conn, ids[:NAMED_FROM])
    order = {e: i for i, e in enumerate(ids)}
    central.sort(key=lambda r: order[r["id"]])
    rows = sorted(store.entries_by_ids(conn, ids), key=lambda r: (r["entry_date"], r["id"]), reverse=True)
    tags, _ = _shared(conn, ids)
    linked: Counter[tuple[int, str, str]] = Counter()
    for ns in store.entities_for_entries(conn, ids).values():
        linked.update((n["id"], n["name"], n["type"]) for n in ns)
    return Detail(
        theme=found,
        central=central,
        newest=rows[:newest],
        tags=sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_SHOWN],
        notes=[(i, t, k, n) for (i, t, k), n in sorted(linked.items(), key=lambda kv: (-kv[1], kv[0][1]))[:TOP_SHOWN]],
    )


def in_range(
    conn: sqlite3.Connection, date_from: date | None, date_to: date | None, limit: int, least: int = 2
) -> list[tuple[Theme, int]]:
    """The named themes with the most entries in this range, and how many, for answers."""
    placed = membership(conn)
    if not placed:
        return []
    lo = date_from.isoformat() if date_from else ""
    hi = date_to.isoformat() if date_to else "9999"
    counts: Counter[int] = Counter()
    for r in _light(conn, list(placed)):
        if lo <= r["entry_date"] <= hi:
            counts[placed[r["id"]][0]] += 1
    themes = {t.id: t for t in overview(conn, placed) if t.name}
    ranked = sorted(
        ((themes[i], n) for i, n in counts.items() if i in themes and n >= least),
        key=lambda tn: (-tn[1], tn[0].id),
    )
    return ranked[:limit]


def theme_json(theme: Theme) -> dict[str, object]:
    return {
        "id": theme.id, "name": theme.name, "about": theme.about, "label": theme.label,
        "entries": theme.entries, "first": theme.first, "last": theme.last, "mood": theme.mood,
        "months": theme.months,
    }


def status_json(found: Status) -> dict[str, object]:
    return {
        "made_at": found.made_at, "themes": found.themes, "grouped": found.grouped,
        "placed": found.placed, "stale": found.stale,
    }
