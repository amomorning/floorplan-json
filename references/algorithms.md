# 几何算法：从 floorplan-konva 源码到 Python

数据协议只给参数，几何全在源码里算。要得到与编辑器一致的结果，必须复刻下列
算法；每一条都对应一个 TypeScript 源文件与函数，后面给出可直接照抄的公式。
参考实现见 `scripts/floorplan_io.py`（函数名一一对应）；它只做 §1–§8，摘要与出图在
`scripts/floorplan_report.py`，可选实体（柱/家具/文字/轴网/竖向构件）见 recipes.md §10–§12。

## 0. 依赖

| 用途 | 库 | 对应源码里的 |
|---|---|---|
| 2D 缓冲区、布尔运算、面提取 | `shapely>=2.0`（GEOS） | `jsts`（JTS 移植） |
| 三角化拉伸（可选，recipe §13） | `trimesh` + `mapbox-earcut` | `ifcWriter` 的 `IfcExtrudedAreaSolid` |

jSTS 与 GEOS 同源（都是 JTS 的移植），所以 `BufferOp` / `OverlayOp` 的行为可以直接用
`shapely.buffer` / `union` / `difference` 复现——但**参数必须对齐**，否则形状在
拐角处会差几个百分点（见 §7 的实测）。

## 1. 比例尺（`TransformService`）

```
s = ( hypot(a,b) + hypot(c,d) ) / 2        // mm/px
θ = atan2(b, a)                            // 弧度；网格方向
```

## 2. 墙链（`WallBufferService.buildWallChains`）

```
key(w) = (thickness, curveMode ?? 'straight')
walk:  取一面未访问的墙 → 沿两端向两边扩展，
       只在下一点的“同类型墙体子集”中度数为 2 时继续，
       且下一面墙未访问、key 相同。
```

两条容易踩的坑：

1. **必须按墙体类型分别建链**。导出路径是
   `buildPresentationWallRingsByType → buildPresentationWallGeometry(typeWalls, …)`，
   即“一次只喂一种类型”。若把全部墙一起建链，与其它类型共享的节点度数为 3，
   链会在那里断开——实测面积差 0.8%。
2. 度数只数**给定的这批墙**，不是 `connectedWallIds`。

## 3. 曲线墙采样（`WallGeometryService` / `utils/curve.ts`）

链级向心 Catmull-Rom（α = 0.5），逐边转三次 Bézier：

```
d01 = |P1−P0|^0.5,  d12 = |P2−P1|^0.5,  d23 = |P3−P2|^0.5
k   = tension / 3
w1  = d12 / (d01 + d12)        w2 = d12 / (d12 + d23)
C1  = P1 + (P2 − P0) · k · w1
C2  = P2 − (P3 − P1) · k · w2
B(t)= (1−t)³P1 + 3(1−t)²t·C1 + 3(1−t)t²·C2 + t³P2
```

参数解析：`tension = min(curveTension ?? 0.95, 1.5)`、
`segments = clamp(curveSegments ?? 16, 4, 64)`，**取该链第一面墙的值**。

采样点分配（`chainSampledSpans`）：每条边发 `segments` 个采样，但**非最后一条边只发
`segments − 1` 个**（避免重复顶点）；开放链最后一条边发 `segments + 1` 个。
因此开放链：节点 k 恰好落在第 `k·segments` 个采样点上，第 k 面墙的跨度 =
`samples[k·s .. k·s+s]`。

闭合链（首节点 == 末节点）：先丢掉重复末节点，用 `closed=true` 插值，第 k 面墙 =
`samples[k·s .. k·s+s−1]` 再补上下一节点采样（环绕边补 `samples[0]`）。

最后按墙自身的参数化方向（`t=0` 在 `startNodeId`）决定是否反转该跨度。

直线墙直接取 `[startNode, endNode]`——与曲线墙共用同一套弧长参数化，
所以直线墙上的历史数据逐位不变。

## 4. 墙身缓冲（`WallBufferService.bufferChain`）

```
half        = thickness / 2                       // 毫米
quad_segs   = 16                                  // ARC_STEPS
mitre_limit = 5
```

- **闭合链**（首节点 == 末节点且节点数 ≥ 4）：按环缓冲 →
  `LineString(闭合点列).buffer(half, cap_style='flat', join_style=J, mitre_limit=5, quad_segs=16)`，
  结果为带孔的环带（洞口环=院子上空）。`J` 取链内统一的转角样式，混合则 `mitre`。
- **开放链**：内部节点逐个映射 `cornerStyle` → `join_style`
  （`mitre`/`round`/`bevel`）；端点映射 `capStyle` → `cap_style`
  （缺省 `flat`；`round`/`square` 另外补一段端头几何再并集）。
  所有内部节点 join 样式一致时一次缓冲完，否则按 3 节点子链分别缓冲再并集。

## 5. 跨类型修整（`buildPresentationWallRingsByType`）

```
顺序（强 → 弱）：exterior > brick_wall > curtain_wall > interior
for type in 顺序:
    geom  = 该类型的链并集，退开口洞          // 有洞版本
    if 已处理过更强类型: geom = geom.difference(更强类型的“实心带”并集)
    band  = 该类型的链并集，不退洞口          // 实心带 = 修整参照
    记录 band 供更弱类型使用
railing：不进这套流程，单独缓冲（细线惯例），不退洞口
level_change：无实体
```

注意修整参照是**未退洞口的实心带**：重墙上的门洞不该让轻墙从门洞里钻出来。

## 6. 洞口（`OpeningGeometryService`）

```
anchor_len = 锚定墙采样中线长度
span       = spanWallIds 若非空：按共享节点拼成连续折线（含锚墙），否则 = 锚墙
s_start    = position · anchor_len
s_end      = s_start + width                      // width 是毫米，沿弧长
start,end  = span 上按弧长取点与局部切向
wallNormal = (-tangent.y, tangent.x)              // 左手法向
centerPts  = span 上 [s_start, s_end] 的截取（截断到墙内）
切割带     = 沿每个采样点的**局部法向**偏移 ±(thickness/2 + eps) 得到的条带
eps        = max(0.5px · s, 0.02 · thickness/2)    // 原实现用世界单位（px）0.5
```

局部法向公式（`symbolNormals`，同时用于切割带与门窗符号）：

```
n_i = normalize( -(P_{i+1} − P_{i−1}).y , (P_{i+1} − P_{i−1}).x )
```

首尾点用自身做邻居（即弦法向）。2 点跨度 → 常量弦法向，与旧矩形切口逐位一致。

圆弧长度参数化（`polylinePointAtArc`）**不夹取**：编辑器允许洞口越过墙端；
`polylineSlice` 则夹取到 `[0, 总长]`。直接照抄会得到“端点外插、切割带被夹取”的
自相矛盾结果，所以读入器把两端都夹到 `[0, span_len]`，并把实际可用长度写进
`OpeningGeom.effective_width_mm`（`width_mm` 仍保留导出值，`clamped` 标记两者不一致）。

## 7. 面提取与房间匹配

编辑器用 DCEL 半边结构（`RoomService.extractRooms`）：

1. 用**采样中线**建线段，按所有两两交点打断（网格哈希加速）；
2. 半边环遍历，取有界面（有向面积 > 0，CCW）；
3. 面 id = 排序后的 `wallIds` 拼串；面积 = 有向面积。

Shapely 等价做法：

```python
noded = shapely.ops.unary_union([LineString(span_segment) for 每个采样段])
faces = list(shapely.ops.polygonize(noded))       # 有界面
wall_ids = [w for w in walls if face.boundary.intersection(w.centerline).length > ε]
```

这是同一件事：`unary_union` = 打断 + 去重，`polygonize` = 环遍历取有界面。
实测面积与 DCEL 完全一致（8 个面，误差 0）。

**唯一已知偏差**：悬空墙（一端是自由端、位于房间内的隔断）在 DCEL 环里会出现两次
（去而复返），因此被计入该面的 `wallIds`；GEOS 的 `polygonize` 会直接丢弃悬空边，
所以本实现的面 `wallIds` 不含悬空墙。面积不受影响。

房间匹配（`findRoomFaceByWallIds`）：`set(room.wallIds) == set(face.wallIds)` 精确匹配。
真实数据里 `wallIds` 常已过期，故本实现追加容差匹配（Jaccard ≥ 0.5），并把这个
判断记录为 issue。

面积定义：

- `area_centerline_m2`：墙中线围合面积（= 编辑器侧栏显示的面积，不含墙厚）；
- `area_net_m2`：面多边形减去**实心墙体带**（§5 的未退洞版本）后的可用面积。

## 8. 3D 拉伸（`ifcModel.ts`）

楼层高度是导出时假设值（`DEFAULT_IFC_PARAMS`）：墙 3000、栏杆 1100、门 2100、
窗台 900 + 窗高 1500、柱 3000。

墙身按 Z 分段：所有洞口高度值把 `[0, wall_height]` 切成若干段；每段只减去
**在该段内贯通**的洞口：

```
段 [z0, z1] 的活动洞口 = { op | op.z0 ≤ z0 且 op.z1 ≥ z1 }
```

于是门洞段与窗洞段的墙身截面不同（门窗高度范围内的墙是“有洞”截面，
窗台以下是无洞截面）。读入器只产出这些分段（`plan.wall_slabs()` →
`Slab(z0, z1, polygon)`）；拉伸成 mesh 是 recipe §13 的事。

房间（IfcSpace 等价物）按墙中线多边形拉伸到墙高；柱按其轮廓、家具/竖向构件按
包围盒。

## 9. 与源码逐项对齐的实测记录

样本：`D:/Amo/Desktop/floorplan-dsh/data/floorplan-1795589871235.json`（11 房间、49 墙、13 洞口）。

| 量 | 编辑器（TS） | 本实现（Python） |
|---|---|---|
| `scale` | 25.731177959342716 | 一致 |
| 外墙实心带（不退洞） | 14.914740 m² | 14.914740 |
| 内墙实心带（不退洞） | 6.853415 m² | 6.853415 |
| 外墙（退洞） | 10.970546 m² | 10.970546 |
| 内墙（退洞） | 5.682175 m² | 5.682175 |
| 栏杆 | 0.768863 m² | 0.768863 |
| 面的面积（8 个） | 50.213369 / 28.589562 / 18.572581 / 16.520263 / 12.495449 / 7.536889 / 3.104544 / 3.012263 | 一致（<1e-9） |
| 相邻面对数 | 13 | 13 |
| 洞口端点坐标 | 13 个洞口 | 最大偏差 2e-12 mm |

这些数字写进了 `tests/test_reference_parity.py`，改动算法后必须重跑。

合成算例（90° 拐角、20° 尖角、闭合矩形、T 形交接 + 内墙修整）四个案例的墙身面积
也与 TS 逐位一致——说明缓冲参数与修整顺序没有偏差。

## 10. 复刻时的检查清单

1. 比例尺取**两列范数的均值**，不是 `a`，也不是 `sqrt(det)`。
2. 建链**先按类型分组**，再用 `(thickness, curveMode)` 作键，度数为 2 才继续。
3. 曲线采样取**链第一面墙**的 tension/segments；非末边少发一个采样点。
4. 缓冲：`half = thickness/2`、`quad_segs=16`、`mitre_limit=5`；闭合链按环缓冲。
5. 修整顺序 外墙 > 砖墙 > 幕墙 > 内墙，参照系是**未退洞口**的实心带。
6. 洞口 `position` 是**弧长比例**，`width` 沿弧长；切割带用局部法向。
7. 面提取用采样中线打断成平面图后取有界面；房间按 `wallIds` 集合匹配。
8. 面积区分中线面积与净面积，别混用。
9. 3D 按洞口高度分段拉伸，否则门窗洞会在整个墙高上贯通。
