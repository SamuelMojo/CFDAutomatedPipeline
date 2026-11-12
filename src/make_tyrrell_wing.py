#!/usr/bin/env python3
"""Validation case: single-element wing in ground effect (Zerihan 2001; Zerihan & Zhang 2000).

The wing is the 80%-scale main element of the 1998 Tyrrell 026 F1 front wing, tested at the University of
Southampton over a moving ground at many ride heights, with balance forces and surface pressures.
Geometry from the thesis:
  * chord c = 223.4 mm, constant; span 1100 mm; profile coordinates from Table 2 (test_data/zerihan_wing/),
    which are already at the reference incidence (1 deg quoted = 3.45 deg true); trailing edge 1.65 mm thick;
  * rectangular endplates 250 x 100 x 4 mm, one at each tip.
ASSUMPTION (not given numerically in the thesis): endplates centred on the chord (13 mm overhang at each end)
with their lower edge level with the wing's lowest point (Fig. 1a shows them roughly so).
Ride height h = ground to the lowest point of the wing.

Pipeline convention: flow +X, leading edge at X = 0, span along Y (centred), ground Z = 0, downforce = NEGATIVE Cl.

    python src/make_tyrrell_wing.py --h-over-c 0.224 --out test_data/zerihan_wing/tyrrell_h0224.stl
    python src/make_tyrrell_wing.py --write-jobs            # STLs + job files for the ride-height sweep

Needs CadQuery for the STL. The data helpers (profile, references, job files) do not.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "test_data" / "zerihan_wing"
CHORD, SPAN = 0.2234, 1.100
EP_LEN, EP_HEIGHT, EP_THICK = 0.250, 0.100, 0.004
SPEED = 30.0                       # m/s; q = 56.25 mm water in the tests, Re_c about 4.5e5
AREF = CHORD * SPAN                # the thesis normalises by chord x span
SWEEP_HEIGHTS = (0.448, 0.313, 0.224, 0.179, 0.134, 0.090)
MODELS = {"sa": "SpalartAllmaras", "sst": "kOmegaSST"}

# Mesh for a 0.22 m chord wing (base cell 0.1 m: level n = 0.1 / 2^n m). Surface 1.56-0.78 mm, the gap and the
# region round the wing 3.1 mm (6 cells across the gap at h/c = 0.09), near-wake 6.25 mm. One wall layer of 0.6 mm
# for first-cell y+ of about 30, the value Genua (2009) found best for this wing with wall functions.
# Domain width and height as the Southampton 2.1 m x 1.7 m tunnel (slip side and top walls).
CASE = {"speed": SPEED, "ground": "moving", "aref": round(AREF, 6), "lref": CHORD, "processors": 14,
        "base_cell": 0.1, "surface_level": [6, 7], "feature_level": 7, "near_level": 5, "wake_level": 4,
        "layers": 1, "layer_thickness": 0.0006, "upstream": 3.0, "downstream": 6.0, "half_width": 1.05,
        "height": 1.7}


def load_profile(path: Path | None = None) -> tuple[np.ndarray, np.ndarray]:
    """(suction, pressure) arrays of (x/c, y/c), leading edge first."""
    path = path or ROOT / "test_data" / "zerihan_wing" / "profile_table2.csv"
    rows = list(csv.DictReader(l for l in open(path) if not l.startswith("#")))
    out = []
    for s in ("suction", "pressure"):
        a = np.array([(float(r["x_c"]), float(r["y_c"])) for r in rows if r["surface"] == s])
        if len(a) < 10 or not np.all(np.diff(a[:, 0]) > 0):
            raise ValueError(f"{s} surface in {path} is not a monotonic list of points")
        out.append(a)
    return out[0], out[1]


def outline(chord: float = CHORD) -> np.ndarray:
    """Closed outline in metres (x, z) through the thesis points: suction TE -> LE -> pressure TE
    (a straight trailing-edge base closes it)."""
    suc, pre = load_profile()
    return np.vstack([suc[::-1], pre[1:]]) * chord


def _natural_spline(t: np.ndarray, v: np.ndarray, tq: np.ndarray) -> np.ndarray:
    """Natural cubic spline through (t, v), evaluated at tq (numpy only)."""
    n = len(t)
    h = np.diff(t)
    a, b, c, d = np.zeros(n), np.ones(n), np.zeros(n), np.zeros(n)   # tridiagonal system for 2nd derivatives
    a[1:-1], b[1:-1], c[1:-1] = h[:-1] / 6, (h[:-1] + h[1:]) / 3, h[1:] / 6
    d[1:-1] = (v[2:] - v[1:-1]) / h[1:] - (v[1:-1] - v[:-2]) / h[:-1]
    for i in range(1, n):                                           # Thomas algorithm
        w = a[i] / b[i - 1]
        b[i] -= w * c[i - 1]
        d[i] -= w * d[i - 1]
    m = np.zeros(n)
    m[-1] = d[-1] / b[-1]
    for i in range(n - 2, -1, -1):
        m[i] = (d[i] - c[i] * m[i + 1]) / b[i]
    k = np.clip(np.searchsorted(t, tq) - 1, 0, n - 2)
    hk, s0, s1 = h[k], tq - t[k], t[k + 1] - tq
    return (m[k] * s1 ** 3 + m[k + 1] * s0 ** 3) / (6 * hk) + (v[k] / hk - m[k] * hk / 6) * s1 \
        + (v[k + 1] / hk - m[k + 1] * hk / 6) * s0


def smooth_outline(chord: float = CHORD, n: int = 1200) -> np.ndarray:
    """Dense, smooth outline: a cubic spline through the thesis points against arc length, sampled with
    points clustered at the leading edge (about 0.1 mm apart there, about 0.5 mm at the trailing edge). Building the
    CAD from these short straight segments avoids fitting one long spline through all the points."""
    p = outline(chord)
    t = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(p, axis=0).T))])
    i_le = int(np.argmin(p[:, 0]))
    t_le, total = t[i_le], t[-1]
    u = np.linspace(-1, 1, n)
    s = 0.3 * np.abs(u) + 0.7 * (1 - np.cos(np.pi / 2 * np.abs(u)))   # 0 at the LE, 1 at the TE
    tq = np.where(u < 0, t_le * (1 - s), t_le + (total - t_le) * s)
    tq = np.unique(np.concatenate([tq, t]))             # keep every measured point exactly
    out = np.column_stack([_natural_spline(t, p[:, 0], tq), _natural_spline(t, p[:, 1], tq)])
    keep = [0]                                          # drop near-duplicates (zero-length CAD edges fail)
    for i in range(1, len(out)):
        if np.hypot(*(out[i] - out[keep[-1]])) > 2e-5:
            keep.append(i)
    return out[keep]


def profile_area(chord: float = CHORD) -> float:
    """Cross-section area of the wing profile (shoelace formula on the smoothed outline), in m^2 for a chord in m."""
    p = smooth_outline(chord)
    x, z = p[:, 0], p[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(z, -1)) - np.dot(z, np.roll(x, -1)))


def expected_volume() -> float:
    """Expected solid volume: profile area times span, plus the two endplates."""
    return profile_area() * SPAN + 2 * EP_LEN * EP_THICK * EP_HEIGHT


def lowest_z(chord: float = CHORD) -> float:
    """Lowest z coordinate of the profile outline; used to set the ride height."""
    return float(outline(chord)[:, 1].min())


def build(h_over_c: float):
    """CadQuery solid: wing + endplates as ONE fused solid, lowest point at h above Z = 0."""
    import cadquery as cq
    if not 0.02 <= h_over_c <= 4:
        raise ValueError("h/c must be between 0.02 and 4")
    pts = [tuple(map(float, q)) for q in smooth_outline()]
    # Profile drawn in the XY plane (profile height along +Y), extruded along +Z by the span, then turned
    # +90 deg about X: profile height -> +Z (up), span -> -Y. No reliance on a named plane's orientation.
    wing = (cq.Workplane("XY").polyline(pts).close().extrude(SPAN)
            .rotate((0, 0, 0), (1, 0, 0), 90).translate((0, SPAN / 2, 0)))
    bb = wing.val().BoundingBox()
    if not (abs(bb.ymin + SPAN / 2) < 1e-6 and abs(bb.ymax - SPAN / 2) < 1e-6 and bb.zmax > 0.01):
        raise RuntimeError(f"wing ended up in the wrong place: {bb.xmin, bb.ymin, bb.zmin} .. {bb.xmax, bb.ymax, bb.zmax}")
    if abs(wing.val().Volume() / (profile_area() * SPAN) - 1) > 0.005:
        raise RuntimeError(f"wing volume {wing.val().Volume():.6f} does not match the profile area x span "
                           f"{profile_area() * SPAN:.6f}")
    z_low = bb.zmin
    x0 = (CHORD - EP_LEN) / 2
    for side in (1, -1):
        y0 = SPAN / 2 if side > 0 else -SPAN / 2 - EP_THICK
        ep = cq.Workplane("XY").box(EP_LEN, EP_THICK, EP_HEIGHT, centered=False).translate((x0, y0, z_low))
        wing = wing.union(ep)
    return wing.translate((0, 0, h_over_c * CHORD - z_low))


def check(body, h_over_c: float) -> dict:
    """Check a built wing against the expected geometry and return the measurements.

    Compares the lowest point (h/c times chord), span, endplate positions and volume with the design values,
    and tests that there is one valid solid, the right way up. info['problems'] lists anything that is off.
    """
    s = body.val()
    bb = s.BoundingBox()
    info = {"bbox_min": [bb.xmin, bb.ymin, bb.zmin], "bbox_max": [bb.xmax, bb.ymax, bb.zmax],
            "volume": s.Volume(), "expected_volume": expected_volume(), "solids": len(body.solids().vals())}
    probs = []
    for got, want, name in ((bb.zmin, h_over_c * CHORD, "lowest point"), (bb.ymin, -SPAN / 2 - EP_THICK, "y min"),
                            (bb.ymax, SPAN / 2 + EP_THICK, "y max"), (bb.xmin, (CHORD - EP_LEN) / 2, "x min")):
        if abs(got - want) > 2e-4:
            probs.append(f"{name} {got:.5f} (expected {want:.5f})")
    if abs(info["volume"] / info["expected_volume"] - 1) > 0.01:
        probs.append(f"volume {info['volume']:.6f} vs expected {info['expected_volume']:.6f}")
    if info["solids"] != 1:
        probs.append(f"{info['solids']} solids after the union (expected 1)")
    if not s.isValid():
        probs.append("OpenCascade reports the solid as invalid")
    # Right way up: the suction surface (lowest point at x/c ~ 0.2) faces the ground, so just above the
    # lowest point is solid at x/c 0.2 but air at x/c 0.9 (where the profile has risen towards the TE).
    import cadquery as cq
    z_probe = bb.zmin + 0.003
    if not s.isInside(cq.Vector(0.2 * CHORD, 0, z_probe)) or s.isInside(cq.Vector(0.9 * CHORD, 0, z_probe)):
        probs.append("wing is upside down or mis-shaped (suction surface should face the ground)")
    info["problems"] = probs
    return info


# --------------------------------------------------------------------------- #
# Reference data and job files
# --------------------------------------------------------------------------- #
def reference_forces(path: Path | None = None, transition: str = "fixed") -> np.ndarray:
    """Balance data (h/c, CL, CD), sorted by height. transition 'fixed' (tripped boundary layer: the like-for-like
    comparison for fully turbulent RANS, and the default) or 'free' (natural transition)."""
    name = {"fixed": "forces_3d_fixed_digitised.csv", "free": "forces_3d_digitised.csv"}[transition]
    path = path or ROOT / "test_data" / "zerihan_wing" / name
    rows = list(csv.DictReader(l for l in open(path) if not l.startswith("#")))
    a = np.array([(float(r["h_c"]), float(r["CL"]), float(r["CD"])) for r in rows])
    return a[np.argsort(a[:, 0])]


def reference_at(h_over_c: float, transition: str = "fixed") -> tuple[float, float]:
    """(CL downforce, CD) interpolated from the digitised balance data."""
    a = reference_forces(transition=transition)
    if not a[0, 0] <= h_over_c <= a[-1, 0]:
        raise ValueError(f"h/c {h_over_c} outside the measured range {a[0, 0]}..{a[-1, 0]}")
    return float(np.interp(h_over_c, a[:, 0], a[:, 1])), float(np.interp(h_over_c, a[:, 0], a[:, 2]))


def tag(h_over_c: float) -> str:
    """File-name tag for a ride height: h/c 0.224 becomes 'h0224'."""
    return f"h{round(h_over_c * 1000):04d}"


def write_reference(h_over_c: float, folder: Path = DATA) -> Path:
    """Write the measured reference forces for this h/c as a one-row CSV (cd, cl); downforce is negative Cl."""
    cl, cd = reference_at(h_over_c)
    p = folder / f"ref_{tag(h_over_c)}.csv"
    p.write_text(f"cd,cl\n{cd:.4f},{-cl:.4f}\n")         # pipeline sign: downforce is negative Cl
    return p


def job_spec(h_over_c: float, model_key: str) -> dict:
    """The job file (as a dictionary) for one ride height and one turbulence model, with its reference file."""
    t = tag(h_over_c)
    return {"name": f"tyrrell_{t}_{model_key}",
            "notes": f"Validation: Zerihan single-element wing in ground effect, h/c = {h_over_c}, "
                     f"{MODELS[model_key]}. Reference = balance forces, transition fixed, digitised from Zerihan (2001) Fig. 21.",
            "geometry": f"../../test_data/zerihan_wing/tyrrell_{t}.stl", "units": "m", "preset": "standard",
            "case": {**CASE, "turbulence_model": MODELS[model_key]},
            "reference": f"../../test_data/zerihan_wing/ref_{t}.csv"}


def write_jobs(heights=SWEEP_HEIGHTS, models=("sa", "sst"), jobs_dir: Path = ROOT / "job_specs" / "zerihan",
               make_stl=True) -> list[Path]:
    """Write the reference files, any missing STLs and the job files for every height and model; returns the job paths.
    """
    jobs_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for h in heights:
        write_reference(h, folder=DATA)
        if make_stl:
            stl = DATA / f"tyrrell_{tag(h)}.stl"
            if not stl.is_file():
                rc = main(["--h-over-c", str(h), "--out", str(stl)])
                if rc:
                    raise RuntimeError(f"geometry for h/c {h} failed")
        for m in models:
            p = jobs_dir / f"tyrrell_{tag(h)}_{m}.json"
            p.write_text(json.dumps(job_spec(h, m), indent=2))
            out.append(p)
    return out


def main(argv=None) -> int:
    """Command-line entry point: build one wing STL, or with --write-jobs the whole validation set. Returns 0, or non-
    zero on failure.
    """
    ap = argparse.ArgumentParser(description="Zerihan single-element wing in ground effect (validation case).")
    ap.add_argument("--h-over-c", type=float, help="ride height / chord")
    ap.add_argument("--out", type=Path, help="STL to write (metres)")
    ap.add_argument("--write-jobs", action="store_true",
                    help=f"write STLs, references and job files for h/c {SWEEP_HEIGHTS}, SA and SST")
    ap.add_argument("--heights", type=float, nargs="+", help="h/c values for --write-jobs (default: the standard sweep)")
    ap.add_argument("--tolerance", type=float, default=1e-5, help="STL chord tolerance, m (default 0.01 mm)")
    ap.add_argument("--angular", type=float, default=0.05, help="STL angular tolerance, rad")
    a = ap.parse_args(argv)
    if a.write_jobs:
        paths = write_jobs(heights=tuple(a.heights) if a.heights else SWEEP_HEIGHTS)
        print(f"{len(paths)} job files in {paths[0].parent}. Run e.g.:\n"
              f"  python src/run_batch.py job_specs/zerihan/tyrrell_h0224_sa.json      (single test first)\n"
              f"  python src/run_batch.py job_specs/zerihan/*_sa.json                  (the SA sweep)")
        return 0
    if a.h_over_c is None or a.out is None:
        ap.error("give --h-over-c and --out, or --write-jobs")
    try:
        body = build(a.h_over_c)
    except ImportError:
        print("ERROR: CadQuery is not installed (pip install cadquery).", file=sys.stderr)
        return 2
    info = check(body, a.h_over_c)
    print(f"Tyrrell wing, h/c {a.h_over_c:g} ({a.h_over_c * CHORD * 1000:.1f} mm): volume {info['volume'] * 1e6:.1f} cm^3 "
          f"(expected {info['expected_volume'] * 1e6:.1f}), bbox {[round(v, 4) for v in info['bbox_min']]} .. "
          f"{[round(v, 4) for v in info['bbox_max']]}")
    if info["problems"]:
        print("ERROR: " + "; ".join(info["problems"]), file=sys.stderr)
        return 1
    a.out.parent.mkdir(parents=True, exist_ok=True)
    body.val().exportStl(str(a.out), tolerance=a.tolerance, angularTolerance=a.angular)
    print(f"Written {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
