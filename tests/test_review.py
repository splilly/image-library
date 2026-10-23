import shutil

import pytest
from conftest import file_row, register, scan

from imagelib import review
from imagelib.previews import PreviewStore
from imagelib.queries import PhashIndex


@pytest.fixture
def lib(drive, catalog):
    root, rel = drive
    vid = register(catalog, root)
    scan(catalog, root, vid)
    ids = {name: file_row(catalog, vid, path)["id"] for name, path in rel.items()}
    return catalog, vid, root, rel, ids


def decision_of(cat, file_id):
    return review.file_decisions(cat, [file_id]).get(file_id)


def test_discarding_every_copy_of_an_intact_image_is_refused(lib):
    cat, vid, root, rel, ids = lib
    with pytest.raises(review.Refused) as err:
        review.decide(cat, [ids["photo1"], ids["dup1"]], review.DISCARD)
    assert err.value.details[0]["copies"] == 2
    assert decision_of(cat, ids["photo1"]) is None and decision_of(cat, ids["dup1"]) is None  # all or nothing

    review.decide(cat, [ids["dup1"]], review.DISCARD)
    with pytest.raises(review.Refused):
        review.decide(cat, [ids["photo1"]], review.DISCARD)          # would now be the last copy
    review.decide(cat, [ids["photo5"]], review.KEEP)                   # keeping is always fine


def test_corrupt_content_may_lose_every_copy(lib):
    cat, vid, root, rel, ids = lib
    review.decide(cat, [ids["truncated"], ids["empty"]], review.DISCARD, "problem")
    assert review.summary(cat)["discard"]["files"] == 2


def test_decision_lapses_when_the_file_changes(lib):
    cat, vid, root, rel, ids = lib
    review.decide(cat, [ids["dup2"]], review.DISCARD, "duplicate")
    shutil.copy2(root / rel["photo6"], root / rel["dup2"])  # different content now
    scan(cat, root, vid)
    assert decision_of(cat, ids["dup2"]) is None
    s = review.summary(cat)
    assert s["discard"]["files"] == 0 and s["stale"] == 1


def test_duplicate_groups_and_suggestions(lib):
    cat, vid, root, rel, ids = lib
    groups = review.duplicate_groups(cat)
    assert groups["total"] == 3 and len(groups["items"]) == 3
    assert all(g["copies"] == 2 and g["same_drive"] for g in groups["items"])
    group = review.duplicate_group(cat, bytes.fromhex(groups["items"][0]["hash"]), online={vid})
    keeper = next(c for c in group["copies"] if c["file_id"] == group["suggested_keep"])
    assert keeper["path"].startswith("photos/") and "backup" in group["suggestion_reason"]

    result = review.apply_suggestions(cat, [bytes.fromhex(g["hash"]) for g in groups["items"]])
    assert result == {"groups": 3, "discarded": 3}
    assert {decision_of(cat, ids[f"dup{i}"]) for i in (1, 2, 3)} == {review.DISCARD}
    assert {decision_of(cat, ids[f"photo{i}"]) for i in (1, 2, 3)} == {review.KEEP}
    assert review.duplicate_groups(cat, undecided=True)["total"] == 0
    # Already-decided groups are left alone by a second pass.
    assert review.apply_suggestions(cat, [bytes.fromhex(g["hash"]) for g in groups["items"]])["groups"] == 0


def test_suggestion_prefers_originals_over_backups_then_the_oldest():
    C = review.Copy
    copies = [C(1, "v", "D", "Backup/2004/a.jpg", 1, False), C(2, "v", "D", "Photos/2004/a.jpg", 5, False)]
    assert review.suggest_keeper(copies)[0] == 2
    copies = [C(1, "v", "D", "x/a.jpg", 9, False), C(2, "v", "D", "y/a.jpg", 3, False)]
    assert review.suggest_keeper(copies) == (2, "oldest file date (most likely the original)")
    copies = [C(1, "v", "D", "Pictures/a (1).jpg", 1, False), C(2, "v", "D", "Pictures/a.jpg", 1, False)]
    assert review.suggest_keeper(copies)[0] == 2


def test_duplicate_cache_rebuilds_only_when_files_change(lib):
    cat, vid, root, rel, ids = lib
    review.refresh_duplicates(cat)
    assert review.refresh_duplicates(cat) is False
    shutil.copy2(root / rel["photo5"], root / "photos/2004/IMG_0005 copy.jpg")
    scan(cat, root, vid)
    assert review.refresh_duplicates(cat) is True
    assert review.duplicate_groups(cat)["total"] == 4


def test_problem_queue_and_intact_versions(lib):
    cat, vid, root, rel, ids = lib
    items = review.problems(cat)["items"]
    assert {i["path"] for i in items} == {rel[n] for n in ("truncated", "zeroed", "empty", "garbage_raw", "grey_band")}
    review.decide(cat, [ids["empty"]], review.DISCARD, "problem")
    assert rel["empty"] not in {i["path"] for i in review.problems(cat, undecided=True)["items"]}
    assert [i["path"] for i in review.problems(cat, status="warning")["items"]] == [rel["grey_band"]]

    previews = PreviewStore(cat.previews_path)
    for name in ("zeroed", "truncated"):
        damaged = bytes.fromhex(next(i["hash"] for i in items if i["path"] == rel[name]))
        intact = review.intact_versions(cat, previews, PhashIndex(), damaged)
        assert intact and intact[0]["locations"][0]["path"] == rel["photo4"], name
        assert "identical above the damaged area" in intact[0]["why"]
    # An unrelated damaged file finds nothing to pair with.
    grey = bytes.fromhex(next(i["hash"] for i in items if i["path"] == rel["grey_band"]))
    assert all(v["locations"][0]["path"] != rel["photo4"] for v in
               review.intact_versions(cat, previews, PhashIndex(), grey))
