"""Reporting layer: plan digest + 2D drawing.

`floorplan_io` reads the JSON and rebuilds the derived geometry; this module
only turns a parsed plan into a JSON digest or a picture (mm geometry, y
flipped so the plan reads on screen). Nothing here is needed to *read* a plan,
and nothing here is protocol knowledge.

    from floorplan_io import load_floorplan
    import floorplan_report as report

    plan = load_floorplan("plan.json")
    print(json.dumps(report.summarize(plan), ensure_ascii=False))
    report.write_svg(plan, "plan.svg")

Other outputs (columns / furniture / verticals / axis grids / 3D GLB) are
10–30 line snippets over `plan.raw` and `plan.wall_slabs()`: see
references/recipes.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from floorplan_io import SOLID_WALL_TYPES, FloorPlan, HeightParams, OPENING_TYPES

# What each step of the pipeline actually calls, and what the geometry idea is.
# The CLI prints these lines after every action, so a reader can connect
# "平整的墙" / "一个房间面" to a concrete shapely or trimesh primitive.
PIPELINE: dict[str, tuple[str, str, str]] = {
    "scale": ("比例尺",
              "s = (math.hypot(a, b) + math.hypot(c, d)) / 2",
              "imageTransform 是相似变换矩阵，两列长度的均值就是 mm/px"),
    "wall": ("墙中线",
             "LineString([p, q])",
             "直线墙就是两端点连成的线段；曲线墙先按 Catmull-Rom 采样成折线"),
    "body": ("墙身",
             "line.buffer(half, join_style='mitre', cap_style='flat', quad_segs=16)",
             "缓冲 = 沿中线向两侧等距偏移出厚度；mitre 让转角斜接"),
    "trim": ("跨类型修整",
             "polygon.difference(更强类型的实心带)",
             "布尔差集（GEOS overlay）：轻墙被重墙截断，不重复计量"),
    "opening": ("门窗洞",
                "polygon.difference(洞口条带)",
                "条带由中线沿局部法向偏移 thickness/2 + ε 得到，再整体挖掉"),
    "face": ("房间面",
             "unary_union(线段) → polygonize(...)",
             "先节点化打断，再遍历平面图的环取有界面（与源码 DCEL 等价）"),
    "area": ("面积",
             "polygon.area",
             "GEOS 用鞋带公式算有向面积；m² = mm² / 1e6"),
    "extrude": ("3D 拉伸",
                "trimesh.creation.extrude_polygon(poly, height=z1 - z0)",
                "多边形沿 Z 拉伸成闭合三角网；MultiPolygon 先拆成单个 Polygon"),
}

WALL_FILL = {
    "exterior": "#2563EB",
    "brick_wall": "#B45309",
    "curtain_wall": "#0891B2",
    "interior": "#93A3B8",
    "railing": "#64748B",
    "level_change": "#A1A1AA",
}
ROOM_STROKE = "#334155"
OPENING_STROKE = {"door": "#DC2626", "window": "#059669"}


# ── digest ─────────────────────────────────────────────────────────────────

def summarize(plan: FloorPlan) -> dict[str, Any]:
    """JSON-serializable digest: counts, areas, adjacency, connectivity, issues."""
    heights = HeightParams()
    wall_rows: dict[str, dict[str, float]] = {}
    for wtype in SOLID_WALL_TYPES + ("level_change",):
        walls = [w for w in plan.walls.values() if w.type == wtype]
        if not walls:
            continue
        solid = plan.wall_solid(wtype)
        height = heights.railing_height if wtype == "railing" else heights.wall_height
        wall_rows[wtype] = {
            "count": len(walls),
            "length_m": sum(w.length_mm for w in walls) / 1000,
            "solid_area_m2": float(solid.area) / 1e6,
            "volume_m3": 0.0 if wtype == "level_change" else float(solid.area) * height / 1e9,
        }

    opening_rows: dict[str, dict[str, float]] = {}
    for otype in sorted(OPENING_TYPES):
        ops = [o for o in plan.openings.values() if o.type == otype]
        if not ops:
            continue
        opening_rows[otype] = {
            "count": len(ops),
            "total_width_m": sum(o.width_mm for o in ops) / 1000,
            "mean_width_mm": sum(o.width_mm for o in ops) / len(ops),
        }

    room_rows = [{
        "id": r.id, "label": r.label, "label_resolved": r.label_text,
        "preset_label": r.preset_label, "match": r.match,
        "area_m2": r.area_m2, "net_area_m2": r.net_area_m2,
        "doors": [o.id for o in r.doors], "windows": [o.id for o in r.windows],
    } for r in plan.rooms.values()]

    edges = plan.room_adjacency()
    parent = {rid: rid for rid, room in plan.rooms.items() if room.face}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for edge in edges:
        if edge.passable:
            ra, rb = find(edge.room_a.id), find(edge.room_b.id)
            if ra != rb:
                parent[ra] = rb
    clusters: dict[str, list[str]] = {}
    for rid in parent:
        clusters.setdefault(find(rid), []).append(rid)

    counts = {key: len(value or {}) for key, value in (
        ("nodes", plan.raw.get("nodes")), ("walls", plan.walls),
        ("openings", plan.openings), ("columns", plan.raw.get("columns")),
        ("rooms", plan.rooms), ("verticals", plan.raw.get("verticals")),
        ("furniture", plan.raw.get("furniture")), ("texts", plan.raw.get("texts")),
        ("axisGrids", plan.raw.get("axisGrids")))}

    return {
        "version": plan.version,
        "exportedAt": plan.exported_at,
        "frame": {
            "scale_mm_per_px": plan.frame.scale,
            "rotation_deg": plan.frame.rotation_deg,
            "image_size_px": [plan.frame.img_w, plan.frame.img_h],
            "calibrated": plan.frame.calibrated,
            "y_up": plan.frame.y_up,
        },
        "counts": counts,
        "walls": {
            "by_type": wall_rows,
            "total_length_m": sum(w.length_mm for w in plan.walls.values()) / 1000,
            "total_solid_area_m2": float(plan.wall_solid_all().area) / 1e6,
            "room_faces": len(plan.faces),
        },
        "openings": {
            "by_type": opening_rows,
            "exterior_doors": sum(1 for o in plan.openings.values() if o.outside and o.passable),
            "total_door_width_m": sum(o.width_mm for o in plan.openings.values()
                                      if o.passable) / 1000,
        },
        "rooms": {
            "count": len(plan.rooms),
            "matched": sum(1 for r in plan.rooms.values() if r.face is not None),
            "total_area_m2": sum(r.area_centerline_mm2 for r in plan.rooms.values()) / 1e6,
            "total_net_area_m2": sum(r.area_net_mm2 for r in plan.rooms.values()) / 1e6,
            "items": room_rows,
        },
        "adjacency": {
            "edges": len(edges),
            "passable": sum(1 for e in edges if e.passable),
            "pairs": [{"a": e.room_a.id, "b": e.room_b.id, "walls": list(e.shared_wall_ids),
                       "openings": list(e.opening_ids), "passable": e.passable,
                       "shared_length_mm": e.shared_length_mm} for e in edges],
        },
        "connectivity": {
            "components": [sorted(v) for v in clusters.values() if len(v) > 1],
            "isolated": sorted(v[0] for v in clusters.values() if len(v) == 1),
        },
        "issues": {
            "errors": sum(1 for i in plan.issues if i.level == "error"),
            "warnings": sum(1 for i in plan.issues if i.level == "warning"),
            "items": [i.to_dict() for i in plan.issues],
        },
    }

# ── 2D output ──────────────────────────────────────────────────────────────

def _rings(geom: Any) -> list[list[tuple[float, float]]]:
    """Outer + inner rings of a (Multi)Polygon as coordinate lists."""
    if geom.is_empty:
        return []
    polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    out: list[list[tuple[float, float]]] = []
    for poly in polys:
        out.append([(float(x), float(y)) for x, y in poly.exterior.coords])
        for interior in poly.interiors:
            out.append([(float(x), float(y)) for x, y in interior.coords])
    return out


def write_svg(plan: FloorPlan, path: str | Path, *, show_openings: bool = True) -> None:
    """Dependency-free 2D SVG: room tints, wall bodies, centerlines, openings."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    layout = []
    for wtype in SOLID_WALL_TYPES:
        geom = plan.wall_solid(wtype)
        if not geom.is_empty:
            layout.append(geom)
    layout.extend(f.polygon for f in plan.faces)
    for op in plan.openings.values():
        if not op.footprint.is_empty:
            layout.append(op.footprint)
        if op.center_points:
            layout.append(LineString(op.center_points))
    for wall in plan.walls.values():
        if not wall.centerline.is_empty:
            layout.append(wall.centerline)
    if not layout:
        raise ValueError("nothing to draw: the plan has no geometry")

    minx, miny, maxx, maxy = unary_union(layout).bounds
    pad = max(maxx - minx, maxy - miny) * 0.03 + 100
    minx, miny, maxx, maxy = minx - pad, miny - pad, maxx + pad, maxy + pad
    width, height = maxx - minx, maxy - miny

    def fx(x: float) -> float:
        return x - minx

    def fy(y: float) -> float:
        return maxy - y          # y-up mm -> y-down SVG, box-relative

    def path_d(geom: Any) -> str:
        parts = []
        for ring in _rings(geom):
            if len(ring) >= 3:
                parts.append("M " + " L ".join(f"{fx(x):.2f} {fy(y):.2f}"
                                               for x, y in ring) + " Z")
        return " ".join(parts)

    def polyline_d(points: Any) -> str:
        return " ".join(f"{fx(x):.2f},{fy(y):.2f}" for x, y in points)

    font = "Microsoft YaHei, Noto Sans CJK SC, sans-serif"
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.2f} {height:.2f}"',
        f' width="{width / 10:.0f}" height="{height / 10:.0f}">',
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
    ]
    for room in plan.rooms.values():                    # tints under the walls
        if room.face is None:
            continue
        out.append(f'<path d="{path_d(room.face.polygon)}" fill="{room.color or "#E2E8F0"}" '
                   f'fill-opacity="0.22" stroke="none"/>')
    for wtype in SOLID_WALL_TYPES:
        geom = plan.wall_solid(wtype)
        if geom.is_empty:
            continue
        out.append(f'<path d="{path_d(geom)}" fill="{WALL_FILL[wtype]}" fill-rule="evenodd" '
                   f'stroke="{WALL_FILL[wtype]}" stroke-width="1"/>')
    for wall in plan.walls.values():
        if not wall.centerline.is_empty:
            out.append(f'<polyline points="{polyline_d(wall.centerline.coords)}" fill="none" '
                       f'stroke="#94A3B8" stroke-width="1" stroke-dasharray="20 12"/>')
    if show_openings:
        for op in plan.openings.values():
            if not op.center_points:
                continue
            width_mm = max(20.0, (op.wall.thickness if op.wall else 200) * 0.45)
            out.append(f'<polyline points="{polyline_d(op.center_points)}" fill="none" '
                       f'stroke="{OPENING_STROKE.get(op.type, "#DC2626")}" '
                       f'stroke-width="{width_mm:.0f}" stroke-linecap="butt" stroke-opacity="0.9"/>')
    for room in plan.rooms.values():
        if room.face is None or not room.label_text:
            continue
        x, y = room.label_anchor_mm or room.face.centroid
        size = min(width, height) * 0.022
        out.append(f'<text x="{fx(x):.2f}" y="{fy(y):.2f}" font-size="{size:.1f}" '
                   f'font-family="{font}" text-anchor="middle" fill="#0F172A">'
                   f'{room.label_text}</text>')
    out.append("</svg>")
    Path(path).write_text("\n".join(out), encoding="utf-8")


def write_geojson(plan: FloorPlan, path: str | Path) -> None:
    """FeatureCollection in mm: wall bodies, centerlines, openings, rooms."""
    from shapely.geometry import mapping

    features = []
    for wtype in SOLID_WALL_TYPES:
        geom = plan.wall_solid(wtype)
        if geom.is_empty:
            continue
        features.append({"type": "Feature", "geometry": mapping(geom),
                         "properties": {"kind": "wall_solid", "wall_type": wtype,
                                        "area_m2": round(geom.area / 1e6, 6)}})
    for wall in plan.walls.values():
        if wall.centerline.is_empty:
            continue
        features.append({"type": "Feature", "geometry": mapping(wall.centerline),
                         "properties": {"kind": "wall_centerline", "id": wall.id,
                                        "wall_type": wall.type, "thickness_mm": wall.thickness,
                                        "length_mm": round(wall.length_mm, 3)}})
    for op in plan.openings.values():
        features.append({"type": "Feature", "geometry": mapping(op.footprint),
                         "properties": {"kind": "opening", "id": op.id,
                                        "opening_type": op.type,
                                        "width_mm": round(op.width_mm, 3),
                                        "passable": op.passable, "outside": op.outside,
                                        "wall_id": op.wall.id if op.wall else None,
                                        "z0_mm": op.z_range_mm[0], "z1_mm": op.z_range_mm[1]}})
    for room in plan.rooms.values():
        if room.face is None:
            continue
        features.append({"type": "Feature", "geometry": mapping(room.face.polygon),
                         "properties": {"kind": "room", "id": room.id,
                                        "label": room.label_text, "match": room.match,
                                        "area_m2": round(room.area_m2, 6),
                                        "net_area_m2": round(room.net_area_m2, 6),
                                        "doors": [o.id for o in room.doors],
                                        "windows": [o.id for o in room.windows]}})
    Path(path).write_text(json.dumps({"type": "FeatureCollection", "features": features},
                                     ensure_ascii=False, indent=1), encoding="utf-8")


def _compound_patch(geom: Any, **kwargs: Any) -> Any:
    """matplotlib PathPatch that keeps polygon holes open."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as MplPath

    paths = []
    for poly in ([geom] if geom.geom_type == "Polygon" else list(getattr(geom, "geoms", []))):
        for ring in [poly.exterior, *poly.interiors]:
            if len(ring.coords) >= 3:
                paths.append(MplPath(list(ring.coords), closed=True))
    if not paths:
        return None
    return PathPatch(MplPath.make_compound_path(*paths), **kwargs)


def write_png(plan: FloorPlan, path: str | Path, dpi: int = 150) -> None:
    """Matplotlib raster of the same layers (needs `uv sync --extra png`)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:                       # pragma: no cover
        raise ImportError("matplotlib is required for --png: uv sync --extra png") from exc

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
                                       "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(14, 10))
    for room in plan.rooms.values():
        if room.face is None:
            continue
        patch = _compound_patch(room.face.polygon, facecolor=room.color or "#E2E8F0",
                                alpha=0.22, edgecolor=room.color or ROOM_STROKE,
                                linewidth=0.8, zorder=1)
        if patch is not None:
            ax.add_patch(patch)
    for wtype in SOLID_WALL_TYPES:
        patch = _compound_patch(plan.wall_solid(wtype), facecolor=WALL_FILL[wtype],
                                edgecolor="none", zorder=2)
        if patch is not None:
            ax.add_patch(patch)
    for op in plan.openings.values():
        if not op.center_points:
            continue
        xs, ys = zip(*op.center_points)
        ax.plot(xs, ys, color=OPENING_STROKE.get(op.type, "#DC2626"), linewidth=2.5, zorder=4)
    for room in plan.rooms.values():
        if room.face is not None and room.label_text:
            x, y = room.label_anchor_mm or room.face.centroid
            ax.text(x, y, room.label_text, ha="center", va="center", fontsize=8, zorder=5)

    ax.set_aspect("equal")
    ax.set_facecolor("white")
    ax.grid(False)
    ax.set_title(f"floorplan-json · scale {plan.frame.scale:.2f} mm/px · "
                 f"{len(plan.rooms)} rooms · {len(plan.walls)} walls", fontsize=8)
    ax.tick_params(labelsize=6)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def write_glb(plan: FloorPlan, path: str | Path, params: HeightParams | None = None) -> None:
    """3D scene in GLB (mm, Z up): wall bodies + room volumes.

    Two trimesh primitives do all the work:

    * `trimesh.creation.extrude_polygon(polygon, height)` — pulls a 2D
      cross-section along Z into a closed triangle mesh;
    * `trimesh.util.concatenate(meshes)` — merges the z-slices of one wall
      type into a single node so the GLB stays one object per type.

    The z-slices come from `plan.wall_slabs()`, which already subtracts the
    openings per slice (a door hole does not run through the window band).
    """
    try:
        import trimesh
    except ImportError as exc:                       # pragma: no cover
        raise ImportError("trimesh is required for --glb: uv sync --extra glb") from exc

    params = params or HeightParams()
    groups: dict[str, list] = {}

    def add(polygon, z0: float, z1: float, name: str) -> None:
        polys = [polygon] if polygon.geom_type == "Polygon" else list(polygon.geoms)
        for poly in polys:
            if poly.is_empty or poly.area <= 1e-6 or z1 - z0 <= 1e-9:
                continue
            mesh = trimesh.creation.extrude_polygon(poly, height=float(z1 - z0))
            if z0:
                mesh.apply_translation((0.0, 0.0, float(z0)))
            groups.setdefault(name, []).append(mesh)

    for wtype in SOLID_WALL_TYPES:
        for slab in plan.wall_slabs(wtype, params):
            add(slab.polygon, slab.z0, slab.z1, f"walls:{wtype}")
    for rid, room in plan.rooms.items():
        if room.face is not None:
            add(room.face.polygon, 0.0, params.wall_height, f"rooms:{rid}")

    scene = trimesh.Scene()
    for name, meshes in groups.items():
        merged = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
        scene.add_geometry(merged, geom_name=name, node_name=name)
    scene.export(str(path))
