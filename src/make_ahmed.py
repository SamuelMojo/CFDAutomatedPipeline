#!/usr/bin/env python3
"""Generate the standard Ahmed body (Ahmed et al., SAE 840300, 1984) as an STL in metres.

Dimensions (mm): length 1044, width 389, height 288, slant length 222 (measured along the
slant), front edges rounded with radius 100, ground clearance 50. The slant angle is a
parameter (25 deg and 35 deg are the Lienhart/Becker wind-tunnel cases).
Stilts (the four legs that hold the model in the tunnel) are NOT modelled, as in most CFD
studies and in AhmedML.

Placed in the pipeline's convention: flow along +X, rear face at X = 0 (so the body spans
X = -1.044 .. 0), symmetric about Y = 0, underside at Z = 0.050 (floor at Z = 0).

Needs CadQuery:
    python src/make_ahmed.py --slant 25 --out test_data/ahmed_std/ahmed_25.stl
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

L, W, H = 1.044, 0.389, 0.288
SLANT_LENGTH = 0.222
FRONT_RADIUS = 0.100
CLEARANCE = 0.050


def slant_geometry(angle_deg: float) -> tuple[float, float]:
    """(horizontal length, vertical drop) of the slant, both in metres."""
    a = math.radians(angle_deg)
    return SLANT_LENGTH * math.cos(a), SLANT_LENGTH * math.sin(a)


def expected_volume(angle_deg: float) -> float:
    """Approximate solid volume (m^3): box minus slant wedge minus front-edge rounding.
    Edge rounding of radius r removes (1 - pi/4) r^2 per unit edge length; the overlap at the
    four front corners is ignored (it is about 0.1% of the volume)."""
    dx, dz = slant_geometry(angle_deg)
    wedge = 0.5 * dx * dz * W
    edges = (1 - math.pi / 4) * FRONT_RADIUS ** 2 * (2 * W + 2 * H)
    return L * W * H - wedge - edges


def build(angle_deg: float):
    """Build the Ahmed body for a rear slant angle (0 to 40 degrees) as a CadQuery solid in the pipeline frame.

    The body is made with the front at x = 0, then moved so the rear face is at x = 0 and the underside 50 mm up.
    Raises ValueError for an angle outside the published range and RuntimeError if the cutting wedge is misplaced.
    """
    import cadquery as cq
    if not 0 <= angle_deg <= 40:
        raise ValueError("Slant angle must be between 0 and 40 degrees (the published range).")
    dx, dz = slant_geometry(angle_deg)

    # Box with x 0..L (front at x = 0), y 0..W, z 0..H; round the four edges of the front face.
    body = (cq.Workplane("XY").box(L, W, H, centered=False)
            .faces("<X").edges().fillet(FRONT_RADIUS))

    if angle_deg > 0:
        # Triangular prism removed from the rear top: side profile in X-Z, extruded through the width.
        # The prism overshoots the roof and the rear face by 10 mm so the cut never leaves slivers;
        # its sloped side passes exactly through (L - dx, H) and (L, H - dz).
        m = 0.01
        z_end = H - dz - m * dz / dx
        wedge = (cq.Workplane("XZ")
                 .polyline([(L - dx, H), (L - dx, H + m), (L + m, H + m), (L + m, z_end)])
                 .close().extrude(1.0, both=True))
        bb = wedge.val().BoundingBox()
        if bb.zmin < 0:                      # the XZ workplane's local Y pointed down: mirror it
            wedge = wedge.mirror("XY")
            bb = wedge.val().BoundingBox()
        if not (abs(bb.zmax - (H + m)) < 1e-6 and abs(bb.zmin - z_end) < 1e-6 and bb.ymin < -0.5 and bb.ymax > W + 0.5):
            raise RuntimeError(f"Slant wedge ended up in the wrong place (z {bb.zmin:.4f}..{bb.zmax:.4f}).")
        body = body.cut(wedge)

    body = body.translate((-L, -W / 2, CLEARANCE))
    return body


def check(body, angle_deg: float) -> dict:
    """Verify the built shape: bounding box, rear-face height and volume."""
    s = body.val()
    bb = s.BoundingBox()
    dx, dz = slant_geometry(angle_deg)
    info = {"bbox_min": [bb.xmin, bb.ymin, bb.zmin], "bbox_max": [bb.xmax, bb.ymax, bb.zmax],
            "volume": s.Volume(), "expected_volume": expected_volume(angle_deg),
            "slant_dx": dx, "slant_dz": dz, "rear_face_height": H - dz}
    probs = []
    for got, want, name in ((bb.xmin, -L, "front x"), (bb.xmax, 0.0, "rear x"), (bb.ymin, -W / 2, "y min"),
                            (bb.ymax, W / 2, "y max"), (bb.zmin, CLEARANCE, "underside z"),
                            (bb.zmax, CLEARANCE + H, "roof z")):
        if abs(got - want) > 1e-4:
            probs.append(f"{name} {got:.4f} (expected {want:.4f})")
    if abs(info["volume"] / info["expected_volume"] - 1) > 0.01:
        probs.append(f"volume {info['volume']:.5f} vs expected {info['expected_volume']:.5f}")
    if not s.isValid():
        probs.append("OpenCascade reports the solid as invalid")
    info["problems"] = probs
    return info


def main(argv=None) -> int:
    """Command-line entry point: build, check and write the Ahmed body STL. Returns 0, 1 if the geometry check fails, 2
    without CadQuery.
    """
    ap = argparse.ArgumentParser(description="Generate the standard Ahmed body STL (metres).")
    ap.add_argument("--slant", type=float, required=True, help="rear slant angle in degrees (e.g. 25 or 35)")
    ap.add_argument("--out", type=Path, required=True)
    # Fine facets on the rounded nose: at 0.02 mm chord / 0.02 rad, each 90 deg arc of the 100 mm radius
    # gets about 80 facets (about 2 mm each), finer than the 3 mm surface cells of the standard mesh.
    ap.add_argument("--tolerance", type=float, default=2e-5, help="STL chord tolerance, m (default 0.02 mm)")
    ap.add_argument("--angular", type=float, default=0.02, help="STL angular tolerance, rad (default 0.02)")
    a = ap.parse_args(argv)
    try:
        body = build(a.slant)
    except ImportError:
        print("ERROR: CadQuery is not installed (pip install cadquery).", file=sys.stderr)
        return 2
    info = check(body, a.slant)
    print(f"Ahmed body, slant {a.slant:g} deg: slant {info['slant_dx'] * 1000:.1f} mm long x "
          f"{info['slant_dz'] * 1000:.1f} mm drop; rear face {info['rear_face_height'] * 1000:.1f} mm high")
    print(f"  bbox {[round(v, 4) for v in info['bbox_min']]} .. {[round(v, 4) for v in info['bbox_max']]} m")
    print(f"  volume {info['volume']:.5f} m^3 (expected about {info['expected_volume']:.5f})")
    if info["problems"]:
        print("ERROR: " + "; ".join(info["problems"]), file=sys.stderr)
        return 1
    a.out.parent.mkdir(parents=True, exist_ok=True)
    body.val().exportStl(str(a.out), tolerance=a.tolerance, angularTolerance=a.angular)
    print(f"Written {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
