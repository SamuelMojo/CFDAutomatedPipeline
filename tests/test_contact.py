"""Tests for non-manifold localisation, contact handling and problem-edge export.
NumPy only: solids are tessellated by hand and passed in with group ids."""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import geometry_fixtures as fx  # noqa: E402
import inspect_geometry as ig  # noqa: E402


def two_cubes(offset):
    """Two unit cubes, the second moved by offset, as one triangle array plus a solid id (0 or 1) for every triangle."""
    a = fx.cube(1.0)
    b = fx.cube(1.0, origin=offset)
    tris = np.concatenate([a, b])
    groups = np.concatenate([np.zeros(len(a), int), np.ones(len(b), int)])
    return tris, groups


def test_face_contact_is_between_solids():
    # cube B shares the whole x=1 face with A: 4 square edges are shared by 4 triangles
    """Two cubes sharing a whole face give non-manifold edges that are all reported as contact between solids 1 and 2.
    """
    tris, groups = two_cubes((1.0, 0.0, 0.0))
    m = ig.analyse_mesh(tris, groups=groups)
    assert m["non_manifold_edges"] >= 1
    assert all(g["kind"] == "between_solids" and g["solids"] == [1, 2] for g in m["non_manifold_groups"])
    assert sum(g["edges"] for g in m["non_manifold_groups"]) == m["non_manifold_edges"]


def test_no_groups_gives_unknown_kind():
    """Without solid ids the origin of a non-manifold edge cannot be known, so its kind is 'unknown'."""
    tris, _ = two_cubes((1.0, 0.0, 0.0))
    m = ig.analyse_mesh(tris)
    assert {g["kind"] for g in m["non_manifold_groups"]} == {"unknown"}


def test_edge_contact_between_solids():
    # share exactly one edge (x=1,y=1)
    """Two cubes sharing a single edge are still contact between solids."""
    tris, groups = two_cubes((1.0, 1.0, 0.0))
    m = ig.analyse_mesh(tris, groups=groups)
    assert m["non_manifold_edges"] >= 1
    assert {g["kind"] for g in m["non_manifold_groups"]} == {"between_solids"}


def test_within_solid_fault_is_not_contact():
    # one solid made of two cubes touching: same group id -> within_solid
    """Touching parts of one solid (same id) are a fault within that solid, not contact."""
    tris, _ = two_cubes((1.0, 1.0, 0.0))
    m = ig.analyse_mesh(tris, groups=np.zeros(len(tris), int))
    assert {g["kind"] for g in m["non_manifold_groups"]} == {"within_solid"}


def checks(m, **kw):
    """Gate levels by code from evaluate() for a mesh analysis."""
    c, _ = ig.evaluate(m, "m", False, None, **kw)
    return {x["code"]: x["level"] for x in c}


def test_allow_contact_downgrades_only_between_solids():
    """allow_contact turns contact between solids into a warning but leaves a fault within a solid as FAIL."""
    tris, groups = two_cubes((1.0, 0.0, 0.0))
    m = ig.analyse_mesh(tris, groups=groups)
    assert checks(m)["NON_MANIFOLD"] == "FAIL"
    assert checks(m, allow_contact=True)["CONTACT"] == "WARN"
    m2 = ig.analyse_mesh(tris, groups=np.zeros(len(tris), int))
    assert checks(m2, allow_contact=True)["NON_MANIFOLD"] == "FAIL"


def test_cli_contact_and_problem_edges(tmp_path):
    """The problem-edge VTK export has a valid header, one 2-point line per edge, and the right solid id."""
    tris, groups = two_cubes((1.0, 0.0, 0.0))
    m = ig.analyse_mesh(tris, groups=groups)
    internal = m.pop("_internal")
    out = tmp_path / "e.vtk"
    ig.write_vtk_segments(out, internal["nm_segments"], internal["nm_solids"])
    lines = out.read_text().splitlines()
    n = len(internal["nm_segments"])
    assert lines[0].startswith("# vtk DataFile") and f"POINTS {2 * n} float" in lines
    assert f"LINES {n} {3 * n}" in lines
    assert all(s == 2 for s in internal["nm_solids"])


def test_degenerate_stripped_on_export(tmp_path):
    """A zero-area triangle gives a WARN, and the exported STL is left with only the 12 cube triangles."""
    p = tmp_path / "d.stl"
    ig.write_stl_binary(p, fx.with_degenerate(fx.cube(1.0)))
    out = tmp_path / "o.stl"
    r = ig.inspect(p, units="m", export_stl=out)
    assert r["status"] == "WARN"
    assert len(ig.read_stl(out)) == 12


def test_stl_nonmanifold_fail_hint(tmp_path):
    """An STL has no solid ids, so contact cannot be proven and the file still FAILs even with allow_contact."""
    tris, _ = two_cubes((1.0, 0.0, 0.0))
    p = tmp_path / "t.stl"
    ig.write_stl_binary(p, tris)
    r = ig.inspect(p, units="m", allow_contact=True)
    assert r["status"] == "FAIL"  # STL carries no solid info, contact cannot be proven
