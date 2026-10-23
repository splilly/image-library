"""Folder trees holding the same things: found at their top, compared file by file."""

import os
import random

import pytest
from conftest import register, scan

from imagelib import trees


def put(root, rel, data: bytes):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def drive(tmp_path):
    """Two backups of the same folders, each thinned out differently by an old de-duplication."""
    rng = random.Random(7)
    blob = {name: rng.randbytes(3000 + i) for i, name in enumerate("abcdefghijkl")}
    root = tmp_path / "drive"
    for name in "abcdefg":                                # backup 1: a-g
        put(root, f"Backup 2014/Pictures/2009/{name}.jpg.bak", blob[name])
    for name in "cdefghi":                                # old mac: c-i (a, b gone; h, i only here)
        put(root, f"Old Mac/Pictures/2009/{name}.jpg.bak", blob[name])
    put(root, "Backup 2014/Documents/letter.docx", blob["j"])
    put(root, "Old Mac/Documents/letter.docx", blob["j"])
    put(root, "Backup 2014/Documents/notes.txt", b"version one")      # same path, different content
    put(root, "Old Mac/Documents/notes.txt", b"version two, longer")
    for name in "kl":                                     # a flattened copy of two folders
        put(root, f"Scans/Family/{name}1.tif.bak", blob[name])
        put(root, f"Scans/Trips/{name}2.tif.bak", blob[name][::-1])
        put(root, f"Flat/{name}1.tif.bak", blob[name])
        put(root, f"Flat/{name}2.tif.bak", blob[name][::-1])
    put(root, "Unrelated/thing.bin", os.urandom(5000))
    return root


def scanned_pairs(catalog, root):
    vid = register(catalog, root)
    assert scan(catalog, root, vid).state == "done"
    tree = trees._Tree(catalog.conn)
    found = {(tree.dirs[a].path, tree.dirs[b].path) for a, b in trees.find_pairs(tree)}
    return vid, found


def test_trees_are_reported_at_their_top_not_folder_by_folder(drive, catalog):
    vid, found = scanned_pairs(catalog, drive)
    assert ("Backup 2014", "Old Mac") in found
    assert not any(a.startswith("Backup 2014/") and b.startswith("Old Mac/") for a, b in found)
    assert ("Flat", "Scans") in found  # several folders copied into one flat folder
    assert not any("Unrelated" in a or "Unrelated" in b for a, b in found)


def test_comparison_says_what_a_merge_would_do(drive, catalog):
    vid, _ = scanned_pairs(catalog, drive)
    c = trees.compare(trees.tree_files(catalog.conn, vid, "Backup 2014"),
                      trees.tree_files(catalog.conn, vid, "Old Mac"))
    assert (c.a.files, c.b.files) == (9, 9)
    assert c.a.in_other == 6 and c.b.in_other == 6       # c-g and the letter
    assert c.a.only == 2 and c.b.only == 2               # a, b only in the backup; h, i only on the old mac
    assert c.same_path == 6 and c.conflicts == 1         # notes.txt differs
    assert c.shared_folders == 2
    assert 0.6 < trees.strength(c) <= 1


def test_children_break_a_pair_down_one_level(drive, catalog):
    vid, _ = scanned_pairs(catalog, drive)
    rows = {r["a_name"] or r["b_name"]: r for r in trees.children(catalog.conn, vid, "Backup 2014", vid, "Old Mac")}
    assert set(rows) == {"Pictures", "Documents"}
    assert rows["Documents"]["conflicts"] == 1 and rows["Pictures"]["a"]["only"] == 2


def test_index_builds_in_the_background(drive, catalog):
    import time

    vid, _ = scanned_pairs(catalog, drive)
    index = trees.TreeIndex()
    deadline = time.monotonic() + 30
    while not (listing := index.list(catalog))["ready"] or listing["building"]:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    roots = {(i["a_root"], i["b_root"]) for i in listing["items"]}
    assert ("Backup 2014", "Old Mac") in roots and listing["error"] is None


def test_sub_folders_find_their_content_anywhere_in_the_other_tree(drive, catalog):
    vid, _ = scanned_pairs(catalog, drive)
    rows = {r["a_name"] or r["b_name"]: r for r in trees.children(catalog.conn, vid, "Flat", vid, "Scans")}
    assert set(rows) == {"Family", "Trips"}  # only on the right: Flat has no sub-folders
    assert rows["Family"]["b"]["in_other"] == 2 and rows["Family"]["b"]["only"] == 0
