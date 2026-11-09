#!/usr/bin/env python3
"""Generate an OpenFOAM v14 external-aero case for a wing STL (metres).

It does NOT write dictionaries from scratch. It copies the OpenFOAM motorBikeSteady
tutorial (which is known to run on this machine) and edits specific entries, failing
loudly if an expected piece of text is not found. It also writes a job.json with the
inputs, parameters and tool versions, and an Allrun.wing script with the run commands.

Run it only on an STL that passed inspect_geometry.py and is in metres.

Example (Ubuntu, OpenFOAM environment loaded):
    python src/make_wing_case.py test_data/wing_m.stl --case $FOAM_RUN/wing_v1

Conventions: flow along +X, ground plane at Z = 0 moving at the free-stream speed,
lift direction +Z (so downforce shows as NEGATIVE Cl), full model (no symmetry plane).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inspect_geometry as ig  # noqa: E402

__version__ = "0.1.0"
TUTORIAL_REL = "incompressibleFluid/motorBikeSteady"
HEADER_END = "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //"


class CaseError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def frontal_area(tris: np.ndarray, res: float = 0.004) -> float:
    """Area of the silhouette seen along +X (projection onto the Y-Z plane), by rasterising."""
    y, z = tris[:, :, 1], tris[:, :, 2]
    y0, z0 = y.min(), z.min()
    ys, zs = (y - y0) / res, (z - z0) / res
    img = np.zeros((int(ys.max()) + 3, int(zs.max()) + 3), bool)
    for a, b in zip(ys, zs):
        i0, i1 = int(a.min()), int(a.max()) + 1
        j0, j1 = int(b.min()), int(b.max()) + 1
        d = (b[1] - b[2]) * (a[0] - a[2]) + (a[2] - a[1]) * (b[0] - b[2])
        if abs(d) < 1e-12:
            continue
        gi, gj = np.meshgrid(np.arange(i0, i1) + 0.5, np.arange(j0, j1) + 0.5, indexing="ij")
        l1 = ((b[1] - b[2]) * (gi - a[2]) + (a[2] - a[1]) * (gj - b[2])) / d
        l2 = ((b[2] - b[0]) * (gi - a[2]) + (a[0] - a[2]) * (gj - b[2])) / d
        img[i0:i1, j0:j1] |= (l1 >= 0) & (l2 >= 0) & (l1 + l2 <= 1)
    return float(img.sum() * res * res)


# --------------------------------------------------------------------------- #
# Text editing helpers (fail loudly)
# --------------------------------------------------------------------------- #
def sub_once(text: str, old: str, new: str, what: str) -> str:
    if text.count(old) != 1:
        raise CaseError(f"Expected exactly one '{old}' in {what}, found {text.count(old)}. "
                        "The tutorial layout is not what this script was written for.")
    return text.replace(old, new)


def regex_once(text: str, pattern: str, new: str, what: str, flags=re.S | re.M) -> str:
    out, n = re.subn(pattern, lambda m: new, text, count=0, flags=flags)
    if n != 1:
        raise CaseError(f"Expected exactly one match of /{pattern}/ in {what}, found {n}.")
    return out


def foam_header(cls: str, location: str, obj: str) -> str:
    return (
        "FoamFile\n{\n    format      ascii;\n"
        f"    class       {cls};\n    location    \"{location}\";\n    object      {obj};\n}}\n"
        f"{HEADER_END}\n\n"
    )


def fmt(x: float) -> str:
    return f"{x:.6g}"


# --------------------------------------------------------------------------- #
# Case generation
# --------------------------------------------------------------------------- #
def make_case(stl: Path, case: Path, tutorial: Path, speed: float, base_cell: float,
              surface_level: tuple[int, int], feature_level: int, near_level: int,
              wake_level: int, end_time: int, aref: float | None, lref: float | None,
              layers: bool, upstream: float, downstream: float, half_width: float,
              height: float) -> dict:
    stl, case, tutorial = Path(stl), Path(case), Path(tutorial)
    if not stl.is_file():
        raise CaseError(f"STL not found: {stl}")
    if case.exists():
        raise CaseError(f"Case folder already exists: {case}. Choose a new name; cases are never overwritten.")
    if not tutorial.is_dir():
        raise CaseError(f"Tutorial folder not found: {tutorial}. Is the OpenFOAM environment loaded "
                        "(echo $FOAM_TUTORIALS)?")

    tris = ig.read_stl(stl)
    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    if hi[0] - lo[0] > 20 or hi[2] > 5 or lo[2] < -1e-6:
        raise CaseError(f"Geometry bounds {lo} .. {hi} do not look like a wing in metres above Z = 0.")
    if aref is None:
        aref = frontal_area(tris)
    length = float(hi[0] - lo[0])
    lref = float(lref) if lref else length

    # Domain, snapped to whole base cells so cells are cubes.
    def snapped(start: float, stop: float):
        n = max(1, round((stop - start) / base_cell))
        return start, start + n * base_cell, n

    x0, x1, nx = snapped(lo[0] - upstream, hi[0] + downstream)
    w = half_width
    y0, y1, ny = snapped(-w, w)
    z0, z1, nz = 0.0, height, max(1, round(height / base_cell))
    z1 = z0 + nz * base_cell
    # Point that is outside the wing and not on any cell face at any refinement level.
    s = base_cell / 0.2
    inside = (x0 + base_cell * 5 + 0.0731 * s,
              y0 + base_cell * (ny // 2) + 0.0273 * s,
              z0 + base_cell * (nz // 2) + 0.0317 * s)

    shutil.copytree(tutorial, case, ignore=shutil.ignore_patterns("log.*", "processor*", "geometry"))
    (case / "constant" / "geometry").mkdir(parents=True, exist_ok=True)
    shutil.copy2(stl, case / "constant" / "geometry" / "wing.stl")

    def edit(rel: str, fn):
        p = case / rel
        p.write_text(fn(p.read_text(), rel))

    # blockMeshDict: new box, same patch names as the tutorial
    def block_mesh(t, rel):
        v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        vtxt = "\n".join(f"    ({fmt(a)} {fmt(b)} {fmt(c)})" for a, b, c in v)
        t = regex_once(t, r"^vertices\s*\(.*?^\);", f"vertices\n(\n{vtxt}\n);", rel)
        return regex_once(t, r"hex \(0 1 2 3 4 5 6 7\) \([0-9 ]+\)",
                          f"hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz})", rel)
    edit("system/blockMeshDict", block_mesh)

    # snappyHexMeshDict
    near = (lo[0] - 0.3, lo[1] - 0.2, 0.0, hi[0] + 0.6, hi[1] + 0.2, hi[2] + 0.25)
    wake = (lo[0] - 0.3, lo[1] - 0.4, 0.0, hi[0] + 3.0, hi[1] + 0.4, hi[2] + 0.7)

    def snappy(t, rel):
        t = regex_once(t, r"^geometry\s*\{.*?^\};", f"""geometry
{{
    wing
    {{
        type triSurface;
        file "wing.stl";
    }}
    nearBox
    {{
        type box;
        min ({fmt(near[0])} {fmt(near[1])} {fmt(near[2])});
        max ({fmt(near[3])} {fmt(near[4])} {fmt(near[5])});
    }}
    wakeBox
    {{
        type box;
        min ({fmt(wake[0])} {fmt(wake[1])} {fmt(wake[2])});
        max ({fmt(wake[3])} {fmt(wake[4])} {fmt(wake[5])});
    }}
}};""", rel)
        t = regex_once(t, r'file "motorBike.eMesh";\s*level 6;',
                       f'file "wing.eMesh";\n            level {feature_level};', rel)
        t = regex_once(t, r"^    refinementSurfaces\s*\{.*?^    \}", f"""    refinementSurfaces
    {{
        wing
        {{
            level ({surface_level[0]} {surface_level[1]});
            patchInfo
            {{
                type wall;
                inGroups (wingGroup);
            }}
        }}
    }}""", rel)
        t = regex_once(t, r"^    refinementRegions\s*\{.*?^    \}", f"""    refinementRegions
    {{
        nearBox
        {{
            mode    inside;
            level   {near_level};
        }}
        wakeBox
        {{
            mode    inside;
            level   {wake_level};
        }}
    }}""", rel)
        t = regex_once(t, r"insidePoint\s*\([^)]*\);",
                       f"insidePoint ({fmt(inside[0])} {fmt(inside[1])} {fmt(inside[2])});", rel)
        t = sub_once(t, "maxGlobalCells 2000000;", "maxGlobalCells 20000000;", rel)
        t = sub_once(t, '"(lowerWall|motorBike).*"', '"(lowerWall|wing).*"', rel)
        t = regex_once(t, r"^addLayers\s+(true|false);", f"addLayers       {'true' if layers else 'false'};", rel)
        return t
    edit("system/snappyHexMeshDict", snappy)

    edit("system/surfaceFeaturesDict",
         lambda t, rel: sub_once(t, 'surfaces ("motorBike.obj");', 'surfaces ("wing.stl");', rel))
    edit("system/controlDict",
         lambda t, rel: regex_once(t, r"^endTime\s+\d+;", f"endTime         {end_time};", rel))

    # Boundary-condition files: only the geometry group name changes
    for f in ("U.orig", "p", "k", "omega", "nut"):
        edit(f"0/{f}", lambda t, rel: sub_once_all(t, "motorBikeGroup", "wingGroup", rel))

    # Free-stream values (k and omega scaled from the tutorial's 20 m/s values)
    k = 0.24 * (speed / 20.0) ** 2
    om = 1.78 * (speed / 20.0)
    def init_cond(t, rel):
        for name, val in (("flowVelocity", f"({fmt(speed)} 0 0)"), ("turbulentKE", fmt(k)),
                          ("turbulentOmega", fmt(om))):
            t = regex_once(t, rf"^{name}\s+[^;]+;", f"{name:<20} {val};", rel)
        return t
    edit("0/include/initialConditions", init_cond)

    # Force coefficients: written out in full so every reference value is visible
    cofr = (float(lo[0] + 0.5 * length), 0.0, float(0.5 * (lo[2] + hi[2])))
    (case / "system/forceCoeffs").write_text(f"""type            forceCoeffs;
libs            ("libforces.so");
writeControl    timeStep;
timeInterval    1;
log             yes;
patches         (wingGroup);
rho             rhoInf;      // Indicates incompressible
rhoInf          1;           // Redundant for incompressible
liftDir         (0 0 1);     // downforce is NEGATIVE Cl
dragDir         (1 0 0);
CofR            ({fmt(cofr[0])} {fmt(cofr[1])} {fmt(cofr[2])});  // PLACEHOLDER: wing mid-chord; moments depend on this
pitchAxis       (0 1 0);
magUInf         {fmt(speed)};
lRef            {fmt(lref)};        // chord (X length of the geometry)
Aref            {fmt(aref)};      // frontal area from the geometry (silhouette along X)
""")
    # Only the force function: the tutorial's cutPlane/streamlines were set for the motorbike
    (case / "system/functions").write_text(
        foam_header("dictionary", "system", "functions") + "#includeFunc forceCoeffs\n\n" + "// " + "*" * 73 + " //\n")

    for junk in ("Allrun", "Allclean", "Allrun-parallel"):
        (case / junk).unlink(missing_ok=True)
    nproc = decompose_count(case / "system/decomposeParDict")
    (case / "Allrun.wing").write_text(f"""#!/bin/bash
# Generated by make_wing_case.py. Same stages as the motorBikeSteady tutorial, run by hand earlier.
cd "$(dirname "$0")" || exit 1
set -e -o pipefail
surfaceFeatures | tee log.surfaceFeatures
blockMesh | tee log.blockMesh
decomposePar -copyZero | tee log.decomposePar
mpirun -np {nproc} snappyHexMesh -parallel | tee log.snappyHexMesh
mpirun -np {nproc} patchSummary -parallel | tee log.patchSummary
mpirun -np {nproc} potentialFoam -parallel | tee log.potentialFoam
mpirun -np {nproc} foamRun -parallel | tee log.foamRun
reconstructPar -latestTime | tee log.reconstructPar
touch {case.name}.foam
""")
    os.chmod(case / "Allrun.wing", 0o755)

    job = {
        "tool": "make_wing_case.py", "tool_version": __version__,
        "created_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "input_stl": {"path": str(stl), "sha256": ig.sha256_of(stl), "triangles": int(len(tris)),
                      "bbox_min": lo.tolist(), "bbox_max": hi.tolist()},
        "openfoam": {"version": os.environ.get("WM_PROJECT_VERSION", "unknown"),
                     "project": os.environ.get("WM_PROJECT", "unknown"),
                     "template": str(tutorial)},
        "python": platform.python_version(), "numpy": np.__version__,
        "parameters": {
            "speed_m_s": speed, "base_cell_m": base_cell, "surface_level": list(surface_level),
            "feature_level": feature_level, "near_level": near_level, "wake_level": wake_level,
            "end_time_iterations": end_time, "layers": layers, "processors": nproc,
            "domain_min": [float(x0), float(y0), float(z0)], "domain_max": [float(x1), float(y1), float(z1)],
            "base_cells": [nx, ny, nz],
            "inside_point": [float(v) for v in inside],
            "Aref_m2": aref, "lRef_m": lref, "CofR": list(cofr),
            "ground": "Z = 0, moving at free-stream speed", "symmetry": "none (full model)",
            "turbulence_inlet": {"k": k, "omega": om, "note": "tutorial values scaled with speed; not a measured intensity"},
        },
    }
    (case / "job.json").write_text(json.dumps(job, indent=2))
    return job


def sub_once_all(text: str, old: str, new: str, what: str) -> str:
    if old not in text:
        raise CaseError(f"'{old}' not found in {what}.")
    return text.replace(old, new)


def decompose_count(path: Path) -> int:
    m = re.search(r"numberOfSubdomains\s+(\d+);", path.read_text())
    if not m:
        raise CaseError("numberOfSubdomains not found in decomposeParDict.")
    return int(m.group(1))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Generate an OpenFOAM v14 wing case from a metres STL.")
    p.add_argument("stl", type=Path, help="wing STL in metres (from inspect_geometry.py --export-stl)")
    p.add_argument("--case", type=Path, required=True, help="new case folder (must not exist)")
    p.add_argument("--tutorial", type=Path,
                   default=Path(os.environ.get("FOAM_TUTORIALS", "")) / TUTORIAL_REL,
                   help="template tutorial folder (default: $FOAM_TUTORIALS/" + TUTORIAL_REL + ")")
    p.add_argument("--speed", type=float, default=20.0, help="free-stream and ground speed, m/s")
    p.add_argument("--base-cell", type=float, default=0.2, help="background cell size, m")
    p.add_argument("--surface-level", type=int, nargs=2, default=(4, 5), metavar=("MIN", "MAX"))
    p.add_argument("--feature-level", type=int, default=5)
    p.add_argument("--near-level", type=int, default=3)
    p.add_argument("--wake-level", type=int, default=2)
    p.add_argument("--end-time", type=int, default=500, help="solver iterations")
    p.add_argument("--aref", type=float, help="reference area m^2 (default: computed frontal area)")
    p.add_argument("--lref", type=float, help="reference length m (default: X length of the geometry)")
    p.add_argument("--layers", action="store_true", help="enable boundary-layer addition (off by default)")
    p.add_argument("--upstream", type=float, default=5.0, help="inlet distance ahead of the wing, m")
    p.add_argument("--downstream", type=float, default=8.0, help="outlet distance behind the wing, m")
    p.add_argument("--half-width", type=float, default=4.0, help="side boundaries at +-this Y, m")
    p.add_argument("--height", type=float, default=4.0, help="top boundary height above ground, m")
    a = p.parse_args(argv)
    try:
        job = make_case(a.stl, a.case, a.tutorial, a.speed, a.base_cell, tuple(a.surface_level),
                        a.feature_level, a.near_level, a.wake_level, a.end_time, a.aref, a.lref,
                        a.layers, a.upstream, a.downstream, a.half_width, a.height)
    except (CaseError, ig.GeometryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    pr = job["parameters"]
    print(f"Case written to {a.case}")
    print(f"  domain {pr['domain_min']} .. {pr['domain_max']} m, base cells {pr['base_cells']}")
    print(f"  Aref = {pr['Aref_m2']:.4f} m^2, lRef = {pr['lRef_m']:.3f} m, speed = {pr['speed_m_s']} m/s")
    print(f"  CofR = {pr['CofR']} (placeholder)")
    print(f"Run it with:  {a.case}/Allrun.wing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
