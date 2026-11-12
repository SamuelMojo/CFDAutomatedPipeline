"""Tests for the pure helpers in src/render_flow.py (no ParaView needed; the scenes themselves run under pvbatch)."""
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import render_flow as rf  # noqa: E402

PARAVIEW_NAMES = ["internalMesh", "patch/body", "patch/inlet", "patch/outlet", "patch/lowerWall", "patch/upperWall",
                  "patch/frontAndBack", "patch/centrePlane", "group/bodyGroup", "group/wall"]


def test_pick_regions_modern_names():
    """The mesh, body and ground are found from ParaView's 'patch/...' region names."""
    r = rf.pick_regions(PARAVIEW_NAMES)
    assert r == {"mesh": "internalMesh", "body": ["patch/body"], "ground": "patch/lowerWall"}


def test_pick_regions_plain_names_and_several_body_patches():
    """Plain names work, and every patch that is not part of the tunnel box counts as body."""
    r = rf.pick_regions(["internalMesh", "inlet", "outlet", "lowerWall", "main_plane", "flap_1", "centrePlane"])
    assert r["mesh"] == "internalMesh" and r["body"] == ["main_plane", "flap_1"] and r["ground"] == "lowerWall"


def test_pick_regions_nothing_found():
    """With only an internal mesh there is no body and no ground."""
    r = rf.pick_regions(["internalMesh"])
    assert r["body"] == [] and r["ground"] is None


def test_camera_front_side_and_above():
    """Azimuth 0 puts the camera upstream, 90 degrees to the side, elevation 90 degrees above, negative elevation below.
    """
    f = [0.4, 0.0, 0.17]
    assert rf.camera_position(f, 2.0, 0, 0) == pytest.approx([-1.6, 0.0, 0.17])        # upstream of the wing
    side = rf.camera_position(f, 2.0, 90, 0)
    assert side == pytest.approx([0.4, -2.0, 0.17], abs=1e-9)
    top = rf.camera_position(f, 2.0, 0, 90)
    assert top[2] == pytest.approx(2.17) and top[0] == pytest.approx(0.4, abs=1e-9)
    below = rf.camera_position(f, 2.0, 0, -30)
    assert below[2] < f[2]
    d = math.dist(rf.camera_position(f, 3.0, 123, 25), f)
    assert d == pytest.approx(3.0)                                                      # always on the sphere


def test_seed_levels_span_road_to_above_the_wing():
    """Seed heights run from 25 mm above the road to 0.12 m above the wing, in increasing order."""
    z = rf.seed_levels(0.29, 8)
    assert len(z) == 8 and z[0] == pytest.approx(0.025) and z[-1] == pytest.approx(0.41)
    assert z == sorted(z)
    assert rf.seed_levels(0.29, 1) == [0.025]


def test_quick_mode_and_stray_double_dash():
    """--quick shrinks the output, a stray '--' from pvbatch is ignored, and the defaults are full size."""
    a = rf.parse(["--", "--case", "x.foam", "--quick"])
    assert a.width == 960 and a.frames == 48 and a.density == 24
    b = rf.parse(["--case", "x.foam", "--scenes", "hero,side", "--caption", "hi"])
    assert b.scenes == "hero,side" and b.caption == "hi" and b.width == 2400


def test_refuses_to_run_without_paraview(capsys):
    """Without the paraview module main() exits with status 2 instead of crashing."""
    if rf.pv is not None:
        pytest.skip("paraview is available")
    assert rf.main(["--case", "x.foam"]) == 2
