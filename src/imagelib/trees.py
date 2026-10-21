"""Folder trees that hold the same things: backups of backups, partly de-duplicated.

Pairs are found from evidence that two places hold the same thing (the same content, or
a folder of the same name) and climbed upwards while the folder names keep matching, so
``Backup/Pictures/2009/x.jpg`` and ``Pictures/2009/x.jpg`` meet at the two ``Pictures``
folders. A pair of differently named folders (``Backup 2014`` and ``Old Mac``) is
reported instead when most of both is made of matched sub-trees. Pairs inside a
reported pair are not reported again.

Each reported pair is then compared file by file, by content and by relative path, so
the numbers say how strongly the trees correlate and what a merge would do.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .catalog import Catalog

MAX_COPIES = 20          # contents (or folder names) found in more places than this are too common to say much
ROLL_UP_COVERAGE = 0.8   # share of both parents' files that matched sub-trees must hold to report the parents
MIN_SHARED = 2           # fewer shared items than this is a coincidence, not a related tree


@dataclass
class _Dir:
    volume: str
    path: str            # '' = top of the drive
    parent: int | None
    name: str            # lower-cased, for matching
    files: int = 0       # recursive
    dirs: int = 0        # recursive, not counting itself


class _Tree:
    """Every folder that holds files, and which folder each content is in."""

    def __init__(self, conn):
        self.dirs: list[_Dir] = []
        self._ids: dict[tuple[str, str], int] = {}
        self.by_hash: dict[bytes, list[int]] = defaultdict(list)
        for volume, rel, h, size in conn.execute(
                "SELECT volume_id, rel_path, content_hash, size FROM files WHERE state != 'missing'"):
            cut = rel.rfind("/")
            d = self._dir(volume, rel[:cut] if cut >= 0 else "")
            self.dirs[d].files += 1
            if h is not None and size > 0:
                self.by_hash[h].append(d)
        for i in range(len(self.dirs) - 1, -1, -1):  # children were created after their parents
            p = self.dirs[i].parent
            if p is not None:
                self.dirs[p].files += self.dirs[i].files
                self.dirs[p].dirs += self.dirs[i].dirs + 1

    def _dir(self, volume: str, path: str) -> int:
        key = (volume, path)
        found = self._ids.get(key)
        if found is not None:
            return found
        parent = None
        if path:
            cut = path.rfind("/")
            parent = self._dir(volume, path[:cut] if cut >= 0 else "")
        self._ids[key] = len(self.dirs)
        self.dirs.append(_Dir(volume, path, parent, path[path.rfind("/") + 1:].lower()))
        return self._ids[key]

    def related(self, a: int, b: int) -> bool:
        """Same folder, or one inside the other: never a pair."""
        x, y = self.dirs[a], self.dirs[b]
        if x.volume != y.volume:
            return False
        return x.path == y.path or not x.path or not y.path or \
            x.path.startswith(y.path + "/") or y.path.startswith(x.path + "/")

    def align(self, a: int, b: int) -> tuple[int, int] | None:
        """Climb from two folders while their names match; the highest matching pair."""
        if self.related(a, b):
            return None
        while self.dirs[a].name == self.dirs[b].name:
            pa, pb = self.dirs[a].parent, self.dirs[b].parent
            if pa is None or pb is None or self.related(pa, pb):
                break
            a, b = pa, pb
        return (a, b) if (self.dirs[a].volume, self.dirs[a].path) < (self.dirs[b].volume, self.dirs[b].path) \
            else (b, a)

    def within(self, d: int, top: int) -> bool:
        while d is not None:
            if d == top:
                return True
            d = self.dirs[d].parent
        return False


def find_pairs(tree: _Tree) -> list[tuple[int, int]]:
    """Folder pairs worth comparing: the highest level at which two trees line up."""
    content: dict[tuple[int, int], int] = defaultdict(int)   # shared contents found under the pair
    layout: dict[tuple[int, int], int] = defaultdict(int)    # same-named folders found under the pair
    for dirs in tree.by_hash.values():
        places = sorted(set(dirs))
        if 2 <= len(places) <= MAX_COPIES:
            for i, a in enumerate(places):
                for b in places[i + 1:]:
                    pair = tree.align(a, b)
                    if pair:
                        content[pair] += 1
    by_name: dict[str, list[int]] = defaultdict(list)
    for i, d in enumerate(tree.dirs):
        if d.path:
            by_name[d.name].append(i)
    for places in by_name.values():
        if 2 <= len(places) <= MAX_COPIES:
            for i, a in enumerate(places):
                for b in places[i + 1:]:
                    pair = tree.align(a, b)
                    if pair:
                        layout[pair] += 1

    def strong(pair: tuple[int, int]) -> bool:
        a, b = (tree.dirs[x] for x in pair)
        smaller = min(a.files, b.files)
        return (content.get(pair, 0) >= MIN_SHARED and content.get(pair, 0) >= 0.05 * smaller) or \
            (layout.get(pair, 0) >= MIN_SHARED and layout.get(pair, 0) >= 0.3 * min(a.dirs, b.dirs))

    pairs = {p for p in set(content) | set(layout) if strong(p)}

    # Roll up: a folder made mostly of matched sub-trees is the one to report, on both sides
    # (Backup 2014 <-> Old Mac) or on one side only (several folders copied into one flat folder).
    def key(d: int) -> tuple[str, str]:
        return tree.dirs[d].volume, tree.dirs[d].path

    while True:
        candidates: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
        for a, b in pairs:
            pa, pb = tree.dirs[a].parent, tree.dirs[b].parent
            for x, y in ((pa, pb), (pa, b), (a, pb)):
                if x is None or y is None or tree.related(x, y):
                    continue
                cand, child = ((x, y), (a, b)) if key(x) < key(y) else ((y, x), (b, a))
                if cand not in pairs:
                    candidates[cand].append(child)
        added = False
        for (x, y), kids in candidates.items():
            def covered(top: int, sides: set[int]) -> int:
                return tree.dirs[top].files if top in sides else sum(tree.dirs[d].files for d in sides)

            if covered(x, {k[0] for k in kids}) >= ROLL_UP_COVERAGE * tree.dirs[x].files and \
                    covered(y, {k[1] for k in kids}) >= ROLL_UP_COVERAGE * tree.dirs[y].files:
                pairs.add((x, y))
                added = True
        if not added:
            break

    # Report the highest pairs only: drop any pair lying inside another pair (both sides).
    by_left: dict[int, list[int]] = defaultdict(list)
    for a, b in pairs:
        by_left[a].append(b)

    def inside_another(a: int, b: int) -> bool:
        x: int | None = a
        while x is not None:
            if any((x, other) != (a, b) and tree.within(b, other) for other in by_left.get(x, ())):
                return True
            x = tree.dirs[x].parent
        return False

    return [(a, b) for a, b in pairs if not inside_another(a, b)]


# -- comparing two trees ------------------------------------------------------------------------

def tree_files(conn, volume: str, root: str) -> list[tuple[str, bytes | None, int]]:
    """(path below root, content hash, size) of every file in a tree."""
    if root:
        prefix = root.rstrip("/") + "/"
        rows = conn.execute("SELECT rel_path, content_hash, size FROM files WHERE volume_id = ? AND state != 'missing' "
                            "AND rel_path >= ? AND rel_path < ?", (volume, prefix, prefix[:-1] + "0"))
        return [(r[0][len(prefix):], r[1], r[2]) for r in rows]
    return [tuple(r) for r in conn.execute(
        "SELECT rel_path, content_hash, size FROM files WHERE volume_id = ? AND state != 'missing'", (volume,))]


@dataclass
class Side:
    files: int = 0
    bytes: int = 0
    in_other: int = 0          # files whose content is anywhere in the other tree
    in_other_bytes: int = 0
    only: int = 0              # neither its content nor its path is in the other tree: a merge brings it over
    only_bytes: int = 0
    unhashed: int = 0          # not read yet: compared by path only
    folders: int = 0


@dataclass
class Comparison:
    a: Side = field(default_factory=Side)
    b: Side = field(default_factory=Side)
    same_path: int = 0         # same relative path and same content
    conflicts: int = 0         # same relative path, different content: a merge keeps both
    shared_folders: int = 0    # relative folder paths present in both


def _hashes(rows: list) -> set[bytes]:
    return {r[1] for r in rows if r[1] is not None}


def _paths(rows: list) -> dict[str, bytes | None]:
    return {r[0].lower(): r[1] for r in rows}


def compare(rows_a: list, rows_b: list, all_a: set[bytes] | None = None, all_b: set[bytes] | None = None) -> Comparison:
    """Two trees file by file. Content is looked up in ``all_a`` / ``all_b`` when given (the whole
    trees, when comparing their sub-folders); paths only ever between ``rows_a`` and ``rows_b``."""
    out = Comparison()
    paths = (_paths(rows_a), _paths(rows_b))
    hashes = (_hashes(rows_a) if all_a is None else all_a, _hashes(rows_b) if all_b is None else all_b)
    for rows, side, other_hashes, other_paths in ((rows_a, out.a, hashes[1], paths[1]),
                                                  (rows_b, out.b, hashes[0], paths[0])):
        side.files = len(rows)
        for rel, h, size in rows:
            side.bytes += size
            if h is None:
                side.unhashed += 1
            if h is not None and h in other_hashes:
                side.in_other += 1
                side.in_other_bytes += size
            elif rel.lower() not in other_paths:
                side.only += 1
                side.only_bytes += size
    for rel, h in paths[0].items():
        if rel in paths[1]:
            if h is not None and h == paths[1][rel]:
                out.same_path += 1
            else:
                out.conflicts += 1
    folders = []
    for rows in (rows_a, rows_b):
        folders.append({r[0].lower().rsplit("/", 1)[0] for r in rows if "/" in r[0]})
    out.a.folders, out.b.folders = len(folders[0]), len(folders[1])
    out.shared_folders = len(folders[0] & folders[1])
    return out


def comparison_dict(c: Comparison) -> dict:
    def side(s: Side) -> dict:
        return dict(vars(s))
    return {"a": side(c.a), "b": side(c.b), "same_path": c.same_path, "conflicts": c.conflicts,
            "shared_folders": c.shared_folders}


def children(conn, a_volume: str, a_root: str, b_volume: str, b_root: str) -> list[dict]:
    """One level down: each sub-folder of either tree against its namesake on the other side (paths)
    and against the whole other tree (content: it may sit elsewhere there)."""
    rows = (tree_files(conn, a_volume, a_root), tree_files(conn, b_volume, b_root))
    everything = (_hashes(rows[0]), _hashes(rows[1]))
    groups: list[dict[str, list]] = [defaultdict(list), defaultdict(list)]
    names: dict[str, list[str | None]] = {}
    for i, side_rows in enumerate(rows):
        for rel, h, size in side_rows:
            if "/" not in rel:
                continue
            name, rest = rel.split("/", 1)
            key = name.lower()
            groups[i][key].append((rest, h, size))
            names.setdefault(key, [None, None])[i] = name
    out = []
    for key in sorted(names, key=str.casefold):
        c = compare(groups[0].get(key, []), groups[1].get(key, []), *everything)
        a_name, b_name = names[key]
        out.append({"a_name": a_name, "b_name": b_name, **comparison_dict(c)})
    return out


# -- background index --------------------------------------------------------------------------

@dataclass
class TreePair:
    a_volume: str
    a_root: str
    b_volume: str
    b_root: str
    comparison: Comparison


class TreeIndex:
    """Related folder trees, recomputed in the background when the catalog changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stamp = None
        self.pairs: list[TreePair] | None = None
        self.building = False
        self.progress = ""
        self.built_at: float | None = None
        self.error: str | None = None

    def state(self, cat: Catalog) -> dict:
        self._refresh(cat)
        return {"building": self.building, "progress": self.progress, "built_at": self.built_at,
                "error": self.error, "ready": self.pairs is not None}

    def _refresh(self, cat: Catalog) -> None:
        with self._lock:
            if self.building:
                return
            stamp = cat.conn.execute("SELECT value FROM meta WHERE key = 'files_version'").fetchone()[0]
            if stamp == self._stamp:
                return
            self.building, self.error = True, None
        threading.Thread(target=self._build, args=(cat, stamp), name="trees", daemon=True).start()

    def _build(self, cat: Catalog, stamp) -> None:
        try:
            conn = cat.conn
            self.progress = "reading the catalog"
            tree = _Tree(conn)
            self.progress = "matching folders"
            found = find_pairs(tree)
            pairs = []
            for i, (a, b) in enumerate(found):
                self.progress = f"comparing {i + 1:,} of {len(found):,}"
                da, db = tree.dirs[a], tree.dirs[b]
                c = compare(tree_files(conn, da.volume, da.path), tree_files(conn, db.volume, db.path))
                if c.a.in_other + c.b.in_other >= MIN_SHARED or c.shared_folders >= MIN_SHARED:
                    pairs.append(TreePair(da.volume, da.path, db.volume, db.path, c))
            self.pairs, self._stamp, self.built_at = pairs, stamp, time.time()
        except Exception as e:  # shown to the user; the previous result keeps serving
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self.progress = ""
            self.building = False

    def list(self, cat: Catalog, volume: str | None = None, sort: str = "shared", offset: int = 0,
             limit: int = 50) -> dict:
        state = self.state(cat)
        pairs = [p for p in self.pairs or [] if not volume or volume in (p.a_volume, p.b_volume)]
        keys = {
            "shared": lambda p: -(p.comparison.a.in_other_bytes + p.comparison.b.in_other_bytes),
            # In steps of 10%, then by data in common: tiny folders inside huge ones do not lead.
            "strength": lambda p: (-round(strength(p.comparison), 1),
                                   -(p.comparison.a.in_other_bytes + p.comparison.b.in_other_bytes)),
            "size": lambda p: -(p.comparison.a.bytes + p.comparison.b.bytes),
        }
        pairs.sort(key=keys.get(sort, keys["shared"]))
        labels = dict(cat.conn.execute("SELECT id, label FROM volumes"))
        items = [{"a_volume": p.a_volume, "a_label": labels.get(p.a_volume, "?"), "a_root": p.a_root,
                  "b_volume": p.b_volume, "b_label": labels.get(p.b_volume, "?"), "b_root": p.b_root,
                  "strength": round(strength(p.comparison), 3), **comparison_dict(p.comparison)}
                 for p in pairs[offset:offset + limit]]
        return {**state, "total": len(pairs), "items": items}


def strength(c: Comparison) -> float:
    """How strongly two trees correlate (0-1): how much of either one the other already holds,
    by content, or by layout (same sub-folders, same file paths) for backups thinned out since."""
    content = max(c.a.in_other / max(1, c.a.files), c.b.in_other / max(1, c.b.files))
    layout = c.shared_folders / max(1, min(c.a.folders, c.b.folders)) if c.a.folders and c.b.folders else 0.0
    paths = (c.same_path + c.conflicts) / max(1, min(c.a.files, c.b.files))
    return max(content, (layout + paths) / 2)
