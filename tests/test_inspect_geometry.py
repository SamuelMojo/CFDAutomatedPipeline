"""Tests for src/inspect_geometry.py. Run with:  python -m pytest -v"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import geometry_fixtures as fx  # noqa: E402
import inspect_geometry as ig  # noqa: E402

SCRIPT = ROOT / "src" / "inspect_geometry.py"


def run_inspect(tmp_path, tris, name="g.stl", **kw):
    """Write tris as a binary STL in tmp_path and return the inspection result for it."""
    p = tmp_path / name
    ig.write_stl_binary(p, tris)
    return ig.inspect(p, **kw)


def codes(result, level):
    """Set of check codes that have the given level (PASS, WARN or FAIL) in an inspection result."""
    return {c["code"] for c in result["checks"] if c["level"] == level}


# ---- reading ---------------------------------------------------------------
def test_ascii_and_binary_stl_read_the_same(tmp_path):
    """The same triangles read back identically from an ASCII and a binary STL."""
    tris = fx.cube()
    b = tmp_path / "b.stl"
    a = tmp_path / "a.stl"
    ig.write_stl_binary(b, tris)
    a.write_text(fx.to_ascii_stl(tris))
    np.testing.assert_allclose(ig.read_stl(a), ig.read_stl(b), atol=1e-6)


def test_garbage_file_is_an_error(tmp_path):
    """A file that is not an STL raises GeometryError."""
    p = tmp_path / "junk.stl"
    p.write_text("this is not an stl")
    with pytest.raises(ig.GeometryError):
        ig.read_stl(p)


def test_unsupported_extension(tmp_path):
    """A file type other than STL or STEP raises GeometryError."""
    p = tmp_path / "x.obj"
    p.write_text("v 0 0 0")
    with pytest.raises(ig.GeometryError):
        ig.inspect(p)


# ---- topology --------------------------------------------------------------
def test_cube_is_closed_single_body_with_unit_volume(tmp_path):
    """A unit cube is one closed body of volume 1 with no open, non-manifold or inconsistent edges."""
    r = run_inspect(tmp_path, fx.cube(), units="m")
    g = r["geometry"]
    assert r["status"] == "PASS"
    assert len(g["bodies"]) == 1
    assert g["open_edges"] == 0 and g["non_manifold_edges"] == 0 and g["inconsistent_edges"] == 0
    assert g["bodies"][0]["volume"] == pytest.approx(1.0, rel=1e-9)
    assert g["vertices_welded"] == 8


def test_open_surface_fails_and_allow_open_downgrades(tmp_path):
    """A cube with two triangles missing fails with four open edges; allow_open turns that into a warning."""
    tris = fx.open_copy(fx.cube(), drop=2)
    r = run_inspect(tmp_path, tris, units="m")
    assert r["status"] == "FAIL" and r["exit_code"] == 1
    assert "OPEN_EDGES" in codes(r, "FAIL")
    assert r["geometry"]["open_edges"] == 4
    r2 = run_inspect(tmp_path, tris, units="m", allow_open=True)
    assert r2["status"] == "WARN" and r2["exit_code"] == 0


def test_two_separate_bodies_warn(tmp_path):
    """Two separate cubes warn about multiple bodies and their volumes add up."""
    tris = np.concatenate([fx.cube(), fx.cube(origin=(5, 0, 0))])
    r = run_inspect(tmp_path, tris, units="m")
    assert len(r["geometry"]["bodies"]) == 2
    assert r["status"] == "WARN" and "BODIES" in codes(r, "WARN")
    assert r["geometry"]["volume_total"] == pytest.approx(2.0, rel=1e-9)


def test_inverted_body_warns_with_negative_volume(tmp_path):
    """A cube with reversed normals warns as inverted and has volume -1, but its winding is consistent."""
    r = run_inspect(tmp_path, fx.inverted(fx.cube()), units="m")
    assert "INVERTED" in codes(r, "WARN")
    assert r["geometry"]["bodies"][0]["volume"] == pytest.approx(-1.0, rel=1e-9)
    assert r["geometry"]["inconsistent_edges"] == 0


def test_one_flipped_triangle_is_inconsistent_winding(tmp_path):
    """One reversed triangle makes edges inconsistent and fails the WINDING check."""
    r = run_inspect(tmp_path, fx.flip_one(fx.cube(), 3), units="m")
    assert r["geometry"]["inconsistent_edges"] > 0
    assert "WINDING" in codes(r, "FAIL")


def test_degenerate_triangle_counted_and_ignored(tmp_path):
    """A zero-area triangle is counted and warned about but creates no open edges."""
    r = run_inspect(tmp_path, fx.with_degenerate(fx.cube()), units="m")
    assert r["geometry"]["degenerate_triangles"] == 1
    assert r["geometry"]["open_edges"] == 0
    assert "DEGENERATE" in codes(r, "WARN")


def test_wing_solid_volume_matches_analytic(tmp_path):
    """The wing fixture passes and its measured volume equals the analytic area times span."""
    tris, vol = fx.wing()
    r = run_inspect(tmp_path, tris, units="m")
    assert r["status"] == "PASS"
    assert r["geometry"]["bodies"][0]["volume"] == pytest.approx(vol, rel=1e-6)
    assert len(r["geometry"]["bodies"]) == 1


def test_many_bodies_labelled_correctly(tmp_path):
    """Seven separated cubes give seven closed bodies."""
    cubes = [fx.cube(origin=(3 * i, 0, 0)) for i in range(7)]
    r = run_inspect(tmp_path, np.concatenate(cubes), units="m")
    assert len(r["geometry"]["bodies"]) == 7
    assert all(b["closed"] for b in r["geometry"]["bodies"])


# ---- units -----------------------------------------------------------------
def test_missing_units_needs_confirmation(tmp_path):
    """Without declared units the result is NEEDS_UNITS (exit code 3)."""
    r = run_inspect(tmp_path, fx.cube())
    assert r["status"] == "NEEDS_UNITS" and r["exit_code"] == 3


@pytest.mark.parametrize(
    "size, expected",
    [(1.0, "m"), (1.8, "m"), (1800.0, "mm"), (200.0, "mm")],
)
def test_unit_guess(size, expected):
    """The size-based unit guess: about 1 to 2 units long is metres, hundreds or thousands is millimetres."""
    assert ig.guess_units(size)[0] == expected


def test_declared_units_conflicting_with_size_warns(tmp_path):
    """A 300 mm wing declared as metres warns that the size is implausible."""
    tris, _ = fx.wing()
    r = run_inspect(tmp_path, tris * 1000.0, units="m")  # 300 mm wing declared as metres
    assert "SIZE_IMPLAUSIBLE" in codes(r, "WARN")


def test_export_scales_to_metres_and_only_when_ok(tmp_path):
    """The exported STL is in metres with the right chord and span, and geometry that fails is never exported."""
    tris, _ = fx.wing()
    out = tmp_path / "out_m.stl"
    r = run_inspect(tmp_path, tris * 1000.0, units="mm", export_stl=out)
    assert r["status"] == "PASS" and out.exists()
    back = ig.read_stl(out).reshape(-1, 3)
    size = back.max(axis=0) - back.min(axis=0)  # per-axis size in metres
    np.testing.assert_allclose(size[0], 0.3, atol=1e-4)   # chord
    np.testing.assert_allclose(size[1], 1.0, atol=1e-4)   # span
    # failing geometry is never exported
    bad = tmp_path / "bad_m.stl"
    run_inspect(tmp_path, fx.open_copy(tris), units="mm", export_stl=bad, name="h.stl")
    assert not bad.exists()


# ---- CAD cross-check -------------------------------------------------------
def test_cad_volume_comparison():
    """Volumes within about 1% of the CAD value pass; a 20% mismatch is reported; a zero CAD volume is ignored."""
    assert ig.compare_cad_volume(1.0, 1.01) is None
    assert ig.compare_cad_volume(1.0, 0.8) is not None
    assert ig.compare_cad_volume(0.0, 5.0) is None


# ---- command line ----------------------------------------------------------
def cli(args):
    """Run src/inspect_geometry.py as a separate process and return the completed process."""
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


def test_cli_exit_codes_and_report(tmp_path):
    """Exit codes: 0 pass, 3 units needed, 1 failed (or 0 with --allow-open), 2 missing file; the JSON report has the
    status and a SHA-256.
    """
    good = tmp_path / "good.stl"
    bad = tmp_path / "bad.stl"
    ig.write_stl_binary(good, fx.wing()[0])
    ig.write_stl_binary(bad, fx.open_copy(fx.wing()[0]))

    p = cli([good, "--units", "m"])
    assert p.returncode == 0, p.stdout + p.stderr
    rep = json.loads((tmp_path / "good.stl.report.json").read_text())
    assert rep["status"] == "PASS" and len(rep["input"]["sha256"]) == 64

    assert cli([good]).returncode == 3
    p = cli([bad, "--units", "m"])
    assert p.returncode == 1 and "open edges" in p.stdout
    assert cli([bad, "--units", "m", "--allow-open"]).returncode == 0
    assert cli([tmp_path / "missing.stl"]).returncode == 2


def test_step_without_cadquery_gives_clear_error(tmp_path):
    """Reading a STEP file without CadQuery installed raises an error that names CadQuery (skipped if it is installed).
    """
    pytest.importorskip  # noqa: B018
    try:
        import cadquery  # noqa: F401
        pytest.skip("CadQuery is installed; this test covers the missing-CadQuery message")
    except ImportError:
        pass
    p = tmp_path / "x.step"
    p.write_text("ISO-10303-21;")
    with pytest.raises(ig.GeometryError, match="CadQuery"):
        ig.inspect(p)


def test_weld_joins_points_that_straddle_a_grid_boundary():
    """Two points 0.2 micrometre apart on either side of a hashing-grid boundary are still welded together."""
    tol = 1e-3
    pts = np.array([[0.0099999, 0, 0], [0.0100001, 0, 0], [0.5, 0.5, 0.5]])   # 0.2 um apart across x = 10 tol
    lab = ig.weld_vertices(pts, tol)
    assert lab[0] == lab[1] != lab[2]


def test_exported_stl_is_closed_with_exactly_shared_vertices(tmp_path):
    """CAD-style STL: each face tessellated on its own, so shared corners differ by ~1e-6. The export must
    write identical coordinates for them (OpenFOAM joins triangles only on exactly equal points)."""
    rng = np.random.default_rng(3)
    tris = fx.cube(1.0) + [0, 0, 0.1]
    jittered = tris + rng.normal(0, 1e-7, tris.shape)                  # every triangle's corners differ slightly
    src = tmp_path / "jit.stl"
    ig.write_stl_binary(src, jittered)
    out = tmp_path / "welded.stl"
    ig.inspect(src, units="m", export_stl=out)
    t = ig.read_stl(out)
    _, inv = np.unique(t.reshape(-1, 3), axis=0, return_inverse=True)
    f = np.asarray(inv).reshape(-1, 3)
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    assert (counts == 2).all() and len(np.unique(inv)) == 8              # closed, 8 exact corners
