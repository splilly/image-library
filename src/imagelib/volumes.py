"""Volume identity: a small marker file at the root of each registered drive.

The marker survives drive letters changing, the drive moving between machines and
between operating systems, which a mount path or OS-specific serial number does not.
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


def locate_online(known: dict[str, str | None]) -> dict[str, Path]:
    """Map volume id -> current root for every registered volume that is reachable now.

    ``known`` maps volume id -> last seen mount path, which is checked first so that
    volumes registered at arbitrary folders are found too.
    """
    found: dict[str, Path] = {}
    for volume_id, last_path in known.items():
        if last_path:
            marker = read_marker(Path(last_path))
            if marker and marker.id == volume_id:
                found[volume_id] = marker.root
    if len(found) < len(known):
        for root in candidate_mount_roots():
            marker = read_marker(root)
            if marker and marker.id in known and marker.id not in found:
                found[marker.id] = marker.root
    return found


def marker_present(root: Path, volume_id: str) -> bool:
    marker = read_marker(root)
    return marker is not None and marker.id == volume_id
