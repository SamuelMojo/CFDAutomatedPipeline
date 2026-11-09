"""compare_lienhart.py: loaders, surface offsets, probes parsing and the analysis, with a fake
probes output (no OpenFOAM). Uses small synthetic .dat files in the ERCOFTAC format."""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import compare_lienhart as cl  # noqa: E402
import make_ahmed as ma  # noqa: E402

HDR = "#####\n# Origin of coordinate system: x=0: end of the car\n%Column1Name = x\n%End Of Metadata\n"


def write_vel(path, pts):
    rows = [f"{x:.3f}\t{y:.3f}\t{z:.3f}\t{u:.3f}\t0.0\t0.0" + "\t0.0" * 12 for x, y, z, u in pts]
    path.write_text(HDR + "\n".join(rows) + "\n")


def make_data(d):
    d.mkdir()
    dx, dz = ma.slant_geometry(25)
    roof = (ma.CLEARANCE + ma.H) * 1000
    # symmetry plane: a column above the slant and one in the wake
    focus = [(-100.0, 0.0, roof - (dx * 1000 - 100) * dz / dx + 5, -2.0),   # just above slant, reversed
             (-100.0, 0.0, 380.0, 41.0), (37.0, 0.0, 200.0, 5.0), (37.0, 0.0, 300.0, 30.0)]
    write_vel(d / "Ahmed_25_y=0_focus.dat", focus)
    write_vel(d / "Ahmed_25_x=200.dat", [(200.0, y, 200.0, 20.0 + y / 50) for y in (-100.0, 0.0, 100.0)])
    cp = [(-215.0, 0.0, 338.0, -0.5), (-100.0, 0.0, roof - (dx * 1000 - 100) * dz / dx, -0.6),
          (0.0, 0.0, 150.0, -0.25), (-50.0, -194.5, 200.0, -0.3), (-100.0, 0.0, 50.0, -0.15)]
    (d / "Ahmed_25_Cp.dat").write_text(HDR + "\n".join(f"{a}\t{b}\t{c}\t{e}" for a, b, c, e in cp)
                                       + "\n-21.0\t-194.5\t133.0\n")          # a short (incomplete) row
    return d


def test_loaders_convert_mm_and_skip_short_rows(tmp_path):
    d = make_data(tmp_path / "data")
    v = cl.load_velocity(d / "Ahmed_25_y=0_focus.dat")
    assert v["xyz"][1].tolist() == pytest.approx([-0.1, 0.0, 0.38])
    cp = cl.load_cp(d / "Ahmed_25_Cp.dat")
    assert len(cp["Cp"]) == 5


def test_surface_offsets_point_into_the_air(tmp_path):
    cp = cl.load_cp(make_data(tmp_path / "data") / "Ahmed_25_Cp.dat")
    moved, kinds = cl.surface_offset(cp["xyz"], 25)
    assert kinds == ["roof", "slant", "rear", "side", "underside"]
    d = moved - cp["xyz"]
    assert d[0, 2] > 0 and d[1, 0] > 0 and d[1, 2] > 0 and d[2, 0] > 0 and d[3, 1] < 0 and d[4, 2] < 0
    assert np.allclose(np.linalg.norm(d, axis=1), 0.001)


def fake_probes(case, pts, U, p, time="1500"):
    out = case / "postProcessing" / "lienhartProbes" / time
    out.mkdir(parents=True)
    head = "".join(f"# Probe {i} ({x} {y} {z})\n" for i, (x, y, z) in enumerate(pts)) + "#   Probe 0 1\n#  Time\n"
    (out / "U").write_text(head + "1500 " + " ".join(f"({a} {b} {c})" for a, b, c in U) + "\n")
    (out / "p").write_text(head + "1500 " + " ".join(f"{v}" for v in p) + "\n")


def test_end_to_end_with_fake_probes(tmp_path, monkeypatch):
    data = make_data(tmp_path / "data")
    case = tmp_path / "job" / "case"
    (case / "system").mkdir(parents=True)
    monkeypatch.setattr(cl, "VELOCITY_FILES", ["Ahmed_25_y=0_focus.dat", "Ahmed_25_x=200.dat"])
    prep = cl.prepare(case, data, 25)
    text = (case / "system" / "lienhartProbes").read_text()
    assert "type            probes;" in text and text.count("\n        (") == 4 + 3 + 5
    # simulated = measured + 2 m/s everywhere, except the slant point is attached (+5 m/s);
    # Cp simulated = measured - 0.1; one probe missing (outside the mesh)
    U = np.vstack([v["U"] for v in prep["vel"]]).copy()
    U[:, 0] += 2.0
    U[0, 0] = 5.0
    p = (prep["cp"]["Cp"] - 0.1) * 0.5 * 40 ** 2
    p = np.append(np.zeros(0), p)
    allU = np.vstack([U, np.zeros((5, 3))])
    allp = np.concatenate([np.zeros(len(U)), p])
    allU[-1] = [1e300, 1e300, 1e300]
    pts = np.vstack([v["xyz"] for v in prep["vel"]] + [prep["cp"]["xyz"]])
    fake_probes(case, pts, allU, allp)
    r = cl.analyse(case, tmp_path / "job" / "compare_lienhart", prep, 25)
    focus = r["velocity"][0]
    assert focus["found"] == 4 and focus["mean_error_over_Ub"] > 0
    sf = r["slant_flow"]
    assert len(sf["stations"]) == 1 and sf["measured_reversed"] == 1 and sf["simulated_reversed"] == 0
    assert sf["stations"][0]["height_above_slant_mm"] == pytest.approx(5.0, abs=0.1)
    assert r["cp"]["all"]["rms_error"] == pytest.approx(0.1, abs=1e-6)
    assert (tmp_path / "job" / "compare_lienhart" / "compare_lienhart.md").is_file()


def test_probe_count_mismatch_is_an_error(tmp_path):
    f = tmp_path / "U"
    f.write_text("# Probe 0 (0 0 0)\n# Probe 1 (1 0 0)\n1500 (1 2 3)\n")
    with pytest.raises(ValueError, match="2 probe locations but 1 values"):
        cl.parse_probes(f, vector=True)


def test_mean_fields_preferred(tmp_path):
    case = tmp_path / "case"
    (case / "system").mkdir(parents=True)
    (case / "0").mkdir()
    (case / "2000").mkdir()
    assert cl.field_names(case) == ("U", "p")
    (case / "2000" / "UMean").write_text("x")
    (case / "2000" / "pMean").write_text("x")
    assert cl.field_names(case) == ("UMean", "pMean")
    assert "fields          (UMean pMean);" in cl.probes_dict(np.zeros((1, 3)), ("UMean", "pMean"))
