#!/usr/bin/env python3
"""Run several job files one after another (e.g. overnight) and summarise them in one table.

    python src/run_batch.py job_specs/ahmed25_standard.json job_specs/ahmed35_standard.json

All job files are checked BEFORE anything runs, so a typo in job 3 does not surface at 3 am.
A job that fails or is stopped by a gate does not stop the batch: the next job starts.
At the end (and after every job) a batch report is written to
$FOAM_RUN/jobs/batch_<timestamp>.md (+ .json) with one row per job.

Exit code: 0 if every job finished with PASS or WARN, 1 otherwise, 2 if a job file is invalid.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_case as mc  # noqa: E402
import run_job as rj  # noqa: E402

__version__ = "0.1.0"


def row_for(spec: dict, job: rj.Job | None, result: dict | None, outcome: str, seconds: float) -> dict:
    """One row of the batch report; also works for a job that failed before it started (job is None)."""
    f = (result or {}).get("final") or {}
    comp = (result or {}).get("comparison") or {}
    st = job.status if job else {}
    return {
        "job": spec["name"], "outcome": outcome, "result": (result or {}).get("status"),
        "cells": st.get("cells"), "iterations": f.get("iterations"), "stopped_because": st.get("stopped_because"),
        "Cd": f.get("Cd_mean"), "Cl": f.get("Cl_mean"),
        "Cd_vs_ref": comp.get("cd", {}).get("relative") if comp.get("cd") else None,
        "Cl_vs_ref": comp.get("cl", {}).get("relative") if comp.get("cl") else None,
        "minutes": round(seconds / 60, 1), "folder": str(job.dir) if job else None,
        "reason": st.get("reason"),
    }


def fmt(v, spec="{:.4f}"):
    """Format a number for the report table, or an en dash if it is missing."""
    return "–" if v is None else spec.format(v)


def report_md(rows: list[dict], started: str) -> str:
    """The batch report as markdown: one table row per job, then a list of problems."""
    lines = [f"# Batch started {started}", "",
             "| Job | Outcome | Result | Cells | Iterations | Cd | Cd vs ref | Cl | Cl vs ref | Minutes |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['job']} | {r['outcome']} | {r['result'] or '–'} | {fmt(r['cells'], '{:,}')} | "
                     f"{fmt(r['iterations'], '{}')} | {fmt(r['Cd'])} | {fmt(r['Cd_vs_ref'], '{:+.1%}')} | "
                     f"{fmt(r['Cl'])} | {fmt(r['Cl_vs_ref'], '{:+.1%}')} | {r['minutes']} |")
    notes = [f"- **{r['job']}**: {r['reason']}" for r in rows if r.get("reason")]
    if notes:
        lines += ["", "Problems:", ""] + notes
    lines += ["", "Each job's folder has its own summary.md with the quality gates and stage timings.", ""]
    return "\n".join(lines)


def run_batch(job_files: list[Path], jobs_dir: Path, tutorial: Path, runner=rj.run_command) -> list[dict]:
    """Validate every job file first, then run the jobs one after another and return one row per job.

    The report (batch_<time>.md and .json) is rewritten after each job. A job that fails or is stopped by a gate
    does not stop the batch. runner is how commands are executed; tests pass a fake one.
    """
    specs = [rj.load_job(p) for p in job_files]           # validate everything first
    for s in specs:
        if not s["geometry"].is_file():
            raise rj.JobError(f"{s['name']}: geometry not found: {s['geometry']}")
        if s["reference"] and not s["reference"].is_file():
            raise rj.JobError(f"{s['name']}: reference not found: {s['reference']}")
    names = [s["name"] for s in specs]
    if len(set(names)) != len(names):
        raise rj.JobError("Two job files use the same 'name'; give each job a unique name.")
    jobs_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_md, out_json = jobs_dir / f"batch_{stamp}.md", jobs_dir / f"batch_{stamp}.json"
    rows: list[dict] = []
    print(f"Batch of {len(specs)} jobs; report: {out_md}", flush=True)
    for i, spec in enumerate(specs, 1):
        print(f"\n=== [{i}/{len(specs)}] {spec['name']} ===", flush=True)
        t0, job, result = time.time(), None, None
        try:
            job = rj.Job(spec, jobs_dir, tutorial, runner=runner)
            result = job.run()
            outcome = "finished"
        except rj.GateStop:
            outcome = "stopped by gate"
        except Exception as exc:                            # keep going with the next job
            outcome = "error"
            if job is None:
                print(f"ERROR: {exc}", flush=True)
        rows.append(row_for(spec, job, result, outcome, time.time() - t0))
        if job is None and outcome == "error":
            rows[-1]["reason"] = "could not create the job"
        out_md.write_text(report_md(rows, stamp))
        out_json.write_text(json.dumps({"tool": "run_batch.py", "tool_version": __version__, "jobs": rows}, indent=2))
    print("\n" + report_md(rows, stamp), flush=True)
    return rows


def main(argv=None) -> int:
    """Command-line entry point. Returns 0 if every job finished with PASS or WARN, 1 otherwise, 2 if nothing was run.
    """
    ap = argparse.ArgumentParser(description="Run several CFD job files one after another.")
    ap.add_argument("jobs", nargs="+", type=Path, help="job files (JSON)")
    ap.add_argument("--jobs-dir", type=Path, default=Path(os.environ.get("FOAM_RUN", str(Path.home()))) / "jobs")
    ap.add_argument("--tutorial", type=Path, default=Path(os.environ.get("FOAM_TUTORIALS", "")) / mc.TUTORIAL_REL)
    a = ap.parse_args(argv)
    try:
        missing = [t for t in ("foamRun", "snappyHexMesh", "mpirun") if shutil.which(t) is None]
        if missing:
            raise rj.JobError(f"OpenFOAM tools not found on PATH: {missing}")
        if not a.tutorial.is_dir():
            raise rj.JobError(f"Template tutorial not found: {a.tutorial}")
        rows = run_batch(a.jobs, a.jobs_dir, a.tutorial)
    except rj.JobError as exc:
        print(f"ERROR (nothing was run): {exc}", file=sys.stderr)
        return 2
    ok = all(r["outcome"] == "finished" and r["result"] in ("PASS", "WARN") for r in rows)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
