#!/usr/bin/env python3
"""Draw docs/images/core_count.png: how the run time of one job changes with the number of processors.

    python docs/make_core_count_chart.py            # writes docs/images/core_count.png

The numbers are the core-count benchmark of 12 Nov 2026 (PERRINN front wing, SA, half model, about 6.7 M cells,
300 fixed solver iterations per run). They are the output of `python src/compare_cores.py` on the five
perrinn_fw_bench_p* job folders, and the whole-job times from `python src/run_batch.py`. To redraw for a new
benchmark, edit RUNS below.

Left panel: wall-clock minutes for the whole job, split into meshing, solving and the rest (inspection, case set-up
and finishing). Right panel: speed-up compared with the 4-processor run, against the ideal (twice the cores, half
the time).
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

# (processors, mesh minutes, solve minutes, whole-job minutes, mesh speed-up, solve speed-up, solve efficiency %),
# as printed by compare_cores.py and run_batch.py.
RUNS = [
    (4, 39.2, 49.1, 89.2, 1.00, 1.00, 100),
    (8, 27.8, 42.5, 71.2, 1.41, 1.15, 58),
    (12, 24.2, 38.2, 63.4, 1.62, 1.28, 43),
    (14, 23.8, 38.8, 63.5, 1.65, 1.26, 36),
    (16, 22.9, 37.9, 61.7, 1.71, 1.29, 32),
]

# Colours: the first two categorical slots of the chart palette, and neutral ink for text and chrome.
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS = "#e1e0d9", "#c3c2b7"
MESH, SOLVE, REST = "#2a78d6", "#eb6834", "#c9c8c1"

OUT = Path(__file__).resolve().parent / "images" / "core_count.png"


def pick_font() -> str:
    """Inter if it is installed, otherwise the matplotlib default."""
    names = {f.name for f in font_manager.fontManager.ttflist}
    return "Inter" if "Inter" in names else "DejaVu Sans"


def style_axes(ax) -> None:
    """Quiet axes: hairline grid on the value axis only, light baseline, no box."""
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.tick_params(colors=INK2, length=0, labelsize=10.5)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def time_panel(ax) -> None:
    """Stacked columns: minutes spent meshing, solving and on everything else, with the total above."""
    gap = 0.5                                              # minutes of surface colour between touching segments
    x = range(len(RUNS))
    for i, (_, mesh, solve, total, *_) in enumerate(RUNS):
        rest = total - mesh - solve
        base = 0.0
        for height, colour, label in ((mesh, MESH, f"{mesh:.1f}"), (solve, SOLVE, f"{solve:.1f}"), (rest, REST, "")):
            lo = base + (gap if base else 0.0)
            ax.bar(i, height - (gap if base else 0.0), bottom=lo, width=0.56, color=colour, linewidth=0)
            if label:
                ax.text(i, lo + (height - gap) / 2, label, ha="center", va="center", color="white",
                        fontsize=10.5, fontweight="semibold")
            base += height
        ax.text(i, total + 1.8, f"{total:.1f}", ha="center", va="bottom", color=INK, fontsize=11.5,
                fontweight="bold")
    ax.set_xticks(list(x), [str(r[0]) for r in RUNS])
    ax.set_xlabel("Processors", color=INK2, fontsize=11, labelpad=8)
    ax.set_ylabel("Minutes for the whole job", color=INK2, fontsize=11, labelpad=8)
    ax.set_ylim(0, 121)
    ax.set_xlim(-0.6, len(RUNS) - 0.4)
    ax.set_yticks(range(0, 101, 20))
    style_axes(ax)
    ax.set_title("Time to run the job", loc="left", color=INK, fontsize=12.5, fontweight="semibold", pad=14)
    # Legend: identity never rests on colour alone.
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in (MESH, SOLVE, REST)]
    ax.legend(handles, ["Meshing", "Solving (300 iterations)", "Set-up and finish (about 1 min)"],
              loc="upper right", frameon=False, fontsize=10, labelcolor=INK2, handlelength=1.1,
              handleheight=1.1, borderaxespad=0.2)
    ax.text(len(RUNS) - 0.55, 90, "12 processors finish within 3%\nof 16 (63.4 against 61.7 min)",
            ha="right", va="top", color=INK2, fontsize=10, bbox=dict(facecolor=SURFACE, edgecolor="none", pad=3))


def speedup_panel(ax) -> None:
    """Speed-up lines for meshing and solving against the ideal, with the end values labelled."""
    procs = [r[0] for r in RUNS]
    ideal = [p / procs[0] for p in procs]
    ax.plot(procs, ideal, color=MUTED, linewidth=1.6, linestyle=(0, (5, 4)),
            label="Ideal (twice the cores, half the time)")
    for index, colour, name in ((4, MESH, "Meshing"), (5, SOLVE, "Solving")):
        ys = [r[index] for r in RUNS]
        ax.plot(procs, ys, color=colour, linewidth=2.2, solid_capstyle="round", label=name)
        ax.plot(procs, ys, linestyle="none", marker="o", markersize=8.5, markerfacecolor=colour,
                markeredgecolor=SURFACE, markeredgewidth=2)
        ax.text(procs[-1] + 0.45, ys[-1], f"{ys[-1]:.2f}×", va="center", ha="left", color=INK, fontsize=11,
                fontweight="semibold")
    ax.text(procs[-1] + 0.45, ideal[-1], f"{ideal[-1]:.2f}×", va="center", ha="left", color=INK2, fontsize=11)
    ax.set_xticks(procs, [str(p) for p in procs])
    ax.set_xlim(3.2, 17.9)
    ax.set_ylim(0.8, 4.35)
    ax.set_yticks([1, 2, 3, 4], ["1×", "2×", "3×", "4×"])
    ax.set_xlabel("Processors", color=INK2, fontsize=11, labelpad=8)
    ax.set_ylabel("Speed-up compared with 4 processors", color=INK2, fontsize=11, labelpad=8)
    style_axes(ax)
    ax.set_title("How much faster than 4 processors", loc="left", color=INK, fontsize=12.5, fontweight="semibold",
                 pad=14)
    ax.legend(loc="upper left", frameon=False, fontsize=10, labelcolor=INK2, handlelength=2.2, borderaxespad=0.2)
    eff8, eff16 = RUNS[1][6], RUNS[-1][6]                # solve efficiency: speed-up divided by the processor ratio
    note = f"Solving uses its processors\nless well the more it has:\n{eff8}% efficient at 8, {eff16}% at 16"
    ax.text(16.2, 2.55, note,
            ha="right", va="top", color=INK2, fontsize=10, bbox=dict(facecolor=SURFACE, edgecolor="none", pad=3))


def main() -> None:
    plt.rcParams["font.family"] = pick_font()
    fig, (left, right) = plt.subplots(1, 2, figsize=(12.4, 5.6), dpi=200, facecolor=SURFACE,
                                      gridspec_kw={"width_ratios": [1.0, 1.0], "wspace": 0.22})
    time_panel(left)
    speedup_panel(right)
    fig.text(0.045, 0.955, "More processors speed up the mesh, but barely the solve", color=INK, fontsize=17,
             fontweight="bold", ha="left", va="top")
    fig.text(0.045, 0.905, "PERRINN 2017 front wing, half model, 6.7 M cells, 300 solver iterations per run, "
             "Ryzen 9 5950X (16 cores)", color=INK2, fontsize=11, ha="left", va="top")
    fig.text(0.045, 0.02, "Cell counts differ by under 0.2% between runs (6.739 to 6.751 M) and Cd by under 0.4% "
             "(0.2111 to 0.2118). Likely cause of the flat solve: memory bandwidth (not tested).",
             color=MUTED, fontsize=9.5, ha="left", va="bottom")
    fig.subplots_adjust(left=0.07, right=0.97, top=0.78, bottom=0.17)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, facecolor=SURFACE)
    print(f"Written {OUT}")


if __name__ == "__main__":
    main()
