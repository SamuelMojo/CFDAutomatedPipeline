"""make_case.py tests. They need the OpenFOAM tutorials ($FOAM_TUTORIALS), so they are
skipped automatically where OpenFOAM is not installed."""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import geometry_fixtures as fx  # noqa: E402
import inspect_geometry as ig  # noqa: E402
import make_case as mc  # noqa: E402

TUT = Path(os.environ.get("FOAM_TUTORIALS", "/nonexistent")) / mc.TUTORIAL_REL
pytestmark = pytest.mark.skipif(not TUT.is_dir(), reason="OpenFOAM tutorials not found")


def box_stl(tmp_path):
    p = tmp_path / "box.stl"
    tris = fx.cube(1.0) * [1.0, 0.4, 0.3] + [-1.0, -0.2, 0.05]   # 1.0 x 0.4 x 0.3 m, 50 mm off the ground
    ig.write_stl_binary(p, tris)
    return p


def test_case_files_and_job(tmp_path):
    case = tmp_path / "c1"
    job = mc.make_case(box_stl(tmp_path), case, TUT, speed=40, ground="fixed", aref=0.12)
    for f in ("system/blockMeshDict", "system/snappyHexMeshDict", "system/forceCoeffs",
              "constant/geometry/body.stl", "Allrun.case", "job.json"):
        assert (case / f).is_file(), f
    assert "noSlip" in (case / "0/U.orig").read_text()
    assert "bodyGroup" in (case / "system/forceCoeffs").read_text()
    assert "Aref            0.12;" in (case / "system/forceCoeffs").read_text()
    exp = json.loads((case / "job.json").read_text())["expected"]
    assert abs(exp["domain_volume_m3"] - exp["fluid_volume_m3"] - 0.12) < 1e-6
    assert job["parameters"]["ground"] == "fixed"
    assert "distributor     hierarchical;" in (case / "system/decomposeParDict").read_text()


def test_never_overwrites(tmp_path):
    case = tmp_path / "c2"
    case.mkdir()
    with pytest.raises(mc.CaseError):
        mc.make_case(box_stl(tmp_path), case, TUT)


def test_rejects_open_surface(tmp_path):
    p = tmp_path / "open.stl"
    ig.write_stl_binary(p, fx.open_copy(fx.cube(1.0) + [0, 0, 0.1]))
    with pytest.raises(mc.CaseError):
        mc.make_case(p, tmp_path / "c3", TUT)


def test_spalart_allmaras_option(tmp_path):
    case = tmp_path / "sa"
    job = mc.make_case(box_stl(tmp_path), case, TUT, speed=40, ground="fixed", aref=0.12,
                       turbulence_model="SpalartAllmaras")
    assert "model               SpalartAllmaras;" in (case / "constant/momentumTransport").read_text()
    nut = (case / "0/nut").read_text()
    assert "nutUSpaldingWallFunction" in nut and "nutkWallFunction" not in nut
    nt = (case / "0/nuTilda").read_text()
    assert '#include        "include/initialConditions"' in nt   # defines $turbulentNuTilda
    assert "object      nuTilda;" in nt and "$turbulentNuTilda" in nt and "bodyGroup" in nt
    assert "turbulentNuTilda     4.5e-05;" in (case / "0/include/initialConditions").read_text()
    assert not (case / "0/k").exists() and not (case / "0/omega").exists()
    assert "div(phi,nuTilda) bounded Gauss upwind;" in (case / "system/fvSchemes").read_text()
    sol = (case / "system/fvSolution").read_text()
    assert "    nuTilda\n    {" in sol and "nuTilda         0.5;" in sol
    assert job["parameters"]["turbulence_model"] == "SpalartAllmaras"


def test_default_model_unchanged_and_bad_model(tmp_path):
    case = tmp_path / "sst"
    mc.make_case(box_stl(tmp_path), case, TUT)
    assert "kOmegaSST" in (case / "constant/momentumTransport").read_text()
    assert (case / "0/k").is_file() and not (case / "0/nuTilda").exists()
    with pytest.raises(mc.CaseError, match="turbulence_model"):
        mc.make_case(box_stl(tmp_path), tmp_path / "bad", TUT, turbulence_model="kEpsilonX")


def test_open_surface_needs_allow_open(tmp_path):
    import inspect_geometry as ig
    import numpy as np
    stl = box_stl(tmp_path)
    tris = ig.read_stl(stl)
    ig.write_stl_binary(stl, tris[1:])                    # remove one triangle: a hole
    with pytest.raises(mc.CaseError, match="allow_open"):
        mc.make_case(stl, tmp_path / "a", TUT)
    job = mc.make_case(stl, tmp_path / "b", TUT, allow_open=True)
    assert job["input_stl"]["surface_warnings"] and job["input_stl"]["volume_m3"] > 0


def test_half_model_with_symmetry_plane(tmp_path):
    import inspect_geometry as ig
    import re as _re
    full = mc.make_case(box_stl(tmp_path), tmp_path / "full", TUT, aref=0.12)
    case = tmp_path / "half"
    job = mc.make_case(box_stl(tmp_path), case, TUT, aref=0.12, half_model=True)
    p, pf = job["parameters"], full["parameters"]
    assert p["domain_max"][1] == 0.0 and p["domain_min"][1] == pytest.approx(pf["domain_min"][1])
    assert p["base_cells"][1] * 2 == pf["base_cells"][1] and p["inside_point"][1] < 0
    bm = (case / "system/blockMeshDict").read_text()
    assert _re.search(r"centrePlane\s*\{\s*type symmetryPlane;\s*faces\s*\(\s*\(3 7 6 2\)\s*\);", bm)
    assert not _re.search(r"^\s*symmetry\s*$", bm, _re.M)   # that name would clash with setConstraintTypes
    assert _re.search(r"frontAndBack\s*\{\s*type patch;\s*faces\s*\(\s*\(1 5 4 0\)\s*\);", bm)
    tris = ig.read_stl(case / "constant/geometry/body.stl")
    assert len(tris) == len(ig.read_stl(box_stl(tmp_path)))      # full closed surface; the domain does the cutting
    assert "Aref            0.06;" in (case / "system/forceCoeffs").read_text()   # half area: full-body coefficients
    full_body = pf["domain_max"][1] * 0 + (full["expected"]["domain_volume_m3"] - full["expected"]["fluid_volume_m3"])
    half_body = job["expected"]["domain_volume_m3"] - job["expected"]["fluid_volume_m3"]
    assert half_body == pytest.approx(full_body / 2)
    assert job["expected"]["domain_volume_m3"] == pytest.approx(full["expected"]["domain_volume_m3"] / 2)


def test_half_model_needs_a_symmetric_body(tmp_path):
    import inspect_geometry as ig
    p = tmp_path / "off.stl"
    ig.write_stl_binary(p, fx.cube(1.0) * [1.0, 0.4, 0.3] + [-1.0, -0.1, 0.05])     # Y -0.1 .. 0.3
    with pytest.raises(mc.CaseError, match="symmetric"):
        mc.make_case(p, tmp_path / "c", TUT, half_model=True)


def test_merge_layer_faces_switch(tmp_path):
    mc.make_case(box_stl(tmp_path), tmp_path / "a", TUT, layers=1, layer_thickness=0.001)
    assert "mergeFaces" not in (tmp_path / "a/system/snappyHexMeshDict").read_text()     # default: as the tutorial
    mc.make_case(box_stl(tmp_path), tmp_path / "b", TUT, layers=1, layer_thickness=0.001, merge_layer_faces=False)
    import re as _re
    d = (tmp_path / "b/system/snappyHexMeshDict").read_text()
    assert _re.search(r"addLayersControls\s*\{\s*mergeFaces\s+false;", d)


def test_half_model_refuses_a_body_edited_on_one_side_only(tmp_path):
    # the bounding box is symmetric about Y = 0, but the right-hand body is much smaller than the left-hand one
    import inspect_geometry as ig
    left = fx.cube(1.0) * [1.0, 0.2, 0.3] + [-1.0, -0.3, 0.05]          # Y -0.3 .. -0.1
    right = fx.cube(1.0) * [0.5, 0.2, 0.15] + [-1.0, 0.1, 0.05]          # Y  0.1 ..  0.3
    p = tmp_path / "lopsided.stl"
    ig.write_stl_binary(p, np.concatenate([left, right]))
    with pytest.raises(mc.CaseError, match="surface area differs"):
        mc.make_case(p, tmp_path / "c", TUT, half_model=True)
