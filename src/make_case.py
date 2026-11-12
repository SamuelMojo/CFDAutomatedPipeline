#!/usr/bin/env python3
"""Generate an OpenFOAM v14 external-aero case for a body STL in metres.

Replaces make_wing_case.py (same approach, no longer wing-specific).

It does NOT write OpenFOAM dictionaries from scratch. It copies the motorBikeSteady
tutorial (known to run on this machine) and edits specific entries, failing loudly if
an expected piece of text is not found. It writes:
  job.json    inputs, parameters, reference values, tool versions, expected fluid volume
  Allrun.case meshing + checkMesh + solve, stopping at the first failed stage
Afterwards run check_case.py on the case to apply the automatic quality gates.

Conventions: flow along +X, ground plane at Z = 0, lift direction +Z (downforce shows
as NEGATIVE Cl), full model (no symmetry plane).

Example (Ahmed body, AhmedML conditions: Re_H = 7.68e5, fixed floor, Aref 0.112):
    python src/make_case.py test_data/ahmed/ahmed_1.stl --case $FOAM_RUN/ahmed1_v1 \
        --speed 40 --ground fixed --aref 0.112 --lref 1.044
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inspect_geometry as ig  # noqa: E402

__version__ = "0.5.0"
TUTORIAL_REL = "incompressibleFluid/motorBikeSteady"
HEADER_END = "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //"
FOOTER = "// " + "*" * 73 + " //\n"


class CaseError(Exception):
    """The case cannot be generated: bad inputs, or the template tutorial is not the expected version."""
    pass


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def frontal_area(tris: np.ndarray, res: float = 0.004) -> float:
    """Area of the silhouette seen along +X (projection onto the Y-Z plane), by rasterising."""
    y, z = tris[:, :, 1], tris[:, :, 2]
    ys, zs = (y - y.min()) / res, (z - z.min()) / res
    img = np.zeros((int(ys.max()) + 3, int(zs.max()) + 3), bool)
    for a, b in zip(ys, zs):
        i0, i1 = int(a.min()), int(a.max()) + 1
        j0, j1 = int(b.min()), int(b.max()) + 1
        d = (b[1] - b[2]) * (a[0] - a[2]) + (a[2] - a[1]) * (b[0] - b[2])
        if abs(d) < 1e-12:
            continue
        gi, gj = np.meshgrid(np.arange(i0, i1) + 0.5, np.arange(j0, j1) + 0.5, indexing="ij")
        l1 = ((b[1] - b[2]) * (gi - a[2]) + (a[2] - a[1]) * (gj - b[2])) / d
        l2 = ((b[2] - b[0]) * (gi - a[2]) + (a[0] - a[2]) * (gj - b[2])) / d
        img[i0:i1, j0:j1] |= (l1 >= 0) & (l2 >= 0) & (l1 + l2 <= 1)
    return float(img.sum() * res * res)


def surface_area(tris: np.ndarray) -> float:
    """Total area of the triangles (n, 3, 3), in the square of the STL's length unit."""
    return float(0.5 * np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1).sum())


# --------------------------------------------------------------------------- #
# Text editing helpers (fail loudly)
# --------------------------------------------------------------------------- #
def sub_once(text: str, old: str, new: str, what: str) -> str:
    """Replace old with new, requiring exactly one occurrence, so a changed template is noticed."""
    if text.count(old) != 1:
        raise CaseError(f"Expected exactly one '{old}' in {what}, found {text.count(old)}. "
                        "The tutorial layout is not what this script was written for.")
    return text.replace(old, new)


def sub_all(text: str, old: str, new: str, what: str) -> str:
    """Replace every occurrence of old; fail if there is none."""
    if old not in text:
        raise CaseError(f"'{old}' not found in {what}.")
    return text.replace(old, new)


def regex_once(text: str, pattern: str, new: str, what: str, flags=re.S | re.M) -> str:
    """Replace exactly one regex match with literal text new; fail on zero or several matches."""
    out, n = re.subn(pattern, lambda m: new, text, flags=flags)
    if n != 1:
        raise CaseError(f"Expected exactly one match of /{pattern}/ in {what}, found {n}.")
    return out


def foam_header(cls: str, location: str, obj: str) -> str:
    """FoamFile header for a new OpenFOAM dictionary file of the given class, location and object name."""
    return ("FoamFile\n{\n    format      ascii;\n"
            f"    class       {cls};\n    location    \"{location}\";\n    object      {obj};\n}}\n"
            f"{HEADER_END}\n\n")


def fmt(x: float) -> str:
    """Number with 6 significant figures, as written into OpenFOAM files."""
    return f"{x:.6g}"


def vec(v) -> str:
    """Vector as an OpenFOAM '(x y z)' string."""
    return "(" + " ".join(fmt(c) for c in v) + ")"


def split_xy(n: int) -> tuple[int, int]:
    """Split n processors into (along X, along Y) for hierarchical decomposition, X-heavy
    because the domain is longest in X. E.g. 6 -> (3, 2), 12 -> (4, 3), 16 -> (4, 4)."""
    if n < 1:
        raise CaseError("--processors must be at least 1.")
    best = (n, 1)
    for a in range(1, n + 1):
        if n % a == 0 and a >= n // a and a - n // a < best[0] - best[1]:
            best = (a, n // a)
    return best


def decompose_count(path: Path) -> int:
    """Number of processors (numberOfSubdomains) set in the tutorial's decomposeParDict."""
    m = re.search(r"numberOfSubdomains\s+(\d+);", path.read_text())
    if not m:
        raise CaseError("numberOfSubdomains not found in decomposeParDict.")
    return int(m.group(1))


# --------------------------------------------------------------------------- #
# Case generation
# --------------------------------------------------------------------------- #
DEFAULTS = dict(speed=20.0, nu=1.5e-5, ground="moving", base_cell=0.2, surface_level=(4, 5),
                feature_level=5, near_level=4, wake_level=3, end_time=1000, aref=None, lref=None,
                cofr=None, layers=0, layer_thickness=None, processors=None, upstream=3.0, downstream=6.0, half_width=2.0, height=2.0,
                turbulence_model="kOmegaSST", half_model=False,
                merge_layer_faces=None)
TURBULENCE_MODELS = ("kOmegaSST", "SpalartAllmaras")
NU_TILDA_RATIO = 3.0     # free-stream nuTilda / nu for SpalartAllmaras (fully turbulent inflow: 3-5)

NU_TILDA = """#include        "include/initialConditions"

dimensions      [turbulentViscosity];

internalField   uniform $turbulentNuTilda;

boundaryField
{
    //- Set patchGroups for constraint patches
    #includeEtc "caseDicts/setConstraintTypes"

    //- Define inlet conditions
    #include "include/fixedInlet"

    outlet
    {
        type            inletOutlet;
        inletValue      $internalField;
    }

    lowerWall
    {
        type            zeroGradient;
    }

    bodyGroup
    {
        type            zeroGradient;
    }

    #include "include/frontBackUpperPatches"
}

"""


def use_spalart_allmaras(case: Path, nu_tilda: float, edit) -> None:
    """Switch a k-omega SST tutorial copy to Spalart-Allmaras (one equation, for nuTilda).
    Wall treatment: nutUSpaldingWallFunction (valid across y+, unlike nutkWallFunction, which needs k);
    nuTilda zeroGradient at walls, as in OpenFOAM's SpalartAllmaras tutorials (e.g. airFoil2D)."""
    edit("constant/momentumTransport",
         lambda t, rel: regex_once(t, r"^(\s*model\s+)kOmegaSST;", r"    model               SpalartAllmaras;", rel))
    edit("0/nut", lambda t, rel: sub_all(t, "nutkWallFunction", "nutUSpaldingWallFunction", rel))
    edit("0/include/initialConditions",
         lambda t, rel: sub_once(t, "turbulentOmega", f"turbulentNuTilda     {fmt(nu_tilda)};\nturbulentOmega", rel))
    (case / "0/nuTilda").write_text(foam_header("volScalarField", "0", "nuTilda") + NU_TILDA + FOOTER)
    for f in ("k", "omega"):                                     # not used by SA; removed to avoid confusion
        (case / "0" / f).unlink()
    edit("system/fvSchemes", lambda t, rel: regex_once(
        t, r"^(\s*div\(phi,omega\)\s+[^;]+;)", "    div(phi,omega)  bounded Gauss upwind;\n"
        "    div(phi,nuTilda) bounded Gauss upwind;", rel))

    def solution(t, rel):
        """fvSolution edit: add the nuTilda solver block and its relaxation factor (SA replaces omega)."""
        t = regex_once(t, r"^    omega\n    \{", "    nuTilda\n    {\n        solver          smoothSolver;\n"
                       "        smoother        GaussSeidel;\n        tolerance       1e-8;\n        relTol          0.1;\n"
                       "        nSweeps         1;\n    }\n\n    omega\n    {", rel)
        return regex_once(t, r"^(\s*omega\s+0\.5;)", "        omega           0.5;\n        nuTilda         0.5;", rel)
    edit("system/fvSolution", solution)


def make_case(stl: Path, case: Path, tutorial: Path, allow_open: bool = False, **kw) -> dict:
    """allow_open: accept a surface with open or non-manifold edges (e.g. zero-width cracks left by CAD
    tessellation) after inspection has warned about them; the FLUID_VOLUME gate after meshing then decides
    whether the body was really cut out of the mesh."""
    o = {**DEFAULTS, **{k: v for k, v in kw.items() if v is not None}}
    stl, case, tutorial = Path(stl), Path(case), Path(tutorial)
    if not stl.is_file():
        raise CaseError(f"STL not found: {stl}")
    if case.exists():
        raise CaseError(f"Case folder already exists: {case}. Choose a new name; cases are never overwritten.")
    if not tutorial.is_dir():
        raise CaseError(f"Tutorial folder not found: {tutorial}. Is the OpenFOAM environment loaded "
                        "(echo $FOAM_TUTORIALS)?")
    if o["ground"] not in ("moving", "fixed"):
        raise CaseError("--ground must be 'moving' or 'fixed'.")
    if o["turbulence_model"] not in TURBULENCE_MODELS:
        raise CaseError(f"turbulence_model must be one of {', '.join(TURBULENCE_MODELS)}, not '{o['turbulence_model']}'.")

    tris = ig.read_stl(stl)
    m = ig.analyse_mesh(tris)
    m.pop("_internal")
    surface_warnings = []
    if m["open_edges"] or m["non_manifold_edges"] or m["inconsistent_edges"]:
        if not allow_open:
            raise CaseError("The STL is not a clean closed surface. Run inspect_geometry.py and fix it first "
                            "(or set allow_open if the defects are known to be harmless).")
        surface_warnings.append(f"{m['open_edges']} open, {m['non_manifold_edges']} non-manifold, "
                                f"{m['inconsistent_edges']} inconsistent edges accepted (allow_open)")
        if abs(m["volume_total"]) < 1e-9:
            raise CaseError("allow_open: the surface encloses no volume, so it cannot be meshed as a solid body.")
    lo, hi = np.array(m["bbox_min"]), np.array(m["bbox_max"])
    if hi[0] - lo[0] > 20 or hi[2] > 5 or lo[2] < -1e-6:
        raise CaseError(f"Geometry bounds {lo} .. {hi} do not look like a body in metres above Z = 0.")
    body_volume = abs(m["volume_total"])
    half = bool(o["half_model"])
    if half:
        # Symmetric body, centreline at Y = 0: mesh only Y <= 0 with a symmetryPlane at Y = 0 (half the cells).
        if abs(lo[1] + hi[1]) > 1e-3 * max(hi[1] - lo[1], 1e-9):
            raise CaseError(f"half_model needs a body symmetric about Y = 0; its Y range is {lo[1]:.4f} .. {hi[1]:.4f}.")
        # The bounding box can be symmetric while the shape is not (an element deleted on one side only), and the
        # half model would silently copy the kept side. Compare the surface area either side of Y = 0.
        area = 0.5 * np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1)
        a_neg, a_pos = float(area[tris[..., 1].mean(axis=1) < 0].sum()), float(area[tris[..., 1].mean(axis=1) > 0].sum())
        asym = abs(a_neg - a_pos) / max(a_neg + a_pos, 1e-12)
        if asym > 0.05:
            raise CaseError(f"half_model needs a body symmetric about Y = 0, but the surface area differs by "
                            f"{asym:.1%} between the two sides ({a_neg:.4f} vs {a_pos:.4f} m^2). Was a part "
                            f"removed from one side only?")
        if asym > 0.01:
            surface_warnings.append(f"half_model: surface area differs by {asym:.1%} between the two sides of Y = 0")
        body_volume /= 2
    length = float(hi[0] - lo[0])
    aref = float(o["aref"]) if o["aref"] else frontal_area(tris)
    lref = float(o["lref"]) if o["lref"] else length
    cofr = tuple(o["cofr"]) if o["cofr"] else (float(lo[0] + 0.5 * length), 0.0, 0.0)
    bc, speed = float(o["base_cell"]), float(o["speed"])

    # Domain, snapped to whole base cells so cells are cubes.
    nx = max(1, round((length + o["upstream"] + o["downstream"]) / bc))
    x0 = float(lo[0] - o["upstream"]); x1 = x0 + nx * bc
    if half:
        ny = max(1, round(o["half_width"] / bc)); y0 = -ny * bc; y1 = 0.0
    else:
        ny = max(2, round(2 * o["half_width"] / bc)); y0 = -ny * bc / 2; y1 = -y0
    nz = max(1, round(o["height"] / bc)); z0 = 0.0; z1 = nz * bc
    if x1 < hi[0] + 1.0 or (not half and y1 < hi[1] + 0.2) or y0 > lo[1] - 0.2 or z1 < hi[2] + 0.2:
        raise CaseError("Domain is too small for this geometry; increase --downstream/--half-width/--height.")
    domain_volume = (x1 - x0) * (y1 - y0) * (z1 - z0)
    # Point in the air, never on a cell face at any refinement level.
    s = bc / 0.2
    inside = (x0 + bc * 2 + 0.0731 * s, (-1 if half else 1) * 0.0273 * s, min(z1 - bc, hi[2] + 3 * bc) + 0.0317 * s)

    # Refinement boxes scaled with body length.
    near = (lo[0] - 0.15 * length, lo[1] - 0.1 * length, 0.0,
            hi[0] + 0.4 * length, hi[1] + 0.1 * length, hi[2] + 0.15 * length)
    wake = (lo[0] - 0.3 * length, lo[1] - 0.25 * length, 0.0,
            hi[0] + 1.5 * length, hi[1] + 0.25 * length, hi[2] + 0.4 * length)

    shutil.copytree(tutorial, case, ignore=shutil.ignore_patterns("log.*", "processor*", "geometry"))
    (case / "constant" / "geometry").mkdir(parents=True, exist_ok=True)
    # Half model: the FULL closed surface is used; the half-width domain does the cutting (the part beyond the
    # symmetry plane lies outside the mesh). Cutting the STL instead leaves an open edge exactly ON the symmetry
    # plane when the CAD has a centreline seam, and snappyHexMesh then leaks into the body (PERRINN wing:
    # FLUID_VOLUME 0%).
    shutil.copy2(stl, case / "constant" / "geometry" / "body.stl")

    def edit(rel: str, fn):
        """Apply fn(text, name) to one file of the new case and write the result back."""
        p = case / rel
        p.write_text(fn(p.read_text(), rel))

    def block_mesh(t, rel):
        """blockMeshDict edit: domain corners and cell counts; for a half model, the Y = 0 face becomes a symmetry
        plane.
        """
        v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        t = regex_once(t, r"^vertices\s*\(.*?^\);",
                       "vertices\n(\n" + "\n".join("    " + vec(p) for p in v) + "\n);", rel)
        t = regex_once(t, r"hex \(0 1 2 3 4 5 6 7\) \([0-9 ]+\)", f"hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz})", rel)
        if half:   # the Y = y1 = 0 face (3 7 6 2) becomes a symmetry plane; Y = y0 stays a slip side wall
            t = regex_once(t, r"\(3 7 6 2\)\s*\(1 5 4 0\)", "(1 5 4 0)", rel)
            # Patch NAME must not be "symmetry": setConstraintTypes matches that name and would give the fields
            # type symmetry, inconsistent with a symmetryPlane patch. "centrePlane" gets symmetryPlane via its type.
            t = regex_once(t, r"^\s*inlet\s*\{", "    centrePlane\n    {\n        type symmetryPlane;\n        faces\n"
                           "        (\n            (3 7 6 2)\n        );\n    }\n    inlet\n    {", rel)
        return t
    edit("system/blockMeshDict", block_mesh)

    def snappy(t, rel):
        """snappyHexMeshDict edit: the body and refinement boxes, refinement levels and the wall-layer settings."""
        t = regex_once(t, r"^geometry\s*\{.*?^\};", f"""geometry
{{
    body
    {{
        type triSurface;
        file "body.stl";
    }}
    nearBox
    {{
        type box;
        min {vec(near[:3])};
        max {vec(near[3:])};
    }}
    wakeBox
    {{
        type box;
        min {vec(wake[:3])};
        max {vec(wake[3:])};
    }}
}};""", rel)
        t = regex_once(t, r'file "motorBike.eMesh";\s*level 6;',
                       f'file "body.eMesh";\n            level {o["feature_level"]};', rel)
        t = regex_once(t, r"^    refinementSurfaces\s*\{.*?^    \}", f"""    refinementSurfaces
    {{
        body
        {{
            level ({o['surface_level'][0]} {o['surface_level'][1]});
            patchInfo
            {{
                type wall;
                inGroups (bodyGroup);
            }}
        }}
    }}""", rel)
        t = regex_once(t, r"^    refinementRegions\s*\{.*?^    \}", f"""    refinementRegions
    {{
        nearBox
        {{
            mode    inside;
            level   {o['near_level']};
        }}
        wakeBox
        {{
            mode    inside;
            level   {o['wake_level']};
        }}
    }}""", rel)
        t = regex_once(t, r"insidePoint\s*\([^)]*\);", f"insidePoint {vec(inside)};", rel)
        t = sub_once(t, "maxGlobalCells 2000000;", "maxGlobalCells 20000000;", rel)
        t = sub_once(t, '"(lowerWall|motorBike).*"', '"(lowerWall|body).*"', rel)
        t = regex_once(t, r"nSurfaceLayers\s+\d+;", f"nSurfaceLayers {max(1, int(o['layers']))};", rel)
        if o["layer_thickness"]:
            # Absolute layer thickness: the same first-cell height on every mesh, so y+ (and the wall-function
            # behaviour) does not change between meshes in a grid study. Equal-thickness layers (ratio 1.0).
            lt = float(o["layer_thickness"])
            t = sub_once(t, "relativeSizes true;", "relativeSizes false;", rel)
            t = regex_once(t, r"finalLayerThickness\s+[0-9.eE+-]+;", f"finalLayerThickness {fmt(lt)};", rel)
            t = regex_once(t, r"minThickness\s+[0-9.eE+-]+;", f"minThickness {fmt(0.4 * lt)};", rel)
            t = regex_once(t, r"expansionRatio\s+[0-9.eE+-]+;", "expansionRatio 1.0;", rel)
        if o["merge_layer_faces"] is not None:
            # OpenFOAM's face merging before layer addition can crash on complex CAD ("Multiple outside loops");
            # addLayersControls { mergeFaces false; } switches it off (keyword read in layerParameters.C, v14).
            t = regex_once(t, r"^addLayersControls\s*\{",
                           "addLayersControls\n{\n    mergeFaces      "
                           + ("true" if o["merge_layer_faces"] else "false") + ";", rel)
        t = regex_once(t, r"^addLayers\s+(true|false);",
                       f"addLayers       {'true' if o['layers'] else 'false'};", rel)
        return t
    edit("system/snappyHexMeshDict", snappy)

    edit("system/surfaceFeaturesDict",
         lambda t, rel: sub_once(t, 'surfaces ("motorBike.obj");', 'surfaces ("body.stl");', rel))
    edit("system/controlDict",
         lambda t, rel: regex_once(t, r"^endTime\s+\d+;", f"endTime         {o['end_time']};", rel))
    edit("constant/physicalProperties",
         lambda t, rel: regex_once(t, r"^nu\s+[^;]+;", f"nu      {fmt(o['nu'])};", rel))

    for f in ("U.orig", "p", "k", "omega", "nut"):
        edit(f"0/{f}", lambda t, rel: sub_all(t, "motorBikeGroup", "bodyGroup", rel))
    if o["ground"] == "fixed":
        edit("0/U.orig", lambda t, rel: regex_once(
            t, r"lowerWall\s*\{\s*type\s+fixedValue;\s*value\s+\$internalField;\s*\}",
            "lowerWall\n    {\n        type            noSlip;\n    }", rel))

    k = 0.24 * (speed / 20.0) ** 2      # tutorial values scaled with speed
    om = 1.78 * (speed / 20.0)

    def init_cond(t, rel):
        """initialConditions edit: free-stream velocity and the turbulence inflow values (k, omega)."""
        for name, val in (("flowVelocity", f"({fmt(speed)} 0 0)"), ("turbulentKE", fmt(k)),
                          ("turbulentOmega", fmt(om))):
            t = regex_once(t, rf"^{name}\s+[^;]+;", f"{name:<20} {val};", rel)
        return t
    edit("0/include/initialConditions", init_cond)
    nu_tilda = NU_TILDA_RATIO * float(o["nu"])
    if o["turbulence_model"] == "SpalartAllmaras":
        use_spalart_allmaras(case, nu_tilda, edit)

    (case / "system/forceCoeffs").write_text(f"""type            forceCoeffs;
libs            ("libforces.so");
writeControl    timeStep;
timeInterval    1;
log             yes;
patches         (bodyGroup);
rho             rhoInf;      // Indicates incompressible
rhoInf          1;           // Redundant for incompressible
liftDir         (0 0 1);     // downforce is NEGATIVE Cl
dragDir         (1 0 0);
CofR            {vec(cofr)};
pitchAxis       (0 1 0);
magUInf         {fmt(speed)};
lRef            {fmt(lref)};
Aref            {fmt(aref / 2 if half else aref)};   // half model: half the area, so coefficients are full-body values
""")
    (case / "system/functions").write_text(
        foam_header("dictionary", "system", "functions") + "#includeFunc forceCoeffs\n\n" + FOOTER)

    for junk in ("Allrun", "Allclean", "Allrun-parallel"):
        (case / junk).unlink(missing_ok=True)
    if o["processors"]:
        nproc = int(o["processors"])
        nx_, ny_ = split_xy(nproc)
        edit("system/decomposeParDict", lambda t, rel: regex_once(
            regex_once(t, r"numberOfSubdomains\s+\d+;", f"numberOfSubdomains  {nproc};", rel),
            r"n\s+\(\s*\d+\s+\d+\s+\d+\s*\);", f"n               ({nx_} {ny_} 1);", rel))
    # Rebalancing during meshing: the tutorial's ptscotch distributor can leave a processor with zero
    # cells ("Size of distribution:1 mesh nCells:0", seen on the standard Ahmed body with 14 cores).
    # The geometric hierarchical method always gives every processor cells.
    edit("system/decomposeParDict", lambda t, rel: regex_once(
        t, r"^distributor\s+\w+;", "distributor     hierarchical;", rel))
    nproc = decompose_count(case / "system/decomposeParDict")
    (case / "Allrun.case").write_text(f"""#!/bin/bash
# Generated by make_case.py. Same stages as the motorBikeSteady tutorial plus checkMesh.
cd "$(dirname "$0")" || exit 1
set -e -o pipefail
surfaceFeatures | tee log.surfaceFeatures
blockMesh | tee log.blockMesh
decomposePar -copyZero | tee log.decomposePar
mpirun -np {nproc} snappyHexMesh -parallel | tee log.snappyHexMesh
mpirun -np {nproc} checkMesh -parallel | tee log.checkMesh
mpirun -np {nproc} patchSummary -parallel | tee log.patchSummary
mpirun -np {nproc} potentialFoam -parallel | tee log.potentialFoam
mpirun -np {nproc} foamRun -parallel | tee log.foamRun
reconstructPar -latestTime | tee log.reconstructPar
touch {case.name}.foam
""")
    os.chmod(case / "Allrun.case", 0o755)

    job = {
        "tool": "make_case.py", "tool_version": __version__,
        "created_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "input_stl": {"path": str(stl), "sha256": ig.sha256_of(stl), "triangles": int(len(tris)),
                      "bbox_min": lo.tolist(), "bbox_max": hi.tolist(),
                      "volume_m3": body_volume, "surface_area_m2": surface_area(tris),
                      "surface_warnings": surface_warnings},
        "openfoam": {"version": os.environ.get("WM_PROJECT_VERSION", "unknown"),
                     "project": os.environ.get("WM_PROJECT", "unknown"), "template": str(tutorial)},
        "python": platform.python_version(), "numpy": np.__version__,
        "parameters": {
            "speed_m_s": speed, "nu_m2_s": float(o["nu"]),
            "reynolds_on_lref": speed * lref / float(o["nu"]),
            "ground": o["ground"],
            "symmetry": "half model: Y <= 0 meshed, symmetryPlane at Y = 0; coefficients are full-body values"
                        if half else "none (full model)",
            "base_cell_m": bc, "surface_level": list(o["surface_level"]), "feature_level": o["feature_level"],
            "near_level": o["near_level"], "wake_level": o["wake_level"], "layers": int(o["layers"]),
            "layer_thickness_m": o["layer_thickness"],
            "end_time_iterations": o["end_time"], "processors": nproc,
            "domain_min": [x0, y0, z0], "domain_max": [x1, y1, z1], "base_cells": [nx, ny, nz],
            "inside_point": list(inside), "near_box": list(near), "wake_box": list(wake),
            "Aref_m2": aref, "lRef_m": lref, "CofR": list(cofr),
            "turbulence_model": o["turbulence_model"],
            "turbulence_inlet": ({"k": k, "omega": om, "note": "tutorial values scaled with speed"}
                                 if o["turbulence_model"] == "kOmegaSST" else
                                 {"nuTilda": nu_tilda, "note": f"{NU_TILDA_RATIO:g} x nu (fully turbulent inflow)"}),
        },
        "expected": {"domain_volume_m3": domain_volume, "fluid_volume_m3": domain_volume - body_volume},
    }
    (case / "job.json").write_text(json.dumps(job, indent=2))
    return job


def main(argv=None) -> int:
    """Command-line entry point: generate a case from a metres STL and report the settings used. Returns 0, or 2 on
    error.
    """
    p = argparse.ArgumentParser(description="Generate an OpenFOAM v14 external-aero case from a metres STL.")
    p.add_argument("stl", type=Path, help="body STL in metres (from inspect_geometry.py --export-stl, or clean source)")
    p.add_argument("--case", type=Path, required=True, help="new case folder (must not exist)")
    p.add_argument("--tutorial", type=Path, default=Path(os.environ.get("FOAM_TUTORIALS", "")) / TUTORIAL_REL)
    p.add_argument("--speed", type=float, help=f"free-stream speed, m/s (default {DEFAULTS['speed']})")
    p.add_argument("--nu", type=float, help=f"kinematic viscosity, m^2/s (default {DEFAULTS['nu']})")
    p.add_argument("--ground", choices=["moving", "fixed"], help="ground moving with the flow, or a fixed floor")
    p.add_argument("--base-cell", type=float, help=f"background cell size, m (default {DEFAULTS['base_cell']})")
    p.add_argument("--surface-level", type=int, nargs=2, metavar=("MIN", "MAX"))
    p.add_argument("--feature-level", type=int)
    p.add_argument("--near-level", type=int)
    p.add_argument("--wake-level", type=int)
    p.add_argument("--end-time", type=int, help=f"solver iterations (default {DEFAULTS['end_time']})")
    p.add_argument("--aref", type=float, help="reference area m^2 (default: computed frontal area)")
    p.add_argument("--lref", type=float, help="reference length m (default: X length of the body)")
    p.add_argument("--cofr", type=float, nargs=3, help="moment reference point (default: mid-length on the ground)")
    p.add_argument("--layers", type=int, help="number of boundary layers (default 0 = off)")
    p.add_argument("--layer-thickness", type=float,
                   help="absolute thickness of each boundary layer, m (default: relative to the local cell size)")
    p.add_argument("--processors", type=int, help="CPU cores to run on (default: the tutorial's 6)")
    p.add_argument("--no-merge-layer-faces", dest="merge_layer_faces", action="store_false", default=None,
                   help="switch off face merging before layer addition (works round a snappyHexMesh crash on complex CAD)")
    p.add_argument("--half-model", action="store_true", default=None,
                   help="mesh only Y <= 0 of a body symmetric about Y = 0, with a symmetry plane (half the cells)")
    p.add_argument("--turbulence-model", choices=TURBULENCE_MODELS,
                   help=f"RANS turbulence model (default {DEFAULTS['turbulence_model']})")
    p.add_argument("--upstream", type=float); p.add_argument("--downstream", type=float)
    p.add_argument("--half-width", type=float); p.add_argument("--height", type=float)
    a = p.parse_args(argv)
    kw = {k: getattr(a, k) for k in DEFAULTS}
    if kw["surface_level"] is not None:
        kw["surface_level"] = tuple(kw["surface_level"])
    try:
        job = make_case(a.stl, a.case, a.tutorial, **kw)
    except (CaseError, ig.GeometryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    pr = job["parameters"]
    print(f"Case written to {a.case}")
    print(f"  domain {[round(v, 3) for v in pr['domain_min']]} .. {[round(v, 3) for v in pr['domain_max']]} m, "
          f"base cells {pr['base_cells']}")
    print(f"  speed {pr['speed_m_s']} m/s, nu {pr['nu_m2_s']:g}, Re(lRef) {pr['reynolds_on_lref']:.3g}, ground {pr['ground']}")
    print(f"  Aref {pr['Aref_m2']:.4f} m^2, lRef {pr['lRef_m']:.3f} m, CofR {[round(v, 3) for v in pr['CofR']]}")
    print(f"Run it with:  {a.case}/Allrun.case   then:  python src/check_case.py {a.case}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
