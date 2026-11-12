"""Tests for src/check_case.py using small synthetic log files (no OpenFOAM needed)."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import check_case as cc  # noqa: E402

DOMAIN, BODY = 81.6, 0.10591


def solver_log(cd, cl, end=True):
    """Synthetic solver log with one forceCoeffs block per iteration; End marker only if end is True."""
    lines = []
    for i, (d, l) in enumerate(zip(cd, cl), start=1):
        lines += [f"Time = {i}s", "", "forceCoeffs forceCoeffs execute:", "    Cm    = 0.01",
                  f"    Cd    = {d}", f"    Cl    = {l}", "    Cl(f) = 0.1", "    Cl(r) = 0.1", ""]
    if end:
        lines += ["End", "", "Finalising parallel run"]
    return "\n".join(lines)


def part(base=0.2, surface=(5, 6), length=1.188, gap=0.05, volume=BODY, area=1.565, half=False):
    """The job.json entries the RESOLUTION gate reads. Defaults: the Ahmed body at the standard preset (a pass).

    volume and area describe the whole part; for a half model job.json holds the volume of the meshed half.
    """
    return {"parameters": {"base_cell_m": base, "surface_level": list(surface),
                           "symmetry": "half model: Y <= 0 meshed" if half else "none (full model)"},
            "input_stl": {"bbox_min": [0.0, -0.2, gap], "bbox_max": [length, 0.2, gap + 0.2],
                          "volume_m3": volume / 2 if half else volume, "surface_area_m2": area}}


# Real parts at real presets, from this repo's own runs: (part arguments, cells in the mesh, expected level).
REAL_CASES = {
    "Zerihan wing, standard (validated)": (dict(base=0.2, surface=(5, 6), length=0.25, gap=0.05, volume=0.00473,
                                                area=0.6055), 4_840_000, "PASS"),
    "PERRINN half wing, 3.1 / 0.8 mm (validated)": (dict(base=0.1, surface=(5, 7), length=0.7373, gap=0.0856,
                                                         volume=0.01579, area=2.8568, half=True), 6_751_080, "PASS"),
    "Ahmed body, standard": (dict(), 2_105_486, "PASS"),
    "Ahmed body, quick (smoke test)": (dict(base=0.4, surface=(3, 4)), 150_000, "WARN"),
    "737 mm demo wing, quick, half": (dict(base=0.4, surface=(3, 4), length=0.8248, gap=0.1651, volume=0.08794,
                                           area=3.8025, half=True), 90_000, "WARN"),
    "223 mm demo wing, quick (gave Cd 0.72, Cl -4.5)": (dict(base=0.4, surface=(3, 4), length=0.25, gap=0.05,
                                                             volume=0.00473, area=0.6055), 7_893, "FAIL"),
}


def make(tmp_path, total_volume=DOMAIN - BODY, cd=None, cl=None, snappy_ok=True, mesh_ok=True, end=True,
         resolution=None, cells=2_000_000):
    """Build a synthetic finished case folder (job.json and the three logs) in tmp_path and return it.

    Defaults give a case that passes every gate; each argument spoils one thing: the fluid volume, the force history,
    the snappyHexMesh result, the checkMesh result, the solver ending, the part's size against the mesh (resolution:
    keyword arguments for part()) or the number of cells.
    """
    c = tmp_path / "case"
    c.mkdir()
    (c / "job.json").write_text(json.dumps({"tool": "make_case.py", "tool_version": "x",
                                            **part(**(resolution or {})),
                                            "expected": {"domain_volume_m3": DOMAIN,
                                                         "fluid_volume_m3": DOMAIN - BODY}}))
    (c / "log.snappyHexMesh").write_text(f"Snapped mesh : cells:{cells}  faces:1  points:1\n"
                                         + ("Finished meshing without any errors\n" if snappy_ok else "FOAM FATAL\n"))
    (c / "log.checkMesh").write_text(f"    Total volume = {total_volume:.6g}.  Cell volumes OK.\n"
                                     + ("\nMesh OK.\n" if mesh_ok else "\nFailed 2 mesh checks.\n"))
    n = 500
    cd = cd if cd is not None else 0.25 + 0.05 * np.exp(-np.arange(n) / 30)
    cl = cl if cl is not None else -0.09 + 0.02 * np.exp(-np.arange(n) / 30)
    (c / "log.foamRun").write_text(solver_log(cd, cl, end))
    return c


def levels(r):
    """Map gate code to level (PASS, WARN or FAIL) for a check result."""
    return {g["code"]: g["level"] for g in r["gates"]}


def test_good_case_passes(tmp_path):
    """A clean case passes, the body volume is found removed from the domain, and all 500 iterations are counted."""
    r = cc.check(make(tmp_path))
    assert r["status"] == "PASS", r["gates"]
    assert abs(r["mesh"]["body_volume_removed_fraction"] - 1) < 0.01
    assert r["final"]["iterations"] == 500


def test_air_inside_body_fails(tmp_path):
    """A mesh whose fluid volume is too large (air inside the body) fails the FLUID_VOLUME gate."""
    r = cc.check(make(tmp_path, total_volume=DOMAIN - 0.001))
    assert levels(r)["FLUID_VOLUME"] == "FAIL"
    assert r["status"] == "FAIL"


def test_unconverged_forces_flagged(tmp_path):
    """Forces still drifting at the end fail the CONVERGED gate."""
    n = 500
    cd = 0.55 - 0.2 * np.arange(n) / n           # still drifting
    r = cc.check(make(tmp_path, cd=cd))
    assert levels(r)["CONVERGED"] == "FAIL"


def test_mesh_failures(tmp_path):
    """A failed mesher, a failed checkMesh and a missing solver End line each fail their own gate."""
    r = cc.check(make(tmp_path, snappy_ok=False, mesh_ok=False, end=False))
    lv = levels(r)
    assert lv["MESH_FINISHED"] == "FAIL" and lv["MESH_QUALITY"] == "FAIL" and lv["SOLVER_END"] == "FAIL"


def test_reference_comparison(tmp_path):
    """Cd is compared with a reference CSV as a relative difference."""
    ref = tmp_path / "ref.csv"
    ref.write_text("cd, cl\n2.3848566658e-01, -9.4516081781e-02\n")
    r = cc.check(make(tmp_path), reference=ref)
    c = r["comparison"]
    assert abs(c["cd"]["ours"] - 0.25) < 1e-3
    assert abs(c["cd"]["relative"] - (0.25 - 0.23848566658) / 0.23848566658) < 1e-3


def test_steady_oscillation_passes(tmp_path):
    # like the real Ahmed run: periodic swing around a fixed mean (period about 19 iterations)
    """A steady periodic swing around a fixed mean counts as converged."""
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
    """A handful of illegal faces from snappyHexMesh is a warning, not a failure."""
    c = make(tmp_path)
    (c / "log.snappyHexMesh").write_text(V3F_TAIL)
    r = cc.check(c)
    g = [x for x in r["gates"] if x["code"] == "MESH_FINISHED"][0]
    assert g["level"] == "WARN" and "4 illegal faces" in g["message"] and "interpolation weights" in g["message"]
    assert r["mesh"]["cells"] == 2105486


def test_many_illegal_faces_fail(tmp_path):
    """Thousands of illegal faces fail the MESH_FINISHED gate."""
    c = make(tmp_path)
    (c / "log.snappyHexMesh").write_text(V3F_TAIL.replace("with 4 illegal", "with 5000 illegal"))
    assert levels(cc.check(c))["MESH_FINISHED"] == "FAIL"


def test_large_swing_gives_unsteady_warning(tmp_path):
    """A converged but large swing (standard Ahmed body) adds an UNSTEADY warning."""
    i = np.arange(1000)
    cd = 0.315 + 0.0236 * np.sin(2 * np.pi * i / 40)          # like the 25 deg standard Ahmed body
    cl = 0.318 + 0.16 * np.sin(2 * np.pi * i / 40)
    r = cc.check(make(tmp_path, cd=cd, cl=cl))
    lv = levels(r)
    assert lv["CONVERGED"] == "PASS" and lv["UNSTEADY"] == "WARN"


def test_small_swing_has_no_unsteady_warning(tmp_path):
    """A small swing adds no UNSTEADY gate at all."""
    i = np.arange(1000)
    r = cc.check(make(tmp_path, cd=0.2584 + 0.0016 * np.sin(2 * np.pi * i / 19),
                      cl=-0.114 + 0.002 * np.sin(2 * np.pi * i / 19)))
    assert "UNSTEADY" not in levels(r)


def _checkmesh(c, stars, faces=25376156):
    """Write a log.checkMesh with the given failure lines (stars) after the usual volume line."""
    (c / "log.checkMesh").write_text(f"    faces:            {faces}\n"
                                     f"    Total volume = {DOMAIN - BODY:.6g}.  Cell volumes OK.\n"
                                     + "".join(f" ***{s}\n" for s in stars) + f"\nFailed {len(stars)} mesh checks.\n")


def test_isolated_skew_faces_only_warn(tmp_path):
    """A few highly skewed faces in a large mesh are a warning, not a failure."""
    c = make(tmp_path)
    _checkmesh(c, ["Max skewness = 7.48736, 11 highly skew faces detected which may impair the quality of the results"])
    r = cc.check(c)
    assert levels(r)["MESH_QUALITY"] == "WARN" and "11 highly skewed faces" in \
        [g for g in r["gates"] if g["code"] == "MESH_QUALITY"][0]["message"]


def test_many_skew_faces_or_other_failures_still_fail(tmp_path):
    """Many skewed faces, or any other failed check next to a few, still fail MESH_QUALITY."""
    c = make(tmp_path)
    _checkmesh(c, ["Max skewness = 9.1, 5000 highly skew faces detected"], faces=1_000_000)
    assert levels(cc.check(c))["MESH_QUALITY"] == "FAIL"
    _checkmesh(c, ["Max skewness = 7.4, 11 highly skew faces detected",
                   "Zero or negative cell volume detected.  Minimum negative volume: -1e-12"])
    assert levels(cc.check(c))["MESH_QUALITY"] == "FAIL"


def test_wing_layer_mesh_338_illegal_faces_is_warn(tmp_path):
    # PERRINN half wing with layers: 338 of 21.3M faces (1.6e-5), checkMesh OK, no leak
    """The 338 illegal faces seen on every layered PERRINN mesh (1.6e-5 of the faces) warn, not fail."""
    c = make(tmp_path)
    (c / "log.snappyHexMesh").write_text(
        "Layer mesh : cells:6751080  faces:21329608  points:1\nChecking faces in error :\n"
        "    faces with interpolation weights (0..1)  < 0.02        : 338\n"
        "Finished meshing with 338 illegal faces (concave, zero area or negative cell pyramid volume)\n")
    assert levels(cc.check(c))["MESH_FINISHED"] == "WARN"


# ---- RESOLUTION: is the mesh fine enough for the part? ---------------------------------------------------------- #
@pytest.mark.parametrize("name", list(REAL_CASES))
def test_resolution_on_the_repos_real_cases(name):
    """The limits pass the validated runs, warn on deliberately rough ones and fail the part that gave Cd 0.72."""
    args, cells, expected = REAL_CASES[name]
    gate, details = cc.resolution_gate(part(**args), cells)
    assert gate["level"] == expected, gate["message"]
    assert details["cells"] == cells and gate["code"] == "RESOLUTION"


def test_tiny_part_fails_and_the_message_says_why():
    """The 223 mm wing at the quick preset fails, naming the three counts and the cell total."""
    args, cells, _ = REAL_CASES["223 mm demo wing, quick (gave Cd 0.72, Cl -4.5)"]
    gate, details = cc.resolution_gate(part(**args), cells)
    assert gate["level"] == "FAIL"
    for text in ("only 5 surface cells along its 250 mm length", "only 0.6 cells across its 15.6 mm mean thickness",
                 "only 7,893 cells in the whole mesh", "50 mm on flat areas, 25 mm on curved areas",
                 "Use a finer preset"):
        assert text in gate["message"], gate["message"]
    assert details["checks"]["length"] == "FAIL" and details["checks"]["thickness"] == "FAIL"
    assert details["checks"]["gap"] == "WARN"                      # a gap never fails on its own


def test_a_gap_alone_never_fails():
    """One cell under a body (as in the Ahmed smoke test) only warns, however small the gap."""
    gate, details = cc.resolution_gate(part(base=0.2, surface=(5, 6), gap=0.002), 2_000_000)
    assert gate["level"] == "WARN" and details["checks"]["gap"] == "WARN"


def test_half_model_volume_is_doubled_back():
    """job.json holds the volume of the meshed half; the mean thickness must be that of the whole part."""
    args = dict(base=0.4, surface=(3, 4), length=0.8248, gap=0.1651, volume=0.08794, area=3.8025)
    full = cc.resolution_gate(part(**args), None)[1]
    half = cc.resolution_gate(part(**args, half=True), None)[1]
    assert abs(full["mean_thickness_m"] - half["mean_thickness_m"]) < 1e-12
    assert abs(full["mean_thickness_m"] - 2 * 0.08794 / 3.8025) < 1e-12


def test_before_meshing_there_is_no_cell_count_to_judge():
    """The early check (before meshing) judges the three sizes only; the cell total joins in afterwards."""
    gate, details = cc.resolution_gate(part(), None)
    assert gate["level"] == "PASS" and "cells in total" not in gate["message"]
    assert "cells" not in details["checks"] and details["cells"] is None
    gate, details = cc.resolution_gate(part(), 5_000)
    assert gate["level"] == "FAIL" and details["checks"]["cells"] == "FAIL" and "only 5,000 cells" in gate["message"]
    assert cc.resolution_gate(part(), 60_000)[0]["level"] == "WARN"


def test_allow_coarse_turns_a_fail_into_a_warning_with_the_reason_kept():
    """A smoke test that is meant to be coarse can go ahead, and the result still says it was coarse."""
    args, cells, _ = REAL_CASES["223 mm demo wing, quick (gave Cd 0.72, Cl -4.5)"]
    gate, _ = cc.resolution_gate(part(**args), cells, allow_coarse=True)
    assert gate["level"] == "WARN" and gate["message"].startswith("Accepted because the job sets allow_coarse.")
    assert "only 0.6 cells across" in gate["message"]


@pytest.mark.parametrize("job", [None, {}, {"parameters": {}}, {"parameters": {"base_cell_m": 0.2, "surface_level": [5, 6]}}])
def test_missing_job_information_warns_instead_of_passing_silently(job):
    """If the size of the part or the cell size is unknown, the gate says it could not check (never a quiet PASS)."""
    gate, details = cc.resolution_gate(job, 1_000_000)
    assert gate["level"] == "WARN" and "Cannot check" in gate["message"] and details is None


def test_check_runs_the_gate_on_a_case_and_records_the_numbers(tmp_path):
    """check() adds RESOLUTION to the gates and stores the measured counts under mesh.resolution."""
    r = cc.check(make(tmp_path))
    assert levels(r)["RESOLUTION"] == "PASS" and r["status"] == "PASS"
    res = r["mesh"]["resolution"]
    assert res["cells"] == 2_000_000 and res["length_cells"] > 150 and list(res["limits"]["length"]) == [8, 30]
    json.dumps(r)                                                  # results.json must be writable


def test_a_coarse_part_fails_the_whole_check_even_when_every_other_gate_passes(tmp_path):
    """The failure that motivated the gate: a clean run on a mesh that cannot draw the part must not read PASS."""
    args, cells, _ = REAL_CASES["223 mm demo wing, quick (gave Cd 0.72, Cl -4.5)"]
    r = cc.check(make(tmp_path, resolution=args, cells=cells))
    lv = levels(r)
    assert lv["RESOLUTION"] == "FAIL" and r["status"] == "FAIL"
    assert all(v == "PASS" for k, v in lv.items() if k != "RESOLUTION")
    ok = cc.check(tmp_path / "case", allow_coarse=True)
    assert levels(ok)["RESOLUTION"] == "WARN" and ok["status"] == "WARN"


def test_a_rough_run_is_a_warning(tmp_path):
    """The quick preset on a decent-sized wing warns (rough look), it does not fail."""
    args, cells, _ = REAL_CASES["737 mm demo wing, quick, half"]
    r = cc.check(make(tmp_path, resolution=args, cells=cells))
    assert levels(r)["RESOLUTION"] == "WARN" and r["status"] == "WARN"
