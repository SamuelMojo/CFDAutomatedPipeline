#!/usr/bin/env python3
"""Phase 3 analysis: does the pipeline rank designs the same way as the reference?

    python src/analyse_sweep.py $FOAM_RUN/jobs/batch_<time>.json

Reads a batch report (from run_batch.py) and, for every finished job with a reference, compares
our mean Cd with the reference Cd:
  * ranking: Spearman rank correlation and the share of shape PAIRS put in the right order;
  * offset: mean relative difference (a consistent bias is correctable and does not hurt ranking);
  * after removing the mean offset, the remaining scatter;
  * differences between designs: for every pair, our drag difference vs the reference difference,
    and whether pairs closer than the noise floor were called correctly or are ties.
Writes <batch>_analysis.md/.json and a scatter plot (matplotlib, if installed).
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np

__version__ = "0.1.0"


def ranks(v: np.ndarray) -> np.ndarray:
    """Average ranks (ties share the mean rank)."""
    order = np.argsort(v, kind="stable")
    r = np.empty(len(v))
    r[order] = np.arange(1, len(v) + 1)
    for val in np.unique(v):
        m = v == val
        r[m] = r[m].mean()
    return r


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank correlation of a and b: 1 means the same order, -1 the opposite order."""
    ra, rb = ranks(a), ranks(b)
    return float(np.corrcoef(ra, rb)[0, 1])


def analyse(rows: list[dict], noise: float) -> dict:
    """rows: [{'job', 'ours', 'ref'}]. noise: relative uncertainty of our Cd (e.g. 0.02)."""
    ours = np.array([r["ours"] for r in rows])
    ref = np.array([r["ref"] for r in rows])
    rel = ours / ref - 1
    offset = float(rel.mean())
    corrected = ours / (1 + offset)
    pairs = []
    for i, j in itertools.combinations(range(len(rows)), 2):
        d_ref = (ref[j] - ref[i]) / ref[i]
        d_ours = (ours[j] - ours[i]) / ours[i]
        same_sign = np.sign(d_ref) == np.sign(d_ours)
        resolvable = abs(d_ref) > 2 * noise          # difference bigger than the noise of two results
        pairs.append({"a": rows[i]["job"], "b": rows[j]["job"], "ref_diff": float(d_ref), "our_diff": float(d_ours),
                      "correct_order": bool(same_sign), "resolvable": bool(resolvable)})
    res_pairs = [p for p in pairs if p["resolvable"]]
    return {
        "n": len(rows),
        "spearman": spearman(ours, ref) if len(rows) > 2 else None,
        "pairs_total": len(pairs),
        "pairs_correct": sum(p["correct_order"] for p in pairs),
        "resolvable_pairs": len(res_pairs),
        "resolvable_correct": sum(p["correct_order"] for p in res_pairs),
        "mean_offset": offset,
        "scatter_after_offset": float(np.sqrt(np.mean((corrected / ref - 1) ** 2))),
        "max_abs_error": float(np.max(np.abs(rel))),
        "noise_floor": noise,
        "pairs": pairs,
        "shapes": [{**r, "rel_error": float(e), "corrected": float(c)} for r, e, c in zip(rows, rel, corrected)],
    }


def report_md(a: dict, source: str) -> str:
    """Markdown report for an analyse() result: per-shape table, ranking, offset and pairs the noise allows us to call.
    """
    L = [f"# Design sweep analysis ({a['n']} shapes)", "", f"Source: `{source}`", "",
         "| Shape | Reference Cd | Our Cd | Difference | Offset-corrected |", "|---|---|---|---|---|"]
    for s in sorted(a["shapes"], key=lambda s: s["ref"]):
        L.append(f"| {s['job']} | {s['ref']:.4f} | {s['ours']:.4f} | {s['rel_error']:+.1%} | {s['corrected']:.4f} |")
    sp = f"{a['spearman']:.3f}" if a["spearman"] is not None else "n/a"
    L += ["", f"- **Ranking:** Spearman rank correlation {sp} (1 = identical order); "
          f"{a['pairs_correct']} of {a['pairs_total']} pairs in the right order.",
          f"- **Pairs the noise allows us to call** (reference difference > 2 x {a['noise_floor']:.0%}): "
          f"{a['resolvable_correct']} of {a['resolvable_pairs']} in the right order.",
          f"- **Consistent offset:** our Cd is {a['mean_offset']:+.1%} on average.",
          f"- **Scatter after removing that offset:** {a['scatter_after_offset']:.1%} RMS.", "",
          "| Pair | Reference difference | Our difference | Order | Resolvable |", "|---|---|---|---|---|"]
    for p in sorted(a["pairs"], key=lambda p: abs(p["ref_diff"])):
        L.append(f"| {p['a']} vs {p['b']} | {p['ref_diff']:+.1%} | {p['our_diff']:+.1%} | "
                 f"{'correct' if p['correct_order'] else 'WRONG'} | {'yes' if p['resolvable'] else 'no (within noise)'} |")
    return "\n".join(L) + "\n"


def plot(a: dict, path: Path) -> bool:
    """Scatter of our Cd against the reference Cd with the offset line and error bars, saved to path. False if
    matplotlib is missing.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    ref = np.array([s["ref"] for s in a["shapes"]])
    ours = np.array([s["ours"] for s in a["shapes"]])
    fig, ax = plt.subplots(figsize=(5.5, 5), dpi=150)
    lo, hi = min(ref.min(), ours.min()) * 0.97, max(ref.max(), ours.max()) * 1.03
    ax.plot([lo, hi], [lo, hi], color="0.6", lw=1, label="perfect agreement")
    ax.plot([lo, hi], [lo * (1 + a["mean_offset"]), hi * (1 + a["mean_offset"])], "--", color="#d62728", lw=1,
            label=f"consistent offset {a['mean_offset']:+.1%}")
    ax.errorbar(ref, ours, yerr=ours * a["noise_floor"], fmt="o", color="#1f77b4", capsize=3, label="shapes")
    for s in a["shapes"]:
        ax.annotate(s["job"].replace("ahmedml_", "run "), (s["ref"], s["ours"]), fontsize=7,
                    xytext=(4, 4), textcoords="offset points")
    ax.set_xlim(lo, hi); ax.set_ylim(lo, hi)
    ax.set_xlabel("Reference Cd (AhmedML)"); ax.set_ylabel("Our Cd (pipeline)")
    sp = f"{a['spearman']:.2f}" if a["spearman"] is not None else "n/a"
    ax.set_title(f"Design sweep: rank correlation {sp}")
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout(); fig.savefig(path); plt.close(fig)
    return True


def rows_from_batch(batch: dict) -> list[dict]:
    """Rows for analyse() from a run_batch.py report: finished jobs that have a reference.

    The reference Cd is recovered from our Cd and the batch's relative difference (Cd_vs_ref).
    """
    rows = []
    for j in batch["jobs"]:
        if j.get("outcome") != "finished" or j.get("Cd") is None or j.get("Cd_vs_ref") is None:
            continue
        ref = j["Cd"] / (1 + j["Cd_vs_ref"])
        rows.append({"job": j["job"], "ours": j["Cd"], "ref": ref, "result": j.get("result")})
    return rows


def main(argv=None) -> int:
    """Command-line entry point: analyse a batch report and write <batch>_analysis.md/.json/.png. Returns 0, or 2 with
    fewer than three usable jobs.
    """
    ap = argparse.ArgumentParser(description="Ranking analysis of a design-sweep batch.")
    ap.add_argument("batch", type=Path, help="batch_<time>.json written by run_batch.py")
    ap.add_argument("--noise", type=float, default=0.02,
                    help="relative uncertainty of one result (default 0.02: phase 1 mesh GCI 1-2%%)")
    a = ap.parse_args(argv)
    rows = rows_from_batch(json.loads(a.batch.read_text()))
    if len(rows) < 3:
        print(f"ERROR: need at least 3 finished jobs with references, found {len(rows)}", file=sys.stderr)
        return 2
    res = analyse(rows, a.noise)
    stem = a.batch.with_suffix("")
    out_md, out_json, out_png = (Path(str(stem) + s) for s in ("_analysis.md", "_analysis.json", "_analysis.png"))
    res["tool"], res["tool_version"] = "analyse_sweep.py", __version__
    out_json.write_text(json.dumps(res, indent=2))
    md = report_md(res, str(a.batch))
    if plot(res, out_png):
        md += f"\nPlot: `{out_png.name}`\n"
    out_md.write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
