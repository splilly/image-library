"""Identify a filesystem without writing to it, for drives registered read-only.

The fingerprint that travels best between operating systems is the 32-bit volume
serial number that Windows shows as ``XXXX-XXXX``:

* Windows reports it directly (and WSL can ask Windows for it);
* Linux reports it as the exFAT/FAT UUID, and as the low half of the 64-bit NTFS UUID;
* macOS does not expose it, so its own volume UUID is recorded as well.

Serial numbers change when a drive is reformatted, and cloned drives share them, so
a match also requires the capacity to agree.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

SERIAL32 = "serial32"         # 'XXXX-XXXX'
FS_UUID = "fs_uuid"           # Linux filesystem UUID (ext4, btrfs, full NTFS id, ...)
MAC_VOLUME_UUID = "mac_volume_uuid"

CAPACITY_TOLERANCE = 0.02     # different OSes report slightly different totals


@dataclass
class FsIdentity:
    fingerprints: set[tuple[str, str]] = field(default_factory=set)
    label: str | None = None
    fs_type: str | None = None
    total_bytes: int | None = None

    def matches(self, fingerprints: set[tuple[str, str]], total_bytes: int | None) -> bool:
        if not self.fingerprints & fingerprints:
            return False
        if total_bytes and self.total_bytes:
            return abs(self.total_bytes - total_bytes) <= CAPACITY_TOLERANCE * total_bytes
        return True


def mount_root(path: Path) -> Path:
    """The root of the filesystem containing ``path`` (a drive letter root on Windows)."""
    path = Path(path).resolve()
    if sys.platform == "win32":
        return Path(path.anchor)
    for candidate in (path, *path.parents):
        if os.path.ismount(candidate):
            return candidate
    return Path(path.anchor)


def identify(root: Path) -> FsIdentity:
    """Fingerprint the filesystem mounted at ``root``; empty if it cannot be identified."""
    root = Path(root)
    ident = FsIdentity()
    try:
        import shutil

        ident.total_bytes = shutil.disk_usage(root).total
    except OSError:
        return ident  # not reachable: nothing to identify
    try:
        if sys.platform == "win32":
            _windows(root, ident)
        elif sys.platform == "darwin":
            _macos(root, ident)
        elif _is_wsl():
            if not _wsl(root, ident):
                _linux(root, ident)
        else:
            _linux(root, ident)
    except Exception:
        pass  # identification is best effort; an empty result means "unknown"
    return ident


def _serial32(value: int) -> str:
    return f"{value >> 16:04X}-{value & 0xFFFF:04X}"


# -- Windows -------------------------------------------------------------------

def _windows(root: Path, ident: FsIdentity) -> None:
    import ctypes

    label = ctypes.create_unicode_buffer(261)
    fs_name = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(str(root)), label, len(label), ctypes.byref(serial), None, None, fs_name, len(fs_name))
    if ok:
        ident.fingerprints.add((SERIAL32, _serial32(serial.value)))
        ident.label = label.value or None
        ident.fs_type = fs_name.value or None


# -- WSL (Windows drives mounted with drvfs) ------------------------------------

def _is_wsl() -> bool:
    return "microsoft" in platform.release().lower()


def _mountinfo_entry(root: Path) -> tuple[str, str] | None:
    """(fs type, source) of the mount at ``root``; the last entry wins for stacked mounts."""
    found = None
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                fields = left.split()
                if len(fields) > 4 and _unescape(fields[4]) == str(root):
                    rfields = right.split()
                    found = (rfields[0], _unescape(rfields[1]))
    except OSError:
        return None
    return found


def _unescape(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _wsl(root: Path, ident: FsIdentity) -> bool:
    entry = _mountinfo_entry(root)
    if not entry or entry[0] not in ("9p", "drvfs"):
        return False
    m = re.match(r"([A-Za-z]):", entry[1])
    if not m:
        return False
    letter = m.group(1).upper()
    cmd = "/mnt/c/Windows/System32/cmd.exe"
    # cwd on a Windows drive avoids cmd's "UNC paths are not supported" warning.
    out = subprocess.run([cmd, "/c", "vol", f"{letter}:"], capture_output=True, timeout=15,
                         cwd="/mnt/c" if os.path.isdir("/mnt/c") else None).stdout.decode(errors="replace")
    serial = re.search(r"\b([0-9A-F]{4}-[0-9A-F]{4})\b", out)
    if not serial:
        return False
    ident.fingerprints.add((SERIAL32, serial.group(1)))
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    # First line is "Volume in drive D is LABEL" (localised; take the text after the last " is ").
    if lines and " is " in lines[0]:
        ident.label = lines[0].rsplit(" is ", 1)[1].strip() or None
    return True


# -- Linux ---------------------------------------------------------------------

def _linux(root: Path, ident: FsIdentity) -> None:
    entry = _mountinfo_entry(root)
    if not entry:
        return
    fs_type, source = entry
    ident.fs_type = fs_type
    if not source.startswith("/dev/"):
        return
    device = os.path.realpath(source)
    by_uuid = Path("/dev/disk/by-uuid")
    for link in by_uuid.iterdir() if by_uuid.is_dir() else []:
        if os.path.realpath(link) == device:
            uuid = link.name.upper()
            ident.fingerprints.add((FS_UUID, uuid))
            if re.fullmatch(r"[0-9A-F]{4}-[0-9A-F]{4}", uuid):        # exFAT / FAT
                ident.fingerprints.add((SERIAL32, uuid))
            elif re.fullmatch(r"[0-9A-F]{16}", uuid):                # NTFS
                ident.fingerprints.add((SERIAL32, _serial32(int(uuid[8:], 16))))
            break
    by_label = Path("/dev/disk/by-label")
    for link in by_label.iterdir() if by_label.is_dir() else []:
        if os.path.realpath(link) == device:
            ident.label = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), link.name)
            break


# -- macOS -----------------------------------------------------------------------

def _macos(root: Path, ident: FsIdentity) -> None:
    import plistlib

    out = subprocess.run(["diskutil", "info", "-plist", str(root)], capture_output=True, timeout=15).stdout
    info = plistlib.loads(out)
    if info.get("VolumeUUID"):
        ident.fingerprints.add((MAC_VOLUME_UUID, info["VolumeUUID"].upper()))
    ident.label = info.get("VolumeName") or None
    ident.fs_type = info.get("FilesystemType") or None
