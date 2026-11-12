"""run_batch.py tests with the fake OpenFOAM from test_run_job (skipped without $FOAM_TUTORIALS)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import run_batch as rb  # noqa: E402
import run_job as rj  # noqa: E402
import test_run_job as trj  # noqa: E402

pytestmark = trj.pytestmark


class Switching(trj.FakeFoam):
    """Fake OpenFOAM whose second job leaks (FLUID_VOLUME fails)."""
    def __init__(self):
        """Count the jobs started, so the second job can be given a leak."""
        super().__init__()
        self.jobs = 0

    def __call__(self, cmd, case, log, append=False):
        """Run the command as the fake does, but at the start of each job (surfaceFeatures) reset the counters; job 2
        leaks.
        """
        if cmd == ["surfaceFeatures"]:
            self.jobs += 1
            self.it = 0
            self.leak = self.jobs == 2
        return super().__call__(cmd, case, log, append)


def two_jobs(tmp_path):
    """Two job files differing only in name; the second is the one that leaks."""
    a = trj.job_file(tmp_path, name="good")
    b = tmp_path / "job2.json"
    spec = json.loads(a.read_text())
    spec["name"] = "leaky"
    b.write_text(json.dumps(spec))
    return [a, b]


def test_batch_continues_after_a_stopped_job(tmp_path):
    """A job stopped by a gate is reported with its reason and the next job still runs; both appear in the batch table.
    """
    rows = rb.run_batch(two_jobs(tmp_path), tmp_path / "jobs", trj.TUT, runner=Switching())
    assert [r["job"] for r in rows] == ["good", "leaky"]
    assert rows[0]["outcome"] == "finished" and rows[0]["Cd"] is not None
    assert rows[1]["outcome"] == "stopped by gate" and "FLUID_VOLUME" in rows[1]["reason"]
    md = next((tmp_path / "jobs").glob("batch_*.md")).read_text()
    assert "| good | finished |" in md and "| leaky | stopped by gate |" in md


def test_batch_validates_all_files_first(tmp_path):
    """One invalid job file stops the batch before any command runs."""
    files = two_jobs(tmp_path)
    bad = json.loads(files[1].read_text())
    bad["preset"] = "nope"
    files[1].write_text(json.dumps(bad))
    fake = trj.FakeFoam()
    with pytest.raises(rj.JobError):
        rb.run_batch(files, tmp_path / "jobs", trj.TUT, runner=fake)
    assert fake.calls == []                                   # nothing ran


def test_duplicate_names_rejected(tmp_path):
    """Two jobs with the same name are refused."""
    a = trj.job_file(tmp_path)
    with pytest.raises(rj.JobError, match="unique"):
        rb.run_batch([a, a], tmp_path / "jobs", trj.TUT, runner=trj.FakeFoam())
