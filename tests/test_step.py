"""STEP-input tests. Skipped automatically if CadQuery is not installed.

Expected results mirror what was observed when the STEP path was first run by hand
(slab, filleted slab, two boxes, sphere). Run with:  python -m pytest -v
"""
import math
import sys
from pathlib import Path

import pytest

cq = pytest.importorskip("cadquery")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import inspect_geometry as ig  # noqa: E402


def make_step(tmp_path, name, workplane):
    """Export a CadQuery workplane to a STEP file in tmp_path and return the path."""
    path = tmp_path / name
    cq.exporters.export(workplane, str(path))
    return path


def warn_codes(result):
    """Set of check codes that are warnings in an inspection result."""
    return {c["code"] for c in result["checks"] if c["level"] == "WARN"}


def test_step_box_is_exact(tmp_path):
    """A STEP box is one solid whose mesh volume and CAD volume agree, with no CAD-volume warning."""
    p = make_step(tmp_path, "slab.step", cq.Workplane("XY").box(300, 1000, 36))
    r = ig.inspect(p, units="mm")
    g = r["geometry"]
    assert r["status"] == "PASS"
    assert len(g["bodies"]) == 1 and g["triangles_read"] == 12
    assert r["step"]["solids"] == 1
    assert r["step"]["cad_volume"] == pytest.approx(300 * 1000 * 36, rel=1e-6)
    assert g["bodies"][0]["volume"] == pytest.approx(300 * 1000 * 36, rel=1e-6)
    assert "CAD_VOLUME" not in warn_codes(r)


def test_step_box_without_units_needs_confirmation(tmp_path):
    """A STEP file without declared units asks for confirmation and guesses millimetres."""
    p = make_step(tmp_path, "slab.step", cq.Workplane("XY").box(300, 1000, 36))
    r = ig.inspect(p)
    assert r["status"] == "NEEDS_UNITS" and r["units"]["guessed"] == "mm"


def test_step_curved_part_stays_closed(tmp_path):
    """A filleted plate with a hole tessellates to one closed manifold body."""
    part = (cq.Workplane("XY").box(300, 1000, 36).edges("|Z").fillet(20)
            .faces(">Z").workplane().hole(60))
    r = ig.inspect(make_step(tmp_path, "filleted.step", part), units="mm")
    g = r["geometry"]
    assert r["status"] == "PASS"
    assert len(g["bodies"]) == 1
    assert g["open_edges"] == 0 and g["non_manifold_edges"] == 0 and g["inconsistent_edges"] == 0


def test_step_two_separate_solids_warn(tmp_path):
    """Two separate solids in one STEP file give two bodies and a warning."""
    a = cq.Workplane("XY").box(100, 100, 100)
    two = a.union(a.translate((500, 0, 0)))
    r = ig.inspect(make_step(tmp_path, "two.step", two), units="mm")
    assert len(r["geometry"]["bodies"]) == 2
    assert r["status"] == "WARN" and "BODIES" in warn_codes(r)


def test_step_sphere_is_closed_and_volume_close(tmp_path):
    """A sphere tessellates closed, within 1% of the exact volume, with only the expected pole slivers."""
    r = ig.inspect(make_step(tmp_path, "sphere.step", cq.Workplane("XY").sphere(50)), units="mm")
    g = r["geometry"]
    assert g["open_edges"] == 0 and len(g["bodies"]) == 1
    assert g["degenerate_triangles"] <= 10          # pole triangles are expected
    exact = 4.0 / 3.0 * math.pi * 50**3
    assert g["bodies"][0]["volume"] == pytest.approx(exact, rel=0.01)
    assert "CAD_VOLUME" not in warn_codes(r)


def test_step_export_in_metres(tmp_path):
    """A STEP file in millimetres is exported as an STL in metres."""
    p = make_step(tmp_path, "slab.step", cq.Workplane("XY").box(300, 1000, 36))
    out = tmp_path / "slab_m.stl"
    r = ig.inspect(p, units="mm", export_stl=out)
    assert r["status"] == "PASS" and out.exists()
    pts = ig.read_stl(out).reshape(-1, 3)
    size = pts.max(axis=0) - pts.min(axis=0)
    assert sorted(size.round(4).tolist()) == [0.036, 0.3, 1.0]


def _touching_boxes(tmp_path):
    """STEP file of two cubes sharing a face, as separate solids of one compound."""
    a = cq.Workplane("XY").box(100, 100, 100)
    b = cq.Workplane("XY").box(100, 100, 100).translate((100, 0, 0))   # shares a face with a
    comp = cq.Workplane("XY").newObject([cq.Compound.makeCompound([a.val(), b.val()])])
    return make_step(tmp_path, "touch.step", comp)


def test_step_touching_solids_fail_then_contact_warn(tmp_path):
    """Touching solids fail by default, and become a CONTACT warning (with problem edges written) when contact is
    allowed.
    """
    p = _touching_boxes(tmp_path)
    r = ig.inspect(p, units="mm")
    assert r["step"]["solids"] == 2
    assert r["status"] == "FAIL"
    g = r["geometry"]["non_manifold_groups"]
    assert g and all(x["kind"] == "between_solids" and x["solids"] == [1, 2] for x in g)
    r2 = ig.inspect(p, units="mm", allow_contact=True, problem_edges_vtk=tmp_path / "e.vtk")
    assert r2["status"] == "WARN" and "CONTACT" in warn_codes(r2)
    assert (tmp_path / "e.vtk").exists()


def test_step_per_solid_report(tmp_path):
    """The report lists each solid separately and says whether it is closed."""
    r = ig.inspect(_touching_boxes(tmp_path), units="mm")
    assert len(r["solids"]) == 2 and all(s["closed"] for s in r["solids"])
