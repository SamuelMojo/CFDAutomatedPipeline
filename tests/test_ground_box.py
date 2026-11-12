"""fix_geometry.py --ground-box: a tilted CFD box defines the ground and flow direction (no CAD library needed)."""
import itertools
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import fix_geometry as fg  # noqa: E402


def tilted_box(rake_deg=0.5, floor_at_origin=-0.02, mm=False, path=None):
    """Corner points of a tilted box, optionally written as a STEP file.

    The box's floor is rake_deg below horizontal and passes floor_at_origin below the origin; mm=True writes the
    coordinates in millimetres. Returns the corners as an array.
    """
    a = np.radians(rake_deg)
    flow, up = np.array([np.cos(a), 0, -np.sin(a)]), np.array([np.sin(a), 0, np.cos(a)])
    side = np.array([0, 1.0, 0])
    p0 = up * floor_at_origin                     # the floor passes this far below the origin
    corners = [p0 + flow * x + side * y + up * z
               for x, y, z in itertools.product((-25, 50), (-15, 0), (0, 15))]
    if path:
        s = 0.001 if mm else 1.0
        lines = ["ISO-10303-21;", "DATA;", "#1=( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(" + (".MILLI." if mm else "$")
                 + ",.METRE.) );"]
        for i, c in enumerate(corners):
            lines.append(f"#{100 + i}=CARTESIAN_POINT('',({c[0] / s:.9f},{c[1] / s:.9f},{c[2] / s:.9f}));")
            lines.append(f"#{200 + i}=VERTEX_POINT('',#{100 + i});")
        path.write_text("\n".join(lines + ["ENDSEC;", "END-ISO-10303-21;"]) + "\n")
    return np.array(corners)


@pytest.mark.parametrize("mm", [False, True])
def test_box_floor_becomes_ground_and_long_axis_the_flow(tmp_path, mm):
    """The box gives the rotation and floor point: its long axis is the flow and its floor is Z = 0.

    Checked for coordinates in metres and in millimetres.
    """
    step = tmp_path / "box.step"
    tilted_box(path=step, mm=mm)
    R, p0 = fg.box_frame(fg.read_step_vertices(step))
    assert np.degrees(np.arctan2(-R[0, 2], R[0, 0])) == pytest.approx(0.5, abs=1e-6)
    assert (np.zeros(3) - p0) @ R[2] == pytest.approx(0.02, abs=1e-7)        # origin 20 mm above the floor
    # a point 0.1 m above the tilted floor, 1 m ahead of the origin, ends up at Z = 0.1
    a = np.radians(0.5)
    flow, up = np.array([np.cos(a), 0, -np.sin(a)]), np.array([np.sin(a), 0, np.cos(a)])
    pts = np.array([[up * -0.02 + flow * -1 + up * 0.1, up * -0.02 + flow * -0.5 + up * 0.3,
                     up * -0.02 + flow * 0 + up * 0.2]])
    w = fg.place_in_tunnel(pts, R, p0)
    assert w[0, :, 2] == pytest.approx([0.1, 0.3, 0.2], abs=1e-7)
    assert w[0, :, 0] == pytest.approx([0, 0.5, 1.0], abs=1e-7) and w[0, :, 1] == pytest.approx([0, 0, 0], abs=1e-7)


def test_not_a_box_is_refused():
    """Eight random points are not a box, so box_frame() raises ValueError."""
    with pytest.raises(ValueError):
        fg.box_frame(np.random.default_rng(1).random((8, 3)))
