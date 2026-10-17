"""Volume identity.

Normally a small marker file at the root of each registered drive: it survives drive
letters changing and the drive moving between machines and operating systems.

Drives that must never be written to are registered *read-only* instead and are
recognised by filesystem fingerprints (see ``fsid``). That is slightly weaker: a
reformat changes the fingerprint and a cloned drive shares it.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import fsid

MARKER_NAME = ".imagelib-volume.json"
QUARANTINE_DIR = "_imagelib_quarantine"


@dataclass(frozen=True)
class VolumeMarker:
    id: str
    label: str
    root: Path


class VolumeError(Exception):
    pass


def read_marker(root: Path) -> VolumeMarker | None:
    try:
        data = json.loads((Path(root) / MARKER_NAME).read_text(encoding="utf-8"))
        return VolumeMarker(id=str(uuid.UUID(data["id"])), label=data.get("label", ""), root=Path(root))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def find_volume_root(path: Path) -> VolumeMarker | None:
    """Find the registered volume containing ``path`` by searching upward for a marker."""
    path = Path(path).resolve()
    for candidate in (path, *path.parents):
        marker = read_marker(candidate)
        if marker:
            return marker
    return None


def init_volume(root: Path, label: str, machine_id: str) -> VolumeMarker:
    """Write a marker at ``root`` (or return the existing one, relabelled)."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise VolumeError(f"{root} is not a directory")
    enclosing = find_volume_root(root)
    if enclosing and enclosing.root != root:
        raise VolumeError(
            f"{root} is inside volume '{enclosing.label}' rooted at {enclosing.root}; "
            "scan a sub-folder of that volume instead of registering a new one"
        )
    existing = read_marker(root)
    volume_id = existing.id if existing else str(uuid.uuid4())
    data = {
        "id": volume_id,
        "label": label,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_by_machine": machine_id,
        "app": "imagelib",
        "about": "Identifies this drive in the imagelib photo catalog. Safe to keep; deleting it "
                 "means the drive must be registered again.",
    }
    tmp = root / (MARKER_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, root / MARKER_NAME)
    except OSError as e:
        raise VolumeError(f"Cannot write volume marker to {root}: {e}") from e
    return VolumeMarker(id=volume_id, label=label, root=root)


def total_bytes(root: Path) -> int | None:
    try:
        return shutil.disk_usage(root).total
    except OSError:
        return None


def candidate_mount_roots() -> list[Path]:
    """Places where removable drives usually appear on this OS."""
    roots: list[Path] = []
    if sys.platform == "win32":
        import string

        roots = [Path(f"{letter}:\\") for letter in string.ascii_uppercase]
    else:
        bases = [Path("/Volumes")] if sys.platform == "darwin" else [Path("/mnt"), Path("/media")]
        user = os.environ.get("USER", "")
        if sys.platform != "darwin" and user:
            bases += [Path("/media") / user, Path("/run/media") / user]
        for base in bases:
            try:
                roots += [p for p in base.iterdir() if p.is_dir()]
            except OSError:
                pass
    return roots


def locate_online(catalog) -> dict[str, Path]:
    """Map volume id -> current root for every catalogued volume reachable now.

    Each volume's last seen mount path is checked first, so volumes registered at
    arbitrary folders are found too; then the usual mount locations are searched.
    """
    refs = [volume_ref(catalog, row) for row in catalog.list_volumes()]
    last_paths = {row["id"]: row["last_seen_path"] for row in catalog.list_volumes()}
    found: dict[str, Path] = {}
    for ref in refs:
        last = last_paths.get(ref.id)
        if last and ref.present_at(Path(last)):
            found[ref.id] = Path(last)
    missing = [ref for ref in refs if ref.id not in found]
    if not missing:
        return found
    for root in candidate_mount_roots():
        marker = read_marker(root)
        if marker:
            if any(m.id == marker.id and not m.read_only for m in missing):
                found[marker.id] = root
            continue
        if any(m.read_only for m in missing):
            ident = fsid.identify(root)
            for m in missing:
                if m.read_only and m.id not in found and ident.matches(set(m.fingerprints), m.total_bytes):
                    found[m.id] = root
    return found


def marker_present(root: Path, volume_id: str) -> bool:
    marker = read_marker(root)
    return marker is not None and marker.id == volume_id


@dataclass(frozen=True)
class VolumeRef:
    """What is needed to recognise a catalogued volume on disk."""
    id: str
    label: str
    read_only: bool
    fingerprints: frozenset = frozenset()
    total_bytes: int | None = None

    def present_at(self, root: Path) -> bool:
        if self.read_only:
            return fsid.identify(root).matches(set(self.fingerprints), self.total_bytes)
        return marker_present(root, self.id)


def volume_ref(catalog, row) -> VolumeRef:
    read_only = bool(row["read_only"])
    return VolumeRef(row["id"], row["label"], read_only,
                     frozenset(catalog.fingerprints(row["id"])) if read_only else frozenset(),
                     row["total_bytes"])


def register_read_only(catalog, path: Path, label: str | None) -> VolumeRef:
    """Register the filesystem containing ``path`` without writing anything to it."""
    root = fsid.mount_root(path)
    if read_marker(root):
        raise VolumeError(f"{root} already has an imagelib marker; register it normally instead")
    ident = fsid.identify(root)
    if not ident.fingerprints:
        raise VolumeError(f"Cannot identify the drive at {root} without writing to it on this OS. "
                          "Register it on another OS, or allow a marker file.")
    existing = catalog.volumes_matching(ident)
    volume_id = existing[0]["id"] if existing else str(uuid.uuid4())
    label = label or (existing[0]["label"] if existing else None) or ident.label or root.name or str(root)
    catalog.upsert_volume(volume_id, label, ident.fs_type, ident.total_bytes, read_only=True)
    catalog.add_fingerprints(volume_id, ident.fingerprints)
    catalog.mark_volume_seen(volume_id, str(root))
    return volume_ref(catalog, catalog.get_volume(volume_id))


def resolve_path(catalog, path: Path) -> tuple[VolumeRef, Path] | None:
    """The catalogued volume containing ``path`` and its current root, if any."""
    marker = find_volume_root(path)
    if marker:
        row = catalog.get_volume(marker.id)
        if row is None:  # registered by another machine's catalog
            catalog.upsert_volume(marker.id, marker.label, None, total_bytes(marker.root))
            row = catalog.get_volume(marker.id)
        return volume_ref(catalog, row), marker.root
    root = fsid.mount_root(path)
    matches = catalog.volumes_matching(fsid.identify(root))
    if len(matches) == 1:
        return volume_ref(catalog, matches[0]), root
    return None
