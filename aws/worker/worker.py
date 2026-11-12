#!/usr/bin/env python3
"""The worker: runs the site's queued CFD jobs on this PC.

    python aws/worker/worker.py --init     write ~/.cfd-worker/worker.json from `terraform output` (once)
    python aws/worker/worker.py --check    test the set-up: AWS login, OpenFOAM, ParaView, folders
    python aws/worker/worker.py            run until you press Ctrl+C
    python aws/worker/worker.py --once     do one queued job (if any) and stop

The worker only makes OUTGOING connections to AWS; nothing is opened on your network, and no job runs while the
program is closed. It does one job at a time:

    claim the oldest queued run -> download the zip and unpack the STEP file (size-capped) -> run
    src/run_job.py with a time limit -> copy the stage and iteration progress to the site every few seconds ->
    make pictures and a video with the Windows ParaView -> upload them -> mark the run done, stopped or failed.

If anything goes wrong the owner is emailed. Start it from a shell where OpenFOAM is loaded and the project's
virtual environment is active, the same as for run_job.py.

Safety: a job runs as YOUR Linux user, so a hostile STEP file would meet the same permissions you have. The
pipeline is started without AWS credentials in its environment. For public use run the worker as a separate,
unprivileged user (see aws/README.md). Uploads are limited by size, unpacked size and run time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda"))
import cfd_common as cc  # noqa: E402

__version__ = "1.0.0"
HOME_DIR = Path.home() / ".cfd-worker"
DEFAULTS = {
    "profile": "cfd-worker",                 # AWS profile holding the worker's access key
    "repo": str(Path(__file__).resolve().parents[2]),
    "jobs_dir": str(Path(os.environ.get("FOAM_RUN", Path.home())) / "web-jobs"),
    "processors": 12,                        # the core-count benchmark: 12 is within 3% of 16
    "max_runtime_minutes": 240,              # the pipeline step; a longer run is stopped and marked failed
    "render_timeout_minutes": 40,
    "poll_seconds": 15,
    "keep_days": 14,                         # finished job folders on this PC are deleted after this long
    "delete_processor_dirs": True,           # the decomposed copies are huge and not needed once reconstructed
    "max_step_mb": 100,
    "max_attempts": 2,                       # a run interrupted twice is marked failed instead of retried again
    "pvbatch": "/mnt/c/Program Files/ParaView 6.2.0/bin/pvbatch.exe",
    "render": {"scenes": "hero,side,wake,under,orbit", "width": 1600, "seconds": 8, "fps": 30},
}
TITLES = {"01_hero.png": "Streamlines", "02_side_slice.png": "Side view, speed",
          "03_wake_slices.png": "Wake cross-sections", "04_vortex_cores.png": "Vortex cores",
          "05_underside_pressure.png": "Underside pressure", "06_road_footprint.png": "Road footprint",
          "orbit.mp4": "360-degree video"}
TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".mp4": "video/mp4", ".md": "text/markdown; charset=utf-8",
         ".zip": "application/zip"}
# The renderer's scene names (src/render_flow.py), in plain words for the message about a partly failed render.
SCENE_NAMES = {"hero": "streamlines", "side": "side view", "wake": "wake cross-sections", "vortex": "vortex cores",
               "under": "underside pressure", "ground": "road footprint", "orbit": "360-degree video"}


def clean_env() -> dict:
    """This process's environment without any AWS_* variables, for programs that must never see cloud keys."""
    return {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}


def log(msg: str) -> None:
    """Print a time-stamped line."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Small helpers (tested on their own)
# --------------------------------------------------------------------------- #
def to_windows_path(path: Path) -> str:
    """The path as Windows programs see it (\\\\wsl.localhost\\...); unchanged where wslpath does not exist."""
    try:
        return subprocess.run(["wslpath", "-w", str(path)], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return str(path)


def run_process(cmd: list, env: dict, log_path: Path, timeout_s: float, tick, every: float = 3.0) -> str:
    """Run a command in its own process group, appending its output to log_path.

    tick() is called every `every` seconds and may return True to stop the command. Returns "exit:<code>",
    "timeout" or "stopped". A stopped or timed-out command and all its children (mpirun, solvers) are ended: first
    politely, then by force after 20 seconds.
    """
    start = time.time()
    with open(log_path, "ab") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        while True:
            try:
                return f"exit:{proc.wait(timeout=every)}"
            except subprocess.TimeoutExpired:
                pass
            verdict = "timeout" if time.time() - start > timeout_s else "stopped" if tick() else None
            if verdict:
                for sig, wait in ((signal.SIGTERM, 20), (signal.SIGKILL, 5)):
                    try:
                        os.killpg(proc.pid, sig)
                        proc.wait(timeout=wait)
                        break
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        continue
                return verdict


def extract_step(zip_path: Path, dest: Path, max_bytes: int) -> str:
    """Unpack the single STEP file from a zip to `dest`, refusing anything bigger than max_bytes; returns its name.

    The name inside the zip is only used for display: the file is always written to `dest`, so a hostile name
    cannot place a file anywhere else. The zip is checked again here even though the site checked it, because this
    program does not trust what is in the cloud.
    """
    size = zip_path.stat().st_size

    def fetch(first: int, last: int) -> bytes:
        """Bytes first..last of the local zip."""
        with open(zip_path, "rb") as f:
            f.seek(first)
            return f.read(last - first + 1)

    info = cc.inspect_step_zip(fetch, size, max_bytes)
    written = 0
    with zipfile.ZipFile(zip_path) as zf, zf.open(info["entry"]) as src, open(dest, "wb") as out:
        while chunk := src.read(1 << 20):
            written += len(chunk)
            if written > max_bytes:
                raise cc.ValidationError("The STEP file is larger than allowed once unpacked.")
            out.write(chunk)
    return info["entry"].rsplit("/", 1)[-1]


def build_job_file(job: dict, step: Path, processors: int) -> dict:
    """The run_job.py job file for one run, built only from re-checked values (never copied from the database)."""
    opts = job.get("options") or {}
    settings = cc.validate_settings(opts)
    if opts.get("units") not in cc.UNITS:
        raise cc.ValidationError("The run has no valid units.")
    return {"name": "w" + job["job_id"][:12], "geometry": str(step), "units": opts["units"],
            "preset": settings["preset"], "notes": f"Web run {job['job_id']}",
            "case": {"speed": settings["speed"], "ground": settings["ground"], "processors": int(processors),
                     "turbulence_model": cc.SOLVERS[settings["solver"]], "half_model": bool(opts.get("half_model"))}}


def digest_status(status: dict) -> tuple[dict, dict, str]:
    """From run_job's status.json: the stage map, the progress numbers and the sentence for the current stage."""
    stages, current = {}, ""
    for entry in status.get("stages", []):
        stages[entry.get("stage", "?")] = {"state": entry.get("state", ""), "seconds": entry.get("seconds")}
        if entry.get("state") == "running":
            current = cc.STAGE_TEXT.get(entry.get("stage"), str(entry.get("stage")))
    progress = {k: status[k] for k in ("cells", "iterations") if status.get(k) is not None}
    return stages, progress, current


def summarise(job_dir: Path, options: dict) -> dict:
    """The numbers shown on the results page, from results.json, status.json and the case's job.json."""
    results = json.loads((job_dir / "results.json").read_text())
    status = json.loads((job_dir / "status.json").read_text())
    final = results.get("final") or {}
    cd, cl = final.get("Cd_mean", final.get("Cd")), final.get("Cl_mean", final.get("Cl"))
    aref = None
    try:
        aref = json.loads((job_dir / "case" / "job.json").read_text())["parameters"]["Aref_m2"]
    except (OSError, KeyError, ValueError):
        pass
    return {"result": results.get("status"), "cd": cd, "cl": cl, "iterations": final.get("iterations"),
            "cells": status.get("cells"), "stopped_because": status.get("stopped_because"), "aref_m2": aref,
            "s_cd": None if None in (cd, aref) else cd * aref, "s_cl": None if None in (cl, aref) else cl * aref,
            "solver": options.get("solver"), "preset": options.get("preset"), "speed": options.get("speed"),
            "gates": [{"code": g["code"], "level": g["level"], "message": g["message"]}
                      for g in results.get("gates", [])]}


def explain_stop(job_dir: Path, reason: str) -> str:
    """A user-readable reason for a stopped run: the failed geometry checks if there are any, a mesh too coarse for the
    part in plain words, else the raw reason.
    """
    try:
        checks = json.loads((job_dir / "inspection.json").read_text()).get("checks", [])
        bad = [c["message"] for c in checks if c.get("level") in ("FAIL", "ACTION")]
        if bad:
            return "The geometry check did not pass: " + " ".join(bad[:3])
    except (OSError, ValueError):
        pass
    marker = "Mesh too coarse for this part:"
    if marker in reason:
        found = re.split(r"\s*\(surface cells", reason.split(marker, 1)[1])[0].strip()
        return (f"This model is too small for the quality you chose, so the mesh cannot draw it ({found}). "
                "Use a higher quality setting if you have not already, and check that the units you picked match the "
                "file: a model entered in the wrong units can look a thousand times too small.")
    return reason or "A quality check stopped the run."


def render_problem(verdict: str, log_path: Path, scenes: str, drawn: int) -> str:
    """One sentence for a picture step that did not end cleanly: which pictures failed, or how it stopped.

    The renderer draws each scene on its own and, if some fail, ends with 'done; FAILED scenes: a, b' in render.log;
    that line gives the names. If it is missing (crash, time limit) the sentence says how the step ended and how many
    files had been drawn by then.
    """
    try:
        text = log_path.read_text(errors="replace")
    except OSError:
        text = ""
    wanted = [s.strip() for s in str(scenes).split(",") if s.strip()]
    m = re.search(r"FAILED scenes: ([A-Za-z0-9_, ]+)", text)
    if m:
        failed = [s.strip() for s in m.group(1).split(",") if s.strip()]
        names = ", ".join(SCENE_NAMES.get(s, s) for s in failed)
        return (f"Drew {max(len(wanted) - len(failed), 0)} of {len(wanted)} pictures and videos; failed: {names}. "
                "See render.log.")
    done = f" {drawn} file{'s were' if drawn != 1 else ' was'} drawn before it stopped." if drawn else ""
    return f"The pictures and video could not be completed ({verdict}); see render.log.{done}"


def clean_old_jobs(jobs_dir: Path, keep_days: float, now: float | None = None) -> int:
    """Delete this worker's own job folders (named w<12 hex>_...) older than keep_days; returns how many."""
    cutoff = (now or time.time()) - keep_days * 86400
    removed = 0
    for d in jobs_dir.glob("w*_*"):
        if d.is_dir() and re.fullmatch(r"w[0-9a-f]{12}_.+", d.name) and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def render_command(cfg: dict, job: dict, foam: Path, out: Path) -> list:
    """The pvbatch command that makes the pictures and video for one run."""
    r, opts = {**DEFAULTS["render"], **cfg.get("render", {})}, job["options"]
    label = re.sub(r"[^A-Za-z0-9 .,_-]", "", job.get("label", ""))[:40].strip()
    cmd = [cfg["pvbatch"], to_windows_path(Path(cfg["repo"]) / "src" / "render_flow.py"),
           "--case", to_windows_path(foam), "--out", to_windows_path(out), "--scenes", str(r["scenes"]),
           "--width", str(int(r["width"])), "--seconds", str(float(r["seconds"])), "--fps", str(int(r["fps"])),
           "--levels", "3", "--density", "30", "--speed", str(float(opts["speed"])),
           "--caption", f"{label} - {opts['speed']:g} m/s".strip(" -")]
    return cmd if opts.get("half_model") else cmd + ["--full-model"]


# --------------------------------------------------------------------------- #
# The worker
# --------------------------------------------------------------------------- #
class Worker:
    """Claims and runs the site's queued jobs, one at a time."""

    def __init__(self, cfg: dict, table, s3, sns, run=run_process, now=time.time):
        """cfg: settings (see DEFAULTS plus the terraform names); table, s3, sns: boto3 objects or stand-ins;
        run: the function that runs a command (tests replace it); now: the clock.
        """
        self.cfg, self.table, self.s3, self.sns, self.run, self.now = cfg, table, s3, sns, run, now
        self.host = socket.gethostname()
        self.jobs_dir = Path(cfg["jobs_dir"]).expanduser()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.stop_requested = False
        self._beat = 0.0

    # ---- small things ------------------------------------------------------------------------------------
    def heartbeat(self, force: bool = False) -> None:
        """Tell the site this worker is alive (at most every 30 seconds unless forced)."""
        if force or self.now() - self._beat >= 30:
            self._beat = self.now()
            try:
                cc.update_fields(self.table, cc.WORKER_KEY,
                                 {"last_seen": cc.utc_now(), "host": self.host, "version": __version__})
            except Exception as exc:                                # noqa: BLE001 - a missed beat is not fatal
                log(f"heartbeat failed: {exc}")

    def alert(self, subject: str, job: dict, detail: str) -> None:
        """Email the owner about one run."""
        text = (f"Run: {job.get('label')} ({job['job_id']})\nUser: {job.get('user_email')}\n"
                f"File: {job.get('filename')}\nOptions: {json.dumps(job.get('options'))}\n\n{detail}\n\n"
                f"Worker: {self.host}, folder {self.jobs_dir}\nMore: python aws/tools/cfd_admin.py jobs")
        cc.send_alert(self.sns, self.cfg["topic_arn"], subject, text)

    def push(self, job_id: str, fields: dict, only_running: bool = True) -> dict | None:
        """Write fields to a job; with only_running, returns None if the job is no longer running (it was cancelled)."""
        try:
            return cc.update_fields(self.table, {"job_id": job_id}, {**fields, "updated_at": cc.utc_now()},
                                    only_if=("status", (cc.RUNNING,)) if only_running else None)
        except Exception as exc:                                    # noqa: BLE001
            if cc.is_conditional_failure(exc):
                return None
            raise

    # ---- claiming ----------------------------------------------------------------------------------------
    def claim_next(self) -> dict | None:
        """Take the oldest queued run (so two workers could never run the same job); None if the queue is empty."""
        for job in cc.queue_order(cc.query_index(self.table, "by_status", "status", cc.QUEUED, limit=10)):
            now = cc.utc_now()
            try:
                return cc.update_fields(
                    self.table, {"job_id": job["job_id"]},
                    {"status": cc.RUNNING, "stage": "Starting", "started_at": now, "updated_at": now,
                     "worker": self.host, "attempts": int(job.get("attempts", 0)) + 1, "message": ""},
                    only_if=("status", (cc.QUEUED,)))
            except Exception as exc:                                # noqa: BLE001
                if not cc.is_conditional_failure(exc):
                    raise
        return None

    def recover_orphans(self) -> None:
        """At start-up: a run still marked running by this PC was interrupted. Queue it again, or fail it if it
        has already been tried max_attempts times.
        """
        for job in cc.query_index(self.table, "by_status", "status", cc.RUNNING, limit=20):
            if job.get("worker") == self.host:
                log(f"recovering interrupted run {job['job_id'][:8]}")
                self.requeue_or_fail(job, "The worker program was restarted during this run.")

    def requeue_or_fail(self, job: dict, why: str) -> None:
        """Put an interrupted run back in the queue, or fail it (and email the owner) after too many attempts."""
        if int(job.get("attempts", 1)) < int(self.cfg["max_attempts"]):
            self.push(job["job_id"], {"status": cc.QUEUED, "stage": "Waiting in the queue", "stages": {},
                                      "progress": {}, "message": why + " It will start again."})
        else:
            self.finish(job, cc.FAILED, f"{why} It was tried {job.get('attempts')} times, so it was stopped.")

    # ---- one job -----------------------------------------------------------------------------------------
    def process(self, job: dict) -> None:
        """Run one claimed job from download to published results. Never raises: every outcome is recorded."""
        job_id = job["job_id"]
        log(f"job {job_id[:8]} '{job.get('label')}' started")
        try:
            self._process(job)
        except cc.ValidationError as exc:
            self.finish(job, cc.FAILED, str(exc))
        except Exception as exc:                                    # noqa: BLE001 - nothing may kill the worker loop
            log(f"job {job_id[:8]} crashed: {type(exc).__name__}: {exc}")
            self.finish(job, cc.FAILED, f"The worker hit an unexpected error ({type(exc).__name__}).",
                        detail=f"{type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(self.jobs_dir / "incoming" / job_id, ignore_errors=True)

    def _process(self, job: dict) -> None:
        """The steps of process(): fetch, run, summarise, render, publish."""
        job_id, name = job["job_id"], "w" + job["job_id"][:12]
        incoming = self.jobs_dir / "incoming" / job_id
        incoming.mkdir(parents=True, exist_ok=True)
        self.push(job_id, {"stage": "Fetching your file"})
        self.s3.download_file(self.cfg["uploads_bucket"], f"uploads/{job_id}/wing.zip", str(incoming / "wing.zip"))
        extract_step(incoming / "wing.zip", incoming / "model.step", int(self.cfg["max_step_mb"]) * 1_000_000)
        spec = build_job_file(job, incoming / "model.step", self.cfg["processors"])
        (incoming / "job.json").write_text(json.dumps(spec, indent=2))

        outcome = self.run_stage(job, [sys.executable, str(Path(self.cfg["repo"]) / "src" / "run_job.py"),
                                       str(incoming / "job.json"), "--jobs-dir", str(self.jobs_dir)],
                                 name, incoming / "worker.log", self.cfg["max_runtime_minutes"] * 60)
        (incoming / "model.step").unlink(missing_ok=True)
        job_dir = next(iter(sorted(self.jobs_dir.glob(f"{name}_*"))), None)
        status = {}
        if job_dir and (job_dir / "status.json").exists():
            status = json.loads((job_dir / "status.json").read_text())
        state = status.get("state", "")
        if status:                                                  # the last progress the polling may have missed
            stages, progress, _ = digest_status(status)
            self.push(job_id, {"stages": stages, "progress": progress})

        if outcome == "stopped":
            return self.after_stop(job)
        if outcome == "timeout":
            return self.finish(job, cc.FAILED, f"The run took longer than the {self.cfg['max_runtime_minutes']} "
                                               "minute limit and was stopped.")
        if state == "stopped by gate":
            return self.finish(job, cc.STOPPED, explain_stop(job_dir, status.get("reason", "")), job_dir=job_dir)
        if job_dir is None or not state.startswith("finished") or not (job_dir / "results.json").exists():
            reason = status.get("reason") or f"The pipeline ended unexpectedly ({outcome})."
            return self.finish(job, cc.FAILED, f"The run failed: {reason}", job_dir=job_dir)

        summary = summarise(job_dir, job["options"])
        files, problem = self.make_results(job, job_dir, spec)
        extra = f" {problem}" if problem else ""
        self.finish(job, cc.DONE, ("The run finished." + extra).strip(), summary=summary, files=files, job_dir=job_dir)
        if summary["result"] == "FAIL":
            self.alert("Run finished with a FAIL check", job, "The pipeline finished but a quality gate failed:\n"
                       + "\n".join(f"[{g['level']}] {g['code']}: {g['message']}" for g in summary["gates"]))
        if problem:
            self.alert("Pictures failed for a run", job, problem)

    def run_stage(self, job: dict, cmd: list, name: str, log_path: Path, timeout_s: float) -> str:
        """Run the pipeline with progress copied to the site every few seconds. Returns the run_process verdict."""
        job_id = job["job_id"]
        env = clean_env()
        last = {"key": None, "at": 0.0, "check": 0.0}

        def tick() -> bool:
            """Report progress, keep the heartbeat, and say whether the run should stop (cancelled or shutting down)."""
            self.heartbeat()
            now = self.now()
            job_dir = next(iter(sorted(self.jobs_dir.glob(f"{name}_*"))), None)
            if job_dir and (job_dir / "status.json").exists():
                try:
                    stages, progress, current = digest_status(json.loads((job_dir / "status.json").read_text()))
                except ValueError:
                    stages = None                                   # caught half-way through a write; next time
                if stages is not None:
                    key = json.dumps([stages, progress], sort_keys=True)
                    if (key != last["key"] and now - last["at"] >= 15) or now - last["at"] >= 120:
                        if self.push(job_id, {"stages": stages, "progress": progress,
                                              "stage": current or "Running"}) is None:
                            return True
                        last["key"], last["at"] = key, now
            if self.stop_requested:
                return True
            if now - last["check"] >= 10:
                last["check"] = now
                item = self.table.get_item(Key={"job_id": job_id}).get("Item") or {}
                return bool(item.get("cancel_requested")) or item.get("status") != cc.RUNNING
            return False

        return self.run(cmd, env, log_path, timeout_s, tick)

    def set_stage(self, job_id: str, stage: str, state: str) -> None:
        """Record one stage's state in the job's stage map (and say so in words while it runs)."""
        item = cc.from_ddb(self.table.get_item(Key={"job_id": job_id}).get("Item") or {})
        stages = {**item.get("stages", {}), stage: {"state": state}}
        self.push(job_id, {"stages": stages, **({"stage": cc.STAGE_TEXT[stage]} if state == "running" else {})})

    def idle_tick(self) -> bool:
        """Called while a long command runs without its own progress reporting: keep the heartbeat, honour Ctrl+C."""
        self.heartbeat()
        return self.stop_requested

    def after_stop(self, job: dict) -> None:
        """The run was stopped from outside: because it was cancelled, or because this worker is shutting down."""
        item = cc.from_ddb(self.table.get_item(Key={"job_id": job["job_id"]}).get("Item") or {})
        if item.get("cancel_requested") or item.get("status") == cc.CANCELLED:
            self.push(job["job_id"], {"status": cc.CANCELLED, "stage": "Cancelled", "finished_at": cc.utc_now(),
                                      "message": "Cancelled by you."}, only_running=False)
        else:
            self.requeue_or_fail(item or job, "The worker program was stopped during this run.")

    def make_results(self, job: dict, job_dir: Path, spec: dict) -> tuple[list, str]:
        """Render the pictures and video, zip everything, upload it, and return ([file records], problem or "").

        A failure here does not lose the run: the numbers are already known, so the run is still marked done and
        the problem is reported. All files are published with plain names; the site only serves names it allows.
        """
        job_id, problem = job["job_id"], ""
        images = job_dir / "images"
        self.set_stage(job_id, "render", "running")
        if self.cfg.get("pvbatch") and Path(self.cfg["pvbatch"]).exists():
            foam = next(iter((job_dir / "case").glob("*.foam")), None)
            cmd = render_command(self.cfg, job, foam, images) if foam else None
            if cmd:
                verdict = self.run(cmd, clean_env(), job_dir / "render.log",
                                   self.cfg["render_timeout_minutes"] * 60, self.idle_tick)
                if verdict != "exit:0":
                    drawn = len([p for p in images.glob("*") if p.suffix.lower() in (".png", ".jpg", ".mp4")]) \
                        if images.is_dir() else 0
                    problem = render_problem(verdict, job_dir / "render.log",
                                             {**DEFAULTS["render"], **self.cfg.get("render", {})}["scenes"], drawn)
            else:
                problem = "No .foam file was found to draw pictures from."
        else:
            problem = "ParaView was not found on the worker, so there are no pictures."
        self.set_stage(job_id, "render", "failed" if problem else "done")
        publish = [p for p in sorted(images.glob("*")) if p.suffix.lower() in (".png", ".jpg", ".mp4")] \
            if images.is_dir() else []
        summary_md = job_dir / "summary.md"
        bundle = job_dir / "results.zip"
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in [*publish, summary_md, job_dir / "results.json"]:
                if p.exists():
                    zf.write(p, p.name)
        records = []
        for path, kind in [*[(p, "video" if p.suffix == ".mp4" else "image") for p in publish],
                           *([(summary_md, "report")] if summary_md.exists() else []), (bundle, "archive")]:
            self.s3.upload_file(str(path), self.cfg["results_bucket"], f"results/{job_id}/{path.name}",
                                ExtraArgs={"ContentType": TYPES.get(path.suffix.lower(), "application/octet-stream")})
            title = TITLES.get(path.name, path.stem.replace("_", " "))
            records.append({"name": path.name, "kind": kind, "title": title, "size": path.stat().st_size})
        if self.cfg["delete_processor_dirs"]:
            for d in (job_dir / "case").glob("processor*"):
                shutil.rmtree(d, ignore_errors=True)
        return records, problem

    def finish(self, job: dict, status: str, message: str, summary: dict | None = None, files: list | None = None,
               job_dir: Path | None = None, detail: str = "") -> None:
        """Record how a run ended (and email the owner unless it was a success or a cancellation)."""
        fields = {"status": status, "message": message, "finished_at": cc.utc_now(),
                  "stage": {cc.DONE: "Finished", cc.STOPPED: "Stopped by a quality check"}.get(status, "Failed"),
                  "summary": summary or {}, "files": files or []}
        if status == cc.FAILED and job_dir and (job_dir / "status.json").exists():
            fields["stages"] = digest_status(json.loads((job_dir / "status.json").read_text()))[0]
        self.push(job["job_id"], fields)
        log(f"job {job['job_id'][:8]} -> {status}: {message}")
        if status == cc.STOPPED:
            self.alert("Run stopped by a quality check", job, message)
        elif status == cc.FAILED:
            self.alert("Run failed", job, message + (f"\n\n{detail}" if detail else ""))

    # ---- the loop -----------------------------------------------------------------------------------------
    def run_forever(self, once: bool = False) -> None:
        """Poll for work until stopped (Ctrl+C or SIGTERM finish the current step cleanly)."""
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop_requested", True))
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop_requested", True))
        self.heartbeat(force=True)
        self.recover_orphans()
        last_clean = 0.0
        log(f"worker {__version__} ready on {self.host}; waiting for jobs (Ctrl+C to stop)")
        while not self.stop_requested:
            self.heartbeat()
            job = self.claim_next()
            if job:
                try:
                    self.process(job)
                except Exception as exc:                            # noqa: BLE001 - keep serving the queue
                    log(f"could not record the outcome of {job['job_id'][:8]}: {type(exc).__name__}: {exc}")
                if once:
                    return
                continue
            if once:
                log("no queued jobs")
                return
            if self.now() - last_clean > 3600:
                last_clean = self.now()
                clean_old_jobs(self.jobs_dir, float(self.cfg["keep_days"]))
            time.sleep(float(self.cfg["poll_seconds"]))
        log("worker stopped")


# --------------------------------------------------------------------------- #
# Set-up and command line
# --------------------------------------------------------------------------- #
def load_settings(path: Path = HOME_DIR / "worker.json") -> dict:
    """The saved settings merged over the defaults; explains how to create the file if it is missing."""
    if not path.exists():
        raise SystemExit(f"{path} not found. Run:  python aws/worker/worker.py --init")
    return {**DEFAULTS, **json.loads(path.read_text())}


def init_settings(path: Path = HOME_DIR / "worker.json") -> Path:
    """Write the settings file: the defaults plus the names from `terraform output -json worker_config`."""
    tf_dir = Path(__file__).resolve().parents[1] / "terraform"
    try:
        out = subprocess.run(["terraform", "output", "-json", "worker_config"], cwd=tf_dir, check=True,
                             capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Could not run `terraform output` in {tf_dir}: {exc}") from None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**DEFAULTS, **json.loads(out)}, indent=2) + "\n")
    path.chmod(0o600)
    return path


def check_setup(cfg: dict, table) -> list[tuple[bool, str]]:
    """Test everything the worker needs and return (ok, sentence) pairs."""
    results = []
    for tool in ("foamRun", "snappyHexMesh", "mpirun"):
        results.append((shutil.which(tool) is not None, f"OpenFOAM tool on PATH: {tool}"))
    repo = Path(cfg["repo"])
    results.append(((repo / "src" / "run_job.py").is_file(), f"pipeline found at {repo}"))
    results.append((bool(os.environ.get("FOAM_TUTORIALS")), "FOAM_TUTORIALS is set (OpenFOAM loaded)"))
    pv = cfg.get("pvbatch")
    results.append((bool(pv) and Path(pv).exists(), f"ParaView pvbatch at {pv} (pictures need it)"))
    try:
        cc.update_fields(table, cc.WORKER_KEY, {"last_seen": cc.utc_now(), "host": socket.gethostname(),
                                                "version": __version__})
        results.append((True, "AWS login works and the jobs table is reachable"))
    except Exception as exc:                                        # noqa: BLE001
        results.append((False, f"AWS: {type(exc).__name__}: {exc}"))
    return results


def main(argv=None) -> int:
    """Command-line entry point."""
    ap = argparse.ArgumentParser(description="Run the CFD site's queued jobs on this PC.")
    ap.add_argument("--init", action="store_true", help="create ~/.cfd-worker/worker.json from terraform output")
    ap.add_argument("--check", action="store_true", help="test the set-up and exit")
    ap.add_argument("--once", action="store_true", help="run one queued job (if any) and exit")
    a = ap.parse_args(argv)
    if a.init:
        print(f"Wrote {init_settings()}. Next: aws configure --profile cfd-worker, then --check.")
        return 0
    cfg = load_settings()
    import boto3
    session = boto3.Session(profile_name=cfg["profile"], region_name=cfg["region"])
    table = session.resource("dynamodb").Table(cfg["jobs_table"])
    if a.check:
        results = check_setup(cfg, table)
        for ok, text in results:
            print(("OK    " if ok else "FAIL  ") + text)
        return 0 if all(ok for ok, _ in results) else 1
    Worker(cfg, table, session.client("s3"), session.client("sns")).run_forever(once=a.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
