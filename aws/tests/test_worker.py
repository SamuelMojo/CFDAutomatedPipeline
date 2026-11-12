"""Tests for the home-PC worker, with a pretend pipeline and renderer instead of OpenFOAM and ParaView."""
import json
import subprocess
import sys
import time
import types
import zipfile
from pathlib import Path

import pytest

import cfd_common as cc
import worker as wk
from conftest import STEP_TEXT, FakeSNS, FakeTable, make_zip

REPO = Path(__file__).resolve().parents[1]
REAL_TO_WINDOWS_PATH = wk.to_windows_path                    # kept, because the fixture below replaces the module's


@pytest.fixture(autouse=True)
def no_wslpath(monkeypatch):
    """Make every test give the same answer on WSL (which has wslpath) and on plain Linux (which has not).

    The pretend renderer writes into the folder it is given, so it must be given a real Linux path. Where wslpath
    exists, the real helper would turn that into a Windows-style name and the files would land in the wrong place.
    The helper itself is tested separately, with a pretend wslpath.
    """
    monkeypatch.setattr(wk, "to_windows_path", lambda path: str(path))


class Clock:
    """A clock the tests move by hand."""

    def __init__(self):
        """Start at an arbitrary time."""
        self.t = 1_000_000.0

    def __call__(self):
        """The current fake time."""
        return self.t


class WorkerS3:
    """download_file / upload_file over dictionaries."""

    def __init__(self):
        """Start empty."""
        self.objects, self.uploaded = {}, {}

    def download_file(self, bucket, key, dest):
        """Write a stored object to a file."""
        Path(dest).write_bytes(self.objects[(bucket, key)])

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        """Remember an uploaded file's key, size and content type."""
        self.uploaded[key] = (Path(path).stat().st_size, (ExtraArgs or {}).get("ContentType"))


class FakeRun:
    """Stands in for run_process: acts out the pipeline (status.json, results.json) or the renderer."""

    def __init__(self, clock, scenario="pass", render="ok", cancel_after=None, table=None):
        """scenario: pass | fail_gate | stopped_gate | error | timeout | cancel | shutdown; render: ok | fail."""
        self.clock, self.scenario, self.render, self.calls = clock, scenario, render, []
        self.cancel_after, self.table, self.envs, self.specs = cancel_after, table, [], []

    def __call__(self, cmd, env, log_path, timeout_s, tick):
        """Pretend to run one command."""
        self.calls.append(cmd)
        self.envs.append(env)
        if cmd[0] != sys.executable:                                # the renderer
            if self.render == "ok":
                out = Path(cmd[cmd.index("--out") + 1])
                out.mkdir(parents=True, exist_ok=True)
                for name in ("01_hero.png", "02_side_slice.png", "orbit.mp4"):
                    (out / name).write_bytes(b"x" * 100)
                return "exit:0"
            return "exit:1"
        spec = json.loads(Path(cmd[2]).read_text())
        self.specs.append(spec)
        d = Path(cmd[4]) / f"{spec['name']}_20261112-150000"
        (d / "case").mkdir(parents=True)
        (d / "case" / "w.foam").touch()
        (d / "case" / "processor0").mkdir()
        (d / "case" / "job.json").write_text(json.dumps({"parameters": {"Aref_m2": 0.5}}))
        stages = []
        for i, stage in enumerate(["inspect", "case", "mesh", "solve"]):
            stages.append({"stage": stage, "state": "running"})
            (d / "status.json").write_text(json.dumps({"state": f"running: {stage}", "stages": stages,
                                                       "cells": 1_000_000 if i >= 2 else None,
                                                       "iterations": 100 * i}))
            self.clock.t += 20
            if tick():
                return "stopped"
            stages[-1].update(state="done", seconds=5.0)
        if self.scenario == "timeout":
            return "timeout"
        if self.scenario == "cancel":
            self.table.items[spec["notes"].split()[-1]]["cancel_requested"] = True
            self.clock.t += 20
            return "stopped" if tick() else "exit:0"
        status = {"stages": stages, "cells": 1_000_000, "iterations": 400, "stopped_because": "converged"}
        if self.scenario == "stopped_gate":
            (d / "inspection.json").write_text(json.dumps({"checks": [
                {"level": "PASS", "message": "ok"}, {"level": "FAIL", "message": "The surface has holes."}]}))
            (d / "status.json").write_text(json.dumps({**status, "state": "stopped by gate",
                                                       "reason": "geometry inspection FAIL; see inspection.json"}))
            return "exit:1"
        if self.scenario == "error":
            (d / "status.json").write_text(json.dumps({**status, "state": "error", "reason": "snappyHexMesh crashed"}))
            return "exit:2"
        result = "FAIL" if self.scenario == "fail_gate" else "PASS"
        (d / "status.json").write_text(json.dumps({**status, "state": f"finished: {result}"}))
        (d / "results.json").write_text(json.dumps({
            "status": result, "final": {"Cd_mean": 0.31, "Cl_mean": -1.8, "iterations": 400},
            "gates": [{"code": "CONVERGED", "level": "PASS" if result == "PASS" else "FAIL", "message": "settled"}]}))
        (d / "summary.md").write_text("# Job\n")
        return "exit:0"


def make_worker(tmp_path, scenario="pass", render="ok", **cfg_extra):
    """A worker wired to in-memory AWS and a pretend pipeline, plus the objects to inspect afterwards."""
    clock, table, s3, sns = Clock(), FakeTable(), WorkerS3(), FakeSNS()
    run = FakeRun(clock, scenario, render, table=table)
    pv = tmp_path / "pvbatch.exe"
    pv.write_text("")
    cfg = {**wk.DEFAULTS, "jobs_dir": str(tmp_path / "jobs"), "repo": str(REPO), "topic_arn": "arn:t",
           "uploads_bucket": "up", "results_bucket": "res", "pvbatch": str(pv), "processors": 4, **cfg_extra}
    return wk.Worker(cfg, table, s3, sns, run=run, now=clock), table, s3, sns, run


def queue_job(table, s3, job_id="a" * 32, status="queued", created="2026-11-12T10:00:00Z", **opts):
    """Add a queued run and its uploaded zip."""
    options = {"units": "mm", "half_model": True, "solver": "SST", "preset": "quick", "speed": 30.0,
               "ground": "moving", **opts}
    table.put_item(Item={"job_id": job_id, "user_sub": "u", "user_email": "a@x.co", "label": "My wing",
                         "filename": "w.zip", "options": cc.to_ddb(options), "status": status, "created_at": created,
                         "stages": {}, "progress": {}})
    s3.objects[("up", f"uploads/{job_id}/wing.zip")] = make_zip({"w.step": STEP_TEXT})
    return job_id


# ---- a normal run ------------------------------------------------------------
def test_full_run_publishes_numbers_pictures_and_video(tmp_path):
    """A good run becomes 'done' with Cd, Cl, S*Cl, gates and downloadable files, and nothing is emailed."""
    w, table, s3, sns, run = make_worker(tmp_path)
    jid = queue_job(table, s3)
    job = w.claim_next()
    assert job["status"] == "running" and job["attempts"] == 1 and job["worker"] == w.host
    w.process(job)
    done = cc.from_ddb(table.items[jid])
    assert done["status"] == "done" and done["stage"] == "Finished" and done["finished_at"]
    s = done["summary"]
    assert (s["result"], s["cd"], s["cl"], s["aref_m2"]) == ("PASS", 0.31, -1.8, 0.5)
    assert s["s_cl"] == pytest.approx(-0.9) and s["gates"][0]["code"] == "CONVERGED" and s["solver"] == "SST"
    assert {f["name"]: f["kind"] for f in done["files"]} == {
        "01_hero.png": "image", "02_side_slice.png": "image", "orbit.mp4": "video", "summary.md": "report",
        "results.zip": "archive"}
    assert s3.uploaded["results/" + jid + "/orbit.mp4"][1] == "video/mp4"
    assert done["stages"]["render"]["state"] == "done" and done["stages"]["solve"]["state"] == "done"
    assert not sns.sent
    assert not list((tmp_path / "jobs").glob("w*/case/processor*"))
    assert not (tmp_path / "jobs" / "incoming" / jid).exists()
    with zipfile.ZipFile(next((tmp_path / "jobs").glob("w*/results.zip"))) as zf:
        assert "01_hero.png" in zf.namelist() and "summary.md" in zf.namelist()


def test_pipeline_gets_the_right_job_file_and_no_cloud_keys(tmp_path, monkeypatch):
    """The job file is built from validated options, and the pipeline's environment has no AWS variables."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-leak")
    monkeypatch.setenv("AWS_PROFILE", "admin")
    w, table, s3, sns, run = make_worker(tmp_path)
    queue_job(table, s3, solver="SA", ground="fixed", half_model=False, units="in")
    w.process(w.claim_next())
    spec = run.specs[0]
    pipeline_cmd = run.calls[0]
    assert pipeline_cmd[0] == sys.executable and pipeline_cmd[1].endswith("run_job.py")
    assert not any(k.startswith("AWS_") for env in run.envs for k in env)
    render_cmd = run.calls[1]
    assert "--full-model" in render_cmd and render_cmd[render_cmd.index("--speed") + 1] == "30.0"
    assert spec["case"] == {"speed": 30.0, "ground": "fixed", "processors": 4,
                            "turbulence_model": "SpalartAllmaras", "half_model": False}
    assert spec["units"] == "in" and spec["geometry"].endswith("incoming/" + "a" * 32 + "/model.step")


def test_job_file_contents():
    """build_job_file maps the site's options onto run_job.py's job file."""
    job = {"job_id": "c" * 32, "options": {"units": "in", "half_model": False, "solver": "SA", "preset": "standard",
                                           "speed": 25.5, "ground": "fixed"}}
    spec = wk.build_job_file(job, Path("/x/model.step"), 12)
    assert spec == {"name": "w" + "c" * 12, "geometry": "/x/model.step", "units": "in", "preset": "standard",
                    "notes": "Web run " + "c" * 32,
                    "case": {"speed": 25.5, "ground": "fixed", "processors": 12,
                             "turbulence_model": "SpalartAllmaras", "half_model": False}}
    for bad in ({"units": "mm", "solver": "evil"}, {"solver": "SA"}):
        with pytest.raises(cc.ValidationError):
            wk.build_job_file({"job_id": "d" * 32, "options": bad}, Path("/x"), 4)


def test_progress_is_pushed_while_running(tmp_path):
    """Stage and iteration progress reach the database during the run, and the worker's heartbeat is kept."""
    w, table, s3, sns, run = make_worker(tmp_path)
    queue_job(table, s3)
    seen = []
    original = w.push

    def spy(job_id, fields, only_running=True):
        """Record progress writes, then do them."""
        if "progress" in fields:
            seen.append((fields["stage"], fields["progress"]))
        return original(job_id, fields, only_running)
    w.push = spy
    w.process(w.claim_next())
    stages = [s for s, _ in seen]
    assert "Checking the geometry" in stages and "Meshing" in stages and "Solving" in stages
    assert {"cells": 1_000_000, "iterations": 300} in [p for _, p in seen]
    assert cc.from_ddb(table.items["_worker"])["host"] == w.host


# ---- things going wrong --------------------------------------------------------
def test_gate_stop_explains_the_geometry_problem_and_emails_the_owner(tmp_path):
    """A geometry that fails inspection is 'stopped' with the failed check in plain words, and the owner is told."""
    w, table, s3, sns, run = make_worker(tmp_path, scenario="stopped_gate")
    jid = queue_job(table, s3)
    w.process(w.claim_next())
    row = table.items[jid]
    assert row["status"] == "stopped" and "The surface has holes." in row["message"]
    assert len(sns.sent) == 1 and "stopped by a quality check" in sns.sent[0]["Subject"]
    assert "My wing" in sns.sent[0]["Message"] and "a@x.co" in sns.sent[0]["Message"]


def test_pipeline_error_fails_the_run_and_emails(tmp_path):
    """An error inside the pipeline fails the run with its reason."""
    w, table, s3, sns, run = make_worker(tmp_path, scenario="error")
    jid = queue_job(table, s3)
    w.process(w.claim_next())
    assert table.items[jid]["status"] == "failed" and "snappyHexMesh crashed" in table.items[jid]["message"]
    assert len(sns.sent) == 1 and "failed" in sns.sent[0]["Subject"]


def test_time_limit_fails_the_run(tmp_path):
    """A run that exceeds the time limit is failed with the limit in the message."""
    w, table, s3, sns, run = make_worker(tmp_path, scenario="timeout", max_runtime_minutes=7)
    jid = queue_job(table, s3)
    w.process(w.claim_next())
    assert table.items[jid]["status"] == "failed" and "7 minute limit" in table.items[jid]["message"]


def test_a_fail_gate_still_publishes_results_but_warns_the_owner(tmp_path):
    """A finished run whose final check says FAIL is 'done' (the user sees the numbers) and the owner is emailed."""
    w, table, s3, sns, run = make_worker(tmp_path, scenario="fail_gate")
    jid = queue_job(table, s3)
    w.process(w.claim_next())
    assert table.items[jid]["status"] == "done" and table.items[jid]["summary"]["result"] == "FAIL"
    assert len(sns.sent) == 1 and "FAIL" in sns.sent[0]["Subject"]


def test_picture_failure_keeps_the_numbers(tmp_path):
    """If ParaView fails the run is still done with its numbers, says why there are no pictures, and emails."""
    w, table, s3, sns, run = make_worker(tmp_path, render="fail")
    jid = queue_job(table, s3)
    w.process(w.claim_next())
    row = table.items[jid]
    assert row["status"] == "done" and "pictures" in row["message"] and row["stages"]["render"]["state"] == "failed"
    assert [f["name"] for f in row["files"]] == ["summary.md", "results.zip"]
    assert len(sns.sent) == 1 and "Pictures failed" in sns.sent[0]["Subject"]


def test_missing_paraview_is_reported_not_fatal(tmp_path):
    """No ParaView on the PC: numbers still published, with an explanation."""
    w, table, s3, sns, run = make_worker(tmp_path, pvbatch=str(tmp_path / "nope.exe"))
    jid = queue_job(table, s3)
    w.process(w.claim_next())
    assert table.items[jid]["status"] == "done" and "ParaView was not found" in table.items[jid]["message"]


def test_hostile_zip_is_refused_by_the_worker_too(tmp_path):
    """The worker re-checks the zip: a zip with two files fails the run before anything is executed."""
    w, table, s3, sns, run = make_worker(tmp_path)
    jid = queue_job(table, s3)
    s3.objects[("up", f"uploads/{jid}/wing.zip")] = make_zip({"a.step": STEP_TEXT, "b.step": STEP_TEXT})
    w.process(w.claim_next())
    assert table.items[jid]["status"] == "failed" and "2 files" in table.items[jid]["message"] and not run.calls


def test_cancel_while_running(tmp_path):
    """Cancelling a running job stops it and records 'cancelled' with no email."""
    w, table, s3, sns, run = make_worker(tmp_path, scenario="cancel")
    jid = queue_job(table, s3)
    w.process(w.claim_next())
    assert table.items[jid]["status"] == "cancelled" and not sns.sent


# ---- queue behaviour ------------------------------------------------------------
def test_oldest_job_first_and_a_job_is_claimed_once(tmp_path):
    """The oldest queued run is taken first; once claimed it cannot be claimed again."""
    w, table, s3, sns, run = make_worker(tmp_path)
    queue_job(table, s3, "b" * 32, created="2026-11-12T10:05:00Z")
    queue_job(table, s3, "a" * 32, created="2026-11-12T10:00:00Z")
    first, second = w.claim_next(), w.claim_next()
    assert (first["job_id"], second["job_id"]) == ("a" * 32, "b" * 32) and w.claim_next() is None


def test_wings_of_one_upload_are_taken_in_the_order_chosen(tmp_path):
    """Runs with the same creation second are taken by their position in the upload, not by id."""
    w, table, s3, sns, run = make_worker(tmp_path)
    for job_id, seq in (("a" * 32, 2), ("b" * 32, 1), ("c" * 32, 0)):
        queue_job(table, s3, job_id)
        table.items[job_id]["seq"] = seq
    assert [w.claim_next()["job_id"] for _ in range(3)] == ["c" * 32, "b" * 32, "a" * 32]


def test_losing_a_race_for_a_job_moves_on_to_the_next_one(tmp_path, monkeypatch):
    """If another worker claims a run between the list and the claim, this worker takes the next run instead."""
    w, table, s3, sns, run = make_worker(tmp_path)
    queue_job(table, s3, "a" * 32, created="2026-11-12T10:00:00Z")
    queue_job(table, s3, "b" * 32, created="2026-11-12T10:05:00Z")
    stale = [dict(table.items["a" * 32]), dict(table.items["b" * 32])]
    table.items["a" * 32].update(status="running", worker="OTHER-PC")
    monkeypatch.setattr(wk.cc, "query_index", lambda *a, **k: stale)
    got = w.claim_next()
    assert got["job_id"] == "b" * 32
    assert table.items["a" * 32]["status"] == "running" and table.items["a" * 32]["worker"] == "OTHER-PC"


def test_interrupted_run_is_queued_again_once_then_failed(tmp_path):
    """After a restart a run left 'running' goes back in the queue; on the second interruption it fails."""
    w, table, s3, sns, run = make_worker(tmp_path)
    jid = queue_job(table, s3)
    w.claim_next()
    w.recover_orphans()
    assert table.items[jid]["status"] == "queued" and "restarted" in table.items[jid]["message"]
    w.claim_next()
    w.recover_orphans()
    assert table.items[jid]["status"] == "failed" and "2 times" in table.items[jid]["message"] and len(sns.sent) == 1


def test_shutdown_during_a_run_requeues_it(tmp_path):
    """Ctrl+C mid-run stops the pipeline and puts the run back in the queue instead of losing it."""
    w, table, s3, sns, run = make_worker(tmp_path)
    jid = queue_job(table, s3)
    w.stop_requested = True
    w.process(w.claim_next())
    assert table.items[jid]["status"] == "queued"


def test_run_forever_once_handles_one_job_and_other_users_jobs_do_not_matter(tmp_path):
    """--once takes one job and stops; with an empty queue it just returns."""
    w, table, s3, sns, run = make_worker(tmp_path)
    queue_job(table, s3)
    w.run_forever(once=True)
    assert table.items["a" * 32]["status"] == "done"
    w.run_forever(once=True)


# ---- helpers ----------------------------------------------------------------------
def test_to_windows_path_asks_wslpath(monkeypatch):
    """Where wslpath exists its answer is used, without the trailing newline."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(stdout="\\\\wsl.localhost\\Ubuntu\\home\\x\\case\n")

    monkeypatch.setattr(wk.subprocess, "run", fake_run)
    assert REAL_TO_WINDOWS_PATH(Path("/home/x/case")) == "\\\\wsl.localhost\\Ubuntu\\home\\x\\case"
    assert calls == [["wslpath", "-w", "/home/x/case"]]


@pytest.mark.parametrize("error", [FileNotFoundError("no wslpath here"), subprocess.CalledProcessError(1, "wslpath")])
def test_to_windows_path_falls_back_to_the_plain_path(monkeypatch, error):
    """Without wslpath (plain Linux), or if it fails, the path is passed on unchanged."""
    def fake_run(cmd, **kwargs):
        raise error

    monkeypatch.setattr(wk.subprocess, "run", fake_run)
    assert REAL_TO_WINDOWS_PATH(Path("/home/x/case")) == "/home/x/case"


def test_render_command_gives_paraview_windows_paths(monkeypatch):
    """ParaView is a Windows program, so the script, the case and the output folder are passed in Windows form."""
    monkeypatch.setattr(wk, "to_windows_path", lambda path: "WIN:" + str(path))
    cfg = {**wk.DEFAULTS, "repo": "/r", "pvbatch": "pvbatch.exe"}
    cmd = wk.render_command(cfg, {"label": "My wing", "options": {"speed": 30.0, "half_model": True}},
                            Path("/c/case.foam"), Path("/c/images"))
    assert cmd[0] == "pvbatch.exe" and cmd[1] == "WIN:/r/src/render_flow.py"
    assert cmd[cmd.index("--case") + 1] == "WIN:/c/case.foam" and cmd[cmd.index("--out") + 1] == "WIN:/c/images"
    assert "--full-model" not in cmd


def test_run_process_real_commands(tmp_path):
    """run_process runs a real command, stops it when told to, and enforces the time limit."""
    env = {"PATH": "/usr/bin:/bin"}
    log = tmp_path / "worker-test.log"
    assert wk.run_process([sys.executable, "-c", "print('hi')"], env, log, 20, lambda: False) == "exit:0"
    assert wk.run_process([sys.executable, "-c", "import sys; sys.exit(3)"], env, log, 20, lambda: False) == "exit:3"
    t0 = time.time()
    assert wk.run_process([sys.executable, "-c", "import time; time.sleep(60)"], env, log, 20,
                          lambda: True, every=0.2) == "stopped"
    assert wk.run_process([sys.executable, "-c", "import time; time.sleep(60)"], env, log, 0.3,
                          lambda: False, every=0.2) == "timeout"
    assert time.time() - t0 < 30


def test_extract_step_caps_size_and_ignores_the_stored_name(tmp_path):
    """The STEP file is written only to the chosen path, whatever the zip calls it, and a size cap is enforced."""
    z = tmp_path / "w.zip"
    z.write_bytes(make_zip({"../../evil.step": STEP_TEXT}))
    name = wk.extract_step(z, tmp_path / "model.step", 10**6)
    assert name == "evil.step" and (tmp_path / "model.step").read_bytes() == STEP_TEXT
    assert not (tmp_path.parent / "evil.step").exists()
    with pytest.raises(cc.ValidationError):
        wk.extract_step(z, tmp_path / "again.step", 20)


def test_extract_step_stops_unpacking_at_the_cap_even_if_the_check_is_fooled(tmp_path, monkeypatch):
    """Second line of defence: the bytes actually written are counted, not just the size the zip claims."""
    z = tmp_path / "w.zip"
    z.write_bytes(make_zip({"model.step": STEP_TEXT}))
    monkeypatch.setattr(wk.cc, "inspect_step_zip", lambda *a, **k: {"entry": "model.step"})
    with pytest.raises(cc.ValidationError, match="larger than allowed"):
        wk.extract_step(z, tmp_path / "out.step", 20)


def test_old_job_folders_are_cleaned_but_other_folders_are_not(tmp_path):
    """Only this worker's folders (w + 12 hex characters) older than the limit are removed."""
    import os
    mine, fresh = tmp_path / ("w" + "a" * 12 + "_1"), tmp_path / ("w" + "b" * 12 + "_2")
    others = [tmp_path / "ahmed_run_1", tmp_path / "wing_tests_1", tmp_path / ("w" + "A" * 12 + "_1")]
    for d in (mine, fresh, *others):
        d.mkdir()
    old = time.time() - 30 * 86400
    for d in (mine, *others):
        os.utime(d, (old, old))
    assert wk.clean_old_jobs(tmp_path, 14) == 1 and not mine.exists() and fresh.exists()
    assert all(d.exists() for d in others)


def test_digest_status_and_check_setup(tmp_path):
    """run_job's status.json becomes the stage map; the set-up check reports each requirement."""
    stages, progress, current = wk.digest_status({"stages": [{"stage": "mesh", "state": "done", "seconds": 4.5},
                                                             {"stage": "solve", "state": "running"}], "cells": 9})
    assert stages["mesh"] == {"state": "done", "seconds": 4.5} and progress == {"cells": 9} and current == "Solving"
    results = wk.check_setup({**wk.DEFAULTS, "repo": str(REPO), "pvbatch": str(tmp_path / "none")}, FakeTable())
    texts = {t: ok for ok, t in results}
    assert texts["AWS login works and the jobs table is reachable"] is True
    assert texts[f"ParaView pvbatch at {tmp_path / 'none'} (pictures need it)"] is False
