#!/usr/bin/env python3
"""Compare a Tyrrell-wing ride-height sweep with the Southampton wind-tunnel data.

    python src/compare_zerihan.py $FOAM_RUN/jobs/batch_<time>.json [more batch files or job folders...]
    python src/compare_zerihan.py $FOAM_RUN/jobs/batch_*.json $FOAM_RUN/jobs/tyrrell_h0224_sa_*/

Picks every finished job called tyrrell_h<hhhh>_<model> from the batch reports, converts our Cl (negative =
downforce) to the thesis' downforce coefficient CL, and reports, per turbulence model:
  * CL and CD at each height vs the measured curve (digitised, about +-0.02 in CL);
  * whether the model finds the downforce PEAK and the drop below it (the stall-in-ground-effect behaviour),
    which the measurements put at h/c = 0.082 (CL 1.72).
Writes zerihan_comparison.md/.png next to the first batch file.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_tyrrell_wing as tw  # noqa: E402

NAME = re.compile(r"^tyrrell_h(\d{4})_(\w+)$")
JOB_DIR = re.compile(r"^(tyrrell_h\d{4}_[a-z]+)_\d{8}-\d{6}$")


def batch_from_job_dir(d: Path) -> dict:
    """A one-job 'batch' from a single job folder (run_job.py), using its results.json."""
    m = JOB_DIR.match(d.name)
    r = json.loads((d / "results.json").read_text())
    f = r.get("final") or {}
    return {"jobs": [{"job": m.group(1) if m else d.name, "outcome": "finished", "result": r.get("status"),
                      "Cd": f.get("Cd_mean", f.get("Cd")), "Cl": f.get("Cl_mean", f.get("Cl"))}]}


def rows_from_batches(batches: list[dict]) -> list[dict]:
    rows = {}
    for b in batches:
        for j in b.get("jobs", []):                  # other JSON (e.g. *_analysis.json) has no jobs: skipped
            m = NAME.match(j.get("job", ""))
            if not m or j.get("outcome") != "finished" or j.get("Cl") is None:
                continue
            h = int(m.group(1)) / 1000
            rows[(h, m.group(2))] = {"h_c": h, "model": m.group(2), "CL": -j["Cl"], "CD": j.get("Cd"),
                                     "result": j.get("result")}
    return sorted(rows.values(), key=lambda r: (r["model"], r["h_c"]))


def analyse(rows: list[dict]) -> dict:
    out = {}
    for model in sorted({r["model"] for r in rows}):
        rs = [r for r in rows if r["model"] == model]
        for r in rs:
            r["free_CL"] = tw.reference_at(r["h_c"], "free")[0]
            ref_cl, ref_cd = tw.reference_at(r["h_c"])
            r["ref_CL"], r["ref_CD"] = ref_cl, ref_cd
            r["CL_error"] = r["CL"] / ref_cl - 1
            r["CD_error"] = (r["CD"] / ref_cd - 1) if r["CD"] else None
        hs = np.array([r["h_c"] for r in rs]); cls = np.array([r["CL"] for r in rs])
        i = int(np.argmax(cls))
        out[model] = {"rows": rs, "peak_h_c": float(hs[i]), "peak_CL": float(cls[i]),
                      "drop_below_peak": bool(i > 0 and cls[0] < cls[i]),    # lowest height below the peak
                      "mean_abs_CL_error": float(np.mean([abs(r["CL_error"]) for r in rs]))}
    return out


def report_md(a: dict) -> str:
    L = ["# Tyrrell wing in ground effect vs Zerihan (2001) wind-tunnel data", "",
         "Reference: balance forces with TRANSITION FIXED (tripped, fully turbulent boundary layer, the like-for-like "
         "case for RANS): downforce peaks at h/c 0.112 (CL 1.39) and then plateaus. With natural (free) transition "
         "the wing makes more downforce, peaking at h/c 0.082 (CL 1.72) before a sharper drop. Values digitised from "
         "the thesis figures (+-0.02 in CL); the tripped-case drag was less repeatable.", ""]
    for model, m in a.items():
        L += [f"## {tw.MODELS.get(model, model)}", "",
              "| h/c | CL ours | CL measured (fixed) | error | CL measured (free) | CD ours | CD measured (fixed) | error |",
              "|---|---|---|---|---|---|---|---|"]
        for r in sorted(m["rows"], key=lambda r: -r["h_c"]):
            cde = f"{r['CD_error']:+.1%}" if r["CD_error"] is not None else "n/a"
            L.append(f"| {r['h_c']:.3f} | {r['CL']:.3f} | {r['ref_CL']:.3f} | {r['CL_error']:+.1%} | {r['free_CL']:.3f} | "
                     f"{r['CD'] or float('nan'):.4f} | {r['ref_CD']:.4f} | {cde} |")
        L += ["", f"- Mean |CL error|: {m['mean_abs_CL_error']:.1%}",
              f"- Highest downforce at h/c {m['peak_h_c']:.3f} (CL {m['peak_CL']:.3f}); measured peak h/c 0.112 "
              f"(transition fixed) / 0.082 (free).",
              f"- Downforce drops below the peak within the heights run: {'yes' if m['drop_below_peak'] else 'NO'}", ""]
    return "\n".join(L) + "\n"


def plot(a: dict, path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    ref, free = tw.reference_forces(), tw.reference_forces(transition="free")
    fig, ax = plt.subplots(1, 2, figsize=(10, 4.2), dpi=150)
    ax[0].plot(ref[:, 0], ref[:, 1], "k-o", ms=4, label="wind tunnel, transition fixed")
    ax[0].plot(free[:, 0], free[:, 1], "-", color="0.6", marker="o", ms=3, label="wind tunnel, transition free")
    ax[1].plot(ref[:, 0], ref[:, 2], "k-o", ms=4, label="wind tunnel, transition fixed")
    ax[1].plot(free[:, 0], free[:, 2], "-", color="0.6", marker="o", ms=3, label="wind tunnel, transition free")
    for (model, m), c in zip(a.items(), ("#1f77b4", "#d62728", "#2ca02c")):
        rs = sorted(m["rows"], key=lambda r: r["h_c"])
        ax[0].plot([r["h_c"] for r in rs], [r["CL"] for r in rs], "s--", color=c, label=tw.MODELS.get(model, model))
        ax[1].plot([r["h_c"] for r in rs], [r["CD"] for r in rs], "s--", color=c, label=tw.MODELS.get(model, model))
    ax[0].set_ylabel("downforce coefficient CL"); ax[1].set_ylabel("drag coefficient CD")
    for x in ax:
        x.set_xlabel("ride height h/c"); x.grid(alpha=0.3); x.legend(fontsize=8)
    ax[0].set_title("Downforce vs ride height"); ax[1].set_title("Drag vs ride height")
    fig.tight_layout(); fig.savefig(path); plt.close(fig)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare a Tyrrell-wing sweep with Zerihan's wind-tunnel data.")
    ap.add_argument("batches", type=Path, nargs="+",
                    help="batch_<time>.json files from run_batch.py and/or single job folders")
    a = ap.parse_args(argv)
    rows = rows_from_batches([batch_from_job_dir(p) if p.is_dir() else json.loads(p.read_text()) for p in a.batches])
    if not rows:
        print("ERROR: no finished tyrrell_h<hhhh>_<model> jobs in those batch files", file=sys.stderr)
        return 2
    res = analyse(rows)
    out = a.batches[0].parent if a.batches[0].is_file() else a.batches[0].resolve().parent
    md = report_md(res)
    if plot(res, out / "zerihan_comparison.png"):
        md += "\nPlot: `zerihan_comparison.png`\n"
    (out / "zerihan_comparison.md").write_text(md)
    (out / "zerihan_comparison.json").write_text(json.dumps(res, indent=2))
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
