#!/usr/bin/env python3
"""Pre-flight geometry inspector for the CFD pipeline.

Run this on a CAD file BEFORE spending compute on meshing. It answers:

  * Can the file be read, and how many triangles/bodies does it contain?
  * Are the units mm or m? (It guesses, but never converts silently: you must
    confirm with --units.)
  * Is the surface closed (watertight), consistently oriented and free of
    non-manifold edges? If not, WHERE are the problems and which solids are
    involved?
  * Are there degenerate triangles or tiny edges that will hurt meshing?

Inputs
  .stl            read directly (binary or ASCII)
  .step / .stp    tessellated solid-by-solid with CadQuery (pip install cadquery)

Exit codes
  0  PASS or WARN (safe to continue)
  1  FAIL (fix the geometry first)
  2  ERROR (file unreadable, unsupported type, CadQuery missing)
  3  NEEDS_UNITS (geometry looks fine but you have not confirmed units)

Example
  python src/inspect_geometry.py wing.step --units mm \
      --export-stl wing_m.stl --export-problem-edges wing_edges.vtk

The mesh checks use plain NumPy only. Only STEP -> triangles needs CadQuery.
"""
from __future__ import annotations

import argparse
import itertools
import datetime as _dt
import hashlib
import json
import platform
import re
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np

__version__ = "0.2.0"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ERROR = 2
EXIT_NEEDS_UNITS = 3

UNIT_SCALE_TO_M = {"m": 1.0, "mm": 1e-3, "cm": 1e-2, "in": 0.0254}

# Binary STL record: normal(3 x f32), 3 vertices (9 x f32), attribute (u16) = 50 bytes
STL_DTYPE = np.dtype([("n", "<f4", (3,)), ("v", "<f4", (3, 3)), ("a", "<u2")])


class GeometryError(Exception):
    """The geometry cannot be read or analysed (exit code 2)."""


# --------------------------------------------------------------------------- #
# Reading and writing STL / VTK
# --------------------------------------------------------------------------- #
def read_stl(path: Path) -> np.ndarray:
    """Return triangles as an (n, 3, 3) float64 array. Handles binary and ASCII."""
    data = Path(path).read_bytes()
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if len(data) == 84 + 50 * n and n > 0:
            rec = np.frombuffer(data, dtype=STL_DTYPE, count=n, offset=84)
            return rec["v"].astype(np.float64).reshape(-1, 3, 3)
    text = data.decode("ascii", errors="ignore")
    found = re.findall(
        r"vertex\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)", text
    )
    if not found:
        raise GeometryError(
            "No triangles found. The file is empty or is not a valid STL."
        )
    arr = np.array(found, dtype=np.float64)
    if len(arr) % 3:
        raise GeometryError("ASCII STL is truncated: vertex count is not a multiple of 3.")
    return arr.reshape(-1, 3, 3)


def write_stl_binary(path: Path, tris: np.ndarray) -> None:
    """Write triangles (n, 3, 3) as a binary STL; normals are recomputed from the vertex order."""
    tris = np.asarray(tris, dtype=np.float32)
    n = len(tris)
    rec = np.zeros(n, dtype=STL_DTYPE)
    nrm = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    length = np.linalg.norm(nrm, axis=1, keepdims=True)
    length[length == 0] = 1.0
    rec["n"] = nrm / length
    rec["v"] = tris
    with open(path, "wb") as f:
        f.write(b"cfd-pipeline inspect_geometry".ljust(80, b"\0"))
        f.write(struct.pack("<I", n))
        f.write(rec.tobytes())


def write_vtk_segments(path: Path, segments: np.ndarray, solids_on_edge: np.ndarray) -> None:
    """Write line segments as a legacy ASCII VTK file that ParaView can open.

    `segments` is (n, 2, 3). Cell data `solids_on_edge` = how many different
    solids meet at that edge (2+ means contact between separate solids).
    """
    n = len(segments)
    with open(path, "w") as f:
        f.write("# vtk DataFile Version 3.0\nnon-manifold edges\nASCII\nDATASET POLYDATA\n")
        f.write(f"POINTS {2 * n} float\n")
        np.savetxt(f, segments.reshape(-1, 3), fmt="%.6g")
        f.write(f"LINES {n} {3 * n}\n")
        idx = np.arange(2 * n).reshape(n, 2)
        np.savetxt(f, np.column_stack([np.full(n, 2), idx]), fmt="%d")
        f.write(f"CELL_DATA {n}\nSCALARS solids_on_edge int 1\nLOOKUP_TABLE default\n")
        np.savetxt(f, np.asarray(solids_on_edge, dtype=int), fmt="%d")


# --------------------------------------------------------------------------- #
# STEP handling (needs CadQuery)
# --------------------------------------------------------------------------- #
def _export_solid_stl(shape, stl_path: Path, tol: float, cq) -> None:
    """Tessellate one CadQuery shape into an STL file, falling back to the exporter API on older CadQuery."""
    try:
        shape.exportStl(str(stl_path), tolerance=tol, angularTolerance=0.1)
    except Exception:
        cq.exporters.export(
            cq.Workplane("XY").newObject([shape]),
            str(stl_path),
            tolerance=tol,
            angularTolerance=0.1,
        )


def step_to_triangles(step_path: Path, tol_rel: float):
    """Tessellate a STEP file one solid at a time.

    Returns (triangles, solid_index_per_triangle, info, per_solid_triangle_lists).
    Tessellating each solid separately lets us say WHICH solids touch each other.
    """
    try:
        import cadquery as cq
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise GeometryError(
            "Reading STEP files needs CadQuery, which is not installed. "
            "Run: pip install cadquery   (or export an STL from your CAD tool)."
        ) from exc

    try:
        wp = cq.importers.importStep(str(step_path))
        shapes = wp.vals()
        if not shapes:
            raise GeometryError("The STEP file contains no shapes.")
        comp = cq.Compound.makeCompound(shapes)
        bb = comp.BoundingBox()
        diag = float(bb.DiagonalLength)
        tol = max(diag * tol_rel, 1e-9)
        solids = list(comp.Solids())
        parts = solids if solids else [comp]
        info = {
            "solids": len(solids),
            "cad_volume": float(sum(s.Volume() for s in solids)),
            "solid_cad_volumes": [float(s.Volume()) for s in solids],
            "valid": bool(comp.isValid()),
            "bbox_min": [float(bb.xmin), float(bb.ymin), float(bb.zmin)],
            "bbox_max": [float(bb.xmax), float(bb.ymax), float(bb.zmax)],
            "tessellation_tolerance": tol,
            "cadquery_version": getattr(cq, "__version__", "unknown"),
        }
        pieces = []
        with tempfile.TemporaryDirectory() as tmp:
            for k, shape in enumerate(parts):
                f = Path(tmp) / f"solid_{k}.stl"
                _export_solid_stl(shape, f, tol, cq)
                try:
                    pieces.append(read_stl(f))
                except GeometryError:
                    pieces.append(np.zeros((0, 3, 3)))
    except GeometryError:
        raise
    except Exception as exc:  # pragma: no cover - depends on CAD content
        raise GeometryError(f"CadQuery could not read or tessellate the STEP file: {exc}") from exc

    nonempty = [p for p in pieces if len(p)]
    if not nonempty:
        raise GeometryError("The STEP file produced no triangles when tessellated.")
    tris = np.concatenate(nonempty)
    groups = np.concatenate([np.full(len(p), k) for k, p in enumerate(pieces) if len(p)])
    return tris, groups, info, pieces


# --------------------------------------------------------------------------- #
# Mesh analysis
# --------------------------------------------------------------------------- #
def connected_components(n: int, edges: np.ndarray) -> np.ndarray:
    """Label connected vertices (label = smallest vertex id in the component)."""
    labels = np.arange(n)
    if len(edges) == 0:
        return labels
    while True:
        a = labels[edges[:, 0]]
        b = labels[edges[:, 1]]
        m = np.minimum(a, b)
        new = labels.copy()
        np.minimum.at(new, edges[:, 0], m)
        np.minimum.at(new, edges[:, 1], m)
        np.minimum.at(new, a, m)
        np.minimum.at(new, b, m)
        new = new[new]  # pointer jumping
        if np.array_equal(new, labels):
            return labels
        labels = new


def summarise_nonmanifold(verts, uedges, inv, nonmanifold, face_group, n_faces,
                          have_groups, max_records=30):
    """Group non-manifold edges by which solids meet there.

    Returns (records, segments, solids_on_edge). Solid ids in the records are
    1-based so they match the order solids appear in the CAD file.
    """
    edge_ids = np.flatnonzero(nonmanifold)
    if len(edge_ids) == 0:
        return [], np.zeros((0, 2, 3)), np.zeros(0, dtype=int)

    row_face = np.arange(len(inv)) % n_faces      # face that each directed edge row came from
    sel = nonmanifold[inv]
    r_edge = inv[sel]
    r_group = face_group[row_face[sel]]
    order = np.argsort(r_edge, kind="stable")
    r_edge, r_group = r_edge[order], r_group[order]
    cut = np.flatnonzero(np.diff(r_edge)) + 1
    starts = np.concatenate(([0], cut))
    ends = np.concatenate((cut, [len(r_edge)]))

    p0 = verts[uedges[:, 0]]
    p1 = verts[uedges[:, 1]]
    mid = (p0 + p1) / 2.0

    recs: dict = {}
    segments = np.zeros((len(starts), 2, 3))
    solids_on_edge = np.zeros(len(starts), dtype=int)
    for i, (s, e) in enumerate(zip(starts, ends)):
        eid = r_edge[s]
        key = tuple(sorted({int(x) + 1 for x in r_group[s:e]}))
        rec = recs.setdefault(key, {"count": 0, "sum": np.zeros(3),
                                    "lo": np.full(3, np.inf), "hi": np.full(3, -np.inf)})
        rec["count"] += 1
        rec["sum"] += mid[eid]
        rec["lo"] = np.minimum(rec["lo"], mid[eid])
        rec["hi"] = np.maximum(rec["hi"], mid[eid])
        segments[i, 0], segments[i, 1] = p0[eid], p1[eid]
        solids_on_edge[i] = len(key)

    out = []
    for key, rec in recs.items():
        if not have_groups:
            kind = "unknown"
        elif len(key) >= 2:
            kind = "between_solids"
        else:
            kind = "within_solid"
        out.append({
            "solids": list(key),
            "kind": kind,
            "edges": int(rec["count"]),
            "centroid": (rec["sum"] / rec["count"]).tolist(),
            "bbox_min": rec["lo"].tolist(),
            "bbox_max": rec["hi"].tolist(),
        })
    out.sort(key=lambda r: -r["edges"])
    return out[:max_records], segments, solids_on_edge


def weld_vertices(pts: np.ndarray, tol: float) -> np.ndarray:
    """Cluster label (0..n-1) per point, merging points closer than tol/2 for certain (and some up to ~tol).

    Plain grid rounding misses pairs that straddle a grid-cell boundary, however close they are. Here the grid
    is laid 8 times, shifted by half a cell in every combination of axes, and points sharing a cell in ANY of
    them are joined (union-find by repeated min-label propagation). Two points closer than tol/2 share a cell
    in at least one of the 8 grids.
    """
    n = len(pts)
    label = np.arange(n)
    keys_all = []
    for shift in itertools.product((0.0, 0.5), repeat=3):
        k = np.floor(pts / tol + np.array(shift)).astype(np.int64)
        _, g = np.unique(k, axis=0, return_inverse=True)
        keys_all.append(np.asarray(g).reshape(-1))
    for _ in range(50):
        changed = False
        for g in keys_all:
            m = np.full(g.max() + 1, n, dtype=np.int64)
            np.minimum.at(m, g, label)
            new = m[g]
            if (new < label).any():
                label = np.minimum(label, new)
                changed = True
        label = label[label]                       # pointer jumping
        if not changed:
            break
    _, inv = np.unique(label, return_inverse=True)
    return np.asarray(inv).reshape(-1)


def analyse_mesh(tris: np.ndarray, weld_tol_rel: float = 1e-5, groups=None) -> dict:
    """Topology and geometry facts about a triangle soup (units: file units).

    `groups` optionally gives a solid index per triangle (used for STEP input).
    The returned dict has a private "_internal" entry (arrays); inspect() removes it.
    """
    if tris.size == 0:
        raise GeometryError("The file contains no triangles.")
    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    extents = hi - lo
    diag = float(np.linalg.norm(extents))
    if diag == 0.0:
        raise GeometryError("All vertices are identical; the geometry has no size.")

    # Weld coincident vertices so shared edges can be recognised.
    tol = weld_tol_rel * diag
    inverse = weld_vertices(pts - lo, tol)
    nv = int(inverse.max()) + 1
    counts = np.bincount(inverse, minlength=nv)
    verts = np.zeros((nv, 3))
    for k in range(3):
        verts[:, k] = np.bincount(inverse, weights=pts[:, k], minlength=nv) / counts
    faces = inverse.reshape(-1, 3)

    # Degenerate triangles (collapsed by welding, or essentially zero area).
    collapsed = (
        (faces[:, 0] == faces[:, 1])
        | (faces[:, 1] == faces[:, 2])
        | (faces[:, 0] == faces[:, 2])
    )
    v0, v1, v2 = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    area2 = np.linalg.norm(np.cross(v1 - v0, v2 - v0), axis=1)
    degenerate = collapsed | (area2 < 1e-12 * diag * diag)
    n_degenerate = int(degenerate.sum())
    keep_idx = np.flatnonzero(~degenerate)
    f = faces[keep_idx]
    if len(f) == 0:
        raise GeometryError("Every triangle is degenerate; the geometry is unusable.")

    # Edge topology.
    directed = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    sign = np.where(directed[:, 0] < directed[:, 1], 1.0, -1.0)
    undirected = np.sort(directed, axis=1)
    uedges, inv, cnt = np.unique(
        undirected, axis=0, return_inverse=True, return_counts=True
    )
    inv = np.asarray(inv).reshape(-1)
    sums = np.bincount(inv, weights=sign, minlength=len(uedges))
    boundary = cnt == 1
    nonmanifold = cnt > 2
    inconsistent = (cnt == 2) & (sums != 0)

    elen = np.linalg.norm(verts[uedges[:, 0]] - verts[uedges[:, 1]], axis=1)

    # Where are the non-manifold edges, and which solids meet there?
    have_groups = groups is not None
    face_group = (np.asarray(groups)[keep_idx] if have_groups
                  else np.zeros(len(f), dtype=int))
    nm_records, nm_segments, nm_solids = summarise_nonmanifold(
        verts, uedges, inv, nonmanifold, face_group, len(f), have_groups
    )

    # Bodies = connected groups of triangles.
    labels = connected_components(nv, uedges)
    face_label = labels[f[:, 0]]
    comp_ids, face_comp = np.unique(face_label, return_inverse=True)
    face_comp = np.asarray(face_comp).reshape(-1)
    ncomp = len(comp_ids)
    comp_index = {int(lbl): i for i, lbl in enumerate(comp_ids)}

    centre = lo + extents / 2.0
    w0, w1, w2 = (verts[f[:, k]] - centre for k in range(3))
    vol_face = np.einsum("ij,ij->i", w0, np.cross(w1, w2)) / 6.0
    vol_comp = np.bincount(face_comp, weights=vol_face, minlength=ncomp)
    tri_comp = np.bincount(face_comp, minlength=ncomp)

    def per_comp_edges(mask: np.ndarray) -> np.ndarray:
        """Count the selected edges in each connected body (the bodies are in 'labels')."""
        if not mask.any():
            return np.zeros(ncomp, dtype=int)
        lab = labels[uedges[mask][:, 0]]
        idx = np.array([comp_index[int(x)] for x in lab])
        return np.bincount(idx, minlength=ncomp)

    open_by_comp = per_comp_edges(boundary)
    nonman_by_comp = per_comp_edges(nonmanifold)

    bodies = []
    for i in range(ncomp):
        sel = f[face_comp == i]
        bv = verts[np.unique(sel)]
        bodies.append(
            {
                "triangles": int(tri_comp[i]),
                "open_edges": int(open_by_comp[i]),
                "non_manifold_edges": int(nonman_by_comp[i]),
                "closed": bool(open_by_comp[i] == 0 and nonman_by_comp[i] == 0),
                "volume": float(vol_comp[i]),
                "bbox_min": bv.min(axis=0).tolist(),
                "bbox_max": bv.max(axis=0).tolist(),
            }
        )

    return {
        "triangles_read": int(len(tris)),
        "triangles_used": int(len(f)),
        "degenerate_triangles": n_degenerate,
        "vertices_welded": int(nv),
        "bbox_min": lo.tolist(),
        "bbox_max": hi.tolist(),
        "extents": extents.tolist(),
        "diagonal": diag,
        "open_edges": int(boundary.sum()),
        "non_manifold_edges": int(nonmanifold.sum()),
        "inconsistent_edges": int(inconsistent.sum()),
        "non_manifold_groups": nm_records,
        "min_edge": float(elen.min()),
        "median_edge": float(np.median(elen)),
        "bodies": bodies,
        "volume_total": float(vol_comp.sum()),
        "_internal": {
            "degenerate_mask": degenerate,
            "collapsed_mask": collapsed,
            "welded_tris": verts[faces],
            "nm_segments": nm_segments,
            "nm_solids": nm_solids,
        },
    }


def analyse_solids(pieces: list, cad_volumes: list | None, weld_tol_rel: float) -> list:
    """Check each STEP solid on its own (is it closed? does its volume match the CAD?)."""
    out = []
    for k, piece in enumerate(pieces):
        entry: dict = {"id": k + 1, "triangles": int(len(piece))}
        if cad_volumes is not None and k < len(cad_volumes):
            entry["cad_volume"] = float(cad_volumes[k])
        if len(piece) == 0:
            entry["error"] = "no triangles after tessellation"
            out.append(entry)
            continue
        try:
            a = analyse_mesh(piece, weld_tol_rel)
        except GeometryError as exc:
            entry["error"] = str(exc)
            out.append(entry)
            continue
        entry.update({
            "open_edges": a["open_edges"],
            "non_manifold_edges": a["non_manifold_edges"],
            "inconsistent_edges": a["inconsistent_edges"],
            "closed": bool(a["open_edges"] == 0 and a["non_manifold_edges"] == 0
                           and a["inconsistent_edges"] == 0),
            "mesh_volume": float(a["volume_total"]),
            "bbox_min": a["bbox_min"],
            "bbox_max": a["bbox_max"],
        })
        out.append(entry)
    return out


# --------------------------------------------------------------------------- #
# Units
# --------------------------------------------------------------------------- #
def guess_units(max_dim: float) -> tuple[str, str, str]:
    """Return (guess, confidence, reason) from the largest dimension in file units."""
    d = f"{max_dim:g}"
    if max_dim >= 500:
        return "mm", "high", f"largest dimension is {d}; that is far too big for metres, so probably millimetres"
    if max_dim >= 50:
        return "mm", "medium", f"largest dimension is {d}; probably millimetres, but could be centimetres"
    if max_dim > 20:
        return "m", "low", f"largest dimension is {d}; could be metres (a big assembly), centimetres or inches"
    if max_dim >= 0.2:
        return "m", "high", f"largest dimension is {d}; sensible for a car part in metres"
    return "m", "low", f"largest dimension is only {d}; either a small part in metres or a model-scale part"


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def compare_cad_volume(cad_volume: float, mesh_volume: float, threshold: float = 0.05):
    """Return a warning message if CAD and mesh volumes disagree, else None."""
    if cad_volume <= 0:
        return None
    rel = abs(abs(mesh_volume) - cad_volume) / cad_volume
    if rel > threshold:
        return (
            f"Mesh volume differs from the CAD volume by {rel * 100:.1f}% "
            "(tessellation may have gaps, overlaps or inverted faces)."
        )
    return None


def _where(centroid) -> str:
    """Format a centroid as '(x, y, z)' with 4 significant figures, for messages."""
    return "(" + ", ".join(f"{c:.4g}" for c in centroid) + ")"


def describe_nonmanifold(groups: list, limit: int = 4) -> str:
    """Summarise the first few groups of non-manifold edges as text: how many, which solids, and where."""
    parts = []
    for g in groups[:limit]:
        if g["kind"] == "between_solids":
            parts.append(f"{g['edges']} between solids {' & '.join(map(str, g['solids']))} near {_where(g['centroid'])}")
        elif g["kind"] == "within_solid":
            parts.append(f"{g['edges']} inside solid {g['solids'][0]} near {_where(g['centroid'])}")
        else:
            parts.append(f"{g['edges']} near {_where(g['centroid'])}")
    more = f"; and {len(groups) - limit} more locations" if len(groups) > limit else ""
    return "; ".join(parts) + more + " (coordinates in file units)"


def evaluate(m: dict, units: str | None, allow_open: bool, step: dict | None,
             allow_contact: bool = False, per_solid: list | None = None) -> tuple[list, dict]:
    """Turn raw mesh facts into PASS/WARN/FAIL/ACTION checks and a units block."""
    checks: list[dict] = []

    def add(level: str, code: str, msg: str) -> None:
        """Record one check result (level, short code, message)."""
        checks.append({"level": level, "code": code, "message": msg})

    hard = "WARN" if allow_open else "FAIL"
    suffix = " (allowed by --allow-open)" if allow_open else ""

    add("PASS", "READ", f"{m['triangles_read']:,} triangles read, {m['vertices_welded']:,} unique vertices after welding.")

    if step is not None:
        if not step["valid"]:
            add("WARN", "CAD_INVALID", "OpenCascade reports the CAD shape as invalid; expect problems.")
        if step["solids"] == 0:
            add("WARN", "NO_SOLIDS", "The STEP file has no solid bodies (surfaces only). Mesh checks below still apply.")
        else:
            add("PASS", "SOLIDS", f"{step['solids']} solids in the CAD file.")

    nb = len(m["bodies"])
    if nb == 1:
        add("PASS", "BODIES", "1 body.")
    else:
        sizes = ", ".join(f"{b['triangles']:,}" for b in m["bodies"][:8])
        more = " ..." if nb > 8 else ""
        add("WARN", "BODIES",
            f"{nb} separate connected bodies (triangles per body: {sizes}{more}). Fine for an assembly, "
            "but overlaps between bodies are not checked.")

    if m["open_edges"] == 0:
        add("PASS", "OPEN_EDGES", "No open edges: the surface is closed.")
    else:
        open_bodies = sum(1 for b in m["bodies"] if b["open_edges"] > 0)
        add(hard, "OPEN_EDGES",
            f"{m['open_edges']:,} open edges in {open_bodies} of {nb} bodies: the surface has holes, "
            "so 'inside' and 'outside' are ambiguous for meshing. Close the holes in CAD"
            f"{suffix}.")

    if m["non_manifold_edges"] == 0:
        add("PASS", "NON_MANIFOLD", "No non-manifold edges.")
    else:
        groups = m.get("non_manifold_groups", [])
        total = m["non_manifold_edges"]
        between = sum(g["edges"] for g in groups if g["kind"] == "between_solids")
        within = sum(g["edges"] for g in groups if g["kind"] == "within_solid")
        unknown = sum(g["edges"] for g in groups if g["kind"] == "unknown")
        detail = describe_nonmanifold(groups)
        if allow_contact and between > 0 and within == 0 and unknown == 0:
            add("WARN", "CONTACT",
                f"{total:,} edges where separate solids touch: {detail}. Allowed by --allow-contact; "
                "whether the mesher copes with touching parts still has to be tested.")
        else:
            hint = ""
            if between > 0 and within == 0 and unknown == 0:
                hint = " These look like separate solids touching; re-run with --allow-contact if that is intended."
            elif within > 0:
                hint = " Edges inside a single solid point to a modelling problem in the CAD."
            elif unknown > 0:
                hint = " STL files carry no solid information, so touching parts cannot be told apart from faults."
            add(hard, "NON_MANIFOLD",
                f"{total:,} edges are shared by more than 2 triangles: {detail}.{hint}{suffix}")

    if m["inconsistent_edges"] == 0:
        add("PASS", "WINDING", "Triangle orientation is consistent.")
    else:
        add(hard, "WINDING",
            f"{m['inconsistent_edges']:,} edges join triangles facing opposite ways "
            f"(inconsistent orientation){suffix}.")

    inverted = [i + 1 for i, b in enumerate(m["bodies"]) if b["closed"] and b["volume"] < 0]
    if inverted:
        add("WARN", "INVERTED",
            f"Body {', '.join(map(str, inverted))} looks inside-out (negative volume): its normals point inward.")

    if m["degenerate_triangles"]:
        pct = 100.0 * m["degenerate_triangles"] / m["triangles_read"]
        add("WARN", "DEGENERATE",
            f"{m['degenerate_triangles']:,} degenerate (zero-area) triangles ({pct:.2f}%); ignored in the "
            "checks above; an exported STL keeps them unless the weld collapsed them (they stitch faces together).")

    ratio = m["min_edge"] / m["diagonal"]
    if ratio < 1e-5:
        add("WARN", "TINY_EDGES",
            f"Shortest edge is {m['min_edge']:.3g}, only {ratio:.1e} of the overall size. "
            "Often tessellation slivers rather than real features, but very small features make meshing slow or fail.")

    if per_solid:
        bad = [s["id"] for s in per_solid if s.get("error") or s.get("closed") is False]
        if bad:
            add("WARN", "SOLID_FAULTS",
                f"Solids with problems on their own: {', '.join(map(str, bad[:12]))}"
                f"{' ...' if len(bad) > 12 else ''}. See the report for details.")
        else:
            add("PASS", "SOLID_FAULTS", f"Each of the {len(per_solid)} solids is closed on its own.")
        off = []
        for s in per_solid:
            cv, mv = s.get("cad_volume"), s.get("mesh_volume")
            if cv and mv is not None and compare_cad_volume(cv, mv):
                off.append(s["id"])
        if off:
            add("WARN", "SOLID_VOLUME",
                f"Mesh volume differs from CAD volume by more than 5% for solids {', '.join(map(str, off[:12]))}.")
    elif step is not None:
        msg = compare_cad_volume(step["cad_volume"], m["volume_total"])
        if msg:
            add("WARN", "CAD_VOLUME", msg)

    # Units
    max_dim = max(m["extents"])
    guess, conf, reason = guess_units(max_dim)
    ublock = {
        "declared": units,
        "guessed": guess,
        "confidence": conf,
        "reason": reason,
        "scale_to_metres": UNIT_SCALE_TO_M[units] if units else None,
    }
    if units is None:
        add("ACTION", "UNITS",
            f"Units not confirmed. Best guess: {guess} ({conf} confidence): {reason}. "
            f"Re-run with --units {guess} (or m, mm, cm, in) to confirm.")
    else:
        size_m = [e * UNIT_SCALE_TO_M[units] for e in m["extents"]]
        dims = " x ".join(f"{s:.4g}" for s in size_m)
        add("PASS", "UNITS", f"Units set to {units}. Size in metres: {dims}.")
        if not (0.01 <= max(size_m) <= 30.0):
            add("WARN", "SIZE_IMPLAUSIBLE",
                f"Largest dimension is {max(size_m):.4g} m, which is unusual for a car part. Check the units.")
        elif conf == "high" and guess != units:
            add("WARN", "UNITS_MISMATCH",
                f"You said {units}, but the size strongly suggests {guess}. Double-check.")
    return checks, ublock


def overall_status(checks: list[dict]) -> tuple[str, int]:
    """Reduce the list of checks to (status, exit code): FAIL beats NEEDS_UNITS beats WARN beats PASS."""
    levels = {c["level"] for c in checks}
    if "FAIL" in levels:
        return "FAIL", EXIT_FAIL
    if "ACTION" in levels:
        return "NEEDS_UNITS", EXIT_NEEDS_UNITS
    if "WARN" in levels:
        return "WARN", EXIT_OK
    return "PASS", EXIT_OK


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def sha256_of(path: Path) -> str:
    """SHA-256 of a file, read in 1 MB chunks. Recorded in the report so a result can be traced to its input."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect(
    path: Path,
    units: str | None = None,
    allow_open: bool = False,
    allow_contact: bool = False,
    weld_tol_rel: float = 1e-5,
    step_tol_rel: float = 1e-4,
    export_stl: Path | None = None,
    problem_edges_vtk: Path | None = None,
) -> dict:
    """Run every check on one geometry file and return the full report as a dictionary.

    path: .stl, .step or .stp file.
    units: units the file is in (m, mm, cm, in). Never guessed silently; without it the status is NEEDS_UNITS.
    allow_open: downgrade open edges, non-manifold edges and winding problems from FAIL to WARN.
    allow_contact: for STEP input, only warn about non-manifold edges where separate solids touch.
    weld_tol_rel: vertex weld tolerance as a fraction of the model size.
    step_tol_rel: STEP tessellation tolerance as a fraction of the model size.
    export_stl: if given (and the checks pass and units are known), write a welded STL in metres here.
    problem_edges_vtk: if given, write the non-manifold edges as VTK segments to view in ParaView.

    Raises GeometryError if the file cannot be read.
    """
    path = Path(path)
    if not path.is_file():
        raise GeometryError(f"File not found: {path}")
    suffix = path.suffix.lower()
    step_info = None
    per_solid = None
    groups = None

    if suffix == ".stl":
        tris = read_stl(path)
        fmt = "STL"
    elif suffix in (".step", ".stp"):
        fmt = "STEP"
        tris, groups, step_info, pieces = step_to_triangles(path, step_tol_rel)
        if step_info["solids"] > 0:
            per_solid = analyse_solids(pieces, step_info["solid_cad_volumes"], weld_tol_rel)
        else:
            groups = None
    else:
        raise GeometryError(f"Unsupported file type '{suffix}'. Use .stl, .step or .stp.")

    mesh = analyse_mesh(tris, weld_tol_rel, groups=groups)
    internal = mesh.pop("_internal")
    checks, ublock = evaluate(mesh, units, allow_open, step_info,
                              allow_contact=allow_contact, per_solid=per_solid)
    status, code = overall_status(checks)
    scale = UNIT_SCALE_TO_M[units] if units else 1.0

    exported = None
    if export_stl is not None:
        if status in ("PASS", "WARN") and units is not None:
            # Write the WELDED vertex positions, so triangles that share a vertex share it exactly (OpenFOAM
            # joins triangles only on exactly equal points), and drop only triangles that collapsed in the
            # weld. Zero-area slivers whose corners stay distinct are kept: they stitch neighbouring CAD faces
            # together, and removing them split a PERRINN wing into 1,199 unconnected pieces (a leaking mesh).
            keep = ~internal["collapsed_mask"]
            write_stl_binary(Path(export_stl), internal["welded_tris"][keep] * scale)
            exported = str(export_stl)
            if (~keep).any():
                checks.append({
                    "level": "INFO", "code": "STRIPPED",
                    "message": f"Exported STL: vertices welded; {int((~keep).sum()):,} collapsed triangles omitted.",
                })
        else:
            checks.append({
                "level": "INFO", "code": "NOT_EXPORTED",
                "message": "STL not exported: it is only written when checks pass and --units is given.",
            })

    edges_file = None
    if problem_edges_vtk is not None:
        if len(internal["nm_segments"]):
            write_vtk_segments(Path(problem_edges_vtk), internal["nm_segments"] * scale,
                               internal["nm_solids"])
            edges_file = str(problem_edges_vtk)
            where = "metres" if units else "file units"
            checks.append({
                "level": "INFO", "code": "PROBLEM_EDGES",
                "message": f"{len(internal['nm_segments']):,} non-manifold edges written to {problem_edges_vtk} "
                           f"(in {where}) for viewing in ParaView.",
            })
        else:
            checks.append({"level": "INFO", "code": "PROBLEM_EDGES",
                           "message": "No non-manifold edges, so no problem-edge file was written."})

    return {
        "input": {"path": str(path), "format": fmt, "sha256": sha256_of(path),
                  "size_bytes": path.stat().st_size},
        "status": status,
        "exit_code": code,
        "units": ublock,
        "geometry": mesh,
        "step": step_info,
        "solids": per_solid,
        "checks": checks,
        "exported_stl_metres": exported,
        "problem_edges_file": edges_file,
        "provenance": {
            "tool": "inspect_geometry.py",
            "tool_version": __version__,
            "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "python": platform.python_version(),
            "numpy": np.__version__,
            "weld_tol_rel": weld_tol_rel,
        },
    }


def print_summary(r: dict) -> None:
    """Print the human-readable report: header, one line per check, and the final RESULT line."""
    g = r["geometry"]
    print(f"Geometry inspection: {r['input']['path']}")
    print(f"  format : {r['input']['format']}   sha256: {r['input']['sha256'][:16]}...")
    ext = " x ".join(f"{e:.6g}" for e in g["extents"])
    print(f"  size   : {ext} (file units)")
    solids = f"   solids in CAD: {r['step']['solids']}" if r["step"] else ""
    print(f"  bodies : {len(g['bodies'])}   triangles: {g['triangles_used']:,}   open edges: {g['open_edges']:,}{solids}")
    print()
    for c in r["checks"]:
        print(f"  [{c['level']:<6}] {c['message']}")
    print()
    n_fail = sum(c["level"] == "FAIL" for c in r["checks"])
    n_warn = sum(c["level"] == "WARN" for c in r["checks"])
    print(f"RESULT: {r['status']}  ({n_fail} failed, {n_warn} warnings)")
    if r["exported_stl_metres"]:
        print(f"Exported (metres): {r['exported_stl_metres']}")


def build_parser() -> argparse.ArgumentParser:
    """Command-line options for the inspector."""
    p = argparse.ArgumentParser(description="Pre-flight check for CFD geometry (STL or STEP).")
    p.add_argument("geometry", type=Path, help="input .stl, .step or .stp file")
    p.add_argument("--units", choices=sorted(UNIT_SCALE_TO_M), help="units the file is in (required to pass)")
    p.add_argument("--allow-open", action="store_true",
                   help="downgrade open edges / non-manifold / winding problems from FAIL to WARN")
    p.add_argument("--allow-contact", action="store_true",
                   help="treat non-manifold edges that only occur where SEPARATE solids touch as a WARN "
                        "(STEP input only; edges inside a single solid still FAIL)")
    p.add_argument("--export-stl", type=Path, help="write a cleaned STL in metres (only if checks pass)")
    p.add_argument("--export-problem-edges", type=Path, metavar="FILE.vtk",
                   help="write the non-manifold edges as a VTK file to overlay in ParaView")
    p.add_argument("--report", type=Path, help="JSON report path (default: <input>.report.json)")
    p.add_argument("--weld-tol-rel", type=float, default=1e-5,
                   help="vertex weld tolerance as a fraction of model size (default 1e-5)")
    p.add_argument("--step-tol-rel", type=float, default=1e-4,
                   help="STEP tessellation tolerance as a fraction of model size (default 1e-4)")
    return p


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point: inspect, print the summary, write <input>.report.json. Returns the exit code."""
    args = build_parser().parse_args(argv)
    try:
        result = inspect(
            args.geometry, units=args.units, allow_open=args.allow_open,
            allow_contact=args.allow_contact, weld_tol_rel=args.weld_tol_rel,
            step_tol_rel=args.step_tol_rel, export_stl=args.export_stl,
            problem_edges_vtk=args.export_problem_edges,
        )
    except GeometryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    report_path = args.report or Path(str(args.geometry) + ".report.json")
    report_path.write_text(json.dumps(result, indent=2))
    print_summary(result)
    print(f"Report written to {report_path}")
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
