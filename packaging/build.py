"""Build a self-contained Image Library for the current OS.

    uv run python packaging/build.py [--skip-web] [--with-cloudflared]

Produces dist/imagelib/ (run imagelib or imagelib.exe inside it) and a zip next to it.
Windows and macOS builds must run on those systems (see .github/workflows/build.yml).
"""

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
CLOUDFLARED = "https://github.com/cloudflare/cloudflared/releases/{version}/download/{asset}"


def run(cmd, cwd=ROOT):
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True, shell=sys.platform == "win32" and cmd[0] == "npm")


def target() -> tuple[str, str]:
    osname = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return osname, arch


def fetch_cloudflared(dest_dir: Path, version: str) -> Path:
    osname, arch = target()
    cf_arch = "arm64" if arch == "arm64" else "amd64"
    asset = {"windows": f"cloudflared-windows-{cf_arch}.exe", "macos": f"cloudflared-darwin-{cf_arch}.tgz",
             "linux": f"cloudflared-linux-{cf_arch}"}[osname]
    ref = "latest" if version == "latest" else f"download/{version}"
    url = CLOUDFLARED.format(version=ref, asset=asset).replace("/download/download/", "/download/")
    print("downloading", url, flush=True)
    data = urllib.request.urlopen(url, timeout=120).read()
    print("cloudflared sha256", hashlib.sha256(data).hexdigest(), flush=True)
    out = dest_dir / ("cloudflared.exe" if osname == "windows" else "cloudflared")
    if asset.endswith(".tgz"):
        tmp = dest_dir / asset
        tmp.write_bytes(data)
        with tarfile.open(tmp) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith("cloudflared"))
            out.write_bytes(tf.extractfile(member).read())
        tmp.unlink()
    else:
        out.write_bytes(data)
    out.chmod(0o755)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-web", action="store_true", help="reuse the already built GUI")
    ap.add_argument("--with-cloudflared", action="store_true", help="bundle cloudflared for Cloudflare Tunnel")
    ap.add_argument("--cloudflared-version", default="latest")
    args = ap.parse_args()

    if not args.skip_web:
        run(["npm", "ci"], cwd=ROOT / "web")
        run(["npm", "run", "build"], cwd=ROOT / "web")
    if not (ROOT / "src" / "imagelib" / "ui" / "index.html").exists():
        sys.exit("The GUI is not built (src/imagelib/ui). Run without --skip-web.")
    run([sys.executable, str(ROOT / "packaging" / "make_icons.py")])
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(DIST),
         "--workpath", str(ROOT / "build"), str(ROOT / "packaging" / "imagelib.spec")])

    app_dir = DIST / "imagelib"
    if args.with_cloudflared:
        fetch_cloudflared(app_dir, args.cloudflared_version)
    shutil.copy(ROOT / "LICENSE", app_dir / "LICENSE.txt")
    (app_dir / "README.txt").write_text(
        "Image Library\n\nStart it by running imagelib (imagelib.exe on Windows). It opens in your web browser;\n"
        "keep the window it starts in open while you use it.\n\nCommand line: imagelib --help\n"
        + ("\nIncludes cloudflared (Apache License 2.0, https://github.com/cloudflare/cloudflared).\n"
           if args.with_cloudflared else ""), encoding="utf-8")

    from imagelib import __version__

    osname, arch = target()
    archive = DIST / f"imagelib-{__version__}-{osname}-{arch}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(app_dir.rglob("*")):
            info = zipfile.ZipInfo.from_file(f, f.relative_to(DIST))
            if f.is_file():
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, f.read_bytes())
    size = sum(f.stat().st_size for f in app_dir.rglob("*") if f.is_file())
    print(f"built {app_dir} ({size / 2**20:.0f} MB), {archive.name} ({archive.stat().st_size / 2**20:.0f} MB)")


if __name__ == "__main__":
    main()
