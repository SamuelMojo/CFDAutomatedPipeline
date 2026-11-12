#!/usr/bin/env python3
"""Export every solid of a STEP file as its own binary STL, placed for FluidX3D.

    python src/step_to_fluidx3d.py car.step --out-dir ".../Fluidx3D/stl/perrinn_parts"

Why one STL per solid: FluidX3D decides inside/outside by counting ray crossings, so parts that overlap
inside ONE STL cancel each other out. Voxelizing each part separately and taking the union avoids that,
and avoids fusing (which threw away ~98% of the PERRINN car's volume).

Output frame, in metres: FluidX3D blows air along +y with z up, so the car's length runs along y with the
nose at y=0, the centreline at x=0 and the lowest point (tyre contact) at z=0.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inspect_geometry import read_stl, write_stl_binary  # noqa: E402


def main(argv=None) -> int:
    """Command-line entry point: export each STEP solid as its own STL in FluidX3D's frame. Returns 0, or 2 on failure.
    """
    ap = argparse.ArgumentParser(description="STEP assembly -> one STL per solid, in FluidX3D's frame (metres).")
    ap.add_argument("step", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True, help="folder for part_0.stl, part_1.stl, ...")
    ap.add_argument("--scale", type=float, default=0.001, help="file units to metres (CadQuery reads STEP in mm)")
    ap.add_argument("--nose", choices=["-x", "+x"], default="-x", help="CAD direction the nose points (default -x)")
    ap.add_argument("--tol", type=float, default=0.5, help="tessellation tolerance in mm (default 0.5)")
    ap.add_argument("--angular", type=float, default=0.2, help="angular tolerance in rad (default 0.2)")
    a = ap.parse_args(argv)

    import cadquery as cq
    wp = cq.importers.importStep(str(a.step))
    solids = [s for o in wp.vals() for s in (o.Solids() if hasattr(o, "Solids") else [o])]
    print(f"{a.step.name}: {len(solids)} solids")

    pieces = []
    with tempfile.TemporaryDirectory() as tmp:
        for k, s in enumerate(solids):
            f = Path(tmp) / f"s{k}.stl"
            try:
                s.exportStl(str(f), tolerance=a.tol, angularTolerance=a.angular)
                tris = read_stl(f)
            except Exception as exc:
                print(f"  solid {k}: skipped ({exc})")
                continue
            if len(tris):
                pieces.append(tris * a.scale)
    if not pieces:
        print("No triangles produced.")
        return 2

    def to_fluidx3d(p):  # CAD length axis (x) -> FluidX3D y, nose towards low y
        """Map CAD coordinates to FluidX3D's frame: the CAD length axis (x) becomes y, nose towards low y."""
        x, y, z = p[..., 0], p[..., 1], p[..., 2]
        return np.stack([-y, x, z], -1) if a.nose == "-x" else np.stack([y, -x, z], -1)

    allv = to_fluidx3d(np.concatenate([p.reshape(-1, 3) for p in pieces]))
    lo, hi = allv.min(0), allv.max(0)
    shift = np.array([-(lo[0] + hi[0]) / 2, -lo[1], -lo[2]])

    a.out_dir.mkdir(parents=True, exist_ok=True)
    for old in a.out_dir.glob("part_*.stl"):
        old.unlink()
    for i, p in enumerate(pieces):
        write_stl_binary(a.out_dir / f"part_{i}.stl", to_fluidx3d(p) + shift)

    ext = hi - lo
    info = {"source": str(a.step), "parts": len(pieces), "solids_in_step": len(solids),
            "width_x_m": float(ext[0]), "length_y_m": float(ext[1]), "height_z_m": float(ext[2]),
            "frame": "FluidX3D: flow +y, z up; nose at y=0, centreline x=0, ground z=0"}
    (a.out_dir / "parts.json").write_text(json.dumps(info, indent=2))
    print(f"Wrote {len(pieces)} parts to {a.out_dir}")
    print(f"Car size: width {ext[0]:.3f} m, length {ext[1]:.3f} m, height {ext[2]:.3f} m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
