# cfd-pipeline

An automated OpenFOAM job pipeline for external aerodynamics (built for a Formula Student team).
Give it a geometry and a job file; it checks the geometry, builds the case, meshes, solves, checks the
result and writes a summary, stopping at the first thing that looks wrong.

**Status: working research tool, not a finished product.** It has been run on a standard Ahmed body, a
single-element wing in ground effect and a real F1 front wing, but only on those. "Runs reliably on any CAD file"
is the aim, not something demonstrated yet; see [What it cannot do](#what-it-cannot-do-yet).

## What a run does

```
geometry (STL/STEP)
  1. inspect     closed surface? units? exports a clean STL in metres
  2. case        builds an OpenFOAM v14 case (half model, ground, refinement boxes, force coefficients)
  3. mesh        snappyHexMesh in parallel
  4. mesh gates  stop if the mesh leaked, failed checkMesh or has too many bad faces
  5. solve       runs in blocks; stops as soon as the mean forces settle, never before min / after max iterations
  6. finish      results.json + summary.md (forces, gate results, stage timings)
```

The checks are the point. A pipeline that always produces a number is dangerous, so each stage can stop the job:

| Gate | What it catches |
|---|---|
| Geometry inspection | open or non-manifold surfaces, flipped faces, unit mistakes, touching solids |
| `FLUID_VOLUME` | air left inside the body (a leaked mesh that otherwise "runs fine") |
| `MESH_QUALITY` | checkMesh failures; isolated skewed faces are a warning, not a failure |
| `CONVERGED` | forces still drifting at the end of the run; reports mean and swing |
| Half-model symmetry | a body edited on one side only (refused above 5% surface-area difference) |

## Quick start

Developed on Ubuntu (WSL2), OpenFOAM Foundation v14, Python 3.12+.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # numpy, pytest, matplotlib
pip install cadquery                      # only needed to read STEP files (large install)
python -m pytest -q                       # tests that need CadQuery or OpenFOAM skip themselves
```

Run one job (needs OpenFOAM sourced, `$FOAM_RUN` and `$FOAM_TUTORIALS` set):

```bash
python src/run_job.py job_specs/ahmed1_quick.json --check-only   # validate the job file and environment
python src/run_job.py job_specs/ahmed1_quick.json                # a few minutes: smoke test
```

Run several one after another (a failed job does not stop the batch):

```bash
python src/run_batch.py job_specs/ahmed25_standard.json job_specs/ahmed35_standard.json
```

Each job gets its own folder in `$FOAM_RUN/jobs/<name>_<timestamp>/` with `job.json`, `inspection.json`,
`case/`, `results.json`, `status.json` (stage and timings) and `summary.md`. Exit codes: 0 finished (PASS or WARN),
1 FAIL or stopped by a gate, 2 error.

A job file is JSON: geometry path and units, a preset (`quick`, `standard`, `fine`), optional case overrides
(speed, ground, reference area, processors, turbulence model, mesh levels, half model), solve limits, and an
optional reference CSV. See `job_specs/` for examples, including a ride-height sweep and design-variant tests.

## Tools

| Script | Purpose |
|---|---|
| `src/run_job.py`, `src/run_batch.py` | one job / many jobs, end to end |
| `src/inspect_geometry.py` | pre-flight geometry checker (writes a JSON report with the input's SHA-256) |
| `src/fix_geometry.py` | turns a multi-part STEP assembly into one closed solid, placed on the ground from a CFD box |
| `src/make_case.py`, `src/check_case.py` | case generator and quality gates |
| `src/grid_study.py` | grid-convergence study (Celik et al. 2008: observed order, Richardson extrapolation, GCI) |
| `src/prepare_sweep.py`, `src/analyse_sweep.py` | design sweep against the AhmedML dataset and a ranking analysis |
| `src/compare_lienhart.py`, `src/compare_zerihan.py` | comparison with wind-tunnel data |
| `src/make_ahmed.py`, `src/make_tyrrell_wing.py` | build the validation geometries from published dimensions |
| `src/render_flow.py` | publication-style pictures and an orbit video from a finished case (ParaView `pvbatch`) |

## Validation, and what it does and does not support

- **Single-element wing in ground effect (Zerihan 2001, Southampton wind tunnel, moving ground).** Spalart-Allmaras
  downforce was within 8% of the measurement at every one of seven ride heights (mean 5.6%) and reproduced the
  peak and the fall-off near the ground. k-omega SST lost up to 23% of the downforce close to the ground, so SA is
  the pipeline's default for wings. Drag was 14-32% high far from the ground.
- **Standard Ahmed body.** Drag was 5-10% high at 25 degrees. On the AhmedML dataset the pipeline ranked designs
  correctly while they stayed in the same flow regime, but it mispredicted a shape in a different regime (steady
  RANS finds the wrong flow), so rankings across a regime change are not trustworthy.
- **Real F1 front wing (PERRINN 2017, wing alone).** Mesh-independent to about 1%; a ride-height sweep and a
  turbulence-model check are consistent, and removing wing elements changes the forces in the expected order.
  There is no measured data for this wing, so these results show the pipeline behaves consistently, not that the
  absolute numbers are right.

### What it cannot do yet

- It has not been shown to work on arbitrary CAD. Real assemblies fail in ways the checks catch (leaks, overlapping
  solids), but only on the three geometries above.
- Steady RANS only. Near the ground at about 45 mm the flow was unsteady and the result is a mean with a swing.
- The geometry is not included (see below), and the OpenFOAM-dependent tests are not run in CI yet.

## Tests

```bash
python -m pytest -q
```

The core tests (about 70) use synthetic geometry and synthetic logs and need only numpy. Tests that need CadQuery
or the OpenFOAM tutorials skip themselves when those are missing, so a result of "passed, N skipped" is normal
outside the development machine. CI runs the core tests on every push (see `.github/workflows/ci.yml`).

## Data and licences

No geometry or third-party data is committed (`test_data/` is ignored). Sources used:

| Data | Source |
|---|---|
| AhmedML runs | Ashton et al. 2024, huggingface.co/datasets/neashton/ahmedml (CC BY-SA 4.0) |
| Standard Ahmed body | built from Ahmed et al. (1984) dimensions by `make_ahmed.py` |
| Lienhart & Becker velocities | ERCOFTAC Knowledge Base case AC1-05 |
| Single-element wing | Zerihan (2001), PhD thesis, University of Southampton; geometry built from its tables |
| F1 front wing | PERRINN 2017 CAD (obtained separately; not redistributed here) |

Regenerable test geometry: `python tests/geometry_fixtures.py test_data/generated`.

This repository does not yet carry a licence; all rights are reserved until one is added.
