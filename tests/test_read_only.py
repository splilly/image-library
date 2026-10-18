"""Read-only volumes: catalogued and scanned without writing a single byte to them."""

import os
from pathlib import Path

import pytest
from conftest import file_row, scan

from imagelib import fsid, volumes
from imagelib.catalog import SCHEMA_VERSION
from imagelib.fsid import SERIAL32, FsIdentity

TB = 1000 ** 4


@pytest.fixture
def mounts(monkeypatch):
    """A fake OS: {mount root: identity}. Paths not in it are unidentifiable."""
    table: dict[Path, FsIdentity] = {}

    def mount_root(path):
        path = Path(path).resolve()
        for root in table:
            if path == root or root in path.parents:
                return root
        return Path(path.anchor)

    monkeypatch.setattr(fsid, "identify", lambda root: table.get(Path(root).resolve(), FsIdentity()))
    monkeypatch.setattr(fsid, "mount_root", mount_root)
    monkeypatch.setattr(volumes, "candidate_mount_roots", lambda: list(table))
    return table


def snapshot(root: Path) -> dict:
    """Every path under root with its size and mtime: proof that nothing was written."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            st = os.lstat(os.path.join(dirpath, name))
            out[os.path.relpath(os.path.join(dirpath, name), root)] = (st.st_size, st.st_mtime_ns)
    return out


def t5(serial="066A-1808", total=8 * TB):
    return FsIdentity({(SERIAL32, serial)}, label="T5 EVO", total_bytes=total)


def test_read_only_volume_is_registered_and_scanned_without_writing(drive, catalog, mounts):
    root, rel = drive
    mounts[root.resolve()] = t5()
    before = snapshot(root)

    ref = volumes.register_read_only(catalog, root / "photos", None)  # any path on the drive
    assert ref.read_only and ref.label == "T5 EVO"
    assert catalog.get_volume(ref.id)["last_seen_path"] == str(root.resolve())
    prog = scan(catalog, root.resolve(), ref.id)

    assert prog.state == "done" and prog.files_done == 22
    assert not (root / volumes.MARKER_NAME).exists()
    assert snapshot(root) == before
    assert file_row(catalog, ref.id, rel["zeroed"])["status"] == "corrupt"


def test_registering_the_same_drive_again_reuses_the_volume(drive, catalog, mounts):
    root, _ = drive
    mounts[root.resolve()] = t5()
    first = volumes.register_read_only(catalog, root, "My T5")
    again = volumes.register_read_only(catalog, root, None)
    assert again.id == first.id and again.label == "My T5"
    assert len(catalog.list_volumes()) == 1


def test_read_only_drive_is_found_at_a_new_mount_point_and_scan_resumes(drive, catalog, mounts, tmp_path):
    from imagelib.scanner import Scanner
    from imagelib.previews import PreviewStore
    import imagelib.discover as discover

    root, _ = drive
    root = root.resolve()
    mounts[root] = t5()
    ref = volumes.register_read_only(catalog, root, None)
    scanner = Scanner(catalog, PreviewStore(catalog.previews_path), workers=2)
    target = scanner.prepare(ref.id, ref.label, root)
    conn = catalog.connect()
    assert discover.discover(conn, ref.id, root, "", target.scan_id, lambda: False, lambda s: None,
                             target.present)
    conn.execute("UPDATE scans SET discovered=1 WHERE id=?", (target.scan_id,))

    # Unplug: the files vanish and the OS no longer identifies the drive there.
    del mounts[root]
    replugged = tmp_path / "E_drive"
    root.rename(replugged)
    prog = scanner.run([target])[target.scan_id]
    assert prog.state == "offline"
    assert {r[0] for r in catalog.conn.execute("SELECT DISTINCT state FROM files")} == {"pending"}

    # Re-plug at another drive letter.
    mounts[replugged] = t5()
    assert volumes.locate_online(catalog) == {ref.id: replugged}
    prog = scan(catalog, replugged, ref.id)
    assert prog.state == "done" and prog.files_done == 22


def test_a_different_drive_with_the_same_serial_is_not_mistaken_for_it(drive, catalog, mounts, tmp_path):
    root, _ = drive
    mounts[root.resolve()] = t5()
    volumes.register_read_only(catalog, root, None)

    other = tmp_path / "other"
    other.mkdir()
    mounts[other] = t5(total=2 * TB)  # same serial (e.g. a clone or coincidence), different capacity
    assert volumes.resolve_path(catalog, other) is None
    del mounts[root.resolve()]
    assert volumes.locate_online(catalog) == {}


def test_unidentifiable_drive_is_refused_rather_than_guessed(drive, catalog, mounts):
    root, _ = drive
    with pytest.raises(volumes.VolumeError, match="Cannot identify"):
        volumes.register_read_only(catalog, root, None)


def test_old_catalogs_are_upgraded(tmp_path):
    import sqlite3

    from imagelib.catalog import _SCHEMA, Catalog

    path = tmp_path / "old"
    path.mkdir()
    conn = sqlite3.connect(path / "catalog.db")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO volumes(id, label, created_at) VALUES('v1', 'Old drive', 0)")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()

    cat = Catalog(path)
    assert cat.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    row = cat.get_volume("v1")
    assert row["label"] == "Old drive" and row["read_only"] == 0


@pytest.mark.skipif(not os.path.isdir("/mnt/c/Windows"), reason="needs WSL with a Windows C: drive")
def test_real_wsl_drive_identity():
    ident = fsid.identify(Path("/mnt/c"))
    assert any(kind == SERIAL32 for kind, _ in ident.fingerprints)
    assert ident.total_bytes
