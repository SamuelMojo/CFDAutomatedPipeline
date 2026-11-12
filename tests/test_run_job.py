"""run_job.py tests with a FAKE OpenFOAM: each command writes a small, realistic log.

They need the OpenFOAM tutorial folder (to copy the case template), so they are skipped
where $FOAM_TUTORIALS is not set. No OpenFOAM solver is actually run.
"""
import json
import os
import re
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import geometry_fixtures as fx  # noqa: E402
import inspect_geometry as ig  # noqa: E402
import make_case as mc  # noqa: E402
import run_job as rj  # noqa: E402

TUT = Path(os.environ.get("FOAM_TUTORIALS", "/nonexistent")) / mc.TUTORIAL_REL
pytestmark = pytest.mark.skipif(not TUT.is_dir(), reason="OpenFOAM tutorials not found")


class FakeFoam:
    """Stands in for OpenFOAM. `cd(i)` gives drag at iteration i; `leak` breaks the volume gate."""

    def __init__(self, cd=lambda i: 0.25 + 0.05 * np.exp(-i / 100), leak=False, mesh_ok=True, cells=123456):
        """cd(i) gives the drag coefficient at iteration i; leak puts air inside the body; mesh_ok False makes
        snappyHexMesh fail; cells is the cell count snappyHexMesh reports.
        """
        self.cd, self.leak, self.mesh_ok, self.cells = cd, leak, mesh_ok, cells
        self.calls = []
        self.it = 0

    def __call__(self, cmd, case, log, append=False):
        """Pretend to run one OpenFOAM command: record it and write the log the real tool would write."""
        self.calls.append(cmd)
        name = next(c for c in cmd if c not in ("mpirun", "-np") and not c.isdigit())
        text = "End\n"
        if name == "snappyHexMesh":
            text = f"Layer mesh : cells:{self.cells}  faces:400000  points:1\n" + (
                "Finished meshing without any errors\n" if self.mesh_ok else "FOAM FATAL ERROR\n")
        elif name == "checkMesh":
            exp = json.loads((case / "job.json").read_text())["expected"]
            vol = exp["domain_volume_m3"] - (0.0 if self.leak else exp["domain_volume_m3"] - exp["fluid_volume_m3"])
            text = f"    Total volume = {vol:.6g}.  Cell volumes OK.\n\nMesh OK.\n"
        elif name == "foamRun":
            end = int(re.search(r"^endTime\s+(\d+);", (case / "system/controlDict").read_text(), re.M).group(1))
            out = []
            for i in range(self.it + 1, end + 1):
                out += [f"Time = {i}s", "    Cm    = 0.01", f"    Cd    = {self.cd(i):.6f}", "    Cl    = -0.1", ""]
            self.it = end
            text = "\n".join(out) + "\nEnd\n"
        with open(log, "a" if append else "w") as f:
            f.write(text)


def job_file(tmp_path, size=(1.0, 0.4, 0.3), **extra):
    """Write a small job file (and its box STL, size = length, width, height in metres) in tmp_path and return the job
    file's path; extra keys override the defaults.
    """
    stl = tmp_path / "box.stl"
    ig.write_stl_binary(stl, fx.cube(1.0) * list(size) + [-size[0], -size[1] / 2, 0.05])
    spec = {"name": "boxtest", "geometry": "box.stl", "units": "m", "preset": "quick",
            "case": {"speed": 40, "ground": "fixed", "aref": 0.12, "processors": 2},
            "solve": {"min_iterations": 200, "max_iterations": 1000, "block": 200}}
    spec.update(extra)
    p = tmp_path / "job.json"
    p.write_text(json.dumps(spec))
    return p




def test_converges_and_stops_early(tmp_path):
    """A settling run stops on the convergence gate before the iteration limit, with every stage timed and the summary
    written.
    """
    fake = FakeFoam()
    spec = rj.load_job(job_file(tmp_path))
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=fake, stamp="t")
    r = job.run()
    st = json.loads((job.dir / "status.json").read_text())
    assert st["stopped_because"] == "converged"
    assert st["iterations"] < 1000                               # did not need the maximum
    assert r["status"] in ("PASS", "WARN") and (job.dir / "summary.md").is_file()
    assert [s["stage"] for s in st["stages"]] == ["inspect", "case", "mesh", "mesh gates", "solve", "finish"]
    assert all("seconds" in s for s in st["stages"])
    ctl = (job.case / "system/controlDict").read_text()
    assert "startFrom       latestTime;" in ctl and "purgeWrite      2;" in ctl
    assert ["mpirun", "-np", "2", "foamRun", "-parallel"] in fake.calls


def test_drifting_run_hits_max(tmp_path):
    """A run whose forces never settle runs to max_iterations and does not pass the CONVERGED gate."""
    fake = FakeFoam(cd=lambda i: 0.30 - 0.0001 * i)               # never settles
    spec = rj.load_job(job_file(tmp_path))
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=fake, stamp="t")
    r = job.run()
    st = json.loads((job.dir / "status.json").read_text())
    assert st["iterations"] == 1000 and st["stopped_because"].startswith("max_iterations")
    assert {g["code"]: g["level"] for g in r["gates"]}["CONVERGED"] != "PASS"


def test_leak_stops_before_solving(tmp_path):
    """A leak stops the job at the mesh gates, and the solver is never started."""
    fake = FakeFoam(leak=True)
    spec = rj.load_job(job_file(tmp_path))
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=fake, stamp="t")
    with pytest.raises(rj.GateStop, match="FLUID_VOLUME"):
        job.run()
    assert not any("foamRun" in c for c in fake.calls)            # no hours wasted solving
    assert json.loads((job.dir / "status.json").read_text())["state"] == "stopped by gate"


def test_bad_geometry_stops_at_inspection(tmp_path):
    """Open geometry stops the job at inspection before any case is created."""
    spec = rj.load_job(job_file(tmp_path))
    ig.write_stl_binary(spec["geometry"], fx.open_copy(fx.cube(1.0) + [0, 0, 0.1]))
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=FakeFoam(), stamp="t")
    with pytest.raises(rj.GateStop, match="inspection"):
        job.run()
    assert not (job.dir / "case").exists()


def test_job_file_validation(tmp_path):
    """Unknown presets and case options are rejected, and preset defaults fill in the rest."""
    with pytest.raises(rj.JobError, match="preset"):
        rj.load_job(job_file(tmp_path, preset="huge"))
    with pytest.raises(rj.JobError, match="Unknown case option"):
        rj.load_job(job_file(tmp_path, case={"spead": 40}))
    spec = rj.load_job(job_file(tmp_path, preset="standard", solve={}))
    assert spec["case"]["layer_thickness"] == 0.00125 and spec["solve"]["max_iterations"] == 4000


def test_field_averaging_block_after_settling(tmp_path):
    """After the run settles, one extra block averages the fields, and the averaging is recorded in status.json."""
    fake = FakeFoam()
    spec = rj.load_job(job_file(tmp_path, solve={"min_iterations": 200, "max_iterations": 1000,
                                                 "block": 200, "average_iterations": 100}))
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=fake, stamp="t")
    job.run()
    st = json.loads((job.dir / "status.json").read_text())
    fa = st["field_average"]
    assert fa["to_iteration"] - fa["from_iteration"] == 100 and fake.it == fa["to_iteration"]
    assert "#includeFunc fieldAverageWindow" in (job.case / "system/functions").read_text()
    assert "mean            yes;" in (job.case / "system/fieldAverageWindow").read_text()


def test_failed_averaging_keeps_the_solution(tmp_path):
    """If the averaging block fails, its error is recorded, its settings are removed, and the result is still delivered.
    """
    class FailAvg(FakeFoam):
        """Fake that raises a JobError when the solver runs with the averaging function enabled."""
        def __call__(self, cmd, case, log, append=False):
            """Raise for foamRun when fieldAverageWindow is in the functions file, otherwise behave normally."""
            if "foamRun" in cmd and "fieldAverageWindow" in (case / "system/functions").read_text():
                raise rj.JobError("fieldAverage syntax error")
            return super().__call__(cmd, case, log, append)
    spec = rj.load_job(job_file(tmp_path))
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=FailAvg(), stamp="t")
    r = job.run()
    st = json.loads((job.dir / "status.json").read_text())
    assert "error" in st["field_average"] and r["status"] in ("PASS", "WARN")
    assert "fieldAverageWindow" not in (job.case / "system/functions").read_text()


def test_z_shift_moves_body_and_refuses_the_ground(tmp_path):
    """z_shift_mm moves the body up or down, and a shift that takes it through the ground is refused."""
    spec = rj.load_job(job_file(tmp_path, z_shift_mm=30))
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=FakeFoam(), stamp="t")
    job.dir.mkdir(parents=True)                          # run() normally makes the job folder; _inspect() does not
    job._inspect()
    z = ig.read_stl(job.stl)[..., 2]
    assert abs(z.min() - 0.08) < 1e-5                    # box was 0.05 m above the ground
    spec = rj.load_job(job_file(tmp_path, z_shift_mm=-60))
    job = rj.Job(spec, tmp_path / "jobs2", TUT, runner=FakeFoam(), stamp="t")
    job.dir.mkdir(parents=True)
    with pytest.raises(rj.JobError):
        job._inspect()


# ---- the RESOLUTION gate in the job chain ------------------------------------------------------------------------ #
SMALL = (0.1, 0.05, 0.02)          # a 100 mm body: the quick preset's 25-50 mm surface cells cannot draw it


def test_too_coarse_a_preset_stops_before_any_meshing(tmp_path):
    """A part far smaller than the preset's cells is refused as soon as the case exists: nothing is meshed."""
    fake = FakeFoam()
    spec = rj.load_job(job_file(tmp_path, size=SMALL))
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=fake, stamp="t")
    with pytest.raises(rj.GateStop, match="RESOLUTION gate failed before meshing"):
        job.run()
    assert fake.calls == []                                       # not even surfaceFeatures or blockMesh
    st = json.loads((job.dir / "status.json").read_text())
    assert st["state"] == "stopped by gate" and "Mesh too coarse" in st["reason"]
    assert [s["stage"] for s in st["stages"]] == ["inspect", "case"] and st["stages"][-1]["state"] == "failed"


def test_allow_coarse_lets_a_deliberate_smoke_test_go_ahead_and_still_says_so(tmp_path):
    """With allow_coarse the same job runs to the end, and the result carries the warning, not a quiet PASS."""
    fake = FakeFoam()
    spec = rj.load_job(job_file(tmp_path, size=SMALL, allow_coarse=True))
    assert spec["allow_coarse"] is True
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=fake, stamp="t")
    r = job.run()
    gate = next(g for g in r["gates"] if g["code"] == "RESOLUTION")
    assert gate["level"] == "WARN" and gate["message"].startswith("Accepted because the job sets allow_coarse.")
    assert r["status"] == "WARN" and "RESOLUTION" in (job.dir / "summary.md").read_text()
    assert any("snappyHexMesh" in c for c in fake.calls) and any("foamRun" in c for c in fake.calls)


def test_few_cells_after_meshing_stop_the_job_before_solving(tmp_path):
    """The gate runs again after meshing with the real cell count: a mesh of 5,000 cells is not solved."""
    fake = FakeFoam(cells=5000)
    spec = rj.load_job(job_file(tmp_path))                        # 1 m box: the sizes alone only warn
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=fake, stamp="t")
    with pytest.raises(rj.GateStop, match="RESOLUTION"):
        job.run()
    assert any("snappyHexMesh" in c for c in fake.calls) and not any("foamRun" in c for c in fake.calls)


def test_the_normal_box_job_only_warns_about_resolution_and_records_the_numbers(tmp_path):
    """The 1 m test box at the quick preset is rough (20 cells along it, 1 in the gap): a warning, and the job finishes."""
    spec = rj.load_job(job_file(tmp_path))
    assert spec["allow_coarse"] is False
    job = rj.Job(spec, tmp_path / "jobs", TUT, runner=FakeFoam(), stamp="t")
    r = job.run()
    assert {g["code"]: g["level"] for g in r["gates"]}["RESOLUTION"] == "WARN"
    res = r["mesh"]["resolution"]
    assert res["cells"] == 123456 and abs(res["length_cells"] - 20) < 0.01
    assert json.loads((job.case / "results.json").read_text())["mesh"]["resolution"]["cells"] == 123456
