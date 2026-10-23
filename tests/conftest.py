import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
os.environ["IMAGELIB_TEST_HOOKS"] = "1"  # inherited by spawned analysis workers

import samplelib  # noqa: E402

from imagelib import volumes  # noqa: E402
from imagelib.catalog import Catalog  # noqa: E402
from imagelib.previews import PreviewStore  # noqa: E402
from imagelib.scanner import Scanner  # noqa: E402


@pytest.fixture(scope="session")
def sample_template(tmp_path_factory):
    root = tmp_path_factory.mktemp("template") / "drive"
    paths = samplelib.build(root)
    return root, {k: v.relative_to(root).as_posix() for k, v in paths.items()}


@pytest.fixture
def drive(sample_template, tmp_path):
    """A fresh copy of the sample library: (root, {name: rel_path})."""
    template, rel = sample_template
    root = tmp_path / "drive"
    shutil.copytree(template, root, copy_function=shutil.copy2)
    return root, rel


@pytest.fixture
def catalog(tmp_path):
    cat = Catalog(tmp_path / "catalog")
    yield cat
    cat.close()


def register(cat: Catalog, root: Path, label: str = "Sample") -> str:
    marker = volumes.init_volume(root, label, cat.machine_id)
    cat.upsert_volume(marker.id, label, None, None)
    return marker.id


def scan(cat: Catalog, root: Path, volume_id: str, subpath: str = "", **kw):
    scanner = Scanner(cat, PreviewStore(cat.previews_path), workers=2, **kw)
    target = scanner.prepare(volume_id, "Sample", root, subpath)
    return scanner.run([target])[target.scan_id]


def file_row(cat: Catalog, volume_id: str, rel: str):
    return cat.conn.execute(
        "SELECT f.*, c.status, c.problems, c.phash, c.width, c.height, c.format, c.has_preview "
        "FROM files f LEFT JOIN contents c ON c.hash=f.content_hash "
        "WHERE f.volume_id=? AND f.rel_path=?", (volume_id, rel)).fetchone()
