"""FloorPlan JSON reader — the minimal must-replicate core (floorplan-json skill).

The export stores parameters, not geometry: wall chains, curved centerlines,
mitred wall bodies, opening cut strips and room faces all have to be recomputed.
That recomputation is what this module implements, in the order the source
(TypeScript) runs it:

    TransformService          imageTransform -> mm/px scale
    WallGeometryService       sampled centerlines (centripetal Catmull-Rom)
    WallBufferService         chains, buffered bands, cross-type trims
    OpeningGeometryService    arc-fraction placement + cut strips
    RoomService               faces (polygonize), wallIds -> room matching
    ifcModel                  z-split wall slabs (heights from ifcParams)

Parameter-only tables (`columns`, `furniture`, `verticals`, `texts`,
`axisGrids`) are NOT parsed here on purpose: their footprints are 10–30 line
snippets over `plan.raw` — see references/recipes.md.

Conventions (references/protocol.md; do not change):
* positions are background-image PIXELS, origin top-left, Y DOWN;
* every stored SIZE (thickness, opening width, ...) is MILLIMETRES;
* `imageTransform` -> scale = (hypot(a, b) + hypot(c, d)) / 2, identity matrix
  means "uncalibrated" on a scan and "1 world unit = 1 mm" on a blank canvas;
* emitted geometry is MILLIMETRES, Y UP: x_mm = x·s, y_mm = (imgH − y)·s.

    from floorplan_io import load_floorplan
    plan = load_floorplan("floorplan.json")
    plan.wall_solid("exterior", cut_openings=False).area / 1e6   # m²
    plan.rooms["<id>"].area_m2            # centerline area (editor sidebar)
    plan.rooms["<id>"].net_area_m2        # wall thickness deducted
    plan.room_adjacency()                 # room pairs + shared walls + doors
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, polylabel, unary_union

__all__ = [
    "DEFAULT_IFC_PARAMS",
    "DEFAULT_SEGMENTS",
    "DEFAULT_TENSION",
    "EXTERIOR_WALL_TYPES",
    "OPENING_TYPES",
    "PASSABLE_OPENING_TYPES",
    "ROOM_PRESETS",
    "SOLID_WALL_TYPES",
    "WALL_TRIM_ORDER",
    "Adjacency",
    "Face",
    "FloorPlan",
    "Frame",
    "HeightParams",
    "Issue",
    "OpeningGeom",
    "RoomGeom",
    "Slab",
    "WallGeom",
    "load_floorplan",
    "parse_floorplan",
    "repair_mojibake",
]

Point2 = tuple[float, float]

# Catmull-Rom / buffer parameters (src/utils/curve.ts, WallBufferService).
DEFAULT_SEGMENTS = 16          # samples per control edge
DEFAULT_TENSION = 0.95
MAX_TENSION = 1.5
MITRE_LIMIT = 5                # jSTS BufferParameters mitre limit
QUAD_SEGS = 16                 # jSTS ARC_STEPS
CAP_EPS_PX = 0.5               # opening cut extension, in world (px) units

# Strength order: a type is trimmed back to everything ABOVE it.
WALL_TRIM_ORDER = ("exterior", "brick_wall", "curtain_wall", "interior")
SOLID_WALL_TYPES = WALL_TRIM_ORDER + ("railing",)
WALL_TYPES = SOLID_WALL_TYPES + ("level_change",)
# Walls on the envelope: an opening here leads outside.
EXTERIOR_WALL_TYPES = frozenset({"exterior", "curtain_wall", "brick_wall", "railing"})

# RoomService.PASSABLE_OPENING_TYPES — windows are NOT passable.
PASSABLE_OPENING_TYPES = frozenset({"door", "door_double", "sliding", "revolving", "opening"})
OPENING_TYPES = PASSABLE_OPENING_TYPES | {"window"}

# src/utils/roomPresets.ts — the colour IS the room class.
ROOM_PRESETS: dict[str, str] = {
    "#D6FC9C": "户外", "#F2DD72": "窗户", "#8DCBEB": "卧室", "#E085F2": "客厅",
    "#F2793D": "门", "#8585CC": "功能房", "#46A770": "厨房", "#D9414E": "走廊",
    "#3D58B3": "卫生间",
}

# src/services/exporters/ifcParams.ts — heights are EXPORT-TIME assumptions.
DEFAULT_IFC_PARAMS: dict[str, float] = {
    "wall_height": 3000.0, "railing_height": 1100.0, "door_height": 2100.0,
    "window_sill": 900.0, "window_height": 1500.0,
}


def repair_mojibake(text: str | None) -> str | None:
    """Undo the classic GBK-bytes-read-as-cp1252 room label.

    Upstream sometimes reads a GBK label with a Latin-1/cp1252 decoder, so 卧室
    (CE D4 CA D2) arrives as 'ÎÔÊÒ'. Deliberately conservative: only pure
    high-byte strings qualify, so real Latin names such as 'Café' (ASCII in
    there) are never touched, and the rewrite only sticks when it lands on CJK.
    """
    if not text or len(text) < 2:
        return text
    if any(ord(ch) < 0xA0 for ch in text):
        return text
    try:
        fixed = text.encode("cp1252").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if "\ufffd" in fixed or not any("\u4e00" <= ch <= "\u9fff" for ch in fixed):
        return text
    return fixed


# ── polyline helpers (pure; point = tuple[float, float] in mm) ──────────────

def _pt2(p: Sequence[float]) -> Point2:
    return (float(p[0]), float(p[1]))


def signed_area(ring: Sequence[Point2]) -> float:
    """RoomService.signedArea convention (> 0 = CCW)."""
    s = 0.0
    for i, a in enumerate(ring):
        b = ring[(i + 1) % len(ring)]
        s += a[0] * b[1] - b[0] * a[1]
    return s / 2.0


def normalize_ring(ring: Sequence[Point2], ccw: bool = True) -> list[Point2]:
    pts = [_pt2(p) for p in ring]
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) < 1e-9:
        pts.pop()
    if (signed_area(pts) < 0) == ccw:
        pts.reverse()
    return pts


def polygon_from_ring(ring: Iterable[Sequence[float]]) -> Polygon:
    """Normalized polygon; empty input -> empty Polygon."""
    pts = [_pt2(p) for p in ring]
    if len(pts) < 3:
        return Polygon()
    poly = Polygon(pts)
    return poly if poly.is_valid else poly.buffer(0)


def catmull_rom(points: Sequence[Sequence[float]], tension: float = DEFAULT_TENSION,
                segments: int = DEFAULT_SEGMENTS, closed: bool = False) -> list[Point2]:
    """Centripetal Catmull-Rom (α = 0.5), port of src/utils/curve.ts.

    Non-final edges emit `segments - 1` samples, the final edge `segments + 1`
    (closed chains differ: see the caller), so the per-wall spans line up.
    """
    pts = [_pt2(p) for p in points]
    segments = max(1, int(segments))
    n = len(pts)
    if n < 2:
        return pts
    if n == 2:
        return [(pts[0][0] + (pts[1][0] - pts[0][0]) * i / segments,
                 pts[0][1] + (pts[1][1] - pts[0][1]) * i / segments)
                for i in range(segments + 1)]

    def get(i: int) -> Point2:
        if closed:
            return pts[i % n]
        return pts[max(0, min(n - 1, i))]

    k = tension / 3.0
    out: list[Point2] = []
    seg_count = n if closed else n - 1
    for s in range(seg_count):
        p0, p1, p2, p3 = get(s - 1), get(s), get(s + 1), get(s + 2)
        d01 = math.dist(p0, p1) ** 0.5
        d12 = math.dist(p1, p2) ** 0.5
        d23 = math.dist(p2, p3) ** 0.5
        w1 = d12 / (d01 + d12) if d01 + d12 > 1e-9 else 0.0
        w2 = d12 / (d12 + d23) if d12 + d23 > 1e-9 else 0.0
        c1 = (p1[0] + (p2[0] - p0[0]) * k * w1, p1[1] + (p2[1] - p0[1]) * k * w1)
        c2 = (p2[0] - (p3[0] - p1[0]) * k * w2, p2[1] - (p3[1] - p1[1]) * k * w2)
        steps = segments if (not closed and s == seg_count - 1) else segments - 1
        for i in range(steps + 1):
            t = i / segments
            t2, t3 = t * t, t * t * t
            inv = 1.0 - t
            inv2, inv3 = inv * inv, inv * inv * inv
            out.append((
                inv3 * p1[0] + 3 * inv2 * t * c1[0] + 3 * inv * t2 * c2[0] + t3 * p2[0],
                inv3 * p1[1] + 3 * inv2 * t * c1[1] + 3 * inv * t2 * c2[1] + t3 * p2[1],
            ))
    return out


def polyline_length(pts: Sequence[Point2]) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def polyline_point_at_arc(pts: Sequence[Point2], arc: float) -> tuple[Point2, Point2] | None:
    """Point + unit tangent at an arc position. NOT clamped at the end: the
    editor lets an opening run past the wall tip (polylinePointAtArc)."""
    if len(pts) < 2:
        return None
    if arc < 0:
        dx, dy = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
        length = math.hypot(dx, dy)
        if length < 1e-12:
            return None
        t = arc / length
        return (pts[0][0] + dx * t, pts[0][1] + dy * t), (dx / length, dy / length)
    remaining = arc
    for i in range(len(pts) - 1):
        dx, dy = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        length = math.hypot(dx, dy)
        if length < 1e-12:
            continue
        if remaining <= length or i + 2 == len(pts):
            t = remaining / length
            return (pts[i][0] + dx * t, pts[i][1] + dy * t), (dx / length, dy / length)
        remaining -= length
    return None


def polyline_slice(pts: Sequence[Point2], s0: float, s1: float) -> list[Point2]:
    """Clamped sub-polyline between two arc positions (polylineSlice)."""
    if len(pts) < 2:
        return []
    total = polyline_length(pts)
    a, b = max(0.0, min(s0, total)), max(0.0, min(s1, total))
    if b - a < 1e-9:
        return []
    out = [polyline_point_at_arc(pts, a)[0]]  # type: ignore[index]
    cum = 0.0
    for i in range(1, len(pts) - 1):
        cum += math.dist(pts[i - 1], pts[i])
        if a + 1e-9 < cum < b - 1e-9:
            out.append(pts[i])
    out.append(polyline_point_at_arc(pts, b)[0])  # type: ignore[index]
    return out


def symbol_normals(pts: Sequence[Point2], fallback: Point2) -> list[Point2]:
    """Per-sample left-hand normals (OpeningGeometryService.symbolNormals)."""
    out: list[Point2] = []
    for i in range(len(pts)):
        prev, nxt = pts[max(0, i - 1)], pts[min(len(pts) - 1, i + 1)]
        dx, dy = nxt[0] - prev[0], nxt[1] - prev[1]
        length = math.hypot(dx, dy)
        out.append(fallback if length < 1e-12 else (-dy / length, dx / length))
    return out


def strip_polygon(center: Sequence[Point2], half_width: float, fallback_normal: Point2) -> Polygon:
    """Band offset along LOCAL normals — opening cut strip, curve-wall band."""
    if len(center) < 2 or half_width <= 0:
        return Polygon()
    normals = symbol_normals(center, fallback_normal)
    outer = [(p[0] + n[0] * half_width, p[1] + n[1] * half_width) for p, n in zip(center, normals)]
    inner = [(p[0] - n[0] * half_width, p[1] - n[1] * half_width) for p, n in zip(center, normals)]
    return polygon_from_ring(outer + list(reversed(inner)))


def min_area_rect(points: Sequence[Sequence[float]]) -> Polygon:
    """Rotating-calipers rectangle around a point cloud (yoloGeometry.minAreaRect)."""
    pts = [_pt2(p) for p in points]
    if len(pts) < 3:
        return Polygon()
    geom = shapely.minimum_rotated_rectangle(shapely.MultiPoint(pts).convex_hull)
    return geom if isinstance(geom, Polygon) else Polygon()


def label_anchor(polygon: Polygon, ring: Sequence[Point2], centroid: Point2) -> Point2:
    """RoomService.labelAnchorForRing: centroid when inside, else the pole of
    inaccessibility (shapely.ops.polylabel)."""
    if polygon.contains(Point(centroid)) or polygon.touches(Point(centroid)):
        return centroid
    try:
        pt = polylabel(polygon, tolerance=max(0.5, min(10.0, polygon.length / 1000)))
        return (float(pt.x), float(pt.y))
    except (shapely.errors.GEOSException, ValueError):
        return centroid


# ── records ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HeightParams:
    """3D extrusion heights (mm); defaults mirror ifcParams.ts."""
    wall_height: float = DEFAULT_IFC_PARAMS["wall_height"]
    railing_height: float = DEFAULT_IFC_PARAMS["railing_height"]
    door_height: float = DEFAULT_IFC_PARAMS["door_height"]
    window_sill: float = DEFAULT_IFC_PARAMS["window_sill"]
    window_height: float = DEFAULT_IFC_PARAMS["window_height"]


@dataclass(frozen=True)
class Frame:
    """px -> mm frame resolved from `imageTransform` + `metadata`."""
    scale: float            # mm per background pixel
    rotation_deg: float
    img_w: float
    img_h: float
    calibrated: bool
    y_up: bool = True

    def pt(self, x: float, y: float) -> Point2:
        """Node position (px) -> mm. y_up: y_mm = (imgH - y) * scale."""
        return (x * self.scale, (self.img_h - y) * self.scale if self.y_up else y * self.scale)

    def ring(self, points: Iterable[Mapping[str, float]]) -> list[Point2]:
        return [self.pt(p["x"], p["y"]) for p in points]

    def aligned_axes(self) -> tuple[Point2, Point2]:
        """Unit vectors of the calibrated grid axes in mm space."""
        theta = math.radians(self.rotation_deg)
        cos, sin = math.cos(theta), math.sin(theta)
        return (cos, sin), (-sin, cos)

    def uv_from_px(self, x: float, y: float) -> Point2:
        """Position (px) in the calibrated grid frame, mm."""
        theta = math.radians(self.rotation_deg)
        cos, sin = math.cos(theta), math.sin(theta)
        return ((x * cos + y * sin) * self.scale, (-x * sin + y * cos) * self.scale)

    def to_grid(self, geom: Any) -> Any:
        """Rotate emitted mm geometry into the calibrated grid frame.

        The reader emits geometry in the background-image frame, so a rotated
        `imageTransform` (`rotation_deg != 0`) makes plans come out tilted.
        Rotating by -rotation_deg about the origin aligns walls with the grid
        axes; this is presentation only, areas are unchanged.
        """
        if abs(self.rotation_deg) < 1e-9:
            return geom
        from shapely import affinity

        return affinity.rotate(geom, -self.rotation_deg, origin=(0.0, 0.0))


@dataclass
class Issue:
    level: str          # 'error' | 'warning' | 'info'
    code: str
    message: str
    entity: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "entity": self.entity,
                "message": self.message}


@dataclass
class WallGeom:
    record: dict[str, Any]
    id: str
    type: str
    thickness: float                     # mm
    start_node: str
    end_node: str
    curve_mode: str
    chain_id: int
    centerline: LineString               # mm, y-up
    length_mm: float
    band: Polygon                        # per-wall band (straight: OBB)
    opening_ids: list[str] = field(default_factory=list)

    @property
    def is_straight(self) -> bool:
        return self.curve_mode != "curve"


@dataclass
class OpeningGeom:
    record: dict[str, Any]
    id: str
    type: str
    wall: WallGeom | None
    width_mm: float
    position: float
    direction: int
    hinge_side: str | None
    span_wall_ids: tuple[str, ...]
    footprint: Polygon                   # mm, cut strip through the wall band
    center_points: list[Point2]
    start_mm: Point2 | None
    end_mm: Point2 | None
    wall_normal: Point2
    z_range_mm: tuple[float, float]
    passable: bool
    outside: bool
    effective_width_mm: float = 0.0      # width actually inside the wall span
    clamped: bool = False                # effective < declared width (overrun/lost)
    room_ids: list[str] = field(default_factory=list)

    def z_range(self, params: HeightParams) -> tuple[float, float]:
        if self.type == "window":
            return (params.window_sill, params.window_sill + params.window_height)
        return (0.0, params.door_height)


@dataclass
class Face:
    """A bounded face of the wall-centerline planar graph (RoomService.RoomFace)."""
    id: str
    wall_ids: tuple[str, ...]
    polygon: Polygon                     # mm
    ring: list[Point2]
    area_mm2: float
    centroid: Point2
    label_anchor: Point2

    @property
    def area_m2(self) -> float:
        return self.area_mm2 / 1e6

    @property
    def area(self) -> float:
        """Polygon area in mm² (same as area_mm2)."""
        return self.area_mm2


@dataclass
class RoomGeom:
    record: dict[str, Any]
    id: str
    label: str | None
    color: str | None
    preset_label: str | None
    match: str | None                    # 'exact' | 'tolerant' | 'custom' | None
    face: Face | None
    area_centerline_mm2: float
    area_net_mm2: float
    label_anchor_mm: Point2 | None
    wall_ids: tuple[str, ...]
    doors: list[OpeningGeom] = field(default_factory=list)
    windows: list[OpeningGeom] = field(default_factory=list)
    openings: list[OpeningGeom] = field(default_factory=list)

    @property
    def area_m2(self) -> float:
        return self.area_centerline_mm2 / 1e6

    @property
    def net_area_m2(self) -> float:
        return self.area_net_mm2 / 1e6

    @property
    def label_text(self) -> str | None:
        """`label`, with GBK-as-cp1252 mojibake repaired; a label carrying
        U+FFFD lost its bytes upstream (irreversible), and the colour-derived
        preset is then the only trustworthy semantic."""
        label = repair_mojibake(self.label)
        if label and "\ufffd" not in label:
            return label
        return self.preset_label or label


@dataclass
class Adjacency:
    room_a: RoomGeom
    room_b: RoomGeom
    shared_wall_ids: tuple[str, ...]
    opening_ids: tuple[str, ...]
    passable: bool
    shared_length_mm: float


@dataclass
class Slab:
    """One z-slice of a wall solid (ifcModel: openings split the extrusion)."""
    wall_type: str
    z0: float
    z1: float
    polygon: Polygon
    area_mm2: float


@dataclass
class _Chain:
    index: int
    wall_ids: list[str]
    node_ids: list[str]
    thickness: float
    curve_mode: str


def _build_chains(walls: Sequence[WallGeom], nodes: Mapping[str, Mapping[str, float]],
                  start_index: int = 0) -> list[_Chain]:
    """WallBufferService.buildWallChains, on ONE wall type at a time:
    key = (thickness, curveMode), continue only through degree-2 nodes."""
    wall_map = {w.id: w for w in walls}
    adj: dict[str, list[tuple[str, str]]] = {}
    for w in walls:
        if w.start_node not in nodes or w.end_node not in nodes:
            continue
        adj.setdefault(w.start_node, []).append((w.id, w.end_node))
        adj.setdefault(w.end_node, []).append((w.id, w.start_node))

    visited: set[str] = set()
    chains: list[_Chain] = []
    for w in walls:
        if w.id in visited or w.start_node not in nodes or w.end_node not in nodes:
            continue
        key = (w.thickness, w.curve_mode)
        visited.add(w.id)
        node_ids, wall_ids = [w.start_node, w.end_node], [w.id]
        for side in ("end", "start"):
            frontier = w.end_node if side == "end" else w.start_node
            while len(adj.get(frontier, ())) == 2:
                nxt = next((e for e in adj[frontier] if e[0] not in visited), None)
                if nxt is None:
                    break
                nw = wall_map.get(nxt[0])
                if nw is None or (nw.thickness, nw.curve_mode) != key:
                    break
                visited.add(nxt[0])
                if side == "end":
                    node_ids.append(nxt[1])
                    wall_ids.append(nxt[0])
                else:
                    node_ids.insert(0, nxt[1])
                    wall_ids.insert(0, nxt[0])
                frontier = nxt[1]
        chains.append(_Chain(start_index + len(chains), wall_ids, node_ids,
                             w.thickness, w.curve_mode))
    return chains


# ═══════════════════════════════════════════════════════════════════════════
# Main reader
# ═══════════════════════════════════════════════════════════════════════════

class FloorPlan:
    """Parsed, analysable FloorPlan JSON. Build it with load_floorplan()."""

    def __init__(self, data: Mapping[str, Any], *, y_up: bool = True) -> None:
        if not isinstance(data, Mapping):
            raise TypeError("FloorPlan JSON root must be an object")
        self.raw: Mapping[str, Any] = data
        self.version = str(data.get("version", ""))
        self.exported_at = str(data.get("exportedAt", ""))
        self.issues: list[Issue] = []
        self.frame = self._read_frame(data, y_up)

        self._nodes_px: dict[str, tuple[float, float]] = {}
        self._node_records: dict[str, dict[str, Any]] = {}
        self._chains: list[_Chain] = []
        self._spans: dict[str, list[Point2]] = {}
        self._solids_cache: dict[bool, dict[str, Polygon]] = {}
        # adjacency is O(faces^2 * walls); a parsed plan is immutable, so cache it
        self._face_adj_cache: list[tuple[Face, Face, tuple[str, ...], float]] | None = None
        self._room_adj_cache: list[Adjacency] | None = None

        self._read_nodes(data)
        self.walls = self._read_walls(data)
        self.openings = self._read_openings(data)
        self.faces = self._read_faces()
        self.rooms = self._read_rooms(data)

        self._link_rooms_and_openings()
        self._validate_records(data)

    # ── frame ──────────────────────────────────────────────────────────────
    def _read_frame(self, data: Mapping[str, Any], y_up: bool) -> Frame:
        matrix = data.get("imageTransform") or {"a": 1, "b": 0, "c": 0, "d": 1, "e": 0, "f": 0}
        meta = data.get("metadata") or {}
        img_w = float(meta.get("imageWidth") or 0)
        img_h = float(meta.get("imageHeight") or 0)
        identity = all(
            abs(float(matrix.get(k, default)) - default) < 1e-12
            for k, default in (("a", 1), ("b", 0), ("c", 0), ("d", 1), ("e", 0), ("f", 0)))
        a, b = float(matrix.get("a", 1)), float(matrix.get("b", 0))
        c, d = float(matrix.get("c", 0)), float(matrix.get("d", 1))
        scale = (math.hypot(a, b) + math.hypot(c, d)) / 2.0
        rotation = math.degrees(math.atan2(b, a))
        has_sheet = img_w > 0 and img_h > 0
        calibrated = not identity

        if not (scale > 0) or not math.isfinite(scale):
            self.issues.append(Issue("error", "invalid_image_transform",
                                     f"imageTransform is degenerate (scale={scale}); "
                                     "falling back to 1 mm/px"))
            scale, calibrated = 1.0, False
        if has_sheet and not calibrated:
            self.issues.append(Issue(
                "error", "uncalibrated",
                "background present but imageTransform is the identity matrix: stored mm "
                "sizes cannot be trusted (the editor refuses DXF/IFC export here too)"))
        if not has_sheet:
            self.issues.append(Issue(
                "info", "blank_canvas",
                "no background image dimensions: blank canvas, 1 world unit = 1 mm"))
        return Frame(scale=scale, rotation_deg=rotation, img_w=img_w, img_h=img_h,
                     calibrated=calibrated, y_up=y_up)

    # ── nodes / walls / chains ─────────────────────────────────────────────
    def _read_nodes(self, data: Mapping[str, Any]) -> None:
        for nid, rec in (data.get("nodes") or {}).items():
            self._node_records[nid] = dict(rec)
            self._nodes_px[nid] = (float(rec["x"]), float(rec["y"]))

    def _read_walls(self, data: Mapping[str, Any]) -> dict[str, WallGeom]:
        walls: dict[str, WallGeom] = {}
        for wid, rec in (data.get("walls") or {}).items():
            a, b = str(rec.get("startNodeId", "")), str(rec.get("endNodeId", ""))
            centerline = LineString()
            if a in self._nodes_px and b in self._nodes_px:
                pa, pb = self._nodes_px[a], self._nodes_px[b]
                if math.dist(pa, pb) > 1e-9:
                    centerline = LineString([self.frame.pt(*pa), self.frame.pt(*pb)])
            walls[wid] = WallGeom(
                record=dict(rec), id=wid, type=str(rec.get("type", "interior")),
                thickness=float(rec.get("thickness", 200)), start_node=a, end_node=b,
                curve_mode=str(rec.get("curveMode") or "straight"), chain_id=-1,
                centerline=centerline, length_mm=0.0, band=Polygon())

        # Chains are built PER TYPE: the export path feeds buildWallChains()
        # one type at a time, so a junction shared with another type does not
        # break the chain (see algorithms.md §2).
        self._chains = []
        by_type: dict[str, list[WallGeom]] = {}
        for wall in walls.values():
            by_type.setdefault(wall.type, []).append(wall)
        for subset in by_type.values():
            self._chains.extend(_build_chains(subset, self._nodes_px,
                                              start_index=len(self._chains)))
        for chain in self._chains:
            self._assign_chain_geometry(chain, walls)

        for wall in walls.values():
            pts = self._spans.get(wall.id, [])
            if len(pts) >= 2:
                wall.centerline = LineString(pts)
            wall.length_mm = polyline_length(pts)
            wall.band = self._wall_band(wall)
        return walls

    def _chain_geometry(self, chain: _Chain) -> tuple[list[Point2], list[Point2], bool]:
        """(mm coordinates for buffering, node ids, closed) for one chain."""
        closed = len(chain.node_ids) >= 4 and chain.node_ids[0] == chain.node_ids[-1]
        ids = chain.node_ids[:-1] if (closed and chain.curve_mode == "curve") else chain.node_ids
        raw = [self._nodes_px[i] for i in ids if i in self._nodes_px]
        if len(raw) < 2:
            return [], [], closed
        if chain.curve_mode == "curve" and len(raw) >= 3:
            first = self.walls[chain.wall_ids[0]]
            tension = min(float(first.record.get("curveTension", DEFAULT_TENSION)), MAX_TENSION)
            segments = int(first.record.get("curveSegments", DEFAULT_SEGMENTS))
            raw = catmull_rom(raw, tension=tension, segments=segments, closed=closed)
        pts = [self.frame.pt(x, y) for x, y in raw]
        if closed and pts[0] != pts[-1]:
            pts.append(pts[0])
        return pts, ids, closed

    def _assign_chain_geometry(self, chain: _Chain, walls: dict[str, WallGeom]) -> None:
        """Per-wall sampled span (WallGeometryService.chainSampledSpans)."""
        closed = len(chain.node_ids) >= 3 and chain.node_ids[0] == chain.node_ids[-1]
        ids = chain.node_ids[:-1] if closed else chain.node_ids
        raw_px = [self._nodes_px[i] for i in ids if i in self._nodes_px]
        for wid in chain.wall_ids:
            walls[wid].chain_id = chain.index

        for wid in chain.wall_ids:                       # chord fallback
            w = walls[wid]
            a, b = self._nodes_px.get(w.start_node), self._nodes_px.get(w.end_node)
            self._spans[wid] = [self.frame.pt(*a), self.frame.pt(*b)] if a and b else []

        if chain.curve_mode != "curve" or len(raw_px) < 3:
            return
        first = walls[chain.wall_ids[0]]
        tension = min(float(first.record.get("curveTension", DEFAULT_TENSION)), MAX_TENSION)
        segments = int(first.record.get("curveSegments", DEFAULT_SEGMENTS))
        samples = [self.frame.pt(x, y)
                   for x, y in catmull_rom(raw_px, tension=tension,
                                           segments=segments, closed=closed)]
        edge_count = len(ids) if closed else len(ids) - 1
        if len(chain.wall_ids) != edge_count or not samples:
            return
        for k in range(edge_count):
            start_idx = k * segments
            if closed:
                base = samples[start_idx:min(start_idx + segments, len(samples))]
                next_idx = (k + 1) * segments
                closing = samples[next_idx] if next_idx < len(samples) else samples[0]
                span = list(base) + [closing]
            else:
                span = samples[start_idx:start_idx + segments + 1]
            if len(span) < 2:
                return
            wall = walls[chain.wall_ids[k]]
            forward = wall.start_node == ids[k]
            self._spans[wall.id] = span if forward else list(reversed(span))

    def _wall_band(self, wall: WallGeom) -> Polygon:
        """Per-wall band: straight -> exact OBB (wallToOBB), curve -> min-area
        rect of the sampled strip. Per-wall quantities only; the real body is
        `wall_solid()` (mitred corners, cross-type trims)."""
        pts = self._spans.get(wall.id, [])
        if len(pts) < 2 or wall.thickness <= 0:
            return Polygon()
        if wall.curve_mode == "curve" and len(pts) >= 3:
            strip: list[Point2] = []
            for p, n in zip(pts, symbol_normals(pts, (-1.0, 0.0))):
                half = wall.thickness / 2
                strip.append((p[0] + n[0] * half, p[1] + n[1] * half))
                strip.append((p[0] - n[0] * half, p[1] - n[1] * half))
            rect = min_area_rect(strip)
            if not rect.is_empty:
                return rect
        a, b = pts[0], pts[-1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return Polygon()
        nx, ny = -dy / length, dx / length            # left-hand normal
        half = wall.thickness / 2.0
        return polygon_from_ring([
            (a[0] + nx * half, a[1] + ny * half), (b[0] + nx * half, b[1] + ny * half),
            (b[0] - nx * half, b[1] - ny * half), (a[0] - nx * half, a[1] - ny * half)])

    # ── wall bodies: chain buffering + cross-type trims + opening cuts ─────
    def _buffer_chain(self, chain: _Chain) -> Polygon | None:
        pts, node_ids, closed = self._chain_geometry(chain)
        if len(pts) < 2 or chain.thickness <= 0:
            return None
        half = chain.thickness / 2.0

        def join_of(node_id: str) -> str:
            style = (self._node_records.get(node_id) or {}).get("cornerStyle") or {}
            return {"round": "round", "bevel": "bevel"}.get(style.get("type", ""), "mitre")

        def cap_of(node_id: str) -> str:
            cap = (self._node_records.get(node_id) or {}).get("capStyle")
            return {"round": "round", "square": "square"}.get(cap, "flat")

        try:
            if closed:
                joins = [join_of(nid) for nid in chain.node_ids[:-1]]
                join = joins[0] if joins and all(j == joins[0] for j in joins) else "mitre"
                geom = LineString(pts).buffer(half, quad_segs=QUAD_SEGS, cap_style="flat",
                                              join_style=join, mitre_limit=MITRE_LIMIT)
                return geom if not geom.is_empty else None

            joins = [join_of(nid) for nid in node_ids[1:-1]]
            start_cap, end_cap = cap_of(node_ids[0]), cap_of(node_ids[-1])
            parts: list[Polygon] = []
            if not joins or all(j == joins[0] for j in joins):
                join = joins[0] if joins else "mitre"
                cap = start_cap if start_cap == end_cap else "flat"
                parts.append(LineString(pts).buffer(half, quad_segs=QUAD_SEGS, cap_style=cap,
                                                    join_style=join, mitre_limit=MITRE_LIMIT))
            else:                                   # mixed corner styles: 3-point pieces
                for i, join in enumerate(joins):
                    parts.append(LineString(pts[i:i + 3]).buffer(
                        half, quad_segs=QUAD_SEGS, cap_style="flat",
                        join_style=join, mitre_limit=MITRE_LIMIT))
            if start_cap != "flat":
                parts.append(self._end_cap(pts[0], pts[1], start_cap, half))
            if end_cap != "flat":
                parts.append(self._end_cap(pts[-1], pts[-2], end_cap, half))
            parts = [p for p in parts if not p.is_empty]
            return unary_union(parts) if parts else None
        except shapely.errors.GEOSException:          # topology error -> no body
            return None

    @staticmethod
    def _end_cap(node: Point2, neighbour: Point2, style: str, half: float) -> Polygon:
        dx, dy = node[0] - neighbour[0], node[1] - neighbour[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return Polygon()
        ux, uy = dx / length, dy / length
        px, py = -uy, ux
        if style == "square":
            return polygon_from_ring([
                (node[0] + px * half, node[1] + py * half),
                (node[0] + ux * half + px * half, node[1] + uy * half + py * half),
                (node[0] + ux * half - px * half, node[1] + uy * half - py * half),
                (node[0] - px * half, node[1] - py * half)])
        ring: list[Point2] = []                       # round:半圆, QUAD_SEGS steps
        a0 = math.atan2(py, px)
        for i in range(QUAD_SEGS + 1):
            ang = a0 - math.pi * i / QUAD_SEGS
            ring.append((node[0] + half * math.cos(ang), node[1] + half * math.sin(ang)))
        return polygon_from_ring(ring)

    def _group_solid(self, walls: Sequence[WallGeom], wall_type: str, cut: bool) -> Polygon:
        chain_ids = {w.chain_id for w in walls}
        geoms = [self._buffer_chain(c) for c in self._chains if c.index in chain_ids]
        geoms = [g for g in geoms if g is not None and not g.is_empty]
        if not geoms:
            return Polygon()
        merged = unary_union(geoms)
        return self._apply_opening_cuts(merged, wall_type) if cut else merged

    def _apply_opening_cuts(self, geom: Polygon, wall_type: str) -> Polygon:
        result = geom
        for op in self.openings.values():
            if op.wall is None or op.wall.type != wall_type or op.footprint.is_empty:
                continue
            try:
                result = result.difference(op.footprint)
            except shapely.errors.GEOSException:
                continue
        try:
            cleaned = result.buffer(0)              # source applies BufferOp(0)
            if not cleaned.is_empty:
                result = cleaned
        except shapely.errors.GEOSException:
            pass
        return result

    def _typed_solids(self, cut: bool) -> dict[str, Polygon]:
        """buildPresentationWallRingsByType: stronger types keep their body,
        weaker types are trimmed back to the stronger UNCUT band."""
        if cut in self._solids_cache:
            return self._solids_cache[cut]
        by_type: dict[str, list[WallGeom]] = {}
        for wall in self.walls.values():
            if wall.type in WALL_TRIM_ORDER:
                by_type.setdefault(wall.type, []).append(wall)

        out: dict[str, Polygon] = {}
        stronger: list[Polygon] = []
        for wtype in WALL_TRIM_ORDER:
            walls = by_type.get(wtype)
            if not walls:
                continue
            geom = self._group_solid(walls, wtype, cut)
            if stronger and not geom.is_empty:
                try:
                    geom = geom.difference(unary_union(stronger))
                except shapely.errors.GEOSException:
                    pass
            band = self._group_solid(walls, wtype, False)
            if not band.is_empty:
                stronger.append(band)
            out[wtype] = geom

        railings = [w for w in self.walls.values() if w.type == "railing"]
        out["railing"] = (self._group_solid(railings, "railing", cut)
                          if railings else Polygon())
        out["level_change"] = Polygon()
        self._solids_cache[cut] = out
        return out

    def wall_solid(self, wall_type: str, *, cut_openings: bool = True) -> Polygon:
        """The real wall body of one type in mm²: chains buffered (mitred at
        corners, round/bevel per node cornerStyle), weaker types trimmed at
        stronger faces, openings carved as real voids. `level_change` is an
        annotation: empty."""
        return self._typed_solids(cut_openings).get(wall_type) or Polygon()

    def wall_solid_all(self, *, cut_openings: bool = True) -> Polygon:
        """Union of every wall body (floor mask / outline)."""
        geoms = [g for g in self._typed_solids(cut_openings).values() if not g.is_empty]
        return unary_union(geoms) if geoms else Polygon()

    def to_grid(self, geom: Any) -> Any:
        """Any emitted mm geometry rotated into the calibrated grid frame.
        See `Frame.to_grid`; presentation only."""
        return self.frame.to_grid(geom)

    # ── openings ───────────────────────────────────────────────────────────
    def _read_openings(self, data: Mapping[str, Any]) -> dict[str, OpeningGeom]:
        out: dict[str, OpeningGeom] = {}
        for oid, rec in (data.get("openings") or {}).items():
            otype = str(rec.get("type", "door"))
            wall = self.walls.get(str(rec.get("wallId", "")))
            geom = self._opening_geometry(rec, wall) if wall is not None else None
            center, start, end, normal, footprint, effective = geom or (
                [], None, None, (0.0, 1.0), Polygon(), 0.0)
            declared = float(rec.get("width", 0))
            is_window = otype == "window"
            z0 = DEFAULT_IFC_PARAMS["window_sill"] if is_window else 0.0
            z1 = z0 + (DEFAULT_IFC_PARAMS["window_height"] if is_window
                       else DEFAULT_IFC_PARAMS["door_height"])
            out[oid] = OpeningGeom(
                record=dict(rec), id=oid, type=otype, wall=wall,
                width_mm=declared, position=float(rec.get("position", 0)),
                direction=int(rec.get("direction", 1)), hinge_side=rec.get("hingeSide"),
                span_wall_ids=tuple(rec.get("spanWallIds") or ()),
                footprint=footprint, center_points=center, start_mm=start, end_mm=end,
                wall_normal=normal, z_range_mm=(z0, z1),
                passable=otype in PASSABLE_OPENING_TYPES,
                outside=wall is not None and wall.type in EXTERIOR_WALL_TYPES,
                effective_width_mm=effective,
                clamped=effective < declared - 1e-6)
            if wall is not None:
                wall.opening_ids.append(oid)
        return out

    def _opening_geometry(self, rec: Mapping[str, Any], wall: WallGeom
                          ) -> tuple[list[Point2], Point2, Point2, Point2, Polygon, float] | None:
        """openingToWorldCoords: `position` is an ARC FRACTION on the anchor
        span; `width` travels along the spanWallIds chain.

        The source lets an opening overrun the wall tip; here both ends are
        CLAMPED to the span and the usable width is returned so the cut strip
        and the reported width never disagree (see `effective_width_mm`)."""
        anchor = self._spans.get(wall.id, [])
        if len(anchor) < 2:
            return None
        anchor_len = polyline_length(anchor)
        if anchor_len < 1e-6:
            return None
        span_ids = tuple(rec.get("spanWallIds") or ())
        pts = (self._join_span(span_ids) if len(span_ids) > 1 else None) or anchor
        span_len = polyline_length(pts)
        if span_len < 1e-6:
            return None

        s_start = min(max(float(rec.get("position", 0)) * anchor_len, 0.0), span_len)
        s_end = min(max(s_start + float(rec.get("width", 0)), s_start), span_len)
        effective = s_end - s_start
        if effective < 1e-6:
            return None
        start_hit = polyline_point_at_arc(pts, s_start)
        end_hit = polyline_point_at_arc(pts, s_end)
        if start_hit is None or end_hit is None:
            return None
        start, tangent = start_hit
        center = polyline_slice(pts, s_start, s_end)
        if len(center) < 2:
            return None
        eps = max(CAP_EPS_PX * self.frame.scale, wall.thickness / 2 * 0.02)
        return (center, start, end_hit[0], (-tangent[1], tangent[0]),
                strip_polygon(center, wall.thickness / 2 + eps, (-tangent[1], tangent[0])),
                effective)

    def _join_span(self, wall_ids: Sequence[str]) -> list[Point2] | None:
        """buildOpeningSpan: walls joined node-by-node into one polyline."""
        if not wall_ids:
            return None
        out: list[Point2] = []
        cursor: str | None = None
        for i, wid in enumerate(wall_ids):
            wall = self.walls.get(wid)
            pts = list(self._spans.get(wid, []))
            if wall is None or len(pts) < 2:
                return None
            if i == 0:
                out.extend(pts)
                cursor = wall.end_node
                continue
            if wall.start_node == cursor:
                out.extend(pts[1:])
                cursor = wall.end_node
            elif wall.end_node == cursor:
                pts.reverse()
                out.extend(pts[1:])
                cursor = wall.start_node
            else:
                return None
        return out if len(out) >= 2 else None

    # ── faces + rooms ──────────────────────────────────────────────────────
    def _read_faces(self) -> list[Face]:
        """DCEL equivalent: node the sampled centerlines, polygonize the bounded
        faces, then tag each face with the walls lying on its boundary."""
        segments: list[tuple[LineString, str]] = []
        for wall in self.walls.values():
            pts = self._spans.get(wall.id, [])
            for i in range(len(pts) - 1):
                if math.dist(pts[i], pts[i + 1]) >= 1e-9:
                    segments.append((LineString([pts[i], pts[i + 1]]), wall.id))
        if not segments:
            return []
        faces: list[Face] = []
        for poly in polygonize(unary_union([seg for seg, _ in segments])):
            if poly.is_empty or poly.area <= 1e-6:
                continue
            boundary = poly.boundary
            wall_ids: list[str] = []
            for line, wid in segments:
                try:
                    if boundary.intersection(line).length > 1e-6:
                        wall_ids.append(wid)
                except shapely.errors.GEOSException:
                    continue
            wall_ids = list(dict.fromkeys(wall_ids))
            ring = [_pt2(p) for p in poly.exterior.coords[:-1]]
            centroid = (float(poly.centroid.x), float(poly.centroid.y))
            faces.append(Face(id="|".join(sorted(wall_ids)), wall_ids=tuple(wall_ids),
                              polygon=poly, ring=ring, area_mm2=float(poly.area),
                              centroid=centroid,
                              label_anchor=label_anchor(poly, ring, centroid)))
        return faces

    def _read_rooms(self, data: Mapping[str, Any]) -> dict[str, RoomGeom]:
        by_walls = {frozenset(f.wall_ids): f for f in self.faces}
        rooms: dict[str, RoomGeom] = {}
        for rid, rec in (data.get("rooms") or {}).items():
            points = rec.get("points") or []
            wall_ids: tuple[str, ...] = tuple(rec.get("wallIds") or ())
            face: Face | None = None
            match: str | None = None

            if len(points) >= 3:                       # hand-drawn room: use the ring
                ring = normalize_ring([self.frame.pt(p["x"], p["y"]) for p in points])
                poly = polygon_from_ring(ring)
                if not poly.is_empty:
                    centroid = (float(poly.centroid.x), float(poly.centroid.y))
                    face = Face(id=f"custom:{rid}", wall_ids=(), polygon=poly, ring=ring,
                                area_mm2=float(poly.area), centroid=centroid,
                                label_anchor=label_anchor(poly, ring, centroid))
                    match = "custom"
            else:
                wanted = frozenset(wall_ids)
                face = by_walls.get(wanted)
                if face is not None:
                    match = "exact"
                else:
                    face = self._tolerant_face(wanted)
                    if face is not None:
                        match = "tolerant"
                        self.issues.append(Issue(
                            "info", "room_face_tolerant", entity=rid,
                            message=f"room wallIds do not match a face exactly; matched "
                                    f"face {face.id} (Jaccard >= 0.5) — the wall graph "
                                    "changed since the room was drawn"))

            preset = ROOM_PRESETS.get(str(rec.get("color")).upper())
            if face is None:
                rooms[rid] = RoomGeom(record=dict(rec), id=rid, label=rec.get("label"),
                                      color=rec.get("color"), preset_label=preset,
                                      match=None, face=None, area_centerline_mm2=0.0,
                                      area_net_mm2=0.0, label_anchor_mm=None,
                                      wall_ids=wall_ids)
                continue
            label_pos = rec.get("labelPos")
            rooms[rid] = RoomGeom(
                record=dict(rec), id=rid, label=rec.get("label"), color=rec.get("color"),
                preset_label=preset, match=match, face=face,
                area_centerline_mm2=face.area_mm2, area_net_mm2=self._net_area(face.polygon),
                label_anchor_mm=(self.frame.pt(label_pos["x"], label_pos["y"])
                                 if label_pos else face.label_anchor),
                wall_ids=face.wall_ids or wall_ids)
        return rooms

    def _tolerant_face(self, wanted: frozenset[str]) -> Face | None:
        best, best_score = None, 0.5                   # Jaccard >= 0.5
        for face in self.faces:
            have = set(face.wall_ids)
            if not have or not (have | wanted):
                continue
            score = len(have & wanted) / len(have | wanted)
            if score > best_score:
                best, best_score = face, score
        return best

    def _net_area(self, polygon: Polygon) -> float:
        """Floor area after subtracting the SOLID wall bands (trimmed, uncut)."""
        bands = [g for g in self._typed_solids(False).values() if not g.is_empty]
        if not bands:
            return float(polygon.area)
        try:
            return float(polygon.area) - float(unary_union(bands).intersection(polygon).area)
        except shapely.errors.GEOSException:
            return float(polygon.area)

    def _link_rooms_and_openings(self) -> None:
        """Bind openings to rooms.

        Wall-bounded rooms match by wall id (an opening on a shared wall belongs
        to both sides). Hand-drawn rooms carry no wallIds, so they match by
        geometry: an opening whose centre lies on the room boundary."""
        for room in self.rooms.values():
            if room.face is None:
                continue
            walls = set(room.wall_ids)
            boundary = room.face.polygon.boundary if not walls else None
            for op in self.openings.values():
                if op.wall is None or not op.center_points:
                    continue
                hit = op.wall.id in walls or bool(walls.intersection(op.span_wall_ids))
                if not hit and boundary is not None:
                    mid = Point(op.center_points[len(op.center_points) // 2])
                    hit = boundary.distance(mid) <= max(50.0, op.wall.thickness / 2)
                if not hit:
                    continue
                room.openings.append(op)
                op.room_ids.append(room.id)
                if op.type == "window":
                    room.windows.append(op)
                elif op.passable:
                    room.doors.append(op)

    def face_adjacency(self) -> list[tuple[Face, Face, tuple[str, ...], float]]:
        """Face pairs sharing a boundary segment > 1 mm, with the walls on it.

        Cached: a parsed plan is immutable, and the pairwise intersection is the
        most expensive scan in the module.
        """
        if self._face_adj_cache is not None:
            return self._face_adj_cache
        out = []
        for i, fa in enumerate(self.faces):
            for fb in self.faces[i + 1:]:
                try:
                    shared = fa.polygon.boundary.intersection(fb.polygon.boundary)
                except shapely.errors.GEOSException:
                    continue
                if shared.is_empty or shared.length <= 1.0:
                    continue
                wall_ids: list[str] = []
                for wall in self.walls.values():
                    pts = self._spans.get(wall.id, [])
                    for k in range(len(pts) - 1):
                        try:
                            if shared.intersection(LineString([pts[k], pts[k + 1]])).length > 1.0:
                                wall_ids.append(wall.id)
                                break
                        except shapely.errors.GEOSException:
                            continue
                out.append((fa, fb, tuple(dict.fromkeys(wall_ids)), float(shared.length)))
        self._face_adj_cache = out
        return out

    def room_adjacency(self) -> list[Adjacency]:
        """Room↔room edges: adjacent faces -> shared walls -> passable openings
        on them (RoomService.roomAdjacency + hasDoorBetween). Rooms resolved
        from the same face are never adjacent: one room per face.

        Note: rooms sharing a face are cross-multiplied here, so one face pair
        can yield several room pairs. Use `face_adjacency()` for a canonical
        one-edge-per-boundary graph (e.g. a bubble diagram).
        """
        if self._room_adj_cache is not None:
            return self._room_adj_cache
        rooms_by_face: dict[str, list[RoomGeom]] = {}
        for room in self.rooms.values():
            if room.face is not None:
                rooms_by_face.setdefault(room.face.id, []).append(room)

        merged: dict[tuple[str, str], Adjacency] = {}
        for fa, fb, wall_ids, shared_len in self.face_adjacency():
            passable = [o for o in self.openings.values()
                        if o.wall is not None and o.wall.id in wall_ids and o.passable]
            for ra in rooms_by_face.get(fa.id, []):
                for rb in rooms_by_face.get(fb.id, []):
                    key = tuple(sorted((ra.id, rb.id)))
                    prev = merged.get(key)
                    merged[key] = Adjacency(
                        room_a=ra, room_b=rb,
                        shared_wall_ids=(wall_ids if prev is None else tuple(
                            dict.fromkeys(prev.shared_wall_ids + wall_ids))),
                        opening_ids=(tuple(o.id for o in passable) if prev is None else tuple(
                            dict.fromkeys(prev.opening_ids + tuple(o.id for o in passable)))),
                        passable=bool(passable) or (prev.passable if prev else False),
                        shared_length_mm=shared_len + (prev.shared_length_mm if prev else 0.0))
        self._room_adj_cache = list(merged.values())
        return self._room_adj_cache

    # ── wall graph (nodes + degrees, no room semantics) ──────────────────────
    def node_adjacency(self) -> dict[str, set[str]]:
        """Node -> neighbouring nodes, derived from `walls` (never from the
        possibly stale `connectedWallIds`)."""
        adj: dict[str, set[str]] = {nid: set() for nid in self._nodes_px}
        for wall in self.walls.values():
            a, b = wall.start_node, wall.end_node
            if a in adj and b in adj and a != b:
                adj[a].add(b)
                adj[b].add(a)
        return adj

    def degrees(self) -> dict[str, int]:
        """Node -> number of walls meeting there (degree 0 = isolated)."""
        return {nid: len(neighbours) for nid, neighbours in self.node_adjacency().items()}

    def components(self) -> list[list[str]]:
        """Connected node groups, largest first (isolated nodes are 1-element)."""
        adj = self.node_adjacency()
        seen: set[str] = set()
        groups: list[list[str]] = []
        for start in adj:
            if start in seen:
                continue
            stack, group = [start], []
            seen.add(start)
            while stack:
                nid = stack.pop()
                group.append(nid)
                for neighbour in adj[nid]:
                    if neighbour not in seen:
                        seen.add(neighbour)
                        stack.append(neighbour)
            groups.append(sorted(group))
        groups.sort(key=len, reverse=True)
        return groups

    def isolated_nodes(self) -> list[str]:
        """Nodes no wall references (degree 0)."""
        return sorted(nid for nid, degree in self.degrees().items() if degree == 0)

    # ── 3D slabs (z-split; extrusion itself is a recipe) ───────────────────
    def wall_slabs(self, wall_type: str, params: HeightParams | None = None) -> list[Slab]:
        """Z-split solids of one wall type (ifcModel): each slab only subtracts
        the openings that run through its full height range."""
        params = params or HeightParams()
        height = params.railing_height if wall_type == "railing" else params.wall_height
        if wall_type == "level_change" or height <= 0:
            return []
        base = self.wall_solid(wall_type, cut_openings=False)
        if base.is_empty:
            return []
        ops = [o for o in self.openings.values() if o.wall and o.wall.type == wall_type]
        z_range = {op.id: op.z_range(params) for op in ops}

        stops = {0.0, height}
        for z0, z1 in z_range.values():
            stops.update(z for z in (z0, z1) if 0.0 < z < height)
        stops = sorted(stops)

        slabs: list[Slab] = []
        for z0, z1 in zip(stops, stops[1:]):
            if z1 - z0 <= 1e-9:
                continue
            active = [o for o in ops
                      if z_range[o.id][0] <= z0 + 1e-9 and z_range[o.id][1] >= z1 - 1e-9]
            geom = base
            for op in active:
                if op.footprint.is_empty:
                    continue
                try:
                    geom = geom.difference(op.footprint)
                except shapely.errors.GEOSException:
                    continue
            if active:
                try:
                    geom = geom.buffer(0)
                except shapely.errors.GEOSException:
                    pass
            if geom.is_empty:
                continue
            slabs.append(Slab(wall_type=wall_type, z0=z0, z1=z1, polygon=geom,
                              area_mm2=float(geom.area)))
        return slabs

    # ── validation ─────────────────────────────────────────────────────────
    def _validate_records(self, data: Mapping[str, Any]) -> None:
        for key in ("nodes", "walls", "openings", "columns", "rooms", "verticals",
                    "furniture", "texts", "axisGrids", "metadata"):
            if key not in data:
                self.issues.append(Issue("info", "missing_section",
                                         f"top-level key '{key}' absent — defaulted to empty"))

        for wid, wall in self.walls.items():
            if wall.start_node not in self._nodes_px or wall.end_node not in self._nodes_px:
                self.issues.append(Issue(
                    "warning", "wall_node_missing", entity=wid,
                    message=f"wall references a missing node "
                            f"({wall.start_node} -> {wall.end_node}); its geometry is "
                            "dropped"))
            elif wall.length_mm < 1e-6:
                self.issues.append(Issue("warning", "zero_length_wall", entity=wid,
                                         message="both nodes coincide; the wall has no body"))
            if wall.type not in WALL_TYPES:
                self.issues.append(Issue("warning", "unknown_wall_type", entity=wid,
                                         message=f"unknown wall type '{wall.type}' "
                                                 "(treated as solid)"))

        seen: dict[tuple[float, float], list[str]] = {}
        for nid, (x, y) in self._nodes_px.items():
            seen.setdefault((round(x, 3), round(y, 3)), []).append(nid)
        for (x, y), ids in seen.items():
            if len(ids) > 1:
                self.issues.append(Issue(
                    "info", "coincident_nodes", entity=",".join(ids),
                    message=f"{len(ids)} distinct nodes share the coordinate ({x}, {y}) — "
                            "zero-length walls and doubled rooms are common here"))

        for nid, rec in self._node_records.items():
            expected = sorted(w.id for w in self.walls.values()
                              if nid in (w.start_node, w.end_node))
            given = sorted(str(i) for i in (rec.get("connectedWallIds") or []))
            if expected != given:
                self.issues.append(Issue(
                    "info", "node_wall_links_inconsistent", entity=nid,
                    message="connectedWallIds does not match the walls referencing this "
                            "node (derived links are authoritative)"))

        for rid, room in self.rooms.items():
            dangling = [w for w in (room.record.get("wallIds") or []) if w not in self.walls]
            if dangling:
                self.issues.append(Issue(
                    "warning", "room_dangling_wall_ids", entity=rid,
                    message=f"room references {len(dangling)} wall(s) that are not in the "
                            f"file ({', '.join(dangling[:4])})"))
            if room.face is None:
                self.issues.append(Issue(
                    "warning", "room_face_unmatched", entity=rid,
                    message="room boundary does not close a face in the wall graph "
                            "(deleted wall, or a custom polygon is required)"))

        by_face: dict[str, list[str]] = {}
        for rid, room in self.rooms.items():
            if room.face is not None:
                by_face.setdefault(room.face.id, []).append(rid)
        for face_id, rids in by_face.items():
            if len(rids) > 1:
                self.issues.append(Issue(
                    "warning", "room_face_shared", entity=",".join(rids),
                    message=f"{len(rids)} rooms resolve to the same face "
                            f"({face_id[:24]}…); the same area is counted more than once"))

        for oid, op in self.openings.items():
            if op.wall is None:
                self.issues.append(Issue("error", "opening_wall_missing", oid,
                                         f"opening's wallId '{op.record.get('wallId')}' "
                                         "is not in the file"))
                continue
            if op.type not in OPENING_TYPES:
                self.issues.append(Issue("warning", "unknown_opening_type", entity=oid,
                                         message=f"unknown opening type '{op.type}'"))
            anchor_len = polyline_length(self._spans.get(op.wall.id, []))
            span_len = anchor_len
            if len(op.span_wall_ids) > 1:
                joined = self._join_span(op.span_wall_ids)
                if joined is None:
                    self.issues.append(Issue(
                        "warning", "opening_span_broken", entity=oid,
                        message="spanWallIds is not a continuous chain; falling back to "
                                "the anchor wall"))
                else:
                    span_len = polyline_length(joined)
            if anchor_len > 0 and op.position * anchor_len + op.width_mm > span_len + 1e-3:
                self.issues.append(Issue(
                    "warning", "opening_exceeds_wall", entity=oid,
                    message=f"opening spans {op.position * anchor_len + op.width_mm:.0f} mm "
                            f"along a {span_len:.0f} mm wall (position + width overrun); "
                            f"geometry is clamped to {op.effective_width_mm:.0f} mm"))

# ── entry points ───────────────────────────────────────────────────────────

def load_floorplan(source: Any, *, y_up: bool = True) -> FloorPlan:
    """Read FloorPlan JSON from a path, a JSON string, or an already-parsed dict.

    y_up=True (default) maps to the DXF/IFC convention
    (x_mm = x·scale, y_mm = (imgH − y)·scale); y_up=False keeps image pixels
    scaled (y down) when you want 2D output aligned with the source image.
    """
    if isinstance(source, FloorPlan):
        return source
    if isinstance(source, Mapping):
        data = source
    elif isinstance(source, (str, bytes, os.PathLike)):
        raw = os.fspath(source)
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if text.lstrip().startswith("{"):
            data = json.loads(text)
        else:
            with open(text, "r", encoding="utf-8") as fh:
                data = json.load(fh)
    else:
        raise TypeError(f"unsupported source type: {type(source)!r}")
    return FloorPlan(data, y_up=y_up)


def parse_floorplan(data: Mapping[str, Any], *, y_up: bool = True) -> FloorPlan:
    """Alias of load_floorplan for already-parsed dicts."""
    return FloorPlan(data, y_up=y_up)
