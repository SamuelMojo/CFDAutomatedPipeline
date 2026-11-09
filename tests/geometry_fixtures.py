"""Synthetic test geometry for the inspector.

Run as a script to write a set of known-good and deliberately-broken STL files:

    python tests/geometry_fixtures.py test_data/generated

The wing-shaped solid is a NACA 0012-style section extruded along the span, so
it has realistic thin trailing edges and a known volume (area x span).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from inspect_geometry import write_stl_binary  # noqa: E402

_CUBE_V = np.array(
    [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)],
    dtype=float,
)
_CUBE_F = [
    (0, 2, 1), (0, 3, 2),  # bottom (-z)
    (4, 5, 6), (4, 6, 7),  # top (+z)
    (0, 1, 5), (0, 5, 4),  # front (-y)
    (3, 7, 6), (3, 6, 2),  # back (+y)
    (0, 4, 7), (0, 7, 3),  # left (-x)
    (1, 2, 6), (1, 6, 5),  # right (+x)
]


def cube(size: float = 1.0, origin=(0.0, 0.0, 0.0)) -> np.ndarray:
    """Closed, outward-facing cube as (12, 3, 3) triangles."""
    v = _CUBE_V * size + np.asarray(origin, dtype=float)
    return np.array([[v[a], v[b], v[c]] for a, b, c in _CUBE_F])


def signed_volume(tris: np.ndarray) -> float:
    return float(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum() / 6.0)


def wing(chord: float = 0.3, span: float = 1.0, thickness: float = 0.12, n: int = 40):
    """Closed wing-like solid. Returns (triangles, analytic_volume)."""
    beta = np.linspace(0.0, np.pi, n)
    x = 0.5 * (1.0 - np.cos(beta))
    yt = 5 * thickness * (
        0.2969 * np.sqrt(x) - 0.1260 * x - 0.3516 * x**2 + 0.2843 * x**3 - 0.1015 * x**4
    )
    upper = [(x[i], yt[i]) for i in range(n)]
    lower = [(x[i], -yt[i]) for i in range(n - 1, 0, -1)]
    poly = np.array(upper + lower) * chord  # (x, z) pairs
    k = len(poly)
    c = poly.mean(axis=0)
    tris = []
    for i in range(k):
        p, q = poly[i], poly[(i + 1) % k]
        a = (p[0], 0.0, p[1]); b = (q[0], 0.0, q[1])
        cc = (q[0], span, q[1]); d = (p[0], span, p[1])
        tris.append((a, b, cc)); tris.append((a, cc, d))             # side wall
        tris.append(((c[0], 0.0, c[1]), b, a))                       # cap at y = 0
        tris.append(((c[0], span, c[1]), d, cc))                     # cap at y = span
    tris = np.array(tris, dtype=float)
    if signed_volume(tris) < 0:
        tris = tris[:, ::-1, :]
    xs, zs = poly[:, 0], poly[:, 1]
    area = 0.5 * abs(np.dot(xs, np.roll(zs, -1)) - np.dot(zs, np.roll(xs, -1)))
    return tris, float(area * span)


def open_copy(tris: np.ndarray, drop: int = 2) -> np.ndarray:
    """Delete triangles to make holes."""
    return tris[drop:]


def inverted(tris: np.ndarray) -> np.ndarray:
    return tris[:, ::-1, :].copy()


def flip_one(tris: np.ndarray, index: int = 0) -> np.ndarray:
    out = tris.copy()
    out[index] = out[index][::-1]
    return out


def with_degenerate(tris: np.ndarray) -> np.ndarray:
    p = tris[0, 0]
    return np.concatenate([tris, np.array([[p, p, p]])])


def to_ascii_stl(tris: np.ndarray, name: str = "test") -> str:
    lines = [f"solid {name}"]
    for t in tris:
        lines += ["facet normal 0 0 0", "outer loop"]
        lines += [f"vertex {float(v[0])!r} {float(v[1])!r} {float(v[2])!r}" for v in t]
        lines += ["endloop", "endfacet"]
    lines.append(f"endsolid {name}")
    return "\n".join(lines) + "\n"


def write_all(outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    w, _ = wing()
    files = {
        "wing_good_m.stl": w,
        "wing_good_mm.stl": w * 1000.0,
        "wing_open.stl": open_copy(w),
        "wing_inside_out.stl": inverted(w),
        "wing_two_bodies.stl": np.concatenate([w, w + np.array([0.0, 2.0, 0.0])]),
        "wing_inconsistent.stl": flip_one(w, 5),
        "wing_degenerate.stl": with_degenerate(w),
        "cube_good.stl": cube(),
    }
    for name, tris in files.items():
        write_stl_binary(outdir / name, tris)
        print(f"wrote {outdir / name}")


if __name__ == "__main__":
    write_all(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test_data/generated"))
