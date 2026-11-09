#!/usr/bin/env python3
"""Grid-convergence study from three finished cases (coarse, medium, fine).

Uses the procedure of Celik et al. (2008), "Procedure for Estimation and Reporting of
Uncertainty Due to Discretization in CFD Applications", J. Fluids Eng. 130(7):
  * representative cell size h = (1 / N)^(1/3) from the cell counts (3D)
  * observed order of convergence p (iterated, allows unequal refinement ratios)
  * Richardson-extrapolated value
  * Grid Convergence Index (GCI) on the fine and medium meshes (safety factor 1.25)

Reads each case's results.json (from check_case.py) for the mean Cd/Cl and the final cell
count from log.snappyHexMesh. Writes grid_study.json next to the fine case unless --out.

    python src/grid_study.py $FOAM_RUN/ahmed1_v3 $FOAM_RUN/ahmed1_v4 $FOAM_RUN/ahmed1_v5 \
        --reference test_data/ahmed/force_mom_1.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from pathlib import Path

__version__ = "0.1.0"


def final_cells(case: Path) -> int:
    log = (case / "log.snappyHexMesh").read_text(errors="replace")
    found = re.findall(r"(?:Layer|Snapped) mesh : cells:(\d+)", log)
    if not found:
        raise ValueError(f"No cell count found in {case / 'log.snappyHexMesh'}")
    return int(found[-1])


def load(case: Path) -> dict:
    r = json.loads((case / "results.json").read_text())
    f = r.get("final") or {}
    if "Cd_mean" not in f:
        raise ValueError(f"{case}/results.json has no mean forces; re-run check_case.py (v0.2+).")
    return {"case": str(case), "cells": final_cells(case), "Cd": f["Cd_mean"], "Cl": f["Cl_mean"],
            "status": r.get("status"),
            "converged_gate": next((g["level"] for g in r["gates"] if g["code"] == "CONVERGED"), None),
            "mean_shift": f.get("mean_shift")}


def gci(phi: tuple[float, float, float], n: tuple[int, int, int], fs: float = 1.25) -> dict:
    """phi, n ordered FINE, MEDIUM, COARSE (index 1, 2, 3 as in Celik et al.)."""
    p1, p2, p3 = phi
    h1, h2, h3 = ((1.0 / c) ** (1 / 3) for c in n)
    r21, r32 = h2 / h1, h3 / h2
    e21, e32 = p2 - p1, p3 - p2
    out = {"r21": r21, "r32": r32, "e21": e21, "e32": e32}
    if e21 == 0 or e32 == 0:
        out.update(behaviour="no change between meshes", p=None, extrapolated=None,
                   gci_fine=0.0 if e21 == 0 else None, gci_medium=None)
        return out
    ratio = e32 / e21
    s = 1.0 if ratio > 0 else -1.0
    behaviour = ("monotonic convergence" if 0 < 1 / ratio < 1 else
                 "oscillatory convergence" if -1 < 1 / ratio < 0 else
                 "divergence (changes growing)" if abs(1 / ratio) > 1 else "unclear")
    if behaviour.startswith("divergence"):
        out.update(behaviour=behaviour, p=None, extrapolated=None, gci_fine=None, gci_medium=None)
        return out
    # Fixed-point iteration for the apparent order p
    p = 1.0
    ok = True
    for _ in range(200):
        q = math.log((r21 ** p - s) / (r32 ** p - s)) if (r21 ** p - s) / (r32 ** p - s) > 0 else float("nan")
        p_new = abs(math.log(abs(ratio)) + q) / math.log(r21)
        if not math.isfinite(p_new):
            ok = False
            break
        if abs(p_new - p) < 1e-10:
            p = p_new
            break
        p = p_new
    if not ok or p <= 0:
        out.update(behaviour=behaviour, p=None, extrapolated=None, gci_fine=None, gci_medium=None)
        return out
    ext = (r21 ** p * p1 - p2) / (r21 ** p - 1)
    ea21 = abs((p1 - p2) / p1) if p1 else float("inf")
    ea32 = abs((p2 - p3) / p2) if p2 else float("inf")
    out.update(behaviour=behaviour, p=p, extrapolated=ext,
               rel_error_fine_vs_extrapolated=abs((ext - p1) / ext) if ext else None,
               gci_fine=fs * ea21 / (r21 ** p - 1),
               gci_medium=fs * ea32 / (r32 ** p - 1),
               asymptotic_check=(fs * ea32 / (r32 ** p - 1)) / (r21 ** p * fs * ea21 / (r21 ** p - 1)))
    return out


def study(cases: list[Path], reference: Path | None = None) -> dict:
    if len(cases) != 3:
        raise ValueError("Give exactly three cases, ordered coarse, medium, fine.")
    runs = [load(Path(c)) for c in cases]
    if not (runs[0]["cells"] < runs[1]["cells"] < runs[2]["cells"]):
        raise ValueError("Cases must be ordered coarse -> fine (increasing cell count).")
    ref = None
    if reference:
        rows = list(csv.reader(open(reference)))
        ref = dict(zip([h.strip().lower() for h in rows[0]], [float(v) for v in rows[1]]))
    res = {"tool": "grid_study.py", "tool_version": __version__, "runs": runs, "quantities": {}}
    fine, med, coarse = runs[2], runs[1], runs[0]
    for k in ("Cd", "Cl"):
        g = gci((fine[k], med[k], coarse[k]), (fine["cells"], med["cells"], coarse["cells"]))
        if ref and k.lower() in ref:
            g["reference"] = ref[k.lower()]
            g["fine_vs_reference"] = (fine[k] - ref[k.lower()]) / abs(ref[k.lower()])
            if g.get("extrapolated") is not None:
                g["extrapolated_vs_reference"] = (g["extrapolated"] - ref[k.lower()]) / abs(ref[k.lower()])
        res["quantities"][k] = g
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Grid-convergence (GCI) study from three cases, coarse to fine.")
    ap.add_argument("cases", nargs=3, type=Path)
    ap.add_argument("--reference", type=Path, help="CSV with cd, cl columns")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    try:
        r = study(a.cases, a.reference)
    except (ValueError, FileNotFoundError, KeyError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    out = a.out or (a.cases[2] / "grid_study.json")
    out.write_text(json.dumps(r, indent=2))
    print("Grid study (coarse -> fine)")
    for run in r["runs"]:
        print(f"  {Path(run['case']).name:<14} {run['cells']:>11,} cells   Cd {run['Cd']:.4f}   Cl {run['Cl']:.4f}"
              f"   convergence gate {run['converged_gate']}")
    for k, g in r["quantities"].items():
        print(f"\n{k}: {g['behaviour']}  (refinement ratios {g['r32']:.3f}, {g['r21']:.3f})")
        if g.get("p") is None:
            print("  No reliable extrapolation: the three results are not in a converging pattern.")
        else:
            print(f"  observed order p = {g['p']:.2f}   extrapolated {k} = {g['extrapolated']:.4f}")
            print(f"  GCI fine = {g['gci_fine'] * 100:.2f}%   GCI medium = {g['gci_medium'] * 100:.2f}%   "
                  f"asymptotic check = {g['asymptotic_check']:.3f} (close to 1 is good)")
        if "reference" in g:
            line = f"  reference {g['reference']:.4f}: fine mesh {g['fine_vs_reference'] * 100:+.1f}%"
            if "extrapolated_vs_reference" in g:
                line += f", extrapolated {g['extrapolated_vs_reference'] * 100:+.1f}%"
            print(line)
    print(f"\nWritten to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
