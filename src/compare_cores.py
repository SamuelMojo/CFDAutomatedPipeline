#!/usr/bin/env python3
"""Summarise a core-count benchmark: how mesh and solve time change with the number of processors.

    python src/compare_cores.py ~/OpenFOAM/samue-14/run/jobs/perrinn_fw_bench_p*

Give it the finished job folders (one per processor count). It reads each folder's job.json and status.json and
prints a markdown table: processors, cells, mesh time, solve time, seconds per iteration, speed-up against the
run with the fewest processors, and parallel efficiency (speed-up divided by the processor ratio).

Notes for reading the table:
  * snappyHexMesh builds a slightly different mesh for each decomposition, so the cell counts differ a little.
    "s per iteration per M cells" removes most of that effect from the solve time.
  * Efficiency 100% means doubling the cores halves the time; real runs fall below that.
  * These runs are for timing, not for forces: they use a fixed iteration count, not the convergence gate.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def stage_seconds(status: dict, name: str):
    """Seconds recorded for one pipeline stage in status.json, or None if that stage did not finish."""
    for st in status.get("stages", []):
        if st.get("stage") == name and "seconds" in st:
            return float(st["seconds"])
    return None


def load(folder: Path) -> dict | None:
    """Read job.json and status.json of one finished job; None (with a message) if the run is incomplete."""
    try:
        job = json.loads((folder / "job.json").read_text())
        status = json.loads((folder / "status.json").read_text())
    except (OSError, ValueError):
        print(f"skipping {folder.name}: job.json or status.json missing or unreadable", file=sys.stderr)
        return None
    procs = (job.get("case") or {}).get("processors")
    iters = status.get("iterations")
    mesh, solve = stage_seconds(status, "mesh"), stage_seconds(status, "solve")
    if not (procs and iters and mesh and solve):
        print(f"skipping {folder.name}: run not finished (state: {status.get('state')})", file=sys.stderr)
        return None
    cells = status.get("cells")
    return {"name": folder.name, "procs": int(procs), "cells": cells, "iters": int(iters),
            "mesh": mesh, "solve": solve}


def main(argv: list[str]) -> int:
    """Command-line entry point: print the scaling table for the given job folders. Returns 0, 1 or 2."""
    if not argv:
        print(__doc__)
        return 2
    rows = [r for r in (load(Path(a)) for a in argv) if r]
    if not rows:
        print("no finished runs found", file=sys.stderr)
        return 1
    rows.sort(key=lambda r: r["procs"])
    base = rows[0]
    print(f"Baseline for speed-up: {base['procs']} processors ({base['name']})\n")
    print("| Processors | Cells | Mesh (min) | Solve (min) | Iterations | s per iteration | "
          "s per iteration per M cells | Mesh speed-up | Solve speed-up | Solve efficiency |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        per_it = r["solve"] / r["iters"]
        per_it_m = per_it / (r["cells"] / 1e6) if r["cells"] else float("nan")
        ratio = r["procs"] / base["procs"]
        s_mesh = base["mesh"] / r["mesh"]
        s_solve = (base["solve"] / base["iters"]) / per_it   # per-iteration, so iteration counts need not match
        eff = s_solve / ratio
        cells = f"{r['cells']:,}" if r["cells"] else "n/a"
        print(f"| {r['procs']} | {cells} | {r['mesh'] / 60:.1f} | {r['solve'] / 60:.1f} | {r['iters']} | "
              f"{per_it:.2f} | {per_it_m:.2f} | {s_mesh:.2f}x | {s_solve:.2f}x | {eff * 100:.0f}% |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
