#!/usr/bin/env python3
"""One-command CFD job: geometry in, checked results out.

    python src/run_job.py jobs/ahmed1_standard.json

Reads a job file (JSON) and runs the whole chain, stopping at the first failure:

  1. inspect     inspect_geometry.py: closed surface, units, exports a clean STL in metres
  2. case        make_case.py: builds the OpenFOAM case from a preset plus overrides
  3. mesh        surfaceFeatures, blockMesh, decomposePar, snappyHexMesh, checkMesh, patchSummary
  4. mesh gates  check_case.py mesh gates; a bad mesh (leak, failed checkMesh) stops the job
                 BEFORE hours of solving are spent
  5. solve       potentialFoam, then foamRun in blocks of iterations. After each block the
                 convergence gate is applied; the run stops as soon as the running mean has
                 settled (and at least min_iterations are done), or at max_iterations
  6. finish      reconstructPar, final check_case.py (with optional reference), summary.md

Everything for one job lives in one folder: the job file as run, the inspection report, the
case, results.json, status.json (stage, timings) and summary.md. Old time folders are purged
(only the last 2 kept) so long runs do not fill the disk.

Exit codes: 0 finished (PASS or WARN), 1 finished with a FAIL gate or stopped by a gate,
2 error (bad job file, missing tools, a command failed).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_case as cc  # noqa: E402
import inspect_geometry as ig  # noqa: E402
import make_case as mc  # noqa: E402

__version__ = "0.1.0"

# Mesh/physics presets. "standard" = the phase 1 fixed-layer setup (v3f); "fine" = v4f.
PRESETS = {
    "quick": dict(base_cell=0.4, surface_level=(3, 4), feature_level=4, near_level=3, wake_level=2, layers=0),
    "standard": dict(base_cell=0.2, surface_level=(5, 6), feature_level=6, near_level=5, wake_level=3,
                     layers=1, layer_thickness=0.00125),
    "fine": dict(base_cell=0.1587, surface_level=(5, 6), feature_level=6, near_level=5, wake_level=3,
                 layers=1, layer_thickness=0.00125),
}
SOLVE_DEFAULTS = {
    # tolerance: largest allowed change of the mean Cd/Cl between the last two averaging windows.
    # 0.5% for real runs: phase 1 showed a 0.4% drift on the finest mesh skewed the grid study.
    # average_iterations: after settling, run this many more iterations while OpenFOAM averages the
    # flow field (UMean, pMean). Field comparisons then use the average, not a single snapshot of an
    # oscillating solution (phase 1b: the 25 deg Ahmed swung by 15% in Cd).
    "quick": dict(min_iterations=200, max_iterations=600, block=200, tolerance=0.01, average_iterations=100),
    "standard": dict(min_iterations=1000, max_iterations=4000, block=500, tolerance=0.005, average_iterations=500),
    "fine": dict(min_iterations=1500, max_iterations=5000, block=500, tolerance=0.005, average_iterations=500),
}


class JobError(Exception):
    """The job cannot continue (exit code 2)."""


class GateStop(Exception):
    """A quality gate stopped the job (exit code 1)."""


# --------------------------------------------------------------------------- #
# Job file
# --------------------------------------------------------------------------- #
def load_job(path: Path) -> dict:
    try:
        job = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise JobError(f"Cannot read job file {path}: {exc}") from exc
    base = Path(path).resolve().parent
    for key in ("name", "geometry", "units"):
        if key not in job:
            raise JobError(f"Job file is missing '{key}'.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", job["name"]):
        raise JobError("Job 'name' may only contain letters, digits, '_', '-' and '.'.")
    preset = job.get("preset", "standard")
    if preset not in PRESETS:
        raise JobError(f"Unknown preset '{preset}'. Choose from {sorted(PRESETS)}.")
    case_opts = {**PRESETS[preset], **job.get("case", {})}
    unknown = set(case_opts) - set(mc.DEFAULTS)
    if unknown:
        raise JobError(f"Unknown case option(s): {sorted(unknown)}. Valid: {sorted(mc.DEFAULTS)}.")
    if "surface_level" in case_opts:
        case_opts["surface_level"] = tuple(case_opts["surface_level"])
    solve = {**SOLVE_DEFAULTS[preset], **job.get("solve", {})}
    if not (0 < solve["block"] and solve["min_iterations"] <= solve["max_iterations"]):
        raise JobError("solve: need block > 0 and min_iterations <= max_iterations.")

    def resolve(p):
        q = Path(os.path.expandvars(str(p))).expanduser()
        return q if q.is_absolute() else (base / q)

    return {
        "name": job["name"],
        "geometry": resolve(job["geometry"]),
        "units": job["units"],
        "allow_open": bool(job.get("allow_open", False)),
        "allow_contact": bool(job.get("allow_contact", False)),
        "z_shift_mm": float(job.get("z_shift_mm", 0.0)),
        "preset": preset,
        "case": case_opts,
        "solve": solve,
        "reference": resolve(job["reference"]) if job.get("reference") else None,
        "notes": job.get("notes", ""),
        "raw": job,
    }


# --------------------------------------------------------------------------- #
# Running OpenFOAM commands
# --------------------------------------------------------------------------- #
def run_command(cmd: list[str], case: Path, log: Path, append: bool = False) -> None:
    """Run one command in the case folder, writing all output to `log`. Raises on failure."""
    with open(log, "a" if append else "w") as f:
        r = subprocess.run(cmd, cwd=case, stdout=f, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise JobError(f"'{' '.join(cmd)}' failed (exit {r.returncode}); see {log}")


def set_control(case: Path, end_time: int) -> None:
    """Resume-friendly controlDict: start from the latest saved time, stop at end_time,
    keep only the last 2 saved time folders."""
    p = case / "system" / "controlDict"
    t = p.read_text()
    t = mc.regex_once(t, r"^startFrom\s+\w+;", "startFrom       latestTime;", "controlDict")
    t = mc.regex_once(t, r"^endTime\s+\d+;", f"endTime         {end_time};", "controlDict")
    t = mc.regex_once(t, r"^purgeWrite\s+\d+;", "purgeWrite      2;", "controlDict")
    p.write_text(t)


FIELD_AVERAGE = """type            fieldAverage;
libs            ("libfieldFunctionObjects.so");
writeControl    writeTime;
fields          (U p);
mean            yes;
prime2Mean      no;
base            time;
"""


def add_field_average(case: Path) -> None:
    """Switch on field averaging (UMean, pMean) from the next solver start."""
    (case / "system" / "fieldAverageWindow").write_text(FIELD_AVERAGE)
    f = case / "system" / "functions"
    t = f.read_text()
    if "#includeFunc fieldAverageWindow" not in t:
        t = mc.sub_once(t, "#includeFunc forceCoeffs", "#includeFunc forceCoeffs\n#includeFunc fieldAverageWindow", "functions")
        f.write_text(t)


def remove_field_average(case: Path) -> None:
    f = case / "system" / "functions"
    f.write_text(f.read_text().replace("\n#includeFunc fieldAverageWindow", ""))


def gate_levels(result: dict) -> dict:
    return {g["code"]: g["level"] for g in result["gates"]}


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, spec: dict, jobs_dir: Path, tutorial: Path, runner=run_command, stamp: str | None = None):
        self.spec = spec
        self.tutorial = Path(tutorial)
        self.runner = runner
        stamp = stamp or _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = Path(jobs_dir) / f"{spec['name']}_{stamp}"
        self.case = self.dir / "case"
        self.status = {"job": spec["name"], "folder": str(self.dir), "tool": "run_job.py",
                       "tool_version": __version__, "state": "created", "stages": [], "started_utc": None}

    # -- bookkeeping ---------------------------------------------------------
    def save_status(self) -> None:
        (self.dir / "status.json").write_text(json.dumps(self.status, indent=2, default=str))

    def stage(self, name: str):
        job = self

        class _Stage:
            def __enter__(self):
                self.t0 = time.time()
                job.status["state"] = f"running: {name}"
                job.status["stages"].append({"stage": name, "state": "running",
                                             "started_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")})
                job.save_status()
                job.log(f"--- {name}")
                return self

            def __exit__(self, exc_type, exc, tb):
                entry = job.status["stages"][-1]
                entry["seconds"] = round(time.time() - self.t0, 1)
                entry["state"] = "done" if exc_type is None else "failed"
                if exc is not None:
                    entry["error"] = str(exc)
                job.save_status()
                return False
        return _Stage()

    def log(self, msg: str) -> None:
        line = f"[{_dt.datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(self.dir / "run_job.log", "a") as f:
            f.write(line + "\n")

    def foam(self, cmd: list[str], logname: str, append: bool = False) -> None:
        self.runner(cmd, self.case, self.case / logname, append)

    def mpi(self, *cmd: str) -> list[str]:
        n = self.nproc
        return ["mpirun", "-np", str(n), *cmd, "-parallel"] if n > 1 else list(cmd)

    # -- stages --------------------------------------------------------------
    def run(self) -> dict:
        s = self.spec
        self.dir.mkdir(parents=True, exist_ok=False)
        self.status["started_utc"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        (self.dir / "job.json").write_text(json.dumps(s["raw"], indent=2))
        self.log(f"Job {s['name']} -> {self.dir}")
        try:
            self._inspect()
            self._make_case()
            self._mesh()
            self._mesh_gates()
            self._solve()
            result = self._finish()
            self.status["state"] = f"finished: {result['status']}"
            return result
        except GateStop as exc:
            self.status["state"] = "stopped by gate"
            self.status["reason"] = str(exc)
            self.log(f"STOPPED: {exc}")
            raise
        except Exception as exc:
            self.status["state"] = "error"
            self.status["reason"] = str(exc)
            self.log(f"ERROR: {exc}")
            raise
        finally:
            self.status["finished_utc"] = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
            self.save_status()

    def _inspect(self):
        s = self.spec
        with self.stage("inspect"):
            stl = self.dir / "geometry_m.stl"
            r = ig.inspect(s["geometry"], units=s["units"], allow_open=s["allow_open"],
                           allow_contact=s["allow_contact"], export_stl=stl)
            (self.dir / "inspection.json").write_text(json.dumps(r, indent=2))
            self.log(f"inspection: {r['status']} ({len(r['geometry']['bodies'])} bodies, "
                     f"{r['geometry']['triangles_used']:,} triangles)")
            for c in r["checks"]:
                if c["level"] in ("FAIL", "ACTION", "WARN"):
                    self.log(f"  [{c['level']}] {c['message']}")
            if r["status"] not in ("PASS", "WARN") or not stl.is_file():
                raise GateStop(f"geometry inspection {r['status']}; see inspection.json")
            if s["z_shift_mm"]:
                # move the whole body up (+) or down (-): changes ride height, keeps rake
                tris = ig.read_stl(stl)
                low = float(tris[..., 2].min())
                if low + s["z_shift_mm"] / 1000 <= 0:
                    raise JobError(f"z_shift_mm {s['z_shift_mm']} puts the body through the ground "
                                   f"(lowest point is {low * 1000:.1f} mm above it).")
                ig.write_stl_binary(stl, tris + np.array([0.0, 0.0, s["z_shift_mm"] / 1000]))
                self.log(f"geometry moved {s['z_shift_mm']:+g} mm in Z: lowest point "
                         f"{low * 1000:.1f} -> {low * 1000 + s['z_shift_mm']:.1f} mm above the ground")
            self.stl = stl

    def _make_case(self):
        s = self.spec
        with self.stage("case"):
            opts = dict(s["case"])
            opts["end_time"] = s["solve"]["block"]
            info = mc.make_case(self.stl, self.case, self.tutorial, allow_open=s["allow_open"], **opts)
            self.nproc = info["parameters"]["processors"]
            p = info["parameters"]
            self.log(f"case: base cells {p['base_cells']}, {self.nproc} processors, Aref {p['Aref_m2']:.4f} m^2, "
                     f"speed {p['speed_m_s']} m/s, ground {p['ground']}")

    def _mesh(self):
        with self.stage("mesh"):
            self.foam(["surfaceFeatures"], "log.surfaceFeatures")
            self.foam(["blockMesh"], "log.blockMesh")
            if self.nproc > 1:
                self.foam(["decomposePar", "-copyZero"], "log.decomposePar")
            self.foam(self.mpi("snappyHexMesh"), "log.snappyHexMesh")
            self.foam(self.mpi("checkMesh"), "log.checkMesh")
            self.foam(self.mpi("patchSummary"), "log.patchSummary")
            cells = re.findall(r"(?:Layer|Snapped) mesh : cells:(\d+)",
                               (self.case / "log.snappyHexMesh").read_text(errors="replace"))
            self.status["cells"] = int(cells[-1]) if cells else None
            self.log(f"mesh: {self.status['cells']:,} cells" if self.status["cells"] else "mesh: cell count not found")

    def _mesh_gates(self):
        with self.stage("mesh gates"):
            r = cc.check(self.case)
            lv = gate_levels(r)
            for code in ("MESH_FINISHED", "MESH_QUALITY", "FLUID_VOLUME"):
                g = next(x for x in r["gates"] if x["code"] == code)
                self.log(f"  [{g['level']}] {code}: {g['message']}")
            bad = [c for c in ("MESH_FINISHED", "MESH_QUALITY", "FLUID_VOLUME") if lv.get(c) == "FAIL"]
            if bad:
                raise GateStop(f"mesh gate(s) failed: {', '.join(bad)}; not solving")

    def _solve(self):
        sv = self.spec["solve"]
        with self.stage("solve"):
            self.foam(self.mpi("potentialFoam"), "log.potentialFoam")
            target = sv["min_iterations"]
            first = True
            while True:
                set_control(self.case, target)
                self.foam(self.mpi("foamRun"), "log.foamRun", append=not first)
                first = False
                r = cc.check(self.case, tol_pass=sv["tolerance"])
                conv = next((g for g in r["gates"] if g["code"] == "CONVERGED"), None)
                done = r["final"]["iterations"] if r.get("final") else 0
                self.log(f"solve: {done} iterations; {conv['level'] if conv else 'no forces'}"
                         f"{': ' + conv['message'] if conv else ''}")
                self.status["iterations"] = done
                self.save_status()
                if conv and conv["level"] == "PASS" and done >= sv["min_iterations"]:
                    self.status["stopped_because"] = "converged"
                    break
                if target >= sv["max_iterations"]:
                    self.status["stopped_because"] = "max_iterations reached without settling"
                    self.log("solve: reached max_iterations; result will carry the convergence WARN/FAIL")
                    break
                target = min(target + sv["block"], sv["max_iterations"])
            avg = int(sv.get("average_iterations", 0))
            if avg > 0:
                start = self.status["iterations"]
                add_field_average(self.case)
                set_control(self.case, start + avg)
                try:
                    self.foam(self.mpi("foamRun"), "log.foamRun", append=True)
                    self.status["field_average"] = {"from_iteration": start, "to_iteration": start + avg}
                    self.log(f"solve: averaged the flow field over iterations {start}-{start + avg} (UMean, pMean)")
                except JobError as exc:
                    # Averaging is an extra; a failure here must not lose a finished solution.
                    remove_field_average(self.case)
                    self.status["field_average"] = {"error": str(exc)}
                    self.log(f"solve: WARNING field averaging failed ({exc}); continuing with the last snapshot")
                self.save_status()

    def _finish(self) -> dict:
        s = self.spec
        with self.stage("finish"):
            if self.nproc > 1:
                self.foam(["reconstructPar", "-latestTime"], "log.reconstructPar")
            (self.case / f"{s['name']}.foam").touch()
            r = cc.check(self.case, s["reference"], tol_pass=s["solve"]["tolerance"])
            (self.case / "results.json").write_text(json.dumps(r, indent=2))
            (self.dir / "results.json").write_text(json.dumps(r, indent=2))
        (self.dir / "summary.md").write_text(summary_md(s, r, self.status))
        self.log(f"RESULT: {r['status']}  (summary.md written)")
        return r


def summary_md(spec: dict, r: dict, status: dict) -> str:
    f = r.get("final") or {}
    cells = status.get("cells")
    lines = [f"# Job {spec['name']}", "",
             f"- Geometry: `{spec['geometry']}` ({spec['units']})",
             f"- Preset: {spec['preset']}; cells: {cells:,}" if isinstance(cells, int) else f"- Preset: {spec['preset']}",
             f"- Iterations: {f.get('iterations', 'n/a')} ({status.get('stopped_because', 'n/a')})",
             f"- **Result: {r['status']}**", ""]
    if "Cd_mean" in f:
        lines += ["| | Mean (settled window) | Last iteration |", "|---|---|---|",
                  f"| Cd | {f['Cd_mean']:.4f} | {f['Cd']:.4f} |", f"| Cl | {f['Cl_mean']:.4f} | {f['Cl']:.4f} |", ""]
    if r.get("comparison"):
        lines += ["Reference comparison:", ""]
        for k in ("cd", "cl"):
            c = r["comparison"].get(k)
            if c:
                rel = f"{c['relative'] * 100:+.1f}%" if c["relative"] is not None else "n/a"
                lines.append(f"- {k.upper()}: ours {c['ours']:.4f} vs reference {c['reference']:.4f} ({rel})")
        lines.append("")
    lines += ["Quality gates:", ""] + [f"- [{g['level']}] {g['code']}: {g['message']}" for g in r["gates"]] + [""]
    lines += ["Stage timings:", ""] + [f"- {st['stage']}: {st.get('seconds', 'n/a')} s ({st['state']})"
                                       for st in status["stages"]] + [""]
    if spec.get("notes"):
        lines += ["Notes:", "", spec["notes"], ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one CFD job from a job file.")
    ap.add_argument("job", type=Path, help="job file (JSON)")
    ap.add_argument("--jobs-dir", type=Path,
                    default=Path(os.environ.get("FOAM_RUN", str(Path.home()))) / "jobs",
                    help="where job folders are created (default: $FOAM_RUN/jobs)")
    ap.add_argument("--tutorial", type=Path, default=Path(os.environ.get("FOAM_TUTORIALS", "")) / mc.TUTORIAL_REL)
    ap.add_argument("--check-only", action="store_true", help="validate the job file and environment, then stop")
    a = ap.parse_args(argv)
    try:
        spec = load_job(a.job)
        if not spec["geometry"].is_file():
            raise JobError(f"Geometry not found: {spec['geometry']}")
        if spec["reference"] and not spec["reference"].is_file():
            raise JobError(f"Reference file not found: {spec['reference']}")
        missing = [t for t in ("foamRun", "snappyHexMesh", "mpirun") if shutil.which(t) is None]
        if missing:
            raise JobError(f"OpenFOAM tools not found on PATH: {missing}. Load OpenFOAM (. /opt/openfoam14/etc/bashrc).")
        if not a.tutorial.is_dir():
            raise JobError(f"Template tutorial not found: {a.tutorial}")
        if a.check_only:
            print(f"Job file OK: {spec['name']}, preset {spec['preset']}, solve {spec['solve']}")
            return 0
        result = Job(spec, a.jobs_dir, a.tutorial).run()
    except GateStop:
        return 1
    except (JobError, mc.CaseError, ig.GeometryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 1 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
