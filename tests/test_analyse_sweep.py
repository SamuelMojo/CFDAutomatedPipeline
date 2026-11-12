"""analyse_sweep.py on synthetic batch results."""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import analyse_sweep as an  # noqa: E402


def batch(ours, refs):
    """A run_batch-style report: one finished job per (ours, reference) pair, plus one job stopped by a gate that must
    be ignored.
    """
    return {"jobs": [{"job": f"ahmedml_{i}", "outcome": "finished", "result": "WARN", "Cd": o, "Cd_vs_ref": o / r - 1}
                     for i, (o, r) in enumerate(zip(ours, refs))]
            + [{"job": "broken", "outcome": "stopped by gate", "Cd": None, "Cd_vs_ref": None}]}


def test_consistent_offset_still_ranks_perfectly():
    """A constant 12% under-prediction leaves the ranking perfect and the scatter after removing the offset at zero."""
    refs = np.array([0.22, 0.25, 0.28, 0.30, 0.33])
    rows = an.rows_from_batch(batch(refs * 0.88, refs))
    a = an.analyse(rows, 0.02)
    assert a["n"] == 5 and a["spearman"] == pytest.approx(1.0)
    assert a["pairs_correct"] == a["pairs_total"] == 10
    assert a["mean_offset"] == pytest.approx(-0.12) and a["scatter_after_offset"] < 1e-9


def test_close_pair_swapped_is_within_noise():
    """Swapping two shapes closer than the noise floor is one wrong pair, and it is not a resolvable one."""
    refs = np.array([0.22, 0.250, 0.252, 0.30])
    ours = np.array([0.20, 0.226, 0.224, 0.27])                  # the close pair swapped
    a = an.analyse(an.rows_from_batch(batch(ours, refs)), 0.02)
    wrong = [p for p in a["pairs"] if not p["correct_order"]]
    assert len(wrong) == 1 and not wrong[0]["resolvable"]
    assert a["resolvable_correct"] == a["resolvable_pairs"]


def test_cli_writes_reports(tmp_path):
    """The command line exits 0 and writes the .md and .json reports next to the batch file."""
    refs = np.array([0.22, 0.25, 0.28])
    f = tmp_path / "batch_x.json"
    f.write_text(json.dumps(batch(refs * 1.05, refs)))
    assert an.main([str(f)]) == 0
    assert (tmp_path / "batch_x_analysis.md").is_file() and (tmp_path / "batch_x_analysis.json").is_file()
