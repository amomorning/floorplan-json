# 分析配方

前置：

```python
from floorplan_io import HeightParams, load_floorplan
import floorplan_report as report

plan = load_floorplan("plan.json")
```

所有几何单位是毫米、y 向上；面积换算 m² 时除以 `1e6`。
`references/protocol.md` 里的参数化实体（柱/家具/文字/轴网/竖向构件）不在读入器里，
§10–§13 给出对应配方；3D 拉伸在 §13。

## 1. 面积表（房间清单 + 净面积 + 门窗）

```python
rows = []
for room in plan.rooms.values():
    if room.face is None:          # 边界已失效
        continue
    rows.append({
        "id": room.id,
        "label": room.label_text,           # 自动用 colour 预设兜底
        "preset": room.preset_label,
        "area_m2": round(room.area_m2, 2),        # 墙中线围合
        "net_m2": round(room.net_area_m2, 2),     # 扣墙厚
        "doors": [o.id for o in room.doors],
        "windows": [o.id for o in room.windows],
    })
```

`report.summarize(plan)["rooms"]` 已经给出同样的表，可直接 `json.dumps`。

## 2. 邻接图与连通性

```python
edges = plan.room_adjacency()          # 只含“共享边界”的房间对
passable = [e for e in edges if e.passable]

# 门图连通分量：同一分量内不用出门就能互通
import networkx as nx
g = nx.Graph()
for e in passable:
    g.add_edge(e.room_a.id, e.room_b.id, walls=e.shared_wall_ids, doors=e.opening_ids)
print(list(nx.connected_components(g)))
```

`report.summarize(plan)["connectivity"]` 给出同样的分量列表；`isolated` 是“没有可通行邻居”的房间。

注意：

- 窗户不算可通行（`PASSABLE_OPENING_TYPES` 不含 `window`）。
- 同一面墙两侧的房间都会挂上这扇门，因此门在 `room.doors` 里可能重复出现。
- 两个房间记录指向同一个面时不是邻接关系（本实现已按面去重）。

## 2.1 功能泡泡图（建筑师看的功能图结构）

一个气泡 = 一个房间**面**（同面房间合并成一个气泡），半径 ∝ √面积；边 = 共用墙，
有门实线、只共墙虚线。`room_adjacency()` 会把指向同一面的两个房间交叉相乘，
做功能图用 `face_adjacency()`（或 `summarize()["adjacency"]["pairs"]`）。

```python
import math
import networkx as nx

faces = {}
for room in plan.rooms.values():
    if room.face is not None:
        faces.setdefault(room.face.id, []).append(room)

g = nx.Graph()
for fid, rooms in faces.items():
    face = rooms[0].face
    g.add_node(fid,
               area_m2=face.area_m2,
               radius=math.sqrt(face.area_mm2 / math.pi) * 0.55,   # 真实比例 55%
               pos=face.label_anchor,
               label=" / ".join(r.label_text or "房间" for r in rooms))

for fa, fb, wall_ids, shared_mm in plan.face_adjacency():
    doors = [o for o in plan.openings.values()
             if o.wall is not None and o.wall.id in wall_ids and o.passable]
    g.add_edge(fa.id, fb.id, shared_mm=shared_mm, doors=len(doors))

# 画：geo = 用 pos + radius（圆面积=房间面积）；拓扑 = nx.spring_layout(weight=shared_mm)
# 入口：外墙上的可通行洞口 (o.outside and o.passable) 连一个“室外”气泡
```

`doors == 0` 的边是“两个房间只共墙、没有门”：浴室/卫生间出现这种边，
通常意味着门在别的墙上，或者房间的 `wallIds` 过期了。

## 3. 门窗表 / 开窗率

```python
doors = [o for o in plan.openings.values() if o.passable]
windows = [o for o in plan.openings.values() if o.type == "window"]

exterior = plan.wall_solid("exterior")                   # 已退洞的外墙实心截面
envelope_len = sum(w.length_mm for w in plan.walls.values()
                   if w.type in ("exterior", "curtain_wall", "brick_wall")) / 1000
window_area = sum(o.width_mm for o in windows) / 1000 * windows[0].z_range(HeightParams())[1] / 1000
facade_area = envelope_len * (3000 / 1000)               # 用统一层高
print("窗墙比 ≈", round(window_area / facade_area, 3))
```

洞口高度由 `HeightParams` 决定（门 0–2100，窗 900–2400，可改）：

```python
params = HeightParams(window_sill=1000, window_height=1800, door_height=2200)
z0, z1 = plan.openings["<id>"].z_range(params)
```

## 4. 墙体工程量

```python
for wtype in ("exterior", "brick_wall", "curtain_wall", "interior", "railing"):
    solid = plan.wall_solid(wtype)                 # mm²，已退洞口
    band = plan.wall_solid(wtype, cut_openings=False)
    length = sum(w.length_mm for w in plan.walls.values() if w.type == wtype) / 1000
    print(wtype, {
        "length_m": round(length, 2),
        "solid_area_m2": round(solid.area / 1e6, 3),
        "band_area_m2": round(band.area / 1e6, 3),
        "net_volume_m3": round(solid.area * 3000 / 1e9, 3),   # 层高 3000
    })
```

- `wall_solid()` 是**并集后**的实体截面（拐角斜接、跨类型修整都做了），
  所以它的面积≤ 逐墙矩形面积之和；做工程量用它，做「每面墙一个框」的标注用 `wall.band`。
- 减掉的洞口面积 = `band − solid`；若某个洞口横跨两段墙链，退洞会把它切成两段截面，
  这是正确行为（门窗是真实洞口，不是贴图）。

## 5. 用 GeoJSON / 3D 做外部校验

```bash
uv run python scripts/floorplan_inspect.py plan.json --geojson plan.geojson
```

- GeoJSON：`wall_solid` / `wall_centerline` / `opening` / `room`，坐标 mm。可直接丢进
  QGIS 或 `geopandas.read_file`，与 DXF 导出结果叠图核对。
- 3D：见 §13（`plan.wall_slabs()` + trimesh，`walls:<type>` / `rooms:<id>`）。
  与编辑器 IFC 导出对照体积：

  ```python
  vol = lambda name: scene.geometry[name].volume / 1e9   # m³
  ```

  IFC 里 `IfcSpace` 的 `GrossFloorArea` 用的是**墙中线面积**（`m²`，且
  `IfcAreaMeasure` 恒为 m²，即使长度单位是 mm）——核对时别拿净面积去对。

## 6. 给机器学习用的栅格掩膜

```python
import numpy as np
from PIL import Image, ImageDraw
from shapely.geometry import mapping

W, H = int(plan.frame.img_w), int(plan.frame.img_h)
img = Image.new("L", (W, H), 0)
draw = ImageDraw.Draw(img)

def to_px(ring_mm):                       # mm(y↑) -> 底图像素(y↓)
    s = plan.frame.scale
    return [((x / s), plan.frame.img_h - (y / s)) for x, y in ring_mm]

for room in plan.rooms.values():
    if room.face is not None:
        draw.polygon(to_px(room.face.ring), fill=len(room.id) % 255 or 1)
img.save("rooms.png")
```

要点：掩膜一定要回到**底图像素坐标系**，否则和原图对不上。墙体掩膜用
`plan.wall_solid()` 转为像素后填充；门窗用 `op.footprint`。

## 7. 疏散 / 可达性检查

```python
rooms = {r.id: r for r in plan.rooms.values() if r.face}
exit_doors = [o for o in plan.openings.values() if o.outside and o.passable]
entry_rooms = {rid for o in exit_doors for rid in o.room_ids}

# BFS：每个房间到最近出口经过几个房间
from collections import deque
adj = {}
for e in plan.room_adjacency():
    if e.passable:
        adj.setdefault(e.room_a.id, set()).add(e.room_b.id)
        adj.setdefault(e.room_b.id, set()).add(e.room_a.id)
dist = {rid: 0 for rid in entry_rooms}
queue = deque(entry_rooms)
while queue:
    cur = queue.popleft()
    for nxt in adj.get(cur, ()):
        if nxt not in dist:
            dist[nxt] = dist[cur] + 1
            queue.append(nxt)
unreachable = [rid for rid in rooms if rid not in dist]
```

`o.outside` 表示洞口锚在外墙/幕墙/砖墙/栏杆上（按墙体类型判定，不是几何射线判定）。
需要更严格的“是否真的通向室外”，用 `op.footprint.boundary` 与外墙外表面求交。

## 8. 与 DXF / IFC 对账

| 量 | JSON 里怎么算 | DXF/IFC 里怎么看 |
|---|---|---|
| 墙长 | `sum(w.length_mm)` | DXF 中线（需自己算），IFC 无直接属性 |
| 墙实体面积 | `wall_solid(type).area` | DXF 墙体轮廓面积；IFC 拉伸体积 ÷ 层高 |
| 房间面积 | `room.area_m2`（中线） | IFC `Pset_SpaceCommon.GrossFloorArea`（m²） |
| 门窗尺寸 | `op.width_mm` × `z_range` | IFC `IfcDoor/IfcWindow` 的 `OverallWidth/Height` |
| 洞口位置 | `op.start_mm`/`end_mm` | IFC `IfcOpeningElement` 的 Z 分段截面 |

如果三项以上对不上，优先怀疑：未标定（比例尺=1）、`y` 轴没翻、或把 `level_change`
当成实体墙。

## 9. 常用只读属性速查

| 需求 | 表达式 |
|---|---|
| 楼层轮廓 | `plan.wall_solid("exterior")` 的外环（或全部墙身的并集） |
| 建筑面积 | `sum(r.area_centerline_mm2 for r in plan.rooms.values())/1e6` |
| 使用面积 | `sum(r.area_net_mm2 for r in plan.rooms.values())/1e6` |
| 洞口数量 | `len(plan.openings)`（按 `type` 过滤） |
| 轴网开间 | §12 `axis_bays_mm(plan, plan.raw["axisGrids"][gid])` |
| 立面朝向 | `plan.frame.aligned_axes()` 给出标定后的两个单位向量 |
| 问题清单 | `plan.issues`（`level` ∈ error/warning/info；`message` 是句子，`entity` 是记录 id） |
| 节点度 / 连通分量 / 孤立节点 | `plan.degrees()` / `plan.components()` / `plan.isolated_nodes()` |
| 洞口实际宽度 | `opening.effective_width_mm`（`clamped` 为真 = 导出宽度超出墙长） |
| 去重后的建筑面积 | `summarize(plan)["rooms"]["total_face_area_m2"]`（按面，不重复计同面房间） |
| 旋转到标定网格 | `plan.to_grid(geom)`（`Frame.to_grid`，仅影响展示，面积/长度不变） |

## 10. 柱 / 家具 / 文字（参数表，十行以内）

这三种记录只存包围盒与变换，没有图元；`(x, y)` 是底图像素，宽高是毫米，
`rotation` 是度、屏幕顺时针。

```python
import math
from shapely.geometry import Point, Polygon

def rect(cx, cy, w, h, rotation_deg):
    """yoloGeometry.rectToCorners：旋转是度、屏幕顺时针。"""
    t = math.radians(rotation_deg)
    c, s = math.cos(t), math.sin(t)
    local = [(w / 2, h / 2), (-w / 2, h / 2), (-w / 2, -h / 2), (w / 2, -h / 2)]
    return Polygon([(cx + x * c - y * s, cy + x * s + y * c) for x, y in local])

def column_footprint(plan, rec):
    cx, cy = plan.frame.pt(rec["x"], rec["y"])
    if rec.get("shape") == "circle":
        return Point(cx, cy).buffer(rec["width"] / 2, quad_segs=32)
    w = float(rec["width"])
    h = w if rec.get("shape") == "square" else float(rec.get("height") or w)
    return rect(cx, cy, w, h, float(rec.get("rotation") or 0))

def furniture_footprint(plan, rec):
    cx, cy = plan.frame.pt(rec["x"], rec["y"])
    return rect(cx, cy, float(rec["width"]), float(rec["height"]),
                float(rec.get("rotation") or 0))

def text_anchor(plan, rec):
    return plan.frame.pt(rec["x"], rec["y"])     # 文本框左上角
```

`furniture.symbolId` 指向内置图库（`BUILTIN_SYMBOLS`，见 protocol.md §3.7）；
要画图元得自己按 symbol 重画，包围盒只能做占位。

## 11. 竖向构件包围盒（`VerticalGeometryService` + `StairCalculator`）

楼梯的踏步由层高与梯井长度双向推导：目标踢面 165、最小踏面 260、最小平台 1200，
踏步数 `round(层高/165)` 后夹下限（U 形梯再取偶），踏面 `max((梯井长−平台)/踏步数, 260)`。

```python
import math

TARGET_RISER, MIN_TREAD, MIN_LANDING = 165.0, 260.0, 1200.0

def stair_steps(floor_height, override, minimum, even=False):
    n = int(override) if override else int(round(floor_height / TARGET_RISER))
    n = max(n, minimum)
    return n + 1 if (even and n % 2) else n

def vertical_bbox(rec):
    """局部包围盒 (宽, 长)，毫米；再套 rect() 得到 footprint。"""
    kind = rec.get("kind")
    if kind == "elevator":
        return (rec["shaftWidth"], rec["shaftDepth"])
    if kind == "ramp":
        return (rec["rampLength"], rec["rampWidth"])
    if kind == "escalator":                       # 30° 倾斜 + 两端 2500 平台
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
```

## 12. 轴网（轴位置在轴网自身坐标系）

`horizontalAxes` 是 **u**（纵轴 ①②③ 的坐标，画成竖线），`verticalAxes` 是 **v**
（横轴 A B C，画成横线）；`rotation` 是弧度。

```python
def axis_lines(plan, grid):
    angle = float(grid.get("rotation") or 0)
    c, s = math.cos(angle), math.sin(angle)
    ox, oy = grid.get("gridOffsetX", 0), grid.get("gridOffsetY", 0)
    ext = grid.get("extent") or {"minU": -5000, "maxU": 5000,
                                "minV": -5000, "maxV": 5000}

    def world(u, v):
        return plan.frame.pt(ox + u * c - v * s, oy + u * s + v * c)

    lines = [[world(u, ext["minV"]), world(u, ext["maxV"])]
             for u in grid.get("horizontalAxes") or []]
    lines += [[world(ext["minU"], v), world(ext["maxU"], v)]
              for v in grid.get("verticalAxes") or []]
    return lines

def axis_bays_mm(plan, grid):
    out = {}
    for key in ("horizontalAxes", "verticalAxes"):
        vals = sorted(grid.get(key) or [])
        out[key] = [(b - a) * plan.frame.scale for a, b in zip(vals, vals[1:])]
    return out
```

## 13. 3D：按洞口高度分段拉伸

现成的用 `report.write_glb(plan, "plan.glb")`（需 `uv sync --extra glb`）；
下面是它内部的原理，方便自己改。`plan.wall_slabs(type)` 已按洞口高度切段（规则 9），
槽口也已退掉；每段单独拉伸即可。`MultiPolygon` 必须拆开，`extrude_polygon` 只吃单个 `Polygon`。

```python
import trimesh
from floorplan_io import HeightParams, SOLID_WALL_TYPES

def extrude(polygon, z0, z1):
    polys = [polygon] if polygon.geom_type == "Polygon" else list(polygon.geoms)
    meshes = []
    for poly in polys:
        mesh = trimesh.creation.extrude_polygon(poly, height=z1 - z0)
        if z0:
            mesh.apply_translation((0.0, 0.0, z0))
        meshes.append(mesh)
    return meshes

def extrude_scene(plan, params=None):
    params = params or HeightParams()
    groups = {}
    for wtype in SOLID_WALL_TYPES:
        for slab in plan.wall_slabs(wtype, params):
            groups.setdefault(f"walls:{wtype}", []).extend(extrude(slab.polygon, slab.z0, slab.z1))
    for rid, room in plan.rooms.items():
        if room.face is not None:
            groups[f"rooms:{rid}"] = extrude(room.face.polygon, 0.0, params.wall_height)
    scene = trimesh.Scene()
    for name, meshes in groups.items():
        merged = meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
        scene.add_geometry(merged, geom_name=name, node_name=name)
    return scene

# extrude_scene(plan).export("plan.glb")
```

层高、门高、窗台高等是导出假设值（`HeightParams`，默认 3000/2100/900+1500）；
JSON 里没有层高字段。
