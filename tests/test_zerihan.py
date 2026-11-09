"""Tyrrell wing validation case: data, references, job files and comparison (no CadQuery or OpenFOAM needed),
plus the geometry itself when CadQuery is installed."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import compare_zerihan as cz  # noqa: E402
import make_tyrrell_wing as tw  # noqa: E402
import run_job as rj  # noqa: E402


def test_profile_matches_the_thesis():
    suc, pre = tw.load_profile()
    assert len(suc) == len(pre) == 44
    te = (pre[-1, 1] - suc[-1, 1])                      # trailing edge thickness 0.007c (1.65 mm)
    assert te == pytest.approx(0.0073, abs=2e-4)
    assert suc[:, 1].min() == pytest.approx(-0.0778)     # lowest point, x/c ~ 0.2
    incidence = np.degrees(np.arctan2(0.5 * (suc[-1, 1] + pre[-1, 1]), 1.0))
    assert incidence == pytest.approx(3.45, abs=0.2)     # coordinates are at the true reference incidence
    assert 0.010 < tw.profile_area() / tw.CHORD ** 2 < 0.2


def test_smooth_outline_passes_through_the_thesis_points():
    s, q = tw.smooth_outline(), tw.outline()
    d = np.array([np.hypot(*(s - x).T).min() for x in q])
    assert d.max() < 2.5e-5                               # every measured point is on the outline (0.025 mm)
    seg = np.hypot(*np.diff(s, axis=0).T)
    assert seg.min() > 1e-5 and seg.max() < 1e-3          # no zero-length or long straight segments
    assert tw.profile_area() == pytest.approx(0.5 * abs(np.dot(q[:, 0], np.roll(q[:, 1], -1))
                                                         - np.dot(q[:, 1], np.roll(q[:, 0], -1))), rel=0.01)


def test_reference_interpolation_and_sign(tmp_path):
    cl, cd = tw.reference_at(0.082, "free")
    assert cl == pytest.approx(1.72) and cd > 0
    assert tw.reference_at(0.134, "free")[0] < 1.72      # downforce falls above and below the peak
    assert tw.reference_at(0.044, "free")[0] < 1.72
    assert tw.reference_at(0.112)[0] == pytest.approx(1.39)   # default: transition fixed, peak 1.39 at 0.112
    assert tw.reference_at(0.224)[0] < tw.reference_at(0.224, "free")[0]   # tripping lowers downforce
    with pytest.raises(ValueError):
        tw.reference_at(2.0)
    p = tw.write_reference(0.224, folder=tmp_path)
    cd_s, cl_s = p.read_text().splitlines()[1].split(",")
    assert float(cl_s) < 0                                # pipeline sign: downforce negative


def test_job_files_are_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(tw, "DATA", tmp_path)
    jobs = tw.write_jobs(heights=(0.224, 0.090), jobs_dir=tmp_path / "jobs", make_stl=False)
    assert len(jobs) == 4
    spec = json.loads(jobs[0].read_text())
    assert spec["case"]["turbulence_model"] == "SpalartAllmaras" and spec["case"]["ground"] == "moving"
    # the job runner accepts every case option
    (tmp_path / "jobs" / "tyrrell_h0224.stl").write_text("solid x\nendsolid x\n")
    spec["geometry"] = "tyrrell_h0224.stl"; spec["reference"] = None
    p = tmp_path / "jobs" / "t.json"; p.write_text(json.dumps(spec))
    s = rj.load_job(p)
    assert s["case"]["surface_level"] == (6, 7) and s["case"]["layer_thickness"] == 0.0006


def test_comparison_finds_peak_and_drop(tmp_path):
    ref = tw.reference_forces()
    jobs = []
    for h in (0.224, 0.134, 0.112, 0.060):
        cl, cd = np.interp(h, ref[:, 0], ref[:, 1]), np.interp(h, ref[:, 0], ref[:, 2])
        jobs.append({"job": f"tyrrell_h{round(h*1000):04d}_sa", "outcome": "finished", "Cl": -cl, "Cd": cd})
        jobs.append({"job": f"tyrrell_h{round(h*1000):04d}_sst", "outcome": "finished", "Cl": -(1.0 + 3 * (0.3 - h)), "Cd": cd})
    jobs.append({"job": "ahmedml_1", "outcome": "finished", "Cl": 0.1, "Cd": 0.3})
    b = tmp_path / "batch_x.json"; b.write_text(json.dumps({"jobs": jobs}))
    assert cz.main([str(b)]) == 0
    res = json.loads((tmp_path / "zerihan_comparison.json").read_text())
    assert res["sa"]["drop_below_peak"] and res["sa"]["mean_abs_CL_error"] < 1e-9
    assert not res["sst"]["drop_below_peak"]              # keeps rising: misses the stall
    assert "Highest downforce" in (tmp_path / "zerihan_comparison.md").read_text()


def test_geometry_is_one_clean_solid(tmp_path):
    pytest.importorskip("cadquery")
    import inspect_geometry as ig
    info = tw.check(tw.build(0.224), 0.224)
    assert info["problems"] == []
    out = tmp_path / "w.stl"
    assert tw.main(["--h-over-c", "0.224", "--out", str(out)]) == 0
    r = ig.inspect(out, units="m")
    g = r["geometry"]
    assert r["status"] in ("PASS", "WARN") and len(g["bodies"]) == 1
    assert g["open_edges"] == 0 and g["non_manifold_edges"] == 0
    assert ig.read_stl(out).reshape(-1, 3)[:, 2].min() == pytest.approx(0.224 * tw.CHORD, abs=2e-4)


def test_comparison_reads_single_job_folders(tmp_path):
    d = tmp_path / "tyrrell_h0224_sa_20261104-151512"; d.mkdir()
    (d / "results.json").write_text(json.dumps({"status": "WARN", "final": {"Cd_mean": 0.0548, "Cl_mean": -1.1155}}))
    b = tmp_path / "batch_y.json"
    b.write_text(json.dumps({"jobs": [{"job": "tyrrell_h0134_sa", "outcome": "finished", "Cl": -1.3079, "Cd": 0.0636}]}))
    other = tmp_path / "batch_y_analysis.json"; other.write_text(json.dumps({"n": 6, "pairs": []}))
    assert cz.main([str(b), str(other), str(d)]) == 0     # non-batch JSON matched by batch_*.json is ignored
    res = json.loads((tmp_path / "zerihan_comparison.json").read_text())
    assert sorted(r["h_c"] for r in res["sa"]["rows"]) == [0.134, 0.224]
