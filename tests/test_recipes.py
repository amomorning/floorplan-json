"""The `references/recipes.md` snippets, executed.

These are the outputs that used to live in floorplan_io.py (columns, furniture,
texts, axis grids, verticals, 3D). Running them here proves the claim the skill
makes: the parameter-only tables need no reader code — a few lines over
`plan.raw` + `plan.wall_slabs()` reproduce the same geometry.
"""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Point, Polygon

from floorplan_io import SOLID_WALL_TYPES, HeightParams, load_floorplan
from test_floorplan_io import IMG_H, S, apartment

# ── §10 柱 / 家具 / 文字 ───────────────────────────────────────────────────

def rect(cx: float, cy: float, w: float, h: float, rotation_deg: float) -> Polygon:
    """yoloGeometry.rectToCorners: editor rotation is degrees, CW on screen."""
    t = math.radians(rotation_deg)
    c, s = math.cos(t), math.sin(t)
    local = [(w / 2, h / 2), (-w / 2, h / 2), (-w / 2, -h / 2), (w / 2, -h / 2)]
    return Polygon([(cx + x * c - y * s, cy + x * s + y * c) for x, y in local])


def column_footprint(plan, rec) -> Polygon:
    cx, cy = plan.frame.pt(rec["x"], rec["y"])
    if rec.get("shape") == "circle":
        return Point(cx, cy).buffer(rec["width"] / 2, quad_segs=32)
    w = float(rec["width"])
    h = w if rec.get("shape") == "square" else float(rec.get("height") or w)
    return rect(cx, cy, w, h, float(rec.get("rotation") or 0))


def furniture_footprint(plan, rec) -> Polygon:
    cx, cy = plan.frame.pt(rec["x"], rec["y"])
    return rect(cx, cy, float(rec["width"]), float(rec["height"]),
                float(rec.get("rotation") or 0))


def text_anchor(plan, rec):
    return plan.frame.pt(rec["x"], rec["y"])


# ── §11 竖向构件包围盒（VerticalGeometryService + StairCalculator） ────────

TARGET_RISER, MIN_TREAD, MIN_LANDING = 165.0, 260.0, 1200.0


def stair_steps(floor_height: float, override, minimum: int, even: bool = False) -> int:
    n = int(override) if override else int(round(floor_height / TARGET_RISER))
    n = max(n, minimum)
    return n + 1 if (even and n % 2) else n


def vertical_bbox(rec) -> tuple[float, float]:
    kind = rec.get("kind")
    if kind == "elevator":
        return (rec["shaftWidth"], rec["shaftDepth"])
    if kind == "ramp":
        return (rec["rampLength"], rec["rampWidth"])
    if kind == "escalator":                       # 30° incline + 2 × 2500 landing
        return (2 * 2500 + rec["floorHeight"] / math.tan(math.pi / 6), rec["width"])
    width, length = rec["stairwellWidth"], rec["stairwellLength"]
    if kind == "stair_u":
        gap = rec.get("stairwellGap", 100)
        total = stair_steps(rec["floorHeight"], rec.get("stepCountOverride"), 4, even=True)
        flight = (width - gap) / 2
        landing = max(flight, MIN_LANDING)
        tread = max((length - landing) / (total / 2), MIN_TREAD)
        return (flight * 2 + gap, total / 2 * tread + landing)
    total = stair_steps(rec["floorHeight"], rec.get("stepCountOverride"),
                        1 if kind == "steps" else 3)
    landing = max(width, MIN_LANDING) if kind == "stair_straight" else 0.0
    tread = max((length - landing) / total, MIN_TREAD)
    return (tread * total, width)


# ── §12 轴网 ───────────────────────────────────────────────────────────────

def axis_lines(plan, grid) -> list[list[tuple[float, float]]]:
    """`horizontalAxes` are u (vertical lines ①②③), `verticalAxes` are v
    (horizontal lines A B C); positions live in the grid's own frame."""
    angle = float(grid.get("rotation") or 0)
    c, s = math.cos(angle), math.sin(angle)
    ox, oy = grid.get("gridOffsetX", 0), grid.get("gridOffsetY", 0)
    ext = grid.get("extent") or {"minU": -5000, "maxU": 5000,
                                "minV": -5000, "maxV": 5000}

    def world(u: float, v: float) -> tuple[float, float]:
        return plan.frame.pt(ox + u * c - v * s, oy + u * s + v * c)

    lines = [[world(u, ext["minV"]), world(u, ext["maxV"])]
             for u in grid.get("horizontalAxes") or []]
    lines += [[world(ext["minU"], v), world(ext["maxU"], v)]
              for v in grid.get("verticalAxes") or []]
    return lines


def axis_bays_mm(plan, grid) -> dict[str, list[float]]:
    out = {}
    for key in ("horizontalAxes", "verticalAxes"):
        vals = sorted(grid.get(key) or [])
        out[key] = [(b - a) * plan.frame.scale for a, b in zip(vals, vals[1:])]
    return out


# ── §13 3D：按洞口高度分段拉伸 ────────────────────────────────────────────

def extrude(polygon, z0: float, z1: float) -> list:
    """trimesh extrusion of a Polygon / MultiPolygon between two Z levels."""
    import trimesh

    polys = [polygon] if polygon.geom_type == "Polygon" else list(polygon.geoms)
    meshes = []
    for poly in polys:
        mesh = trimesh.creation.extrude_polygon(poly, height=z1 - z0)
        if z0:
            mesh.apply_translation((0.0, 0.0, z0))
        meshes.append(mesh)
    return meshes


def extrude_scene(plan, params: HeightParams | None = None):
    import trimesh

    params = params or HeightParams()
    groups: dict[str, list] = {}
    for wtype in SOLID_WALL_TYPES:
        for slab in plan.wall_slabs(wtype, params):
            groups.setdefault(f"walls:{wtype}", []).extend(
                extrude(slab.polygon, slab.z0, slab.z1))
    for rid, room in plan.rooms.items():
        if room.face is not None:
            groups[f"rooms:{rid}"] = extrude(room.face.polygon, 0.0, params.wall_height)
    scene = trimesh.Scene()
    for name, meshes in groups.items():
        merged = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
        scene.add_geometry(merged, geom_name=name, node_name=name)
    return scene


# ── assertions ─────────────────────────────────────────────────────────────

def test_column_and_furniture_footprints():
    plan = load_floorplan(apartment())

    col = column_footprint(plan, plan.raw["columns"]["c1"])   # 400 x 600, rotated 90°
    assert col.area == pytest.approx(400 * 600)
    xmin, ymin, xmax, ymax = col.bounds
    assert (xmax - xmin, ymax - ymin) == pytest.approx((600.0, 400.0))

    bed = furniture_footprint(plan, plan.raw["furniture"]["f1"])
    assert bed.area == pytest.approx(1800 * 2000)


def test_text_anchor():
    plan = load_floorplan(apartment())
    assert text_anchor(plan, plan.raw["texts"]["t1"]) == pytest.approx(
        (1500.0, (IMG_H - 70) * S))


def test_vertical_bboxes():
    plan = load_floorplan(apartment())
    assert vertical_bbox(plan.raw["verticals"]["v1"]) == pytest.approx((2000, 2200))
    # straight stair: 18 steps, tread floored at 260 mm -> 4680 x 1500 mm
    assert vertical_bbox(plan.raw["verticals"]["v2"]) == pytest.approx((4680.0, 1500.0))


def test_axis_bays_and_rotated_grid_lines():
    plan = load_floorplan(apartment())
    grid = plan.raw["axisGrids"]["g1"]
    assert axis_bays_mm(plan, grid) == {"horizontalAxes": [300.0, 450.0],
                                        "verticalAxes": []}

    rotated = dict(grid, rotation=math.pi / 2, gridOffsetX=100, gridOffsetY=100,
                   horizontalAxes=[0, 100], verticalAxes=[0, 50],
                   extent={"minU": 0, "maxU": 100, "minV": 0, "maxV": 50})
    lines = axis_lines(plan, rotated)
    assert len(lines) == 4
    # rotation 90°: u maps to +y(px) — in the y-up mm frame the constant-u
    # lines (vertical on the drawing) become horizontal.
    ys = {round(y, 6) for _, y in lines[0]}
    assert len(ys) == 1


def test_3d_extrusion_from_slabs_is_watertight():
    pytest.importorskip("trimesh")
    plan = load_floorplan(apartment())
    params = HeightParams()
    scene = extrude_scene(plan, params)

    meshes = [g for g in scene.geometry.values() if getattr(g, "vertices", None) is not None]
    assert meshes and all(m.is_watertight for m in meshes)
    assert all(m.volume > 0 for m in meshes)

    def slab_volume(slabs) -> float:
        return sum(s.area_mm2 * (s.z1 - s.z0) for s in slabs)

    assert scene.geometry["walls:interior"].volume == pytest.approx(
        slab_volume(plan.wall_slabs("interior", params)))
    assert scene.geometry["walls:exterior"].volume == pytest.approx(
        slab_volume(plan.wall_slabs("exterior", params)))
    assert scene.geometry["rooms:r1"].volume == pytest.approx(
        plan.rooms["r1"].face.area * params.wall_height)
