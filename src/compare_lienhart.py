#!/usr/bin/env python3
"""Compare a finished standard-Ahmed (25 deg) job with the Lienhart & Becker wind-tunnel data.

Data: ERCOFTAC Knowledge Base case AC1-05 (LDA velocities and rear-body Cp, 40 m/s, Re_H 7.68e5),
downloaded into test_data/lienhart/. The data use the same axes as the pipeline:
x = 0 at the rear end of the car, y = 0 on the symmetry plane, z = 0 on the floor.
NOTE: coordinates in the .dat files are in millimetres, although the velocity headers say m.

    python src/compare_lienhart.py $FOAM_RUN/jobs/ahmed25_standard_<time>

What it does:
  1. reads the measurement points (velocity planes and surface Cp points);
  2. writes an OpenFOAM `probes` function (system/lienhartProbes) at exactly those points;
     surface Cp points are moved 1 mm into the air along the surface normal so they fall
     inside a fluid cell;
  3. runs `foamPostProcess -solver incompressibleFluid -latestTime -func lienhartProbes`;
  4. compares simulated and measured streamwise velocity and Cp, writes compare_lienhart.md/.json
     and plots (if matplotlib is installed) into <job>/compare_lienhart/.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_ahmed as ma  # noqa: E402

__version__ = "0.2.0"
U_BULK = 40.0
VELOCITY_FILES = ["Ahmed_25_y=0_focus.dat", "Ahmed_25_y=0_global.dat", "Ahmed_25_x=-178.dat",
                  "Ahmed_25_x=-138.dat", "Ahmed_25_x=-88.dat", "Ahmed_25_x=-38.dat", "Ahmed_25_x=0.dat",
                  "Ahmed_25_x=80.dat", "Ahmed_25_x=200.dat", "Ahmed_25_x=500.dat"]
CP_FILE = "Ahmed_25_Cp.dat"
MISSING = 1e20


# --------------------------------------------------------------------------- #
# Measured data
# --------------------------------------------------------------------------- #
def load_dat(path: Path, ncols: int) -> np.ndarray:
    """Numeric rows of an ERCOFTAC .dat file (comment lines start with # or %); short rows skipped."""
    rows = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if not line.strip() or line.lstrip()[0] in "#%":
            continue
        parts = line.split()
        if len(parts) >= ncols:
            rows.append([float(v) for v in parts[:ncols]])
    if not rows:
        raise ValueError(f"No data rows in {path}")
    return np.array(rows)


def load_velocity(path: Path) -> dict:
    d = load_dat(path, 6)
    return {"name": Path(path).stem.replace("Ahmed_25_", ""), "xyz": d[:, :3] / 1000.0, "U": d[:, 3:6]}


def load_cp(path: Path) -> dict:
    d = load_dat(path, 4)
    return {"name": "Cp", "xyz": d[:, :3] / 1000.0, "Cp": d[:, 3]}


def surface_offset(xyz: np.ndarray, slant_deg: float = 25.0, d: float = 0.001) -> tuple[np.ndarray, list[str]]:
    """Move surface points of the Ahmed body d metres into the air along the local outward normal.
    Faces: side (y = -W/2), underside (z = clearance), rear (x = 0), roof (z = clearance + H),
    slant (plane between roof and rear)."""
    dx, dz = ma.slant_geometry(slant_deg)
    roof = ma.CLEARANCE + ma.H
    a = math.radians(slant_deg)
    n_slant = np.array([math.sin(a), 0.0, math.cos(a)])
    out, kind = xyz.copy(), []
    for i, (x, y, z) in enumerate(xyz):
        if y <= -ma.W / 2 + 1e-4:
            out[i, 1] -= d
            kind.append("side")
        elif z <= ma.CLEARANCE + 1e-4:
            out[i, 2] -= d
            kind.append("underside")
        elif x >= -1e-4:
            out[i, 0] += d
            kind.append("rear")
        elif z >= roof - 1e-4:
            out[i, 2] += d
            kind.append("roof")
        else:
            out[i] += d * n_slant
            kind.append("slant")
    return out, kind


# --------------------------------------------------------------------------- #
# OpenFOAM probes
# --------------------------------------------------------------------------- #
def probes_dict(points: np.ndarray, fields=("U", "p")) -> str:
    pts = "\n".join(f"        ({p[0]:.6f} {p[1]:.6f} {p[2]:.6f})" for p in points)
    return ("type            probes;\nlibs            (\"libsampling.so\");\n"
            f"fields          ({' '.join(fields)});\nprobeLocations\n(\n{pts}\n);\n")


def latest_time(case: Path) -> Path | None:
    times = []
    for d in case.iterdir():
        try:
            times.append((float(d.name), d))
        except ValueError:
            pass
    times = [t for t in times if t[0] > 0 and t[1].is_dir()]
    return max(times)[1] if times else None


def field_names(case: Path) -> tuple[str, str]:
    """('UMean', 'pMean') if the latest time has averaged fields, else ('U', 'p')."""
    t = latest_time(case)
    if t is not None and (t / "UMean").is_file() and (t / "pMean").is_file():
        return "UMean", "pMean"
    return "U", "p"


def parse_probes(path: Path, vector: bool) -> tuple[np.ndarray, np.ndarray]:
    """Read an OpenFOAM probes output file. Returns (probe locations, values at the LAST time).
    Missing probes (outside the mesh) come back as NaN."""
    text = Path(path).read_text(errors="replace").splitlines()
    locs = [tuple(float(v) for v in m.groups()) for line in text
            if (m := re.match(r"#\s*Probe\s+\d+\s+\(([^ )]+)\s+([^ )]+)\s+([^ )]+)\)", line))]
    data = [l for l in text if l.strip() and not l.lstrip().startswith("#")]
    if not data:
        raise ValueError(f"No values in {path}")
    last = data[-1]
    if vector:
        vals = [[float(v) for v in g.split()] for g in re.findall(r"\(([^)]*)\)", last)]
    else:
        vals = [float(v) for v in last.split()[1:]]
    vals = np.array(vals, dtype=float)
    if len(vals) != len(locs):
        raise ValueError(f"{path}: {len(locs)} probe locations but {len(vals)} values")
    vals[np.abs(vals) > MISSING] = np.nan
    return np.array(locs), vals


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def compare_velocity(meas: dict, sim_u: np.ndarray) -> dict:
    um, us = meas["U"][:, 0], sim_u[:, 0]
    ok = ~np.isnan(us)
    err = (us[ok] - um[ok]) / U_BULK
    return {"name": meas["name"], "points": int(len(um)), "found": int(ok.sum()),
            "rms_error_over_Ub": float(np.sqrt(np.mean(err ** 2))) if ok.any() else None,
            "mean_error_over_Ub": float(np.mean(err)) if ok.any() else None}


def slant_flow_state(meas: dict, sim_u: np.ndarray, slant_deg: float = 25.0) -> dict:
    """Flow just above the slant on the symmetry plane: at each measured station over the slant,
    the point closest to the surface (about 16 mm above it in this data set). The key 25 deg question
    is whether the flow stays attached (U > 0, slowed down) or separates (U <= 0)."""
    dx, dz = ma.slant_geometry(slant_deg)
    xyz = meas["xyz"]
    stations = []
    for x0 in np.unique(np.round(xyz[:, 0], 4)):
        if not (-dx < x0 < 0):
            continue
        m = np.flatnonzero(np.isclose(xyz[:, 0], x0) & (np.abs(xyz[:, 1]) < 1e-6))
        if not len(m):
            continue
        i = m[np.argmin(xyz[m, 2])]
        surf = ma.CLEARANCE + ma.H - (x0 + dx) * dz / dx
        stations.append({"x": float(x0), "height_above_slant_mm": round(float((xyz[i, 2] - surf) * 1000), 1),
                         "U_measured": float(meas["U"][i, 0]),
                         "U_simulated": None if np.isnan(sim_u[i, 0]) else float(sim_u[i, 0])})
    sim = [st["U_simulated"] for st in stations if st["U_simulated"] is not None]
    return {"stations": stations,
            "measured_reversed": int(sum(st["U_measured"] <= 0 for st in stations)),
            "simulated_reversed": int(sum(u <= 0 for u in sim)),
            "mean_U_measured": float(np.mean([st["U_measured"] for st in stations])) if stations else None,
            "mean_U_simulated": float(np.mean(sim)) if sim else None}


def compare_cp(meas: dict, sim_p: np.ndarray, kinds: list[str]) -> dict:
    cps = sim_p / (0.5 * U_BULK ** 2)                      # kinematic pressure, freestream reference 0
    ok = ~np.isnan(cps)
    res = {"points": int(len(cps)), "found": int(ok.sum())}
    kinds = np.array(kinds)
    for k in ("all", "roof", "slant", "rear", "side", "underside"):
        m = ok if k == "all" else ok & (kinds == k)
        if m.any():
            e = cps[m] - meas["Cp"][m]
            res[k] = {"n": int(m.sum()), "rms_error": float(np.sqrt(np.mean(e ** 2))),
                      "mean_measured": float(np.mean(meas["Cp"][m])), "mean_simulated": float(np.mean(cps[m]))}
    return res


def plots(outdir: Path, vel: list[dict], sims: list[np.ndarray], cp: dict, cps: np.ndarray, kinds) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    made = []
    for meas, su in zip(vel, sims):
        if not meas["name"].startswith("y=0"):
            continue
        xs = np.unique(np.round(meas["xyz"][:, 0], 4))
        fig, ax = plt.subplots(figsize=(min(2 + 0.9 * len(xs), 22), 5), dpi=140)
        scale = 0.6 * (np.min(np.diff(xs)) if len(xs) > 1 else 0.05) / U_BULK
        for x0 in xs:
            m = np.isclose(meas["xyz"][:, 0], x0)
            z = meas["xyz"][m, 2]
            o = np.argsort(z)
            ax.plot(x0 + meas["U"][m, 0][o] * scale, z[o], "o", ms=2.5, color="#1f77b4",
                    label="wind tunnel (Lienhart)" if x0 == xs[0] else None)
            ax.plot(x0 + su[m, 0][o] * scale, z[o], "-", lw=1.2, color="#d62728",
                    label="CFD" if x0 == xs[0] else None)
            ax.axvline(x0, color="0.85", lw=0.6)
        # body outline (symmetry plane)
        dx, dz = ma.slant_geometry(25.0)
        top, bot = ma.CLEARANCE + ma.H, ma.CLEARANCE
        ax.plot([-ma.L, -dx, 0, 0, -ma.L], [top, top, top - dz, bot, bot], color="k", lw=1)
        ax.set_xlim(xs.min() - 0.05, xs.max() + 0.08)
        ax.set_xlabel("x (m), profiles offset: U scaled to station spacing")
        ax.set_ylabel("z (m)")
        ax.set_title(f"Streamwise velocity, plane {meas['name']}")
        ax.legend(loc="upper left")
        ax.set_aspect("auto")
        f = outdir / f"profiles_{meas['name']}.png"
        fig.tight_layout(); fig.savefig(f); plt.close(fig)
        made.append(f.name)
    # Cp on the symmetry plane, one panel per surface
    kinds = np.array(kinds)
    m0 = np.isclose(cp["xyz"][:, 1], 0.0)
    panels = [("roof + slant", m0 & np.isin(kinds, ["roof", "slant"]), 0, "x (m)"),
              ("rear face", m0 & (kinds == "rear"), 2, "z (m)"),
              ("underside", m0 & (kinds == "underside"), 0, "x (m)")]
    panels = [p for p in panels if p[1].any()]
    if panels:
        fig, axes = plt.subplots(1, len(panels), figsize=(4.2 * len(panels), 3.8), dpi=140, squeeze=False)
        for ax, (title, m, col, xl) in zip(axes[0], panels):
            c = cp["xyz"][m, col]
            o = np.argsort(c)
            ax.plot(c[o], cp["Cp"][m][o], "o", ms=3.5, label="wind tunnel")
            ax.plot(c[o], cps[m][o], "-", color="#d62728", label="CFD")
            ax.set_title(f"Cp, {title} (y = 0)", fontsize=10)
            ax.set_xlabel(xl)
            ax.invert_yaxis()
        axes[0][0].set_ylabel("Cp")
        axes[0][0].legend()
        f = outdir / "cp_symmetry_plane.png"
        fig.tight_layout(); fig.savefig(f); plt.close(fig)
        made.append(f.name)
    return made


# --------------------------------------------------------------------------- #
def find_case(path: Path) -> Path:
    p = Path(path)
    return p / "case" if (p / "case" / "system").is_dir() else p


def prepare(case: Path, data: Path, slant: float) -> dict:
    vel = [load_velocity(data / f) for f in VELOCITY_FILES if (data / f).is_file()]
    if not vel:
        raise FileNotFoundError(f"No Lienhart velocity files in {data}")
    cp = load_cp(data / CP_FILE)
    cp_probe, kinds = surface_offset(cp["xyz"], slant)
    blocks = [v["xyz"] for v in vel] + [cp_probe]
    pts = np.vstack(blocks)
    fields = field_names(case)
    (case / "system" / "lienhartProbes").write_text(probes_dict(pts, fields))
    return {"vel": vel, "cp": cp, "kinds": kinds, "sizes": [len(b) for b in blocks], "fields": fields}


def analyse(case: Path, outdir: Path, prep: dict, slant: float) -> dict:
    base = case / "postProcessing" / "lienhartProbes"
    times = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: float(d.name)) if base.is_dir() else []
    if not times:
        raise FileNotFoundError(f"No probes output in {base}; did foamPostProcess run?")
    fu, fp = prep.get("fields", ("U", "p"))
    _, U = parse_probes(times[-1] / fu, vector=True)
    _, p = parse_probes(times[-1] / fp, vector=False)
    splits = np.cumsum(prep["sizes"])[:-1]
    sims = np.split(U, splits)
    psplit = np.split(p, splits)
    vres = [compare_velocity(m, s) for m, s in zip(prep["vel"], sims[:-1])]
    focus = next((i for i, m in enumerate(prep["vel"]) if m["name"] == "y=0_focus"), None)
    slant_state = slant_flow_state(prep["vel"][focus], sims[focus], slant) if focus is not None else None
    cps = psplit[-1] / (0.5 * U_BULK ** 2)
    cres = compare_cp(prep["cp"], psplit[-1], prep["kinds"])
    outdir.mkdir(exist_ok=True)
    figs = plots(outdir, prep["vel"], sims[:-1], prep["cp"], cps, prep["kinds"])
    res = {"tool": "compare_lienhart.py", "tool_version": __version__, "case": str(case),
           "time": times[-1].name, "fields": [fu, fp], "velocity": vres, "slant_flow": slant_state, "cp": cres, "figures": figs}
    (outdir / "compare_lienhart.json").write_text(json.dumps(res, indent=2))
    (outdir / "compare_lienhart.md").write_text(report_md(res))
    return res


def report_md(r: dict) -> str:
    f = r.get("fields", ["U", "p"])
    note = ("Simulated values: time-averaged fields (UMean, pMean)." if f[0] == "UMean" else
            "Simulated values: LAST ITERATION SNAPSHOT (no averaged fields found); unreliable if the run oscillates.")
    L = ["# CFD vs Lienhart & Becker wind tunnel (Ahmed body, 25 deg)", "", note, "",
         "| Plane | Points | Found in mesh | RMS error in U / Ub | Mean error / Ub |", "|---|---|---|---|---|"]
    for v in r["velocity"]:
        rms = f"{v['rms_error_over_Ub']:.3f}" if v["rms_error_over_Ub"] is not None else "–"
        me = f"{v['mean_error_over_Ub']:+.3f}" if v["mean_error_over_Ub"] is not None else "–"
        L.append(f"| {v['name']} | {v['points']} | {v['found']} | {rms} | {me} |")
    s = r.get("slant_flow")
    if s and s["stations"]:
        L += ["", "Flow just above the slant (symmetry plane, lowest measured point at each station):", "",
              "| x (mm) | Height above slant (mm) | U measured (m/s) | U CFD (m/s) |", "|---|---|---|---|"]
        for st in s["stations"]:
            us = f"{st['U_simulated']:.1f}" if st["U_simulated"] is not None else "–"
            L.append(f"| {st['x'] * 1000:.0f} | {st['height_above_slant_mm']} | {st['U_measured']:.1f} | {us} |")
        L += ["", f"Reversed flow (U <= 0) at {s['measured_reversed']} of {len(s['stations'])} stations in the tunnel, "
              f"{s['simulated_reversed']} in the CFD."]
    c = r["cp"]
    L += ["", "| Surface | Points | RMS error in Cp | Mean Cp measured | Mean Cp simulated |", "|---|---|---|---|---|"]
    for k in ("all", "roof", "slant", "rear", "side", "underside"):
        if k in c:
            L.append(f"| {k} | {c[k]['n']} | {c[k]['rms_error']:.3f} | {c[k]['mean_measured']:.3f} | {c[k]['mean_simulated']:.3f} |")
    if r["figures"]:
        L += ["", "Figures: " + ", ".join(r["figures"])]
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compare an Ahmed 25 deg job with Lienhart & Becker wind-tunnel data.")
    ap.add_argument("job", type=Path, help="job folder (or its case folder)")
    ap.add_argument("--data", type=Path, default=Path(__file__).resolve().parents[1] / "test_data" / "lienhart")
    ap.add_argument("--slant", type=float, default=25.0)
    ap.add_argument("--skip-run", action="store_true", help="only analyse existing probes output")
    a = ap.parse_args(argv)
    case = find_case(a.job)
    if not (case / "system").is_dir():
        print(f"ERROR: no OpenFOAM case in {a.job}", file=sys.stderr)
        return 2
    try:
        prep = prepare(case, a.data, a.slant)
        print(f"{sum(prep['sizes']):,} probe points written to {case / 'system' / 'lienhartProbes'}")
        if not a.skip_run:
            fu, fp = prep["fields"]
            cmd = (["foamPostProcess", "-latestTime", "-fields", f"({fu} {fp})", "-func", "lienhartProbes"]
                   if fu == "UMean" else
                   ["foamPostProcess", "-solver", "incompressibleFluid", "-latestTime", "-func", "lienhartProbes"])
            print(f"Sampling {fu} and {fp}")
            with open(case / "log.lienhartProbes", "w") as f:
                r = subprocess.run(cmd, cwd=case, stdout=f, stderr=subprocess.STDOUT)
            if r.returncode != 0:
                print(f"ERROR: foamPostProcess failed; see {case / 'log.lienhartProbes'}", file=sys.stderr)
                return 2
        outdir = (case.parent if case.name == "case" else case) / "compare_lienhart"
        res = analyse(case, outdir, prep, a.slant)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(report_md(res))
    print(f"Written to {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
