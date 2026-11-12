#!/usr/bin/env python3
"""Rotate a cfd-pipeline STL (metres, flow +x, ground z=0) into FluidX3D's frame (flow +y, ground z=0).

    python comparison/fluidx3d/wing_to_fluidx3d.py test_data/perrinn/perrinn_front_wing.stl perrinn_front_wing_fx.stl

The ride height and rake are kept exactly: z is not touched. The span is centred on x=0 and the leading edge
is moved to y=0, so the FluidX3D setup only has to add an offset in lattice cells.
"""
import struct
import sys

import numpy as np

STL = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])


def main(src: str, dst: str) -> None:
    """Rotate the binary STL src into the FluidX3D frame and write it to dst, then print the new extent.

    Rotation by +90 degrees about z: (x, y, z) becomes (-y, x, z), so flow along +x becomes flow along +y. Normals are
    rotated the same way. Exits with a message if the file is an ASCII STL.
    """
    data = open(src, "rb").read()
    n = struct.unpack("<I", data[80:84])[0]
    if len(data) != 84 + 50 * n:
        sys.exit("Only binary STL is supported (export one with src/inspect_geometry.py --export-stl).")
    rec = np.frombuffer(data, STL, n, 84).copy()
    v = rec["v"].astype(np.float64)
    x, y, z = v[..., 0], v[..., 1], v[..., 2]
    w = np.stack([-y, x, z], -1)                       # flow +x -> +y, z unchanged
    w[..., 0] -= (w[..., 0].min() + w[..., 0].max()) / 2  # centre the span on x = 0
    w[..., 1] -= w[..., 1].min()                       # leading edge at y = 0
    rec["v"] = w.astype(np.float32)
    nx, ny, nz = rec["n"][:, 0].copy(), rec["n"][:, 1].copy(), rec["n"][:, 2].copy()
    rec["n"] = np.stack([-ny, nx, nz], -1)
    header = b"FluidX3D frame: flow +y, z up, ground z=0".ljust(80, b"\0")
    open(dst, "wb").write(header + struct.pack("<I", n) + rec.tobytes())
    lo, hi = w.reshape(-1, 3).min(0), w.reshape(-1, 3).max(0)
    print(f"{n} triangles; extent x {lo[0]:.3f}..{hi[0]:.3f}, y {lo[1]:.3f}..{hi[1]:.3f}, z {lo[2]:.4f}..{hi[2]:.4f} m")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
