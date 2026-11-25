"""Tests for the reference FloorPlan JSON reader.

Written BEFORE the implementation (TDD). The synthetic apartment below pins
the CORE semantics of the floorplan-konva export: positions in background-image
pixels, sizes in millimetres, `imageTransform` carries the mm/px scale.

    scale 25 mm/px, image 400 x 300 px
    exterior chain 200 x 120 px (5000 x 3000 mm), 300 mm thick
    one interior wall (200 mm) splitting it into two 7.5 m2 rooms
"""

import math

import pytest
from shapely.geometry import Point

from floorplan_io import DEFAULT_IFC_PARAMS, HeightParams, load_floorplan
from floorplan_report import summarize

S = 25.0  # mm per px
IMG_W, IMG_H = 400, 300
PX2 = S * S  # mm^2 per px^2


def _matrix(scale=S, rot=0.0):
    # Column-major similarity built like TransformService.similarityMatrix.
    a, b = scale * math.cos(rot), scale * math.sin(rot)
    return {"a": a, "b": b, "c": -b, "d": a, "e": 0, "f": 0}


def apartment():
    nodes = {
        "n1": (50, 60), "n2": (250, 60), "n3": (250, 180), "n4": (50, 180),
        "n5": (150, 60), "n6": (150, 180),
    }
    wall_rows = [  # id, type, thickness(mm), start, end
        ("w1a", "exterior", 300, "n1", "n5"), ("w1b", "exterior", 300, "n5", "n2"),
        ("w2", "exterior", 300, "n2", "n3"), ("w3a", "exterior", 300, "n3", "n6"),
        ("w3b", "exterior", 300, "n6", "n4"), ("w4", "exterior", 300, "n4", "n1"),
        ("w5", "interior", 200, "n5", "n6"),
    ]
    walls = {wid: {"id": wid, "startNodeId": a, "endNodeId": b, "thickness": t, "type": ty}
             for wid, ty, t, a, b in wall_rows}
    for nid, (x, y) in nodes.items():
        nodes[nid] = {
            "id": nid, "x": x, "y": y,
            "connectedWallIds": [wid for wid, w in walls.items()
                                 if nid in (w["startNodeId"], w["endNodeId"])],
        }
    openings = {
        "d1": {"id": "d1", "wallId": "w5", "type": "door", "position": 0.5, "width": 900,
               "direction": 1, "hingeSide": "start"},
        "entry": {"id": "entry", "wallId": "w4", "type": "door", "position": 0.1, "width": 900,
                  "direction": 1, "hingeSide": "end"},
        "win": {"id": "win", "wallId": "w3a", "type": "window", "position": 0.25, "width": 1500,
                "direction": 1},
    }
    rooms = {
        "r1": {"id": "r1", "wallIds": ["w1a", "w5", "w3b", "w4"], "label": "卧室", "color": "#8DCBEB"},
        "r2": {"id": "r2", "wallIds": ["w1b", "w2", "w3a", "w5"], "label": "客厅", "color": "#E085F2"},
    }
    columns = {
        "c1": {"id": "c1", "x": 100, "y": 120, "shape": "rectangle",
               "width": 400, "height": 600, "rotation": 90},
    }
    verticals = {
        "v1": {"id": "v1", "kind": "elevator", "x": 200, "y": 120, "rotation": 0,
               "shaftWidth": 2000, "shaftDepth": 2200},
        "v2": {"id": "v2", "kind": "stair_straight", "x": 10, "y": 10, "rotation": 0,
               "stairwellWidth": 1500, "stairwellLength": 4800, "floorHeight": 3000,
               "direction": 1, "floorType": "mid"},
    }
    furniture = {
        "f1": {"id": "f1", "symbolId": "bed_double", "x": 100, "y": 90, "rotation": 0,
               "flipped": False, "width": 1800, "height": 2000},
    }
    texts = {"t1": {"id": "t1", "x": 60, "y": 70, "text": "主卧", "fontSize": 300, "rotation": 0}}
    axis_grids = {
        "g1": {"id": "g1", "schemeId": "s", "rotation": 0.0, "gridOffsetX": 0, "gridOffsetY": 0,
               "verticalAxes": [], "horizontalAxes": [10, 22, 40]},
    }
    return {
        "version": "1.0.0", "exportedAt": "2026-01-01T00:00:00.000Z",
        "nodes": nodes, "walls": walls, "openings": openings, "columns": columns,
        "rooms": rooms, "verticals": verticals, "furniture": furniture, "texts": texts,
        "axisGrids": axis_grids, "imageTransform": _matrix(),
        "metadata": {"imageWidth": IMG_W, "imageHeight": IMG_H},
    }


def codes(plan):
    return {issue.code for issue in plan.issues}


# ── frame: scale + px → mm ─────────────────────────────────────────────────

def test_frame_scale_and_px_to_mm():
    plan = load_floorplan(apartment())
    assert plan.frame.scale == S
    assert plan.frame.calibrated is True
    # x_mm = x_px * scale ; y_mm = (imgH - y_px) * scale  (exporter convention)
    assert plan.frame.pt(50, 60) == pytest.approx((1250.0, 6000.0))
    assert plan.frame.pt(50, 60 + 120) == pytest.approx((1250.0, 3000.0))

    down = load_floorplan(apartment(), y_up=False)
    assert down.frame.pt(50, 60) == pytest.approx((1250.0, 1500.0))


def test_uncalibrated_and_blank_canvas_frames():
    scan = apartment()
    scan["imageTransform"] = {"a": 1, "b": 0, "c": 0, "d": 1, "e": 0, "f": 0}
    plan = load_floorplan(scan)
    assert plan.frame.scale == 1
    assert "uncalibrated" in codes(plan)
    assert plan.issues[[i.code for i in plan.issues].index("uncalibrated")].level == "error"

    blank = apartment()
    blank["imageTransform"] = {"a": 1, "b": 0, "c": 0, "d": 1, "e": 0, "f": 0}
    blank["metadata"] = {"imageWidth": 0, "imageHeight": 0}
    plan = load_floorplan(blank)
    assert plan.frame.scale == 1
    assert "blank_canvas" in codes(plan)
    assert not [i for i in plan.issues if i.level == "error"]


# ── walls ──────────────────────────────────────────────────────────────────

def test_wall_lengths_in_mm():
    plan = load_floorplan(apartment())
    assert len(plan.walls) == 7
    assert plan.walls["w1a"].length_mm == pytest.approx(2500.0)
    assert plan.walls["w5"].length_mm == pytest.approx(3000.0)
    # Centerline geometry is emitted in mm, Y up.
    w = plan.walls["w1a"]
    assert w.centerline.coords[0] == pytest.approx((1250.0, 6000.0))
    assert w.centerline.coords[-1] == pytest.approx((3750.0, 6000.0))
    # Simple per-wall band (rectToCorners semantics): 2500 x 300 mm.
    assert w.band.area == pytest.approx(2500 * 300)


def test_wall_solids_follow_source_buffer_semantics():
    plan = load_floorplan(apartment())
    ext = plan.wall_solid("exterior", cut_openings=False)
    # Closed chain → mitred annulus: (212*132 - 188*108) px2.
    assert ext.area == pytest.approx(7680 * PX2)
    assert len(ext.interiors) == 1

    # Interior band is trimmed at the exterior band (WALL_TRIM_ORDER):
    # 108 px of 120 remain, 8 px thick.
    interior = plan.wall_solid("interior", cut_openings=False)
    assert interior.area == pytest.approx(864 * PX2)

    # Opening cuts: entry door 36 px + window 60 px through 12 px; door 36 px through 8 px.
    assert plan.wall_solid("exterior").area == pytest.approx(6528 * PX2)
    assert plan.wall_solid("interior").area == pytest.approx(576 * PX2)
    # Annotation types carry no solid body.
    assert plan.wall_solid("level_change").is_empty


def test_curve_wall_sampling_follows_chain_catmull_rom():
    data = apartment()
    data["nodes"] = {k: {"id": k, "x": x, "y": y, "connectedWallIds": ["cw1", "cw2"]}
                     for k, (x, y) in {"c1": (0, 0), "c2": (100, 0), "c3": (100, 100)}.items()}
    data["walls"] = {
        "cw1": {"id": "cw1", "startNodeId": "c1", "endNodeId": "c2", "thickness": 200,
                "type": "exterior", "curveMode": "curve"},
        "cw2": {"id": "cw2", "startNodeId": "c2", "endNodeId": "c3", "thickness": 200,
                "type": "exterior", "curveMode": "curve"},
    }
    for key in ("openings", "rooms", "columns", "verticals", "furniture", "texts", "axisGrids"):
        data[key] = {}
    plan = load_floorplan(data)

    cw1, cw2 = plan.walls["cw1"].centerline, plan.walls["cw2"].centerline
    assert len(cw1.coords) == 17 and len(cw2.coords) == 17  # DEFAULT_SEGMENTS + 1
    assert cw1.coords[0] == pytest.approx((0.0, IMG_H * S))      # c1
    assert cw1.coords[-1] == pytest.approx((2500.0, IMG_H * S))  # c2
    assert cw2.coords[-1] == pytest.approx((2500.0, (IMG_H - 100) * S))  # c3
    mid = cw1.interpolate(0.5, normalized=True)
    assert abs(mid.y - IMG_H * S) > 1.0  # bulges off the chord


# ── openings ───────────────────────────────────────────────────────────────

def test_opening_geometry_and_height_ranges():
    plan = load_floorplan(apartment())
    door = plan.openings["d1"]
    assert door.wall.id == "w5"
    assert door.width_mm == 900
    # Footprint = centerline slice (900 mm) x wall thickness (200 mm).
    xmin, ymin, xmax, ymax = door.footprint.bounds
    assert ymax - ymin == pytest.approx(900.0)
    assert xmax - xmin >= 200.0
    assert door.z_range_mm == (0, DEFAULT_IFC_PARAMS["door_height"])
    win = plan.openings["win"]
    assert win.wall.id == "w3a"
    assert win.z_range_mm == (900, 2400)
    assert plan.walls["w5"].opening_ids == ["d1"]


# ── spatial units: faces + rooms ───────────────────────────────────────────

def test_faces_and_exact_room_matching():
    plan = load_floorplan(apartment())
    assert len(plan.faces) == 2
    assert sorted(round(f.area_mm2 / 1e6, 6) for f in plan.faces) == [7.5, 7.5]

    r1 = plan.rooms["r1"]
    assert r1.match == "exact"
    assert r1.face is not None
    assert r1.area_centerline_mm2 == pytest.approx(12000 * PX2)
    # Net = face minus uncut wall bands: (100-6-4) x (120-12) px2.
    assert r1.area_net_mm2 == pytest.approx(9720 * PX2)
    assert r1.preset_label == "卧室"
    # d1 is the divider door; entry is the exterior door on r1's outer wall.
    assert [o.id for o in r1.doors] == ["d1", "entry"]
    assert [o.id for o in r1.windows] == []
    assert [o.id for o in plan.rooms["r2"].windows] == ["win"]
    assert r1.label_anchor_mm is not None and r1.face.polygon.contains(Point(r1.label_anchor_mm))


def test_tolerant_room_match_when_wall_ids_dangle():
    data = apartment()
    data["rooms"]["r1"]["wallIds"] = data["rooms"]["r1"]["wallIds"] + ["ghost"]
    plan = load_floorplan(data)
    r1 = plan.rooms["r1"]
    assert r1.match == "tolerant"
    assert r1.face is not None and r1.area_centerline_mm2 == pytest.approx(12000 * PX2)
    assert {"room_dangling_wall_ids", "room_face_tolerant"} <= codes(plan)


def test_custom_polygon_room():
    data = apartment()
    data["rooms"]["balcony"] = {"id": "balcony", "wallIds": [], "points": [
        {"x": 260, "y": 60}, {"x": 300, "y": 60}, {"x": 300, "y": 100}, {"x": 260, "y": 100}]}
    plan = load_floorplan(data)
    room = plan.rooms["balcony"]
    assert room.match == "custom"
    assert room.face is not None and room.face.area == pytest.approx(40 * 40 * PX2)


def test_adjacency_only_through_passable_openings():
    plan = load_floorplan(apartment())
    edges = plan.room_adjacency()
    assert len(edges) == 1
    edge = edges[0]
    assert {edge.room_a.id, edge.room_b.id} == {"r1", "r2"}
    assert edge.passable is True
    assert edge.opening_ids == ("d1",)
    # The window is not passable; the entry door opens to the outside.
    assert [o.id for o in plan.openings.values() if o.outside] == ["entry", "win"]
    assert [o.id for o in plan.openings.values() if o.outside and o.passable] == ["entry"]
    digest = summarize(plan)
    assert digest["openings"]["exterior_doors"] == 1
    assert digest["connectivity"]["components"] == [["r1", "r2"]]


# ── 3D ─────────────────────────────────────────────────────────────────────

def test_wall_slabs_split_at_opening_heights():
    plan = load_floorplan(apartment())
    params = HeightParams()

    slabs = plan.wall_slabs("interior", params)
    assert [(s.z0, s.z1) for s in slabs] == [(0, 2100), (2100, 3000)]
    assert slabs[0].area_mm2 == pytest.approx(576 * PX2)
    assert slabs[1].area_mm2 == pytest.approx(864 * PX2)

    ext = plan.wall_slabs("exterior", params)
    # Window sill at 900 mm: below the sill only the entry door voids the wall.
    assert [(s.z0, s.z1, round(s.area_mm2 / PX2)) for s in ext] == [
        (0, 900, 7248), (900, 2100, 6528), (2100, 2400, 6960), (2400, 3000, 7680)]


# ── data-quality issues ────────────────────────────────────────────────────

def test_validation_issues_are_collected_on_load():
    data = apartment()
    # Wall referencing a missing node.
    data["walls"]["w6"] = {"id": "w6", "startNodeId": "n1", "endNodeId": "missing",
                           "thickness": 200, "type": "interior"}
    # Zero-length wall between two coincident nodes.
    data["nodes"]["n7"] = {"id": "n7", "x": 50, "y": 60, "connectedWallIds": ["w0"]}
    data["walls"]["w0"] = {"id": "w0", "startNodeId": "n1", "endNodeId": "n7",
                           "thickness": 200, "type": "interior"}
    # Room pointing at a wall that does not exist and forming no face.
    data["rooms"]["r3"] = {"id": "r3", "wallIds": ["nope"]}
    data["openings"]["wide"] = {"id": "wide", "wallId": "w1a", "type": "window",
                                "position": 0.9, "width": 1500, "direction": 1}
    plan = load_floorplan(data)

    found = codes(plan)
    assert {"wall_node_missing", "zero_length_wall", "coincident_nodes",
            "room_dangling_wall_ids", "room_face_unmatched", "opening_exceeds_wall"} <= found
    assert len(plan.walls) == 9  # every record is kept, unusable ones carry empty geometry
    assert plan.walls["w6"].centerline.is_empty and plan.walls["w0"].band.is_empty
    assert "entry" in plan.openings and "wide" in plan.openings
    assert all(isinstance(i.level, str) for i in plan.issues)


def test_digest_is_json_serializable():
    import json
    plan = load_floorplan(apartment())
    text = json.dumps(summarize(plan), ensure_ascii=False)
    assert '"rooms"' in text and "卧室" in text


def test_frame_aligned_axes_and_uv():
    data = apartment()
    data["imageTransform"] = _matrix(rot=math.pi / 2)
    plan = load_floorplan(data)
    (ex, ey), (nx, ny) = plan.frame.aligned_axes()
    assert (ex, ey) == pytest.approx((0.0, 1.0), abs=1e-9)
    assert (nx, ny) == pytest.approx((-1.0, 0.0), abs=1e-9)
    u, v = plan.frame.uv_from_px(10, 0)
    assert (u, v) == pytest.approx((0.0, -10 * S), abs=1e-9)


# ── polish: issue payloads, clamped openings, custom rooms, graph, grid ────

def test_issue_message_and_entity_are_not_swapped():
    data = apartment()
    data["walls"]["w6"] = {"id": "w6", "startNodeId": "n1", "endNodeId": "missing",
                           "thickness": 200, "type": "interior"}
    data["rooms"]["r1b"] = {"id": "r1b", "wallIds": data["rooms"]["r1"]["wallIds"]}
    plan = load_floorplan(data)

    missing = next(i for i in plan.issues if i.code == "wall_node_missing")
    assert missing.entity == "w6"
    assert missing.message.startswith("wall references a missing node")

    shared = next(i for i in plan.issues if i.code == "room_face_shared")
    assert set(shared.entity.split(",")) == {"r1", "r1b"}
    assert "same face" in shared.message

    # a message is prose, never a bare record id
    assert all(" " in issue.message for issue in plan.issues)


def test_opening_overrunning_the_span_is_clamped_and_reported():
    data = apartment()
    data["openings"]["wide"] = {"id": "wide", "wallId": "w1a", "type": "window",
                                "position": 0.9, "width": 1500, "direction": 1}
    plan = load_floorplan(data)
    op = plan.openings["wide"]

    assert op.width_mm == 1500          # the declared width is kept
    assert op.effective_width_mm == pytest.approx(250.0)   # only 10% of w1a fits
    assert op.clamped is True
    assert "opening_exceeds_wall" in codes(plan)
    # start/end agree with the clamped cut strip: nothing sticks out of the wall
    assert plan.walls["w1a"].centerline.distance(Point(op.end_mm)) < 1e-6
    assert plan.walls["w1a"].length_mm == pytest.approx(2500.0)

    normal = plan.openings["d1"]
    assert normal.clamped is False
    assert normal.effective_width_mm == pytest.approx(normal.width_mm)


def test_custom_room_links_openings_geometrically():
    data = apartment()
    data["rooms"] = {"custom": {"id": "custom", "wallIds": [], "points": [
        {"x": 150, "y": 60}, {"x": 250, "y": 60},
        {"x": 250, "y": 180}, {"x": 150, "y": 180}]}}
    plan = load_floorplan(data)
    room = plan.rooms["custom"]

    assert room.match == "custom"
    # d1 sits on the hand-drawn boundary (w5), win on w3a; entry (w4) is outside.
    assert [o.id for o in room.doors] == ["d1"]
    assert [o.id for o in room.windows] == ["win"]
    assert plan.openings["d1"].room_ids == ["custom"]


def test_digest_uses_deduplicated_face_area():
    data = apartment()
    data["rooms"]["r1b"] = {"id": "r1b", "wallIds": data["rooms"]["r1"]["wallIds"]}
    plan = load_floorplan(data)
    rooms = summarize(plan)["rooms"]

    assert rooms["count"] == 3 and rooms["faces"] == 2
    assert rooms["total_area_m2"] == pytest.approx(22.5)       # 3 room rows
    assert rooms["total_face_area_m2"] == pytest.approx(15.0)  # 2 faces


def test_wall_graph_degrees_components_and_isolated_nodes():
    data = apartment()
    data["nodes"]["lonely"] = {"id": "lonely", "x": 10, "y": 10, "connectedWallIds": []}
    plan = load_floorplan(data)

    assert plan.degrees()["n5"] == 3
    assert plan.isolated_nodes() == ["lonely"]
    groups = plan.components()
    assert groups[0] == sorted(["n1", "n2", "n3", "n4", "n5", "n6"])
    assert ["lonely"] in groups
    assert plan.node_adjacency()["n1"] == {"n5", "n4"}


def test_frame_to_grid_rotates_geometry_by_minus_rotation():
    data = apartment()
    data["imageTransform"] = _matrix(rot=math.radians(30))
    plan = load_floorplan(data)
    line = plan.walls["w1a"].centerline
    aligned = plan.to_grid(line)

    original = math.degrees(math.atan2(line.coords[-1][1] - line.coords[0][1],
                                       line.coords[-1][0] - line.coords[0][0]))
    rotated = math.degrees(math.atan2(aligned.coords[-1][1] - aligned.coords[0][1],
                                      aligned.coords[-1][0] - aligned.coords[0][0]))
    assert rotated - original == pytest.approx(-30.0, abs=1e-9)
    assert aligned.length == pytest.approx(line.length)     # rotation is isometric


def test_label_mojibake_is_repaired_but_latin_names_are_kept():
    from floorplan_io import repair_mojibake

    assert repair_mojibake("\u00ce\u00d4\u00ca\u00d2") == "卧室"   # GBK read as cp1252
    assert repair_mojibake("Café") == "Café"                      # ASCII present: untouched
    assert repair_mojibake("1") == "1"
    assert repair_mojibake(None) is None

    data = apartment()
    data["rooms"]["r1"]["label"] = "\u00ce\u00d4\u00ca\u00d2"
    plan = load_floorplan(data)
    assert plan.rooms["r1"].label_text == "卧室"
