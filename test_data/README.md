# Test data

Geometry files are NOT committed. List each source and its licence here instead.

| File | Source | Licence / terms | Notes |
|---|---|---|---|
| (generated) `generated/*.stl` | `python tests/geometry_fixtures.py test_data/generated` | Own work | Synthetic wing and cube, including deliberately broken variants |
| `2026_wing.step` | Unknown (SolidWorks 2024 assembly, 15 solids) | UNKNOWN: keep out of the public repo | Parked: touching solids; first run kept air inside the nose |
| `ahmed/ahmed_1.stl`, `ahmed/force_mom_1.csv` | AhmedML, run_1 (huggingface.co/datasets/neashton/ahmedml; Ashton et al. 2024, arXiv:2407.20801) | CC BY-SA 4.0: credit the authors; share derivatives under the same licence | Clean closed STL in metres, 0.05 m above the floor. Reference Cd 0.2385, Cl -0.0945 (Aref 0.112 m^2) from transient SA-DDES, about 21M cells |
| motorBike.obj.gz | OpenFOAM tutorials (`$FOAM_TUTORIALS/resources/geometry`) | Ships with OpenFOAM; geometry licence NOT CHECKED, so do not redistribute it | Has 49 open edges: a good test for `--allow-open` |
| `ahmed_std/ahmed_25.stl`, `ahmed_std/ahmed_35.stl` | Generated: `python src/make_ahmed.py --slant 25 --out test_data/ahmed_std/ahmed_25.stl` (same for 35). Dimensions from Ahmed et al. (1984), as listed in the COMSOL Ahmed-body model documentation: 1044 x 389 x 288 mm, slant length 222 mm, 50 mm clearance; front radius 100 mm (standard value, not re-checked against the original paper); no stilts | Own work from published dimensions | Regenerate rather than commit |
| `ahmed_std/ref_25_ahmed1984.csv` | Cd 0.285 for the 25 deg body, Ahmed et al. (1984) wind tunnel, 60 m/s, Re_L 4.29e6 (as tabulated in the COMSOL documentation). Our runs are at 40 m/s, Re_L 2.78e6 | Published value | Lift not included |
| `lienhart/*.dat` | Lienhart & Becker (2000/2003) LDA velocity data, ERCOFTAC Knowledge Base case AC1-05 (kbwiki.ercoftac.org/w/index.php/Test_Data_AC1-05) | ERCOFTAC KB licence: reuse with attribution | 25 deg: planes y=0 and x=-178..500 mm; rear-body Cp |
