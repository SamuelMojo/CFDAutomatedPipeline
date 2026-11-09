#!/usr/bin/env pvbatch
"""Turn a finished steady CFD case into website-ready pictures and an orbit video, with ParaView.

Run it with ParaView's batch interpreter (pvbatch), NOT plain python:

    pvbatch src/render_flow.py --case <job>/case/<name>.foam --out images/flow/perrinn_h856

Scenes (--scenes, default all except the slow ones are all on):
    hero    three-quarter view, graphite wing, glowing streamlines coloured by speed
    side    side-on slice through the wing, speed colours: the "2D" look
    wake    three cross-sections behind the wing: the vortices
    vortex  vortex cores (Q-criterion) coloured by speed
    under   underside of the wing coloured by pressure: where the suction is
    ground  the road seen from below, coloured by pressure: the wing's footprint
    orbit   360-degree orbit of the hero scene, frames + mp4 (needs ffmpeg)

Half models (symmetry plane at Y = 0) are mirrored so the whole wing shows. Every scene runs on its own: if one
fails, the error is printed and the rest still run. Use --list first to see the regions and arrays in the case.
"""
from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

try:
    from paraview import simple as pv          # only available under pvbatch / pvpython
except ImportError:                            # lets the helper functions be tested with plain Python
    pv = None

__version__ = "0.1.0"

ALL_SCENES = ("hero", "side", "wake", "vortex", "under", "ground", "orbit")
# Boundaries of the wind-tunnel box: everything else in the patch list is the body.
BOX_PATCHES = {"inlet", "outlet", "lowerwall", "upperwall", "frontandback", "centreplane", "sidewall",
               "symmetry", "ground", "ceiling"}
GROUND_NAMES = ("lowerwall", "ground", "floor")
BG_TOP, BG_BOTTOM = [0.10, 0.12, 0.17], [0.01, 0.01, 0.02]        # dark blue-black gradient
GRAPHITE = [0.27, 0.28, 0.31]


# --------------------------------------------------------------------------- #
# Pure helpers (tested without ParaView)
# --------------------------------------------------------------------------- #
def tail(name: str) -> str:
    """'patch/body' -> 'body'."""
    return name.split("/")[-1]


def pick_regions(available: list[str]) -> dict:
    """Choose the internal mesh, the body patch(es) and the ground patch from the reader's region names."""
    out = {"mesh": None, "body": [], "ground": None}
    for name in available:
        t = tail(name).lower()
        if name.startswith("group/") or t.endswith("group"):
            continue
        if t == "internalmesh":
            out["mesh"] = name
        elif t in GROUND_NAMES:
            out["ground"] = name
        elif t not in BOX_PATCHES and not name.lower().startswith("lagrangian"):
            out["body"].append(name)
    return out


def camera_position(focal, distance: float, azimuth_deg: float, elevation_deg: float):
    """Camera position on a sphere around `focal`. Azimuth 0 looks at the wing from upstream (the front)."""
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    return [focal[0] - distance * math.cos(el) * math.cos(az),
            focal[1] - distance * math.cos(el) * math.sin(az),
            focal[2] + distance * math.sin(el)]


def seed_levels(zmax: float, count: int) -> list[float]:
    """Heights for the streamline seed lines: from near the road up to well above the wing."""
    top = zmax + 0.12
    lo = 0.025
    if count < 2:
        return [lo]
    return [lo + (top - lo) * i / (count - 1) for i in range(count)]


# --------------------------------------------------------------------------- #
# ParaView helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def trynames(proxy, **props) -> None:
    """Set properties, ignoring names this ParaView version does not have."""
    for k, v in props.items():
        try:
            setattr(proxy, k, v)
        except Exception:                      # noqa: BLE001 - property names differ between versions
            pass


def make_view(size):
    v = pv.CreateView("RenderView")
    v.ViewSize = list(size)
    trynames(v, OrientationAxesVisibility=0, UseFXAA=1)
    try:                                       # ParaView 5.13+/6
        v.UseColorPaletteForBackground = 0
        v.BackgroundColorMode = "Gradient"
        v.Background, v.Background2 = BG_BOTTOM, BG_TOP
    except Exception:                          # noqa: BLE001
        trynames(v, UseGradientBackground=1, Background=BG_BOTTOM, Background2=BG_TOP)
    # paraview.simple.Render() re-fits the camera to everything on its very first call of a
    # session, which would undo the hero camera. Do that first render now, on the empty view.
    try:
        pv.Render(v)
    except Exception:                          # noqa: BLE001
        pass
    return v


def color_map(array: str, preset: str, lo: float, hi: float, invert: bool = False):
    lut = pv.GetColorTransferFunction(array)
    # Preset names differ between ParaView versions, so try the wanted one, then close relatives,
    # then Turbo (which this ParaView has), and never fail a picture over a colour map.
    wanted = [preset]
    if preset.startswith("Inferno"):
        wanted += ["Inferno (matplotlib)", "Inferno", "Black-Body Radiation", "Plasma (matplotlib)", "Plasma"]
    wanted += ["Turbo", "Viridis (matplotlib)", "Viridis", "Cool to Warm"]
    for name in wanted:
        try:
            if lut.ApplyPreset(name, True) is not False:
                break
        except Exception:                      # noqa: BLE001
            continue
    lut.RescaleTransferFunction(lo, hi)
    if invert:
        lut.InvertTransferFunction()
    pv.GetOpacityTransferFunction(array).RescaleTransferFunction(lo, hi)
    return lut


_MIRROR = {"recipe": None}      # filled in the first time a mirror is built and checked


def _spans_both_sides(proxy) -> bool:
    """True when a half model (Y <= 0) now reaches into Y > 0, i.e. the mirror worked."""
    try:
        proxy.UpdatePipeline()
        b = proxy.GetDataInformation().GetBounds()
        return b[2] < -1e-3 and b[3] > 1e-3
    except Exception:                          # noqa: BLE001
        return False


def _reflect_class():
    for name in ("Reflect", "ReflectionFilter", "ReflectFilter"):
        cls = getattr(pv, name, None)
        if cls is not None:
            return cls
    for name in dir(pv):
        if "reflect" in name.lower():
            return getattr(pv, name)
    return None


def _build_mirror(src, recipe):
    """recipe is ('reflect', plane_value) or ('transform', None)."""
    kind, value = recipe
    if kind == "reflect":
        r = _reflect_class()(Input=src)
        # ParaView 6 renamed Plane to ReflectionPlane; setting a name that does not exist is skipped.
        trynames(r, ReflectionPlane=value, Plane=value, Center=0.0, CopyInput=1, FlipAllInputArrays=1)
        return r
    mir = pv.Transform(Input=src)
    trynames(mir, Transform="Transform")
    mir.Transform.Scale = [1.0, -1.0, 1.0]
    grp = getattr(pv, "GroupDatasets", None) or getattr(pv, "AppendDatasets")
    return grp(Input=[src, mir])


def mirror(src, half: bool):
    """Mirror a half model about Y = 0 so the whole wing is shown.

    The first call tries each way of doing it and keeps the first one that really
    produces geometry on both sides; later calls reuse that recipe."""
    if not half:
        return src
    if _MIRROR["recipe"] is not None:
        return _build_mirror(src, _MIRROR["recipe"])
    candidates = []
    if _reflect_class() is not None:
        candidates += [("reflect", "Y"), ("reflect", "Y Max")]
    candidates.append(("transform", None))
    for rec in candidates:
        try:
            out = _build_mirror(src, rec)
        except Exception as e:                 # noqa: BLE001
            log(f"  mirror {rec}: {type(e).__name__}: {e}")
            continue
        if _spans_both_sides(out):
            _MIRROR["recipe"] = rec
            log(f"  mirror: using {rec[0]}" + (f" about plane {rec[1]!r}" if rec[1] else ""))
            return out
        log(f"  mirror {rec}: did not produce a Y > 0 half, trying the next way")
    raise RuntimeError("could not mirror the half model with any method")


def set_camera(view, focal, distance, az, el, up=(0, 0, 1), angle=30):
    view.CameraParallelProjection = 0
    view.CameraViewAngle = angle
    view.CameraFocalPoint = list(focal)
    view.CameraPosition = camera_position(focal, distance, az, el)
    view.CameraViewUp = list(up)


def caption(view, text: str, size: int):
    if not text:
        return None
    t = pv.Text(Text=text)
    d = pv.Show(t, view)
    trynames(d, Color=[0.9, 0.9, 0.92], FontSize=size, WindowLocation="Lower Left Corner")
    return t


def save(view, path: Path, size) -> None:
    pv.Render(view)
    pv.SaveScreenshot(str(path), view, ImageResolution=list(size))
    log(f"  wrote {path.name}")


# --------------------------------------------------------------------------- #
# Case loading
# --------------------------------------------------------------------------- #
def available(proxy, prop: str):
    """The values a reader property can take (region names, array names)."""
    try:
        return proxy.GetProperty(prop).Available
    except Exception:                          # noqa: BLE001
        return getattr(proxy, prop).Available


class Case:
    def __init__(self, foam: Path, use_mean: bool):
        self.foam = foam
        probe = pv.OpenFOAMReader(registrationName="probe", FileName=str(foam))
        probe.UpdatePipelineInformation()
        self.available = list(available(probe, "MeshRegions"))
        self.arrays = list(available(probe, "CellArrays"))
        ts = getattr(probe, "TimestepValues", [])
        self.times = [ts] if isinstance(ts, (int, float)) else list(ts)
        self.time = self.times[-1] if self.times else 0.0
        self.regions = pick_regions(self.available)
        self.vec = "UMean" if (use_mean and "UMean" in self.arrays) else "U"
        self.prs = "pMean" if (use_mean and "pMean" in self.arrays) else "p"
        pv.Delete(probe)

    def reader(self, names, name):
        r = pv.OpenFOAMReader(registrationName=name, FileName=str(self.foam))
        trynames(r, CaseType="Reconstructed Case", Decomposepolyhedra=0, SkipZeroTime=1)
        r.MeshRegions = list(names)
        r.CellArrays = [a for a in (self.vec, self.prs) if a in self.arrays]
        r.UpdatePipeline(self.time)
        return r


def to_points(src):
    """Cell data -> point data, so Calculator, slices and streamlines all find U and p."""
    for name in ("CellDatatoPointData", "CellDataToPointData"):
        f = getattr(pv, name, None)
        if f is not None:
            c = f(Input=src)
            trynames(c, ProcessAllArrays=1, PassCellData=1)
            return c
    return src


def cp_filter(src, prs: str, u_inf: float):
    """Pressure coefficient from OpenFOAM's kinematic pressure: Cp = p / (U^2 / 2)."""
    c = pv.Calculator(Input=to_points(src))
    c.Function = f"{prs}/{0.5 * u_inf * u_inf}"
    c.ResultArrayName = "Cp"
    return c


def speed_filter(src, vec: str):
    c = pv.Calculator(Input=to_points(src))
    c.Function = f"mag({vec})"
    c.ResultArrayName = "Speed"
    return c


def make_tracers(mesh, vec, bounds, density, max_length, upstream=0.15, levels=0):
    """Seed lines upstream of the wing at several heights; returns tube filters carrying 'Speed'."""
    tubes = []
    x0 = bounds[0] - upstream
    y_lo, y_hi = bounds[2] * 0.97, -0.003
    for z in seed_levels(bounds[5], levels if levels >= 2 else max(3, density // 6)):
        try:
            t = pv.StreamTracer(Input=mesh, SeedType="Line")
        except Exception:                      # noqa: BLE001
            t = pv.StreamTracer(Input=mesh, SeedType="High Resolution Line Source")
        t.Vectors = ["POINTS", vec]
        t.SeedType.Point1 = [x0, y_lo, z]
        t.SeedType.Point2 = [x0, y_hi, z]
        t.SeedType.Resolution = max(8, density)
        trynames(t, MaximumStreamlineLength=max_length, MaximumSteps=20000, IntegrationDirection="FORWARD")
        tube = pv.Tube(Input=t)
        trynames(tube, Radius=0.0028, NumberofSides=8)
        tubes.append(tube)
    return tubes


def speed_key(view, lut, rep, show=True):
    """A small horizontal colour key (bottom right) for the Speed colours."""
    colour_key(view, lut, rep, "Air speed (m/s)   blue = slow   yellow = car speed (50)   red = fast",
               [15.0, 30.0, 50.0, 70.0], show)


def colour_key(view, lut, rep, title, labels, show=True):
    """A small horizontal colour key at the bottom right."""
    if not show:
        return
    try:
        rep.SetScalarBarVisibility(view, True)
        sb = pv.GetScalarBar(lut, view)
        for loc in ("Any Location", "AnyLocation"):
            try:
                sb.WindowLocation = loc
                break
            except Exception:                  # noqa: BLE001
                continue
        trynames(sb, Orientation="Horizontal", Position=[0.62, 0.055], ScalarBarLength=0.34,
                 ScalarBarThickness=14, Title=title,
                 ComponentTitle="", TitleFontSize=15, LabelFontSize=14, UseCustomLabels=1,
                 CustomLabels=list(labels), AddRangeLabels=0, DrawTickMarks=1,
                 DrawTickLabels=1, DrawAnnotations=0)
    except Exception as e:                     # noqa: BLE001 - the key is a nicety, never fail a picture over it
        log(f"  (colour key skipped: {type(e).__name__}: {e})")


# --------------------------------------------------------------------------- #
# Scenes
# --------------------------------------------------------------------------- #
class Scenes:
    def __init__(self, case: Case, a):
        self.case, self.a = case, a
        self.size = (a.width, a.width * 9 // 16)
        self.out = Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.half = not a.full_model
        rg = case.regions
        if not rg["mesh"] or not rg["body"]:
            raise RuntimeError(f"Could not find the internal mesh and a body patch in {case.available}")
        self.mesh = case.reader([rg["mesh"]], "mesh")
        self.body = case.reader(rg["body"], "body")
        info = self.body.GetDataInformation().GetBounds()
        self.bounds = list(info)
        self.focal = [(info[0] + info[1]) / 2, 0.0 if self.half else (info[2] + info[3]) / 2, (info[4] + info[5]) / 2]
        self.span = (info[3] - info[2]) * (2 if self.half else 1)
        log(f"body bounds x {info[0]:.3f}..{info[1]:.3f}  y {info[2]:.3f}..{info[3]:.3f}  z {info[4]:.3f}..{info[5]:.3f}")
        self._tubes = None
        self._view = None

    # -- shared pieces ------------------------------------------------------- #
    def wing(self, view, color=GRAPHITE, by_cp=False):
        src = cp_filter(self.body, self.case.prs, self.a.speed) if by_cp else self.body
        d = pv.Show(mirror(src, self.half), view)
        d.Representation = "Surface"
        trynames(d, Specular=0.6, SpecularPower=40, Ambient=0.15, Diffuse=0.9)
        if by_cp:
            pv.ColorBy(d, ("POINTS", "Cp"))
            color_map("Cp", "Turbo", -2.5, 1.0)
            d.SetScalarBarVisibility(view, False)
        else:
            pv.ColorBy(d, None)
            d.DiffuseColor = color
        return d

    def tubes(self):
        if self._tubes is None:
            log("  tracing streamlines ...")
            sp = speed_filter(self.mesh, self.case.vec)
            self._tubes = make_tracers(sp, self.case.vec, self.bounds, self.a.density, self.a.stream_length, self.a.upstream, self.a.levels)
        return self._tubes

    def streamlines(self, view, lo=15.0, hi=70.0, mirrored=True):
        lut = color_map("Speed", "Turbo", lo, hi)
        last = None
        for t in self.tubes():
            d = pv.Show(mirror(t, self.half) if mirrored else t, view)
            pv.ColorBy(d, ("POINTS", "Speed"))
            trynames(d, Ambient=0.55, Diffuse=0.6)
            d.SetScalarBarVisibility(view, False)
            last = d
        if last is not None:
            speed_key(view, lut, last, self.a.key)
        return lut

    def view(self):
        v = make_view(self.size)
        trynames(v, ViewTime=self.case.time)
        caption(v, self.a.caption, max(10, self.size[0] // 70))
        self._view = v
        return v

    # -- the pictures -------------------------------------------------------- #
    def hero(self):
        v = self.view()
        self.wing(v)
        # Streamlines only on the far half (Y < 0) and the camera on the near side (Y > 0):
        # the wing is then in front of the flow, not hidden behind it.
        self.streamlines(v, mirrored=False)
        set_camera(v, [self.focal[0], 0.0, self.focal[2]], self.a.hero_distance * self.span / 1.8, az=self.a.hero_az, el=self.a.hero_el)
        save(v, self.out / "01_hero.png", self.size)

    def side(self):
        v = self.view()
        y = self.bounds[2] * 0.5                      # half-way along the half-span
        sl = pv.Slice(Input=speed_filter(self.mesh, self.case.vec))
        sl.SliceType = "Plane"
        sl.SliceType.Origin = [self.focal[0], y, 0.0]
        sl.SliceType.Normal = [0.0, 1.0, 0.0]
        sl.UpdatePipeline(self.case.time)
        sb = sl.GetDataInformation().GetBounds()
        log(f"  slice: {sl.GetDataInformation().GetNumberOfPoints()} points, bounds x {sb[0]:.2f}..{sb[1]:.2f} z {sb[4]:.2f}..{sb[5]:.2f}")
        log(f"  slice point arrays: {list(sl.PointData.keys())}; mesh cell arrays: {list(self.mesh.CellData.keys())}; mesh point arrays: {list(self.mesh.PointData.keys())}")
        if "Speed" in sl.PointData.keys():
            rng = sl.PointData["Speed"].GetRange()
            log(f"  Speed range on slice: {rng[0]:.2f} .. {rng[1]:.2f} m/s")
        else:
            log("  WARNING: no Speed array on the slice, so it will be drawn as a flat colour")
        d = pv.Show(sl, v)
        pv.ColorBy(d, ("POINTS", "Speed"))
        lut_side = color_map("Speed", "Turbo", 0.0, 75.0)
        d.SetScalarBarVisibility(v, False)
        speed_key(v, lut_side, d, self.a.key)
        v.CameraParallelProjection = 1
        v.CameraFocalPoint = [self.focal[0], y, 0.17]
        v.CameraPosition = [self.focal[0], y - 4.0, 0.17]
        v.CameraViewUp = [0, 0, 1]
        v.CameraParallelScale = 0.40
        save(v, self.out / "02_side_slice.png", self.size)

    def wake(self):
        v = self.view()
        self.wing(v)
        sp = speed_filter(self.mesh, self.case.vec)
        x_end = self.bounds[1]
        for dx in (0.20, 0.55, 1.00):
            sl = pv.Slice(Input=sp)
            sl.SliceType = "Plane"
            sl.SliceType.Origin = [x_end + dx, 0.0, 0.0]
            sl.SliceType.Normal = [1.0, 0.0, 0.0]
            d = pv.Show(mirror(sl, self.half), v)
            pv.ColorBy(d, ("POINTS", "Speed"))
            trynames(d, Opacity=0.88)
            d.SetScalarBarVisibility(v, False)
        lut_wake = color_map("Speed", "Turbo", 20.0, 62.0)
        speed_key(v, lut_wake, d, self.a.key)
        set_camera(v, [self.focal[0] + 0.5, 0.0, self.focal[2]], self.a.wake_distance * self.span / 1.8, az=-150, el=12)
        save(v, self.out / "03_wake_slices.png", self.size)

    def vortex(self):
        """Resampled, smoothed Q-criterion (two nested levels, coloured by pressure);
        falls back to the plain version if this ParaView will not do the resampling."""
        try:
            self.vortex_fine()
        except Exception as e:                 # noqa: BLE001
            log(f"  fine vortex scene failed ({type(e).__name__}: {e}); using the plain version")
            traceback.print_exc()
            if self._view is not None:
                try:
                    pv.Delete(self._view)
                except Exception:              # noqa: BLE001
                    pass
                self._view = None
            self.vortex_plain()

    def vortex_fine(self):
        v = self.view()
        self.wing(v)
        b = self.bounds
        res = self.a.vortex_res
        lo = [b[0] - 0.10, b[2] - 0.15, 0.0]
        hi = [b[1] + 1.60, 0.0, b[5] + 0.45]
        dims = [int((hi[i] - lo[i]) / res) + 1 for i in range(3)]
        log(f"  resampling the flow onto a {dims[0]} x {dims[1]} x {dims[2]} grid ({res * 1000:g} mm); this is the slow step")
        src = cp_filter(self.mesh, self.case.prs, self.a.speed)             # adds Cp, keeps U as point data
        rs = pv.ResampleToImage(Input=src)
        trynames(rs, UseInputBounds=0, SamplingBounds=[lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]], SamplingDimensions=dims)
        try:
            g = pv.Gradient(Input=rs)
        except Exception:                      # noqa: BLE001
            g = pv.GradientOfUnstructuredDataSet(Input=rs)
        g.ScalarArray = ["POINTS", self.case.vec]
        trynames(g, ComputeQCriterion=1, QCriterionArrayName="Q")
        g.UpdatePipeline(self.case.time)
        q = g.PointData["Q"].GetRange() if "Q" in g.PointData.keys() else (0, 0)
        log(f"  Q range {q[0]:.3g} .. {q[1]:.3g}; levels {self.a.q_level:g} (outer) and {self.a.q_level * 5:g} (core); change with --q-level")
        qname, g = self.clear_of_wall(g, res)
        cmin = self.a.vortex_cp_min
        lut = color_map("Cp", "Inferno (matplotlib)", cmin, 0.0, invert=True)   # strongest (lowest pressure) = brightest
        last = None
        for level, opacity in ((self.a.q_level, 0.35), (self.a.q_level * 5.0, 1.0)):
            c = pv.Contour(Input=g)
            c.ContourBy = ["POINTS", qname]
            c.Isosurfaces = [level]
            surf = c
            if "vtkValidPointMask" in g.PointData.keys():          # drop surfaces that touch the wing's own volume
                try:
                    t = pv.Threshold(Input=c)
                    t.Scalars = ["POINTS", "vtkValidPointMask"]
                    trynames(t, LowerThreshold=0.99, UpperThreshold=1.01, ThresholdMethod="Between")
                    trynames(t, ThresholdRange=[0.99, 1.01])
                    surf = t
                except Exception as e:         # noqa: BLE001
                    log(f"  (could not trim wall artefacts: {type(e).__name__}: {e})")
            d = pv.Show(mirror(surf, self.half), v)
            pv.ColorBy(d, ("POINTS", "Cp"))
            trynames(d, Specular=0.5, SpecularPower=30, Opacity=opacity)
            d.SetScalarBarVisibility(v, False)
            last = d
        colour_key(v, lut, last, "Vortex strength   dark = weak   bright = strong (lower pressure)",
                   [round(cmin, 2), round(cmin / 2, 2), 0.0], self.a.key)
        set_camera(v, [self.focal[0] + 0.3, 0.0, self.focal[2]], 2.0 * self.span / 1.8, az=-40, el=22)
        save(v, self.out / "04_vortex_cores.png", self.size)

    def clear_of_wall(self, g, res):
        """Zero Q within --wall-clear metres of the wing (and the road), where it is boundary-layer
        shear and not a vortex. Returns (array name, filter). Falls back to plain Q if it cannot."""
        if "vtkValidPointMask" not in g.PointData.keys() or self.a.wall_clear <= 0:
            return "Q", g
        n = max(1, int(math.ceil(self.a.wall_clear / res)))
        script = f"""
import numpy as np
from vtkmodules.numpy_interface import dataset_adapter as dsa
data = self.GetInputDataObject(0, 0)
out = self.GetOutputDataObject(0)
out.ShallowCopy(data)
nx, ny, nz = data.GetDimensions()
w = dsa.WrapDataObject(data)
q = np.asarray(w.PointData['Q'], dtype=float)
m = np.asarray(w.PointData['vtkValidPointMask']).reshape(nz, ny, nx) > 0
for _ in range({n}):
    p = np.pad(m, 1, mode='edge')
    m = (p[1:-1, 1:-1, 1:-1] & p[:-2, 1:-1, 1:-1] & p[2:, 1:-1, 1:-1] & p[1:-1, :-2, 1:-1]
         & p[1:-1, 2:, 1:-1] & p[1:-1, 1:-1, :-2] & p[1:-1, 1:-1, 2:])
wo = dsa.WrapDataObject(out)
wo.PointData.append(np.where(m.reshape(-1), q, 0.0), 'Qclear')
"""
        try:
            pf = pv.ProgrammableFilter(Input=g)
            pf.OutputDataSetType = "vtkImageData"
            pf.Script = script
            pf.UpdatePipeline(self.case.time)
            if "Qclear" not in pf.PointData.keys():
                raise RuntimeError("no Qclear array was produced")
            log(f"  wall clearance: zeroed Q within {n} cells ({n * res * 1000:g} mm) of the wing and road")
            return "Qclear", pf
        except Exception as e:                 # noqa: BLE001
            log(f"  (could not clear Q near the wall: {type(e).__name__}: {e}; using plain Q)")
            return "Q", g

    def vortex_plain(self):
        v = self.view()
        self.wing(v)
        try:
            g = pv.Gradient(Input=speed_filter(self.mesh, self.case.vec))
        except Exception:                      # noqa: BLE001
            g = pv.GradientOfUnstructuredDataSet(Input=speed_filter(self.mesh, self.case.vec))
        g.ScalarArray = ["POINTS", self.case.vec]
        trynames(g, ComputeQCriterion=1, QCriterionArrayName="Q")
        g.UpdatePipeline(self.case.time)
        q = g.PointData["Q"].GetRange() if "Q" in g.PointData.keys() else (0, 0)
        log(f"  Q range {q[0]:.3g} .. {q[1]:.3g}; iso level {self.a.q_level:g} (change with --q-level)")
        c = pv.Contour(Input=g)
        c.ContourBy = ["POINTS", "Q"]
        c.Isosurfaces = [self.a.q_level]
        trynames(c, ComputeScalars=0)
        d = pv.Show(mirror(c, self.half), v)
        pv.ColorBy(d, ("POINTS", "Speed"))
        color_map("Speed", "Turbo", 20.0, 65.0)
        trynames(d, Specular=0.4, Opacity=0.95)
        d.SetScalarBarVisibility(v, False)
        set_camera(v, [self.focal[0] + 0.3, 0.0, self.focal[2]], 2.0 * self.span / 1.8, az=-40, el=22)
        save(v, self.out / "04_vortex_cores.png", self.size)

    def under(self):
        v = self.view()
        self.wing(v, by_cp=True)
        set_camera(v, self.focal, 1.9 * self.span / 1.8, az=-30, el=-38)
        save(v, self.out / "05_underside_pressure.png", self.size)

    def ground(self):
        name = self.case.regions["ground"]
        if not name:
            raise RuntimeError(f"no ground patch found in {self.case.available}")
        v = self.view()
        g = cp_filter(self.case.reader([name], "ground"), self.case.prs, self.a.speed)
        d = pv.Show(mirror(g, self.half), v)
        pv.ColorBy(d, ("POINTS", "Cp"))
        color_map("Cp", "Cool to Warm", -1.5, 1.0)
        d.SetScalarBarVisibility(v, False)
        v.CameraParallelProjection = 1                      # look up at the road from underneath, flow to the right
        v.CameraFocalPoint = [self.focal[0] + 0.25, 0.0, 0.0]
        v.CameraPosition = [self.focal[0] + 0.25, 0.0, -4.0]
        v.CameraViewUp = [0, -1, 0]
        v.CameraParallelScale = 0.62
        save(v, self.out / "06_road_footprint.png", self.size)

    def orbit(self):
        ext = self.a.frame_format
        frames = self.out / "orbit_frames"
        if frames.exists():
            shutil.rmtree(frames)
        frames.mkdir(parents=True)
        v = self.view()
        self.wing(v)
        self.streamlines(v)
        n = max(2, int(round(self.a.seconds * self.a.fps))) if self.a.seconds > 0 else self.a.frames
        log(f"  orbit: {n} frames at {self.a.fps} fps = {n / self.a.fps:.1f} s for one full turn ({360.0 / (n / self.a.fps):.1f} degrees per second)")
        dist = self.a.orbit_distance * self.span / 1.8
        for i in range(n):
            set_camera(v, self.focal, dist, az=-35 + 360.0 * i / n, el=16 + 5 * math.sin(2 * math.pi * i / n))
            pv.Render(v)
            path = str(frames / f"frame_{i:05d}.{ext}")
            try:
                pv.SaveScreenshot(path, v, ImageResolution=list(self.size), Quality=95) if ext == "jpg" else \
                    pv.SaveScreenshot(path, v, ImageResolution=list(self.size))
            except TypeError:
                pv.SaveScreenshot(path, v, ImageResolution=list(self.size))
            if i % 100 == 0:
                log(f"  frame {i}/{n}")
        ff = shutil.which("ffmpeg")
        mp4 = self.out / "orbit.mp4"
        cmd = ["ffmpeg", "-y", "-framerate", str(self.a.fps), "-i", str(frames / f"frame_%05d.{ext}"),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", str(mp4)]
        if ff:
            subprocess.run(cmd, check=True)
            log(f"  wrote {mp4.name}")
            if self.a.keep_frames:
                log("  kept the frames (--keep-frames)")
            elif mp4.is_file() and mp4.stat().st_size > 10_000:
                shutil.rmtree(frames, ignore_errors=True)
                log("  deleted the orbit frames; the video is all you need (use --keep-frames to keep them)")
            else:
                log("  the video looks missing or empty, so the frames were NOT deleted")
        else:
            log("  ffmpeg not found: frames are in orbit_frames/. Install ffmpeg and run:\n    " + " ".join(cmd))


# --------------------------------------------------------------------------- #
def parse(argv):
    ap = argparse.ArgumentParser(description="Website pictures and an orbit video from a finished case.")
    ap.add_argument("--case", type=Path, required=True, help="the <name>.foam file in the job's case folder")
    ap.add_argument("--out", type=Path, default=Path("flow_images"), help="output folder")
    ap.add_argument("--scenes", default=",".join(ALL_SCENES), help=f"comma list from: {', '.join(ALL_SCENES)}")
    ap.add_argument("--list", action="store_true", help="print the regions and arrays in the case and stop")
    ap.add_argument("--quick", action="store_true", help="small, fast preview (960 px wide, 48 orbit frames)")
    ap.add_argument("--width", type=int, default=2400, help="picture width in pixels (16:9)")
    ap.add_argument("--speed", type=float, default=50.0, help="free-stream speed, m/s (for Cp)")
    ap.add_argument("--q-level", type=float, default=50000.0, help="Q-criterion iso level, 1/s^2 (PERRINN paper used 5e4)")
    ap.add_argument("--density", type=int, default=60, help="streamlines per seed line")
    ap.add_argument("--stream-length", type=float, default=4.0, help="max streamline length, m")
    ap.add_argument("--upstream", type=float, default=0.8, help="how far in front of the wing the streamlines start, m")
    ap.add_argument("--hero-distance", type=float, default=3, help="hero camera distance (smaller = closer)")
    ap.add_argument("--wake-distance", type=float, default=4.4, help="wake-slices camera distance (bigger = further back)")
    ap.add_argument("--hero-az", type=float, default=-70.0, help="hero camera angle round the wing: -90 = square on from the side, 0 = from the front")
    ap.add_argument("--hero-el", type=float, default=14.0, help="hero camera height angle, degrees")
    ap.add_argument("--levels", type=int, default=5, help="number of streamline sheets (heights)")
    ap.add_argument("--no-key", dest="key", action="store_false", help="leave the colour key off the speed pictures")
    ap.add_argument("--vortex-res", type=float, default=0.01, help="grid spacing for the vortex scene, m (smaller = more detail, slower, more memory)")
    ap.add_argument("--seconds", type=float, default=0.0, help="orbit length for one full turn, s (overrides --frames; frames = seconds x fps)")
    ap.add_argument("--orbit-distance", type=float, default=2.3, help="orbit camera distance (bigger = further back; the old fixed value was 1.9)")
    ap.add_argument("--frame-format", choices=("jpg", "png"), default="jpg", help="orbit frame file type (jpg is about 5x smaller on disk)")
    ap.add_argument("--keep-frames", action="store_true", help="keep the orbit frames after the video is made (default: delete them)")
    ap.add_argument("--vortex-cp-min", type=float, default=-1.5, help="pressure coefficient shown as the brightest vortex colour (-3 = only the strongest glow; -1.5 brightens weak ones)")
    ap.add_argument("--wall-clear", type=float, default=0.012, help="hide vortex surface within this distance of the wing, m (0 = off)")
    ap.add_argument("--frames", type=int, default=240, help="orbit frames (8 s at 30 fps)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--caption", default="", help="small text in the corner, e.g. 'PERRINN 2017 front wing - 180 km/h'")
    ap.add_argument("--full-model", action="store_true", help="the case is a full model: do not mirror")
    ap.add_argument("--instant", action="store_true", help="use U/p of the last iteration, not UMean/pMean if present")
    a, _ = ap.parse_known_args([x for x in argv if x != "--"])
    if a.quick:
        a.width, a.frames, a.density = 960, 48, 24
        a.vortex_res = max(a.vortex_res, 0.008)
    return a


def main(argv=None) -> int:
    if pv is None:
        print("This script must be run with ParaView's pvbatch (it needs the paraview module).", file=sys.stderr)
        return 2
    a = parse(sys.argv[1:] if argv is None else argv)
    if not a.case.is_file():
        print(f"ERROR: case file not found: {a.case}", file=sys.stderr)
        return 2
    case = Case(a.case, use_mean=not a.instant)
    log(f"regions: {case.available}")
    log(f"arrays: {case.arrays}; last time: {case.time}; using {case.vec} and {case.prs}")
    log(f"picked: mesh={case.regions['mesh']} body={case.regions['body']} ground={case.regions['ground']}")
    if a.list:
        return 0
    sc = Scenes(case, a)
    failed = []
    for name in [s.strip() for s in a.scenes.split(",") if s.strip()]:
        if name not in ALL_SCENES:
            print(f"unknown scene '{name}'", file=sys.stderr)
            failed.append(name)
            continue
        log(f"scene: {name}")
        try:
            getattr(sc, name)()
        except Exception:                      # noqa: BLE001 - one bad scene must not stop the rest
            failed.append(name)
            traceback.print_exc()
        if sc._view is not None:
            try:
                pv.Delete(sc._view)
            except Exception:                  # noqa: BLE001
                pass
            sc._view = None
    log("done" + (f"; FAILED scenes: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
