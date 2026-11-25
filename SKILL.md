---
name: floorplan-json
description: >
  Read and analyse FloorPlan JSON exported from the floorplan-konva editor at
  https://konva.archialgo.com/ — parametric plan data that stores nodes, walls,
  openings, rooms and an image transform instead of geometry. Rebuilds scale,
  wall bodies (mitre joins, cross-type trimming, opening cuts), room faces and
  adjacency exactly as the TypeScript source does, then answers area, adjacency,
  bubble-diagram, evacuation, SVG / PNG / GeoJSON / GLB questions. Use when a
  task involves floorplan-*.json, floorplan-konva or konva.archialgo.com exports,
  户型图 / 平面图 JSON, 房间面积, 墙身面积, 墙中线面积, 房间邻接, 共墙, 泡泡图,
  门窗表, 开窗率, 疏散距离, 3D 平面图导出.
---

# FloorPlan JSON 读取与分析

`floorplan-konva`（在线平台 <https://konva.archialgo.com/>：画平面图 + AI 识别 + 出图）
导出的 `floorplan-*.json` 是**参数化**平面数据：存的是节点、墙、门窗参数、房间边界 id，
几何要在读取端重算。本技能给出协议、必须复刻的算法、一个可照抄的最小读入器，以及
可选的分析配方。

数据只有一条来源：在 <https://konva.archialgo.com/> 上导入底图并**标定比例尺**
（得到 `imageTransform`），沿底图绘出或让 AI 识别出节点 / 墙 / 门窗 / 房间，再导出
JSON（`ExportService.exportToJSON()`，文件名 `floorplan-<timestamp>.json`，
`version: "1.0.0"`）。本技能**只读**这个文件：不回写、不连平台、不联网。

拿到文件后第一步永远是看 `plan.frame.calibrated` / `plan.issues`：有底图但矩阵是单位阵
的文件（`uncalibrated`）比例尺未知，不能出任何面积或长度结论——回平台重新标定再导出。

## 读取这条路只有四步

1. **比例尺**：`s = (hypot(a,b) + hypot(c,d)) / 2`（mm/px）。单位矩阵 = 未标定。
2. **节点/墙**：位置是底图像素，尺寸是毫米。`x_mm = x·s`，`y_mm = (imgH − y)·s`。
3. **面**：墙中线按采样段 `unary_union` → `polygonize`，取全部有界面。
4. **房间 → 面**：`set(room.wallIds) == set(face.wallIds)` 精确匹配，过期则
   Jaccard ≥ 0.5 容差匹配，再不行丢弃并记 issue。

墙身实体（斜接拐角、跨类型修整、门窗退洞）是第二层，九条约定见下。

**其余实体不需要读入器**：柱、家具、文字、轴网、竖向构件的字段是自足参数，
`references/recipes.md` §10–§13 每项 10–30 行；3D 由 `plan.wall_slabs()` 拉伸。

## 最小读入器（可照抄，约 50 行）

```python
import json, math
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union

def read(path):
    d = json.load(open(path, encoding="utf-8"))
    t = d.get("imageTransform") or {}
    a, b = t.get("a", 1.0), t.get("b", 0.0)
    c, dd = t.get("c", 0.0), t.get("d", 1.0)
    s = (math.hypot(a, b) + math.hypot(c, dd)) / 2          # mm per px
    H = float((d.get("metadata") or {}).get("imageHeight") or 0)
    nodes = {i: (r["x"] * s, (H - r["y"]) * s) for i, r in d["nodes"].items()}
    lines, by_id = [], {}
    for wid, w in d["walls"].items():
        if w.get("curveMode") == "curve":
            continue                       # 曲线墙要 Catmull-Rom，见 algorithms.md §3
        p, q = nodes.get(w["startNodeId"]), nodes.get(w["endNodeId"])
        if not p or not q or p == q:
            continue                       # 缺节点 / 零长墙：保留记录、几何置空
        line = LineString([p, q])
        lines.append(line)
        by_id[wid] = line
    faces = [(poly, [wid for wid, line in by_id.items()
                     if poly.boundary.intersection(line).length > 1e-6])
             for poly in polygonize(unary_union(lines))]

    def match(wanted):                     # 精确 -> Jaccard >= 0.5 容差
        for poly, walls in faces:
            if set(walls) == wanted:
                return poly
        best, score = None, 0.5
        for poly, walls in faces:
            jaccard = len(set(walls) & wanted) / len(set(walls) | wanted)
            if jaccard > score:
                best, score = poly, jaccard
        return best

    rooms = []
    for rid, r in d["rooms"].items():
        hit = match(set(r["wallIds"]))
        rooms.append((rid, r.get("label"), hit.area / 1e6 if hit else None))
    return s, faces, rooms
```

这段**刻意**省略了三件会让数字偏离编辑器的事：曲线墙采样、墙身缓冲、洞口退洞。
补齐它们就是 `scripts/floorplan_io.py`（1374 行，按源码模块分段，函数名一一对应）。
每一步对应的 shapely 图元汇总在 `floorplan_report.PIPELINE`，交互式 CLI 会直接打印。

## 九条硬性约定

以下九条是与编辑器结果一致的充分条件，任何一条漏掉都会出现"看起来对、数字不对"：

| # | 约定 | 漏掉的后果 |
|---|---|---|
| 1 | `s = (hypot(a,b)+hypot(c,d))/2`；矩阵为单位阵 = 未标定 | 比例尺错，面积误差按平方放大 |
| 2 | `x_mm = x·s`，`y_mm = (imageHeight − y)·s` | y 不翻 → 图上下镜像 |
| 3 | 位置是**底图像素**，尺寸是**毫米**，不要统一乘 `s` | 墙厚变 `300·s`、面积整体偏 |
| 4 | 建墙链**先按 type 分组**，再用 `(thickness, curveMode)` 作键、度数 2 才延链 | 与其它类型相接处链断裂，墙身面积差 ~0.8% |
| 5 | 曲线采样取**链第一面墙**的 tension(0.95)/segments(16)，非末边少一个采样点 | 曲线墙几何与编辑器不符 |
| 6 | 缓冲 `half=thickness/2`、`quad_segs=16`、`mitre_limit=5`，闭合链按环缓冲 | 拐角处面积偏差；闭合链会被填实成实心块 |
| 7 | 修整顺序 外墙 > 砖墙 > 幕墙 > 内墙，参照系是**未退洞口**的实心带 | 轻墙穿过重墙门洞；不同墙体重复计量体积 |
| 8 | 洞口 `position` 是**弧长比例**、`width` 沿弧长、切割带沿**局部法向** | 门窗位置漂移，曲线墙上洞口切不穿 |
| 9 | 3D 按洞口高度分段拉伸（每段只减该段贯通的洞口） | 门窗洞贯通整层墙高，体积偏小 |

房间面提取用 `unary_union(采样段) → polygonize` 即可，与源码的半边（DCEL）遍历等价。

## 体检（读入后第一件事）

`plan.issues` 给每条异常打 `error / warning / info`，`report.summarize(plan)["issues"]`
可直接输出。**出现 `uncalibrated`（error）时不要给任何面积/长度结论**——那是有底图
但没标定的文件，mm 尺寸和像素位置不在同一尺度上。

真实文件里出现过、必须容忍的情况：

| issue code | 含义 | 处理 |
|---|---|---|
| `uncalibrated` | 有底图 + 单位矩阵 | 拒绝出量化结论 |
| `zero_length_wall` / `coincident_nodes` | 合并节点残留 | 保留记录、几何置空 |
| `room_dangling_wall_ids` / `room_face_unmatched` | 房间边界墙已删 | 精确匹配 → Jaccard 容差匹配 → 否则丢弃 |
| `room_face_shared` | 两个房间指向同一个面 | 面积不要直接求和；用 `summarize()["rooms"]["total_face_area_m2"]`（按面去重） |
| `opening_exceeds_wall` | `position·墙长 + width > 墙长` | 几何已夹到墙长；`opening.effective_width_mm` 是实际可用宽度（`width_mm` 保留导出值） |
| `node_wall_links_inconsistent` | `connectedWallIds` 过期 | 永远从 `walls[]` 反推度数 |

另外两个纯数据坑：

- `rooms[].label` 可能是 U+FFFD（上游编码丢字节，不可逆）。`room.label_text`
  会自动退回 color → 预设语义（卧室/客厅/走廊/卫生间/厨房/阳台…）。
  纯高位字节的 GBK-as-cp1252 乱码（`ÎÔÊÒ`）由 `repair_mojibake` / `label_text` 自动复原，
  含 ASCII 的拉丁名（`Café`）不动。
- `rooms[].color` 同时是语义分类（`ROOM_PRESETS`），不是装饰色。

## 验证新实现是否正确

三条互不依赖的检查，全过才算对齐：

1. **自洽**：`wall_solid(type, cut_openings=False).area` ≥ `wall_solid(type).area`；
   `band − solid` 应约等于门窗洞口的截面积之和。
2. **回归**：`FLOORPLAN_SAMPLE=<path> uv run pytest -q tests/test_reference_parity.py`（缺省用
   `assets/reference-floorplan.json`），期望 `scale=25.731177959342716`、外墙
   14.914740 / 10.970546 m²、8 个面、13 对相邻面。
   这些数字直接取自 TypeScript 源码的输出。
3. **3D**：`plan.wall_slabs(type)` 的 z 分界应等于洞口高度值（0/900/2100/2400/3000），
   最顶一层等于未退洞的实心带面积；按 recipe §13 拉伸后每个 mesh 应 `is_watertight`。

## 直接可用的实现

```bash
uv add shapely                        # core 只依赖 shapely
uv sync --extra png                   # 出 PNG 时才需要 matplotlib
uv sync --extra glb                   # 出 GLB 时才需要 trimesh
cp <skill>/scripts/floorplan_io.py .  # 读入器（协议 → 几何）
cp <skill>/scripts/floorplan_report.py .   # 摘要 + SVG/PNG/GeoJSON/GLB + 图元对照表
```

```python
from floorplan_io import load_floorplan
import floorplan_report as report

plan = load_floorplan("floorplan-*.json")    # 路径（str/Path）/ JSON 字符串 / dict
print(report.summarize(plan))                # 计数、面积、邻接、连通、问题
plan.wall_solid("exterior")                  # 真实墙身截面（mm²，已退洞）
plan.rooms["<id>"].area_m2                   # 墙中线围合面积
plan.rooms["<id>"].net_area_m2               # 扣墙厚
plan.room_adjacency()                        # 房间对 + 共享墙 + 可通行门
plan.degrees()                               # 节点度；components() / isolated_nodes() 同类
plan.to_grid(geom)                            # 把 mm 几何旋到标定网格（仅展示用）
plan.wall_slabs("exterior")                  # 按洞口高度分段的 3D 截面
plan.raw                                     # 未解析的原始记录（柱/家具/轴网/…）

report.write_png(plan, "plan.png")           # 平面图（matplotlib）
report.write_svg(plan, "plan.svg")
report.write_geojson(plan, "plan.geojson")
report.write_glb(plan, "plan.glb")           # 3D（trimesh，需 --extra glb）
```

命令行**不需要记参数**：直接跑，它会列出 `./data/*.json` 让你选文件、选动作；
每完成一步都会打印用到的图元与算法（内容就是 `report.PIPELINE`）。

```bash
uv run python scripts/floorplan_inspect.py             # 交互：选文件 → 选动作
uv run python scripts/floorplan_inspect.py plan.json   # 脚本用法：只打印摘要
```

菜单：`1` 摘要表（含“两个房间同面”警告与去重后的面合计）、`2` 平面图 PNG+SVG、
`3` GeoJSON、`4` GLB、`5` 讲解（一条流水线从 `LineString` 到 `extrude_polygon`）。
产物写到 `out/<文件名>/`。

技能自带一个合成样例 `assets/sample-floorplan.json`（2 房间 / 7 墙 / 3 洞口）；
`tests/` 里 40 个用例覆盖核心算法、配方、交互式 CLI、SVG/PNG 与 TS 对账。

## 常见追问怎么回

- "每个房间多大" → `room.area_m2`（墙中线，和编辑器侧栏一致）；
  "实际能用多大" → `room.net_area_m2`（扣墙厚）。两者别混用；
  "一层多少面积" → `summarize()["rooms"]["total_face_area_m2"]`（按面去重，房间行会重复计同面）。
- "哪里是承重墙" → 数据只有 `type`，`exterior/brick_wall` 按重墙处理；
  没有材料、结构信息，不要臆造。
- "层高多少" → JSON 里**没有**，是导出假设值（默认 3000，`HeightParams`）。
- "朝向" → `frame.aligned_axes()`（标定后的网格方向），或轴网 `rotation`。
- "导出 3D" → `report.write_glb(plan, "plan.glb")`；原理见 recipe §13。
  要 IFC 请回编辑器导出（本技能只读 JSON）。
- "柱/家具/轴网/楼梯呢" → 没进读入器：字段是自足参数，recipe §10–§12 十行以内。
- "功能关系/泡泡图" → recipe §2.1：气泡=房间面，边=共墙，实线=有门；数据用
  `face_adjacency()` 或 `summarize()["adjacency"]`（面-面，不会把同面房间交叉相乘）。

## 文件

```
SKILL.md                    技能正文（协议要点、九条约定、体检、验证）
README.md                   仓库说明：数据从哪来、怎么装、怎么跑
references/protocol.md      数据协议（字段/单位/语义/异常）
references/algorithms.md    算法复刻（公式 + 参数 + 实测对账）
references/recipes.md       分析配方（面积/邻接/工程量/掩膜/疏散/可选实体/3D）
scripts/floorplan_io.py     读入器：九条约定 + 体检（1374 行，只依赖 shapely）
scripts/floorplan_report.py 摘要 + SVG/PNG/GeoJSON/GLB + PIPELINE 图元对照表
scripts/floorplan_inspect.py 交互式 CLI（选文件 → 选动作 → 打印用到的图元）
assets/sample-floorplan.json 合成样例（2 房间 / 7 墙 / 3 洞口）
assets/reference-floorplan.json 真实导出（回归对账用的样本）
tests/                      40 个用例（含与 TS 源码的回归对账）
```
