"""Standard Ahmed body generator. Needs CadQuery (skipped otherwise)."""
import sys
from pathlib import Path

import pytest

cq = pytest.importorskip("cadquery")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import inspect_geometry as ig  # noqa: E402
import make_ahmed as ma  # noqa: E402


@pytest.mark.parametrize("angle", [25, 35])
def test_dimensions_and_volume(angle):
    """The built body passes the generator's own check (bounding box, volume, valid solid) at 25 and 35 degrees."""
    info = ma.check(ma.build(angle), angle)
    assert info["problems"] == []


@pytest.mark.parametrize("angle", [25, 35])
def test_stl_is_clean_and_slanted_at_the_rear(tmp_path, angle):
    """The STL is one closed manifold body, the rear face is as high as the slant leaves it, and the front reaches roof
    height.
    """
    out = tmp_path / f"a{angle}.stl"
    assert ma.main(["--slant", str(angle), "--out", str(out)]) == 0
    r = ig.inspect(out, units="m")
    assert r["status"] in ("PASS", "WARN")
    g = r["geometry"]
    assert len(g["bodies"]) == 1 and g["open_edges"] == 0 and g["non_manifold_edges"] == 0
    tris = ig.read_stl(out).reshape(-1, 3)
    dx, dz = ma.slant_geometry(angle)
    rear = tris[tris[:, 0] > -1e-4]                           # points on the rear face
    assert rear[:, 2].max() == pytest.approx(ma.CLEARANCE + ma.H - dz, abs=1e-3)
    # everything ahead of the slant reaches roof height (a flat roof only has points at its corners,
    # so test the whole region rather than a narrow band)
    front_top = tris[tris[:, 0] < -dx - 0.01]
    assert front_top[:, 2].max() == pytest.approx(ma.CLEARANCE + ma.H, abs=1e-3)
