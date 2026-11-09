"""Tests for src/check_case.py using small synthetic log files (no OpenFOAM needed)."""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import check_case as cc  # noqa: E402

DOMAIN, BODY = 81.6, 0.10591


def solver_log(cd, cl, end=True):
    lines = []
    for i, (d, l) in enumerate(zip(cd, cl), start=1):
        lines += [f"Time = {i}s", "", "forceCoeffs forceCoeffs execute:", f"    Cm    = 0.01",
                  f"    Cd    = {d}", f"    Cl    = {l}", f"    Cl(f) = 0.1", f"    Cl(r) = 0.1", ""]
    if end:
        lines += ["End", "", "Finalising parallel run"]
    return "\n".join(lines)


def make(tmp_path, total_volume=DOMAIN - BODY, cd=None, cl=None, snappy_ok=True, mesh_ok=True, end=True):
    c = tmp_path / "case"
    c.mkdir()
    (c / "job.json").write_text(json.dumps({"tool": "make_case.py", "tool_version": "x", "parameters": {},
                                            "expected": {"domain_volume_m3": DOMAIN,
                                                         "fluid_volume_m3": DOMAIN - BODY}}))
    (c / "log.snappyHexMesh").write_text("Snapped mesh : cells:1000  faces:1  points:1\n"
                                         + ("Finished meshing without any errors\n" if snappy_ok else "FOAM FATAL\n"))
    (c / "log.checkMesh").write_text(f"    Total volume = {total_volume:.6g}.  Cell volumes OK.\n"
                                     + ("\nMesh OK.\n" if mesh_ok else "\nFailed 2 mesh checks.\n"))
    n = 500
    cd = cd if cd is not None else 0.25 + 0.05 * np.exp(-np.arange(n) / 30)
    cl = cl if cl is not None else -0.09 + 0.02 * np.exp(-np.arange(n) / 30)
    (c / "log.foamRun").write_text(solver_log(cd, cl, end))
    return c


def levels(r):
    return {g["code"]: g["level"] for g in r["gates"]}


def test_good_case_passes(tmp_path):
    r = cc.check(make(tmp_path))
    assert r["status"] == "PASS", r["gates"]
    assert abs(r["mesh"]["body_volume_removed_fraction"] - 1) < 0.01
    assert r["final"]["iterations"] == 500


def test_air_inside_body_fails(tmp_path):
    r = cc.check(make(tmp_path, total_volume=DOMAIN - 0.001))
    assert levels(r)["FLUID_VOLUME"] == "FAIL"
    assert r["status"] == "FAIL"


def test_unconverged_forces_flagged(tmp_path):
    n = 500
    cd = 0.55 - 0.2 * np.arange(n) / n           # still drifting
    r = cc.check(make(tmp_path, cd=cd))
    assert levels(r)["CONVERGED"] == "FAIL"


def test_mesh_failures(tmp_path):
    r = cc.check(make(tmp_path, snappy_ok=False, mesh_ok=False, end=False))
    lv = levels(r)
    assert lv["MESH_FINISHED"] == "FAIL" and lv["MESH_QUALITY"] == "FAIL" and lv["SOLVER_END"] == "FAIL"


def test_reference_comparison(tmp_path):
    ref = tmp_path / "ref.csv"
    ref.write_text("cd, cl\n2.3848566658e-01, -9.4516081781e-02\n")
    r = cc.check(make(tmp_path), reference=ref)
    c = r["comparison"]
    assert abs(c["cd"]["ours"] - 0.25) < 1e-3
    assert abs(c["cd"]["relative"] - (0.25 - 0.23848566658) / 0.23848566658) < 1e-3


def test_steady_oscillation_passes(tmp_path):
    # like the real Ahmed run: periodic swing around a fixed mean (period about 19 iterations)
    i = np.arange(1000)
    cd = 0.2584 + 0.0033 * np.sin(2 * np.pi * i / 19)
    cl = -0.114 + 0.045 * np.sin(2 * np.pi * i / 19)
    r = cc.check(make(tmp_path, cd=cd, cl=cl))
    assert levels(r)["CONVERGED"] == "PASS", r["gates"]


V3F_TAIL = """Layer mesh : cells:2105486  faces:6425522  points:2216152
Checking faces in error :
    non-orthogonality > 65  degrees                        : 0
    faces with skewness > 4   (internal) or 20  (boundary) : 0
    faces with interpolation weights (0..1)  < 0.02        : 4
    faces with face twist < 0.02                           : 0
Finished meshing with 4 illegal faces (concave, zero area or negative cell pyramid volume)
"""


def test_few_illegal_faces_is_warn(tmp_path):
    c = make(tmp_path)
    (c / "log.snappyHexMesh").write_text(V3F_TAIL)
    r = cc.check(c)
    g = [x for x in r["gates"] if x["code"] == "MESH_FINISHED"][0]
    assert g["level"] == "WARN" and "4 illegal faces" in g["message"] and "interpolation weights" in g["message"]
    assert r["mesh"]["cells"] == 2105486


def test_many_illegal_faces_fail(tmp_path):
    c = make(tmp_path)
    (c / "log.snappyHexMesh").write_text(V3F_TAIL.replace("with 4 illegal", "with 5000 illegal"))
    assert levels(cc.check(c))["MESH_FINISHED"] == "FAIL"


def test_large_swing_gives_unsteady_warning(tmp_path):
    i = np.arange(1000)
    cd = 0.315 + 0.0236 * np.sin(2 * np.pi * i / 40)          # like the 25 deg standard Ahmed body
    cl = 0.318 + 0.16 * np.sin(2 * np.pi * i / 40)
    r = cc.check(make(tmp_path, cd=cd, cl=cl))
    lv = levels(r)
    assert lv["CONVERGED"] == "PASS" and lv["UNSTEADY"] == "WARN"


def test_small_swing_has_no_unsteady_warning(tmp_path):
    i = np.arange(1000)
    r = cc.check(make(tmp_path, cd=0.2584 + 0.0016 * np.sin(2 * np.pi * i / 19),
                      cl=-0.114 + 0.002 * np.sin(2 * np.pi * i / 19)))
    assert "UNSTEADY" not in levels(r)


def _checkmesh(c, stars, faces=25376156):
    (c / "log.checkMesh").write_text(f"    faces:            {faces}\n"
                                     f"    Total volume = {DOMAIN - BODY:.6g}.  Cell volumes OK.\n"
                                     + "".join(f" ***{s}\n" for s in stars) + f"\nFailed {len(stars)} mesh checks.\n")


def test_isolated_skew_faces_only_warn(tmp_path):
    c = make(tmp_path)
    _checkmesh(c, ["Max skewness = 7.48736, 11 highly skew faces detected which may impair the quality of the results"])
    r = cc.check(c)
    assert levels(r)["MESH_QUALITY"] == "WARN" and "11 highly skewed faces" in \
        [g for g in r["gates"] if g["code"] == "MESH_QUALITY"][0]["message"]


def test_many_skew_faces_or_other_failures_still_fail(tmp_path):
    c = make(tmp_path)
    _checkmesh(c, ["Max skewness = 9.1, 5000 highly skew faces detected"], faces=1_000_000)
    assert levels(cc.check(c))["MESH_QUALITY"] == "FAIL"
    _checkmesh(c, ["Max skewness = 7.4, 11 highly skew faces detected",
                   "Zero or negative cell volume detected.  Minimum negative volume: -1e-12"])
    assert levels(cc.check(c))["MESH_QUALITY"] == "FAIL"


def test_wing_layer_mesh_338_illegal_faces_is_warn(tmp_path):
    # PERRINN half wing with layers: 338 of 21.3M faces (1.6e-5), checkMesh OK, no leak
    c = make(tmp_path)
    (c / "log.snappyHexMesh").write_text(
        "Layer mesh : cells:6751080  faces:21329608  points:1\nChecking faces in error :\n"
        "    faces with interpolation weights (0..1)  < 0.02        : 338\n"
        "Finished meshing with 338 illegal faces (concave, zero area or negative cell pyramid volume)\n")
    assert levels(cc.check(c))["MESH_FINISHED"] == "WARN"
