"""fix_geometry.py: overlapping and touching parts become one solid (needs CadQuery; skipped without it)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
cq = pytest.importorskip("cadquery")
import fix_geometry as fg  # noqa: E402
import inspect_geometry as ig  # noqa: E402


def write_assembly(path: Path):
    """Three parts in mm: two overlapping boxes, a third touching face-to-face, and a tiny 'bolt'."""
    a = cq.Workplane("XY").box(100, 40, 20, centered=False)
    b = cq.Workplane("XY").box(100, 40, 20, centered=False).translate((50, 0, 10))     # overlaps a
    c = cq.Workplane("XY").box(30, 40, 20, centered=False).translate((150, 0, 10))    # touches b
    bolt = cq.Workplane("XY").box(0.01, 0.01, 0.01, centered=False).translate((20, 20, 20))
    asm = cq.Assembly().add(a, name="a").add(b, name="b").add(c, name="c").add(bolt, name="bolt")
    asm.save(str(path), "STEP")


def test_assembly_becomes_one_clean_solid(tmp_path):
    """Overlapping and touching parts are fused into one valid solid of the right volume, in metres.

    The overlap is counted once, the tiny bolt is dropped, and the written STL is closed, manifold and a single body.
    """
    step = tmp_path / "asm.step"
    write_assembly(step)
    out = tmp_path / "asm_fixed.stl"
    assert fg.main([str(step), "--out", str(out)]) == 0
    rep = json.loads(out.with_suffix(".json").read_text())
    assert rep["solids_in"] >= 3 and rep["solids_out"] == 1 and rep["status"] == "PASS"
    expected = 100 * 40 * 20 * 2 - 50 * 40 * 10 + 30 * 40 * 20                         # overlap counted once
    assert rep["volume_mm3"] == pytest.approx(expected, rel=1e-3)
    r = ig.inspect(out, units="m")
    g = r["geometry"]
    assert len(g["bodies"]) == 1 and g["open_edges"] == 0 and g["non_manifold_edges"] == 0
    assert ig.read_stl(out).reshape(-1, 3).max() == pytest.approx(0.18, abs=1e-6)      # mm -> m
