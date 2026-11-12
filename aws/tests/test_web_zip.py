"""The browser's zip maker (web/zip.js) must produce zips that Python's zip reader and the server's check accept."""
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import cfd_common as cc

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
ROOT = Path(__file__).parent


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Run the Node script once and return the folder it filled."""
    out = tmp_path_factory.mktemp("zips")
    done = subprocess.run(["node", str(ROOT / "web" / "make_zips.js"), str(out)], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    return out


@pytest.mark.parametrize("label", ["small", "medium", "stored", "odd_name"])
def test_zip_is_valid_and_matches_the_original(built, label):
    """Python reads the zip, the checksum passes, the content is identical and the server's check accepts it."""
    path, original = built / f"{label}.zip", (built / f"{label}.step").read_bytes()
    name = (built / f"{label}.name").read_text()
    with zipfile.ZipFile(path) as zf:
        assert zf.testzip() is None
        assert [i.filename for i in zf.infolist()] == [name]
        assert zf.read(name) == original
        assert zf.infolist()[0].date_time[:3] == (2026, 11, 12)
    data = path.read_bytes()
    info = cc.inspect_step_zip(lambda a, b: data[a:b + 1], len(data), 10**9)
    assert info["entry"] == name and info["uncompressed"] == len(original)


def test_deflate_is_used_when_asked_and_stored_otherwise(built):
    """The default path compresses (a real STEP file shrinks several times); the fallback stores it as it is."""
    with zipfile.ZipFile(built / "medium.zip") as zf:
        i = zf.infolist()[0]
        assert i.compress_type == zipfile.ZIP_DEFLATED and 3 < i.file_size / i.compress_size < 100
    with zipfile.ZipFile(built / "stored.zip") as zf:
        assert zf.infolist()[0].compress_type == zipfile.ZIP_STORED


def test_system_unzip_agrees(built):
    """The standard unzip program (if present) also accepts every zip."""
    if shutil.which("unzip") is None:
        pytest.skip("unzip is not installed")
    for label in ("small", "medium", "stored", "odd_name"):
        done = subprocess.run(["unzip", "-tq", str(built / f"{label}.zip")], capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("label", ["small", "medium", "stored", "odd_name"])
def test_local_header_agrees_with_the_directory(built, label):
    """Some readers use the header in front of the data instead of the directory at the end; both must agree."""
    import struct
    raw = (built / f"{label}.zip").read_bytes()
    sig, need, flags, method, _t, _d, crc, packed, size, n_name, n_extra = struct.unpack("<IHHHHHIIIHH", raw[:30])
    assert sig == 0x04034B50 and flags == 0 and n_extra == 0
    with zipfile.ZipFile(built / f"{label}.zip") as zf:
        i = zf.infolist()[0]
        assert (method, crc, packed, size) == (i.compress_type, i.CRC, i.compress_size, i.file_size)
        assert raw[30:30 + n_name].decode() == i.filename and i.header_offset == 0
