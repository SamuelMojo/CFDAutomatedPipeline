#!/usr/bin/env python3
"""Automatic quality gates for a finished OpenFOAM case (made by make_case.py).

Reads the log files and job.json, and answers:
  MESH_FINISHED  did snappyHexMesh finish without errors?
  MESH_QUALITY   did checkMesh pass?
  FLUID_VOLUME   is the body really cut out of the mesh? (catches air left inside a
                 body, as happened in the nose of the first wing run)
  SOLVER_END     did the solver reach the end?
  CONVERGED      have Cd and Cl stopped moving over the last part of the run?
Optionally compares the final Cd/Cl with a reference CSV (e.g. AhmedML force_mom_i.csv).

Writes <case>/results.json. Exit code 0 = no FAIL, 1 = at least one FAIL, 2 = error.

    python src/check_case.py $FOAM_RUN/ahmed1_v1 --reference test_data/ahmed/force_mom_1.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inspect_geometry as ig  # noqa: E402

__version__ = "0.4.0"


def read(path: Path) -> str | None:
    return path.read_text(errors="replace") if path.is_file() else None


def force_history(log: str) -> dict:
    """Cd, Cl, Cm per iteration from the solver log (forceCoeffs prints to the log)."""
    rows: dict[float, dict] = {}
    cur = None
    for line in log.splitlines():
        m = re.match(r"^Time = ([0-9.eE+-]+)s?\s*$", line)
        if m:
            cur = float(m.group(1))
            rows.setdefault(cur, {})
            continue
        m = re.match(r"^\s+(Cd|Cl|Cm)\s+=\s+([0-9.eE+-]+)\s*$", line)
        if m and cur is not None:
            rows[cur].setdefault(m.group(1), float(m.group(2)))   # first value per iteration
    times = [t for t in sorted(rows) if "Cd" in rows[t] and "Cl" in rows[t]]
    return {"time": times, "Cd": [rows[t]["Cd"] for t in times], "Cl": [rows[t]["Cl"] for t in times],
            "Cm": [rows[t].get("Cm") for t in times]}


def dominant_period(x: np.ndarray) -> float | None:
    """Period (in iterations) of the strongest oscillation in x, or None if there is no clear one."""
    if len(x) < 20:
        return None
    x = x - np.polyval(np.polyfit(np.arange(len(x)), x, 1), np.arange(len(x)))   # remove drift
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    if len(spec) < 3 or spec[1:].max() <= 0:
        return None
    k = int(spec[1:].argmax()) + 1
    if spec[k] < 4 * np.median(spec[1:]):          # no clear peak: noise, not an oscillation
        return None
    # refine the peak position with a parabola through the neighbouring bins
    if 1 <= k < len(spec) - 1:
        a, b, c = spec[k - 1], spec[k], spec[k + 1]
        k = k + 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else k
    return float(len(x) / k)


def expected_volumes(case: Path, job: dict | None):
    """(domain volume, body volume) from job.json, or rebuilt from older jobs and the STL."""
    if job and "expected" in job:
        dom = job["expected"]["domain_volume_m3"]
        return dom, dom - job["expected"]["fluid_volume_m3"]
    if not job:
        return None, None
    lo, hi = job["parameters"]["domain_min"], job["parameters"]["domain_max"]
    dom = float(np.prod(np.subtract(hi, lo)))
    stls = sorted((case / "constant" / "geometry").glob("*.stl"))
    if not stls:
        return dom, None
    m = ig.analyse_mesh(ig.read_stl(stls[0]))
    return dom, abs(m["volume_total"])


def check(case: Path, reference: Path | None = None, window_frac: float = 0.2,
          tol_pass: float = 0.01, tol_warn: float = 0.05,
          illegal_frac_warn: float = 1e-4, swing_warn: float = 0.05, skew_frac_warn: float = 1e-5) -> dict:
    case = Path(case)
    if not case.is_dir():
        raise FileNotFoundError(f"Case folder not found: {case}")
    job = json.loads(read(case / "job.json")) if (case / "job.json").is_file() else None
    gates: list[dict] = []

    def gate(level, code, msg):
        gates.append({"level": level, "code": code, "message": msg})

    snappy = read(case / "log.snappyHexMesh")
    if snappy is None:
        gate("FAIL", "MESH_FINISHED", "log.snappyHexMesh not found.")
    elif "Finished meshing without any errors" in snappy:
        gate("PASS", "MESH_FINISHED", "snappyHexMesh finished without errors.")
    else:
        illegal = re.findall(r"Finished meshing with (\d+) illegal faces", snappy)
        faces = re.findall(r"(?:Layer|Snapped) mesh : cells:\d+\s+faces:(\d+)", snappy)
        if illegal and faces:
            n_bad, n_faces = int(illegal[-1]), int(faces[-1])
            # Which quality criteria the bad faces broke, from the final "Checking faces in error" block
            tail = snappy[snappy.rfind("Checking faces in error"):]
            broken = [f"{m.group(1).strip()} ({m.group(2)})" for m in
                      re.finditer(r"^\s+(faces[^:]*?|non-orthogonality[^:]*?)\s*:\s*(\d+)\s*$", tail, re.M)
                      if int(m.group(2)) > 0]
            txt = (f"snappyHexMesh finished with {n_bad} illegal faces out of {n_faces:,} "
                   f"({n_bad / n_faces:.1e}); criteria: {', '.join(broken) or 'not listed'}")
            if n_bad / n_faces <= illegal_frac_warn:
                gate("WARN", "MESH_FINISHED", txt + ". A handful is usually harmless; checkMesh decides below.")
            else:
                gate("FAIL", "MESH_FINISHED", txt + ". Too many: fix the mesh settings.")
        else:
            gate("FAIL", "MESH_FINISHED", "snappyHexMesh did not report finishing (crashed or still running?).")
    regions = re.findall(r"out of (\d+) regions", snappy or "")
    cells = re.findall(r"(?:Layer|Snapped) mesh : cells:(\d+)", snappy or "")

    cm = read(case / "log.checkMesh")
    total_volume = None
    if cm is None:
        gate("WARN", "MESH_QUALITY", "log.checkMesh not found (run: checkMesh | tee log.checkMesh).")
    else:
        failed = re.search(r"Failed (\d+) mesh checks", cm)
        stars = [l.strip().lstrip("*").strip() for l in cm.splitlines() if l.strip().startswith("***")]
        nf = re.search(r"^\s*faces:\s+(\d+)", cm, re.M)
        skew = [re.search(r"(\d+) highly skew faces", s) for s in stars]
        n_skew = sum(int(m.group(1)) for m in skew if m)
        if failed and stars and all(skew) and nf and n_skew <= skew_frac_warn * int(nf.group(1)):
            gate("WARN", "MESH_QUALITY", f"checkMesh failed only the skewness check: {n_skew} highly skewed faces "
                 f"out of {int(nf.group(1)):,} ({n_skew / int(nf.group(1)):.1e}); {stars[0]}. "
                 "A handful is usually harmless.")
        elif failed:
            gate("FAIL", "MESH_QUALITY", f"checkMesh: failed {failed.group(1)} mesh checks"
                 + (": " + "; ".join(stars) if stars else "") + ".")
        elif "Mesh OK" in cm:
            gate("PASS", "MESH_QUALITY", "checkMesh: Mesh OK.")
        else:
            gate("WARN", "MESH_QUALITY", "checkMesh result not recognised; read log.checkMesh.")
        mv = re.search(r"Total volume = ([0-9.eE+-]+)", cm)
        total_volume = float(mv.group(1).rstrip(".")) if mv else None

    dom, body = expected_volumes(case, job)
    removed_frac = None
    if total_volume is None or dom is None or not body:
        gate("WARN", "FLUID_VOLUME", "Cannot check: need checkMesh 'Total volume' and job.json with the domain.")
    else:
        removed_frac = (dom - total_volume) / body
        txt = (f"mesh volume {total_volume:.6g} m^3; domain {dom:.6g} m^3; body {body:.4g} m^3; "
               f"{removed_frac * 100:.0f}% of the body volume is cut out of the mesh")
        if 0.9 <= removed_frac <= 1.1:
            gate("PASS", "FLUID_VOLUME", txt + ".")
        elif removed_frac < 0.5:
            gate("FAIL", "FLUID_VOLUME", txt + ": air cells remain INSIDE the body (leak or overlapping parts). "
                 "Forces are not trustworthy.")
        else:
            gate("WARN", "FLUID_VOLUME", txt + ": partly. Check the geometry and the mesh around it.")

    solver = read(case / "log.foamRun")
    hist = force_history(solver) if solver else {"time": [], "Cd": [], "Cl": [], "Cm": []}
    final = None
    if solver is None:
        gate("FAIL", "SOLVER_END", "log.foamRun not found.")
    else:
        ended = re.search(r"^End\s*$", solver, re.M) is not None
        converged_msg = "solution converged" in solver.lower()
        gate("PASS" if ended else "FAIL", "SOLVER_END",
             f"Solver {'reached End' if ended else 'did NOT reach End'} after {len(hist['time'])} iterations"
             f"{' (residual convergence reached)' if converged_msg else ''}.")
    if hist["time"]:
        n = len(hist["time"])
        w = max(10, int(n * window_frac))
        final = {"Cd": hist["Cd"][-1], "Cl": hist["Cl"][-1], "Cm": hist["Cm"][-1] if hist["Cm"] else None,
                 "iterations": n}
        # Steady solvers on bluff bodies often oscillate around a fixed mean instead of settling.
        # So the gate tests whether the MEAN has stopped moving (last window vs the window before),
        # and reports the oscillation amplitude separately for information.
        # Trim the window to a whole number of oscillation periods, otherwise the part-period
        # left over biases the mean and the comparison depends on where the run happened to stop.
        period = dominant_period(np.array(hist["Cl"][-2 * w:]))
        if period and 2 <= period <= w / 3:
            w = int(round(int(w / period) * period))
        final["oscillation_period_iterations"] = period
        shifts, amps = {}, {}
        for k in ("Cd", "Cl"):
            arr = np.array(hist[k])
            seg = arr[-w:]
            prev = arr[-2 * w:-w] if n >= 2 * w else arr[:-w]
            scale = max(abs(seg.mean()), 0.1)            # avoid dividing by ~0 for small coefficients
            shifts[k] = float(abs(seg.mean() - prev.mean()) / scale) if len(prev) else float("inf")
            amps[k] = float(seg.max() - seg.min())
            final[f"{k}_mean"] = float(seg.mean())
            final[f"{k}_mean_previous"] = float(prev.mean()) if len(prev) else None
        worst = max(shifts.values())
        txt = (f"mean over the last {w} of {n} iterations moved by {shifts['Cd'] * 100:.2f}% (Cd) and "
               f"{shifts['Cl'] * 100:.2f}% (Cl) vs the {w} before; oscillation peak-to-peak "
               f"Cd {amps['Cd']:.4f}, Cl {amps['Cl']:.4f}")
        level = "PASS" if worst <= tol_pass else "WARN" if worst <= tol_warn else "FAIL"
        gate(level, "CONVERGED", txt + ("." if level == "PASS" else ": mean still moving; run longer or check the mesh."))
        # Large swings around a settled mean: the average is usable, but the flow is not steady and a
        # snapshot of the field (e.g. the last iteration) can be far from the average.
        swing_cd = amps["Cd"] / max(abs(final["Cd_mean"]), 0.05)
        swing_cl = amps["Cl"] / max(abs(final["Cl_mean"]), 0.1)
        final["relative_swing"] = {"Cd": float(swing_cd), "Cl": float(swing_cl)}
        if max(swing_cd, swing_cl) > swing_warn:
            gate("WARN", "UNSTEADY",
                 f"forces swing strongly around their mean (peak-to-peak {swing_cd:.0%} of Cd, {swing_cl:.0%} of Cl): "
                 "the steady solver is not finding a steady flow. Use averaged values and averaged fields only.")
        final["mean_window_iterations"] = w
        final["mean_shift"] = shifts
        final["peak_to_peak"] = amps
    elif solver is not None:
        gate("FAIL", "CONVERGED", "No force coefficients found in log.foamRun.")

    comparison = None
    if reference and final:
        rows = list(csv.reader(open(reference)))
        hdr = [h.strip().lower() for h in rows[0]]
        vals = [float(v) for v in rows[1]]
        ref = dict(zip(hdr, vals))
        comparison = {"reference_file": str(reference)}
        for k in ("cd", "cl"):
            if k in ref:
                ours = final[f"{k.capitalize()}_mean"]
                comparison[k] = {"ours": ours, "reference": ref[k], "difference": ours - ref[k],
                                 "relative": (ours - ref[k]) / abs(ref[k]) if ref[k] else None}

    levels = {g["level"] for g in gates}
    status = "FAIL" if "FAIL" in levels else "WARN" if "WARN" in levels else "PASS"
    return {"case": str(case), "status": status, "gates": gates, "final": final,
            "comparison": comparison, "mesh": {"cells": int(cells[-1]) if cells else None,
                                               "regions_reported": regions[-1] if regions else None,
                                               "total_volume_m3": total_volume,
                                               "body_volume_removed_fraction": removed_frac},
            "job": {k: job[k] for k in ("tool", "tool_version", "parameters")} if job else None,
            "tool": "check_case.py", "tool_version": __version__}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Quality gates for a finished OpenFOAM case.")
    p.add_argument("case", type=Path)
    p.add_argument("--reference", type=Path, help="CSV with cd, cl columns to compare against")
    a = p.parse_args(argv)
    try:
        r = check(a.case, a.reference)
    except (FileNotFoundError, ig.GeometryError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    (a.case / "results.json").write_text(json.dumps(r, indent=2))
    print(f"Case check: {a.case}")
    for g in r["gates"]:
        print(f"  [{g['level']:<4}] {g['code']:<13} {g['message']}")
    if r["final"]:
        f = r["final"]
        print(f"  last iteration: Cd {f['Cd']:.4f}  Cl {f['Cl']:.4f}  ({f['iterations']} iterations)")
        if "Cd_mean" in f:
            print(f"  mean of last {f['mean_window_iterations']}: Cd {f['Cd_mean']:.4f}  Cl {f['Cl_mean']:.4f}  (use these)")
    if r["comparison"]:
        for k in ("cd", "cl"):
            c = r["comparison"].get(k)
            if c:
                rel = f"{c['relative'] * 100:+.1f}%" if c["relative"] is not None else "n/a"
                print(f"  {k.upper()}: ours {c['ours']:.4f} vs reference {c['reference']:.4f} ({rel})")
    print(f"RESULT: {r['status']}   (written to {a.case / 'results.json'})")
    return 1 if r["status"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
