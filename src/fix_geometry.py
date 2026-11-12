#!/usr/bin/env python3
"""Turn a multi-part CAD assembly (STEP) into ONE closed solid ready for meshing.

    python src/fix_geometry.py test_data/2026_wing.step --out test_data/2026_wing_fixed.stl
    python src/inspect_geometry.py test_data/2026_wing_fixed.stl --units m

Real team CAD is an assembly: many solids that touch, overlap or nearly touch. Meshers need one sealed outer
surface, so overlapping parts give leaks (air left inside the body) and non-manifold edges. This tool:
  1. reads every solid in the STEP file (OpenCascade via CadQuery) and drops tiny ones (bolts, debris);
  2. FUSES them into one solid with a small "fuzzy" tolerance, so parts that nearly touch are merged too;
     if the all-at-once fuse fails, it fuses part by part and then retries with larger tolerances;
  3. cleans the result (merges faces split by the fuse, repairs small defects);
  4. checks it: one solid, valid, volume no larger than the sum of the parts;
  5. writes an STL in METRES (STEP files are read in mm) and a JSON report next to it;
  6. optionally (--ground-box) puts it in the pipeline's wind-tunnel frame using the CFD box exported from the
     same CAD assembly: the box's bottom face becomes the ground (Z = 0) and its long axis the flow direction (+X),
     so the car's rake and ride height are kept exactly.
Exit codes: 0 one clean solid; 1 written but with problems (see report); 2 could not process.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np

__version__ = "0.1.0"


def read_step_vertices(path: Path) -> np.ndarray:
    """Vertex coordinates (metres) of a simple STEP part, without CAD libraries, as stored in the file.
    Assembly placements are NOT applied: for the PERRINN Onshape export the stored coordinates are already the
    assembly's (checked against an Onshape measurement: origin 19.999 mm above the box floor), while the file's
    placement record is not. Check the reported ride height against the CAD for other sources."""
    t = path.read_text(errors="replace")
    ent = dict(re.findall(r"#(\d+)=(.*?);\s*\n", t, re.S))
    scale = 0.001 if re.search(r"SI_UNIT\(\.MILLI\.,\.METRE\.\)", t) else 1.0
    ids = set(re.findall(r"VERTEX_POINT\('[^']*',#(\d+)\)", t))
    v = np.array([[float(c) for c in re.search(r"\(([^()]*)\)\)", ent[i]).group(1).split(",")] for i in ids])
    return v * scale


def box_frame(vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(R, p0) for a box: rows of R are its flow (+X-ish), side and up (+Z-ish) axes; p0 a point on its floor."""
    v = np.unique(np.round(vertices, 9), axis=0)
    if len(v) != 8:
        raise ValueError(f"expected a box with 8 corners, found {len(v)} distinct vertices")
    c = v[0]
    import itertools
    d = None                                     # the 3 box edges from one corner are mutually perpendicular
    for trio in itertools.combinations(v[1:] - c, 3):
        u = [e / np.linalg.norm(e) for e in trio]
        if max(abs(u[0] @ u[1]), abs(u[0] @ u[2]), abs(u[1] @ u[2])) < 1e-6:
            d = u
            break
    if d is None:
        raise ValueError("the 8 vertices do not form a rectangular box")
    axes = d
    up = max(axes, key=lambda a: abs(a[2]))
    up = up if up[2] > 0 else -up
    flow = max((a for a in axes if a is not up and not np.allclose(abs(a), abs(up))), key=lambda a: abs(a[0]))
    flow = flow if flow[0] > 0 else -flow
    side = np.cross(up, flow)
    R = np.vstack([flow, side, up])
    p0 = v[np.argmin(v @ up)]
    return R, p0


def place_in_tunnel(tris: np.ndarray, R: np.ndarray, p0: np.ndarray) -> np.ndarray:
    """Rotate/translate into the tunnel frame: ground Z = 0, flow +X, leading point at X = 0; Y kept as is."""
    w = (tris - p0) @ R.T
    w[..., 0] -= w[..., 0].min()
    w[..., 1] += p0 @ R[1]                     # keep the car centreline (Y = 0) where it was
    return w


def load_solids(path: Path):
    """All solids in a STEP file as CadQuery objects (CadQuery is imported here so the module loads without it)."""
    import cadquery as cq
    wp = cq.importers.importStep(str(path))
    return [s for o in wp.vals() for s in (o.Solids() if hasattr(o, "Solids") else [o])]


def fuse(solids, tol: float):
    """Fuse all solids into one shape. Returns (shape, method)."""
    first, rest = solids[0], solids[1:]
    if not rest:
        return first, "single solid"
    try:
        out = first.fuse(*rest, tol=tol)
        if out.isValid():
            return out, f"all at once (fuzzy {tol:g} mm)"
    except Exception:                                            # noqa: BLE001 - OCC raises various errors
        pass
    out = first                                                  # fall back: one part at a time
    for s in rest:
        out = out.fuse(s, tol=tol)
    return out, f"one by one (fuzzy {tol:g} mm)"


def fix_shape(path: Path, out: Path, min_volume_frac: float = 1e-6, tols=(0.01, 0.05, 0.2),
              scale: float | None = None, stl_tol_rel: float = 1e-4, ground_box: Path | None = None,
              angular: float = 0.1) -> dict:
    """Fuse the solids of a STEP assembly into one clean solid and write it as an STL in metres.

    path, out: input STEP and output STL (a JSON report is written next to it by main).
    min_volume_frac: solids smaller than this fraction of the largest are dropped as debris (bolts, slivers).
    tols: fuzzy-fuse tolerances to try in turn, in mm; stops at the first that gives one valid solid.
    scale: factor from file units to metres (default 0.001, because OpenCascade reads STEP in mm).
    stl_tol_rel, angular: STL chord tolerance (relative to the part size) and angular tolerance in radians.
    ground_box: optional CFD-box STEP from the same assembly; puts the part in the tunnel frame (ground Z = 0,
        flow +X) and records the rake and lowest point.

    Returns the report dictionary; status is PASS, WARN (problems listed) or ERROR (no fuse succeeded).
    """
    solids = load_solids(path)
    vols = [s.Volume() for s in solids]
    total = sum(vols)
    keep = [s for s, v in zip(solids, vols) if v > min_volume_frac * max(vols)]
    rep = {"tool": "fix_geometry.py", "version": __version__, "input": str(path),
           "solids_in": len(solids), "solids_dropped_tiny": len(solids) - len(keep),
           "sum_of_part_volumes_mm3": total, "attempts": []}
    best = None
    for tol in tols:
        try:
            shape, method = fuse(keep, tol)
            shape = shape.clean().fix()
            n = len(shape.Solids())
            rep["attempts"].append({"fuzzy_mm": tol, "method": method, "solids": n, "valid": shape.isValid()})
            best = (shape, method, tol)
            if n == 1 and shape.isValid():
                break
        except Exception as exc:                                 # noqa: BLE001
            rep["attempts"].append({"fuzzy_mm": tol, "error": str(exc)[:200]})
    if best is None:
        rep["status"] = "ERROR"
        return rep
    shape, method, tol = best
    bb = shape.BoundingBox()
    size = max(bb.xlen, bb.ylen, bb.zlen)
    if scale is None:                                            # OpenCascade reads STEP in mm
        scale = 0.001
    vol = shape.Volume()
    problems = []
    if len(shape.Solids()) != 1:
        problems.append(f"{len(shape.Solids())} separate solids after fusing (parts that do not touch?)")
    if not shape.isValid():
        problems.append("OpenCascade reports the fused shape as invalid")
    if vol > total * 1.001:
        problems.append(f"fused volume {vol:.1f} mm3 is larger than the sum of the parts {total:.1f}")
    rep.update({"method": method, "fuzzy_mm": tol, "solids_out": len(shape.Solids()), "volume_mm3": vol,
                "overlap_removed_mm3": total - vol, "size_mm": [bb.xlen, bb.ylen, bb.zlen],
                "scale_to_m": scale, "problems": problems, "status": "PASS" if not problems else "WARN"})
    out.parent.mkdir(parents=True, exist_ok=True)
    shape.exportStl(str(out), tolerance=stl_tol_rel * size, angularTolerance=angular)     # written in mm ...
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import inspect_geometry as ig
    tris = ig.read_stl(out) * scale                                                    # ... then scaled to m
    if ground_box:
        R, p0 = box_frame(read_step_vertices(ground_box))
        tris = place_in_tunnel(tris, R, p0)
        rep["ground_box"] = {"file": str(ground_box), "rake_deg": float(np.degrees(np.arctan2(-R[0, 2], R[0, 0]))),
                             "lowest_point_above_ground_m": float(tris[..., 2].min()),
                             "size_m": (tris.reshape(-1, 3).max(0) - tris.reshape(-1, 3).min(0)).tolist()}
        if tris[..., 2].min() <= 0:
            rep["problems"].append("geometry goes below the ground plane")
            rep["status"] = "WARN"
    ig.write_stl_binary(out, tris)
    rep["output"] = str(out)
    return rep


def main(argv=None) -> int:
    """Command-line entry point: fix one STEP file, print the attempts and result. Returns 0, 1 or 2 (see module doc).
    """
    ap = argparse.ArgumentParser(description="Fuse a multi-part STEP assembly into one clean solid (STL, metres).")
    ap.add_argument("step", type=Path)
    ap.add_argument("--out", type=Path, help="output STL (default: <input>_fixed.stl)")
    ap.add_argument("--fuzzy", type=float, nargs="+", default=[0.01, 0.05, 0.2],
                    help="fuzzy tolerances to try, mm (default 0.01 0.05 0.2)")
    ap.add_argument("--scale", type=float, help="factor to metres (default 0.001: STEP read in mm)")
    ap.add_argument("--stl-tol-rel", type=float, default=1e-4,
                    help="STL chord tolerance relative to the part size (default 1e-4: 0.18 mm on a 1.8 m wing)")
    ap.add_argument("--angular", type=float, default=0.1, help="STL angular tolerance, rad (default 0.1)")
    ap.add_argument("--ground-box", type=Path,
                    help="CFD box STEP from the same assembly: its floor becomes the ground, its long axis the flow")
    a = ap.parse_args(argv)
    out = a.out or a.step.with_name(a.step.stem.replace(" ", "_") + "_fixed.stl")
    try:
        rep = fix_shape(a.step, out, tols=tuple(a.fuzzy), scale=a.scale, ground_box=a.ground_box,
                        stl_tol_rel=a.stl_tol_rel, angular=a.angular)
    except ImportError:
        print("ERROR: CadQuery is not installed.", file=sys.stderr)
        return 2
    out.with_suffix(".json").write_text(json.dumps(rep, indent=2))
    print(f"{rep['input']}: {rep['solids_in']} solids in ({rep['solids_dropped_tiny']} tiny dropped)")
    for at in rep["attempts"]:
        print(f"  fuzzy {at['fuzzy_mm']} mm: " + (f"{at['solids']} solid(s), valid {at['valid']} [{at['method']}]"
                                                  if "solids" in at else f"failed: {at['error']}"))
    if rep["status"] == "ERROR":
        print("ERROR: no fuse succeeded", file=sys.stderr)
        return 2
    print(f"Result: {rep['solids_out']} solid, volume {rep['volume_mm3'] / 1e3:.1f} cm3 "
          f"(parts summed {rep['sum_of_part_volumes_mm3'] / 1e3:.1f}; overlap removed "
          f"{rep['overlap_removed_mm3'] / 1e3:.1f}), size {[round(s) for s in rep['size_mm']]} mm")
    if "ground_box" in rep:
        g = rep["ground_box"]
        print(f"Placed on the ground from {Path(g['file']).name}: rake {g['rake_deg']:.3f} deg, lowest point "
              f"{g['lowest_point_above_ground_m'] * 1000:.1f} mm above the ground, size {[round(s, 3) for s in g['size_m']]} m")
    print(f"Written {out} (metres) and {out.with_suffix('.json').name}")
    if rep["problems"]:
        print("WARN: " + "; ".join(rep["problems"]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
