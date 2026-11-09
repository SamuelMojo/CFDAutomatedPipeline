#!/usr/bin/env python3
"""Phase 3 set-up: choose AhmedML shapes for a design sweep, download them, write job files.

    python src/prepare_sweep.py --n 6

1. Downloads the reference forces (force_mom_<i>.csv, a few bytes each) for a spread of candidate
   runs from the AhmedML dataset (huggingface.co/datasets/neashton/ahmedml, CC BY-SA 4.0).
2. Picks N shapes whose reference drag spans the range, plus one CLOSE PAIR (two shapes with
   nearly the same drag), because telling close designs apart is the hard part of ranking.
3. Downloads their STLs into test_data/ahmed_sweep/run_<i>/ and writes one job file per shape
   into job_specs/sweep/, all with identical settings (preset standard, 40 m/s, fixed floor,
   Aref 0.112, Lref 1.044), so the only difference between jobs is the shape.

Then:  python src/run_batch.py job_specs/sweep/*.json
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://huggingface.co/datasets/neashton/ahmedml/resolve/main"
CASE = {"speed": 40, "ground": "fixed", "aref": 0.112, "lref": 1.044, "processors": 14}


def fetch(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cfd-pipeline-prepare-sweep"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_forces(data: bytes) -> dict:
    rows = list(csv.reader(io.StringIO(data.decode())))
    return dict(zip([h.strip().lower() for h in rows[0]], [float(v) for v in rows[1]]))


def choose(cands: dict[int, float], n: int) -> list[int]:
    """N runs spread across the drag range (quantiles), one of them forming a close pair."""
    if len(cands) < n:
        raise ValueError(f"Only {len(cands)} candidates for {n} shapes.")
    order = sorted(cands, key=cands.get)
    k = n - 1                                                    # spread picks; +1 for the pair
    idx = sorted({round(i * (len(order) - 1) / (k - 1)) for i in range(k)}) if k > 1 else [0]
    picks = [order[i] for i in idx]
    # close pair: the unpicked candidate nearest in drag to a middle pick
    mid = picks[len(picks) // 2]
    rest = [r for r in order if r not in picks]
    partner = min(rest, key=lambda r: abs(cands[r] - cands[mid]))
    picks.append(partner)
    return sorted(picks, key=cands.get)


def job_spec(run: int, base: Path) -> dict:
    rel = Path("..") / ".." / "test_data" / "ahmed_sweep" / f"run_{run}"
    return {"name": f"ahmedml_{run}",
            "notes": f"Phase 3 design sweep: AhmedML run_{run}, standard preset, identical settings for every shape.",
            "geometry": str(rel / f"ahmed_{run}.stl"), "units": "m", "preset": "standard",
            "case": dict(CASE), "reference": str(rel / f"force_mom_{run}.csv")}


def main(argv=None, fetcher=fetch) -> int:
    ap = argparse.ArgumentParser(description="Choose and download AhmedML shapes for a design sweep.")
    ap.add_argument("--n", type=int, default=6, help="number of shapes (default 6, including the close pair)")
    ap.add_argument("--candidates", type=int, nargs="+",
                    default=list(range(1, 500, 12)), help="AhmedML run numbers to choose from")
    ap.add_argument("--data", type=Path, default=ROOT / "test_data" / "ahmed_sweep")
    ap.add_argument("--jobs", type=Path, default=ROOT / "job_specs" / "sweep")
    a = ap.parse_args(argv)
    if a.n < 3:
        print("ERROR: --n must be at least 3", file=sys.stderr)
        return 2

    cands: dict[int, float] = {}
    forces: dict[int, bytes] = {}
    for run in a.candidates:
        try:
            raw = fetcher(f"{BASE_URL}/run_{run}/force_mom_{run}.csv")
            cands[run] = parse_forces(raw)["cd"]
            forces[run] = raw
        except Exception as exc:                                 # missing run, network blip: skip it
            print(f"  run_{run}: skipped ({exc})")
    print(f"Reference drag for {len(cands)} candidate shapes: {min(cands.values()):.4f} to {max(cands.values()):.4f}"
          if cands else "No candidates could be downloaded.")
    if not cands:
        return 2
    picks = choose(cands, a.n)

    a.jobs.mkdir(parents=True, exist_ok=True)
    for run in picks:
        d = a.data / f"run_{run}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"force_mom_{run}.csv").write_bytes(forces[run])
        stl = d / f"ahmed_{run}.stl"
        if not stl.is_file():
            print(f"  downloading run_{run} geometry...")
            stl.write_bytes(fetcher(f"{BASE_URL}/run_{run}/ahmed_{run}.stl"))
        (a.jobs / f"ahmedml_{run}.json").write_text(json.dumps(job_spec(run, a.jobs), indent=2))
    print("\nChosen shapes (sorted by reference drag):")
    for run in picks:
        print(f"  run_{run:<4} Cd {cands[run]:.4f}")
    (a.jobs / "sweep_selection.json").write_text(json.dumps(
        {"source": BASE_URL, "licence": "CC BY-SA 4.0 (AhmedML, Ashton et al. 2024)",
         "candidates": cands, "chosen": picks}, indent=2))
    print(f"\nJob files written to {a.jobs}. Run them with:  python src/run_batch.py {a.jobs}/ahmedml_*.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
