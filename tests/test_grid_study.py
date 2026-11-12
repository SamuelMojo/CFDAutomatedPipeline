"""Tests for src/grid_study.py with synthetic results (exact power-law data)."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import grid_study as gs  # noqa: E402


def phi(n, exact=0.20, c=2.0, p=2.0):
    """A quantity that converges as exact + c * h^p for a mesh with n cells (h = n^(-1/3))."""
    h = (1.0 / n) ** (1 / 3)
    return exact + c * h ** p


def fake_case(tmp, name, cells, cd, cl):
    """A case folder with just the mesh log and results.json that grid_study reads."""
    c = tmp / name
    c.mkdir()
    (c / "log.snappyHexMesh").write_text(f"Snapped mesh : cells:{cells - 5}\nLayer mesh : cells:{cells}  faces:1\n")
    (c / "results.json").write_text(json.dumps({"status": "PASS", "gates": [{"code": "CONVERGED", "level": "PASS"}],
                                                "final": {"Cd_mean": cd, "Cl_mean": cl}}))
    return c


def test_recovers_exact_power_law_unequal_ratios(tmp_path):
    """On exact power-law data with unequal refinement ratios the order and the extrapolated value are recovered."""
    ns = (2_283_000, 4_516_000, 8_621_000)
    cases = [fake_case(tmp_path, f"v{i}", n, phi(n), phi(n, exact=-0.1, c=-1.0, p=1.5)) for i, n in enumerate(ns)]
    r = gs.study(cases)
    cd, cl = r["quantities"]["Cd"], r["quantities"]["Cl"]
    assert cd["behaviour"] == "monotonic convergence"
    assert cd["p"] == pytest.approx(2.0, rel=1e-6) and cd["extrapolated"] == pytest.approx(0.20, rel=1e-6)
    assert cl["p"] == pytest.approx(1.5, rel=1e-6) and cl["extrapolated"] == pytest.approx(-0.1, rel=1e-6)
    assert cd["asymptotic_check"] == pytest.approx(1.0, rel=1e-3)   # exact only for equal ratios


def test_oscillation_and_order_errors(tmp_path):
    """Oscillating results are reported as such, an unchanged Cl as no change, and cases out of order are rejected."""
    cases = [fake_case(tmp_path, "a", 1_000_000, 0.25, -0.1), fake_case(tmp_path, "b", 2_000_000, 0.22, -0.1),
             fake_case(tmp_path, "c", 4_000_000, 0.23, -0.1)]
    r = gs.study(cases)
    assert r["quantities"]["Cd"]["behaviour"] == "oscillatory convergence"
    assert r["quantities"]["Cl"]["behaviour"] == "no change between meshes"
    with pytest.raises(ValueError):
        gs.study(list(reversed(cases)))


def test_diverging(tmp_path):
    """Results that move further apart on refinement are reported as divergence and give no extrapolated value."""
    cases = [fake_case(tmp_path, "a", 1_000_000, 0.250, -0.1), fake_case(tmp_path, "b", 2_000_000, 0.249, -0.11),
             fake_case(tmp_path, "c", 4_000_000, 0.240, -0.13)]
    r = gs.study(cases)
    assert r["quantities"]["Cd"]["behaviour"].startswith("divergence")
    assert r["quantities"]["Cd"]["extrapolated"] is None
