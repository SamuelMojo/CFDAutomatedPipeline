# Job perrinn_fw_sa_half

- Geometry: `/home/samue/cfd-pipeline/job_specs/perrinn/../../test_data/perrinn/perrinn_front_wing.stl` (m)
- Preset: standard; cells: 6,751,080
- Iterations: 500 (converged)
- **Result: WARN**

| | Mean (settled window) | Last iteration |
|---|---|---|
| Cd | 0.2121 | 0.2121 |
| Cl | -1.1338 | -1.1339 |

Quality gates:

- [WARN] MESH_FINISHED: snappyHexMesh finished with 338 illegal faces out of 21,329,608 (1.6e-05); criteria: faces with interpolation weights (0..1)  < 0.02 (338). A handful is usually harmless; checkMesh decides below.
- [PASS] MESH_QUALITY: checkMesh: Mesh OK.
- [PASS] FLUID_VOLUME: mesh volume 60.6171 m^3; domain 60.625 m^3; body 0.007897 m^3; 100% of the body volume is cut out of the mesh.
- [PASS] SOLVER_END: Solver reached End after 500 iterations.
- [PASS] CONVERGED: mean over the last 100 of 500 iterations moved by 0.03% (Cd) and 0.04% (Cl) vs the 100 before; oscillation peak-to-peak Cd 0.0001, Cl 0.0002.

Stage timings:

- inspect: 14.1 s (done)
- case: 12.7 s (done)
- mesh: 1361.9 s (done)
- mesh gates: 0.0 s (done)
- solve: 3819.8 s (done)
- finish: 24.8 s (done)

Notes:

Phase 5: PERRINN 2017 front wing, HALF model (symmetry plane at the car centreline), on-car position (rake 0.495 deg, lowest point 85.6 mm), moving ground, 50 m/s, Spalart-Allmaras. Surface cells 3.1 mm on flat areas, 0.8 mm on curved areas and edges (the full model at 1.6-0.8 mm passed 18.9 M cells before finishing refinement). Aref = 1 m^2 for the full wing: Cd/Cl are full-wing S.Cd / S.Cl in m^2. First attempt crashed while adding the wall layer (snappyHexMesh face merging, 'Multiple outside loops'); mergeFaces is now switched off.
