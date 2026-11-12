"""prepare_sweep.py with a fake downloader (no network)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import prepare_sweep as ps  # noqa: E402


def test_choose_spreads_and_adds_close_pair():
    """The picks span the full drag range and include one pair about a single step apart."""
    cands = {i: 0.20 + 0.001 * i for i in range(1, 41)}
    picks = ps.choose(cands, 6)
    assert len(picks) == 6 and len(set(picks)) == 6
    cds = sorted(cands[p] for p in picks)
    assert cds[0] == min(cands.values()) and cds[-1] == max(cands.values())
    gaps = [b - a for a, b in zip(cds, cds[1:])]
    assert min(gaps) <= 0.0011                                   # the close pair


def test_main_writes_valid_job_files(tmp_path):
    """With a fake downloader, main() writes one valid job file per shape plus the STL and reference files."""
    def fake(url):
        """Fake downloader: a force CSV whose Cd depends on the run number, or a minimal STL."""
        run = int(url.split("run_")[1].split("/")[0])
        if url.endswith(".csv"):
            return f"cd, cl\n{0.2 + run / 1000:.6f}, -0.1\n".encode()
        return b"solid x\nendsolid x\n"
    data, jobs = tmp_path / "data", tmp_path / "job_specs" / "sweep"
    assert ps.main(["--n", "4", "--candidates", *map(str, range(1, 30, 3)),
                    "--data", str(data), "--jobs", str(jobs)], fetcher=fake) == 0
    files = sorted(jobs.glob("ahmedml_*.json"))
    assert len(files) == 4
    spec = json.loads(files[0].read_text())
    assert spec["preset"] == "standard" and spec["case"]["aref"] == 0.112
    run = spec["name"].split("_")[1]
    assert (data / f"run_{run}" / f"ahmed_{run}.stl").is_file()
    assert (data / f"run_{run}" / f"force_mom_{run}.csv").is_file()
