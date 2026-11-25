# FloorPlan JSON 数据协议

本文件是 `floorplan-konva` 导出 JSON 的完整字段说明书。权威来源是仓库里的
`src/types/*.ts` 与 `src/models/FloorPlan.ts`；本文件只做整理，任何冲突以代码为准。

## 1. 顶层结构

`ExportService.exportToJSON()`（`src/services/ExportService.ts`）输出：

```json
{
  "version": "1.0.0",
  "exportedAt": "2026-11-25T06:57:51.231Z",
  "nodes":      { "<id>": {...} },
  "walls":      { "<id>": {...} },
  "openings":   { "<id>": {...} },
  "columns":    { "<id>": {...} },
  "rooms":      { "<id>": {...} },
  "verticals":  { "<id>": {...} },
  "furniture":  { "<id>": {...} },
  "texts":      { "<id>": {...} },
  "axisGrids":  { "<id>": {...} },
  "imageTransform": { "a": 25.12, "b": -5.55, "c": 5.55, "d": 25.12, "e": 0, "f": 0 },
  "metadata": { "imageWidth": 811, "imageHeight": 648 }
}
```

- 九个实体表永远存在，可以为空对象 `{}`（不是 `null`、不是数组）。
- 读入器（`floorplan_io.py`）只解析会派生几何的三表：`nodes / walls / openings / rooms`。
  `columns / furniture / texts / axisGrids / verticals` 是自足参数，直接读 `plan.raw`，
  配方见 `recipes.md` §10–§12。
- 主键是 UUID 字符串，同时冗余存在每条记录的 `id` 字段里。
- `version` 目前恒为 `"1.0.0"`，没有做语义化兼容分支；`exportedAt` 是 ISO-8601 UTC。
- `imageTransform` 可能缺省（旧数据），缺省按单位矩阵处理。
- `metadata` 为底图像素尺寸；空白画布是 `0 x 0`。

## 2. 单位与坐标（最容易出错的部分）

| 量 | 单位 | 字段 |
|---|---|---|
| 位置 | **底图像素**（原点左上，y 向下） | `nodes[].x/y`、`columns/furniture/verticals/texts[].x/y`、`rooms[].points[]`、`rooms[].labelPos`、`axisGrids[].gridOffsetX/Y`、`axisGrids[].extent`、`metadata.imageWidth/Height` |
| 尺寸 | **毫米** | `walls[].thickness`、`openings[].width`、`columns[].width/height`、`furniture[].width/height`、`verticals[]` 全部尺寸参数、`texts[].fontSize` |
| 角度 | **度，屏幕上顺时针为正** | `columns/furniture/verticals/texts[].rotation` |
| 角度 | **弧度** | `axisGrids[].rotation`；`imageTransform` 的方向 `atan2(b, a)` |

像素→毫米只有一个比例 `s`（`TransformService.extractScale`）：

```
s = ( hypot(a, b) + hypot(c, d) ) / 2        // mm per px
```

编辑器写出的矩阵是相似变换 `similarityMatrix(scale, θ)`：
`a = s·cosθ, b = s·sinθ, c = −s·sinθ, d = s·cosθ, e = f = 0`。因此
`det = s²`、`e/f` 恒为 0——矩阵里没有平移，不要指望它把图纸挪到原点。

三种标定状态必须区分（DXF / IFC 导出器也按这个规则拒绝导出）：

| 状态 | 判据 | 含义 |
|---|---|---|
| 已标定 | 矩阵 ≠ 单位矩阵，且有底图 | `s` 可信，mm 尺寸与像素位置一致 |
| 未标定 | 矩阵 = 单位矩阵，且有底图 | **比例未知**：mm 尺寸（墙厚、门窗宽）与像素位置不在同一尺度上，任何面积/长度结论都不可信 |
| 空白画布 | 无底图（`imageWidth/Height` 为 0） | 1 个世界单位 = 1 mm，`s` 取 1 |

输出坐标建议与 DXF / IFC 导出器保持一致（`ifcModel.ts`）：

```
x_mm = x_px * s
y_mm = (imageHeight − y_px) * s        // 翻成 y 向上
```

这样得到的就是施工图坐标：单位 mm、y 向上、可直接与 DXF/IFC 结果比对。
需要与底图逐像素对齐时（例如生成掩膜）改用 `y_mm = y_px * s`，保持一致即可。

## 3. 实体字段

### 3.1 nodes

```json
{ "id": "...", "x": 520.9, "y": 58.7, "connectedWallIds": ["..."],
  "cornerStyle": { "type": "mitre", "param": 0 }, "capStyle": "flat" }
```

- `connectedWallIds` 是**派生缓存**，真实权威是 `walls[]` 的 `startNodeId/endNodeId`。
  已有真实文件中该字段与墙体不一致，读入后应重算。
- `cornerStyle.type`: `mitre` | `round` | `bevel`（墙体转角样式），`capStyle`:
  `flat` | `round` | `square`（墙链端头样式）。两者都可缺省。
- 不同 id 的两个节点可能坐标完全相同（编辑合并后残留），真实数据里常见。

### 3.2 walls

```json
{ "id": "...", "startNodeId": "...", "endNodeId": "...",
  "thickness": 300, "type": "exterior", "railingSide": 1,
  "curveMode": "straight", "curveTension": 0.95, "curveSegments": 16 }
```

- `type`: `exterior` | `interior` | `curtain_wall` | `railing` | `brick_wall` | `level_change`。
  - 默认厚度（`useCanvasStore`）：外墙 300、内墙 200、幕墙 100、栏杆 80、砖墙 240。
  - `level_change`（高差）**只有标注意义**，没有实体墙身。
  - `railing`（栏杆）画成细线 + 投影线，`railingSide`（±1）决定投影偏移方向。
- `curveMode`: `straight`（缺省）| `curve`。曲线墙几何**不存盘**，由节点坐标 +
  相邻同链曲线墙做向心 Catmull-Rom 插值得到。
- `curveTension`（0–1.5，缺省 0.95）、`curveSegments`（4–64，缺省 16）是**链级**参数：
  同链取第一面墙的值，整链共用。

### 3.3 openings

```json
{ "id": "...", "wallId": "...", "type": "door", "position": 0.2215,
  "width": 1084.46, "direction": 1, "hingeSide": "end",
  "spanWallIds": ["...", "..."], "fromRecognition": true, "sourceBoxId": "..." }
```

- `type`: `door` | `door_double` | `window` | `opening` | `sliding` | `revolving`。
- `wallId` 是**锚定墙**：删除、吸附、分组、`position` 全部以它为准。
- `position` 是**弧长比例（0–1）**，量在锚定墙的**采样中线**上，不是弦长比例；
  直线墙两者相等，曲线墙必须按采样几何算。`width` 是沿弧长的**毫米**宽度。
- 洞口从 `position` 起沿弧长向后铺 `width`；超出墙长时编辑器不裁剪（会画到墙外），
  读取时应报警。
- `spanWallIds`（跨节点洞口）：从锚墙开始的**有序连续**墙链，`spanWallIds[0] === wallId`。
  缺省或单元素等价于普通单墙洞口。约束：同类型、同厚度、逐段共享节点。
- `direction`（±1）只控制门窗符号往墙的哪一侧开；`hingeSide`（`start`/`end`，仅门）
  选择铰链在哪一端。
- `window` 不可通行；其余五种算可通行（`RoomService.PASSABLE_OPENING_TYPES`）。
- 识别产生的数据 `width` 往往不是整数（例如 1084.46），是实测跨度，不必取整。

### 3.4 rooms

```json
{ "id": "...", "wallIds": ["..."], "label": "卧室", "color": "#8DCBEB",
  "points": [{ "x": 1, "y": 2 }], "labelPos": { "x": 1, "y": 2 } }
```

**房间不存多边形**，只存边界墙 id；多边形在运行时由墙体图重算（平面图面提取）。
`points`（手绘房间）存在时改为“直接用这个环”，与 `wallIds` 互斥。

- `wallIds` 里可能出现已删除的墙 id（真实数据里确实有），这时要么按“容差匹配”
  找最接近的面，要么丢弃该房间。
- `color` 不只是颜色：它同时是语义类别，按 `src/utils/roomPresets.ts` 的调色板映射：

  | color | label | 含义 |
  |---|---|---|
  | `#D6FC9C` | 户外 | outdoor |
  | `#F2DD72` | 窗户 | window zone |
  | `#8DCBEB` | 卧室 | bedroom |
  | `#E085F2` | 客厅 | living |
  | `#F2793D` | 门 | door zone |
  | `#8585CC` | 功能房 | utility |
  | `#46A770` | 厨房 | kitchen |
  | `#D9414E` | 走廊 | corridor |
  | `#3D58B3` | 卫生间 | bathroom |

- `label` 是自由文本，可能为空、可能是 `"1"`/`"2"` 这类临时编号，也可能因为上游
  编码问题变成一串 U+FFFD（真实文件里出现过）。此时用 color 映射的语义更可靠。
- `labelPos` 缺省时标签落在“面内心点”（凸多边形的形心，凹多边形的
  pole of inaccessibility）。

### 3.5 columns

```json
{ "id": "...", "x": 100, "y": 120, "shape": "rectangle",
  "width": 400, "height": 600, "rotation": 90 }
```

- `shape`: `circle` | `square` | `rectangle`。
- `circle` 用 `width` 作**直径**（忽略 `height`）；`square` 用 `width` 作边长。
- 中心点定位，`rotation` 是度、屏幕顺时针。

### 3.6 verticals（竖向构件）

`kind` 决定参数集，几何在运行时由 `VerticalGeometryService` 生成（本仓库不存盘）：

| kind | 参数 | 包围盒（mm） |
|---|---|---|
| `stair_straight` | `stairwellWidth, stairwellLength, floorHeight, direction, floorType, stepCountOverride?` | `tread × 总踏步数` × `梯段净宽` |
| `stair_u` | `stairwellWidth, stairwellLength, floorHeight, stairwellGap, floorType, stepCountOverride?` | `2·梯段宽 + 梯井` × `每跑踏步·踏面 + 平台深` |
| `elevator` | `shaftWidth, shaftDepth` | `shaftWidth × shaftDepth` |
| `escalator` | `width, floorHeight, direction` | `2·2500 + floorHeight/tan30°` × `width` |
| `steps` | `stairwellWidth, stairwellLength, floorHeight, direction, stepCountOverride?` | `踏面·踏步数` × `台阶宽` |
| `ramp` | `rampWidth, rampLength, slopePercent, direction` | `rampLength × rampWidth` |

踏步/踏面由 `StairCalculator` 双向推导：目标踢面 165 mm、最小踏面 260 mm、
最小平台 1200 mm；`stairwellLength` 决定踏面（`platform` 之外的余量均分），
再被 260 mm 下限夹住。

### 3.7 furniture

```json
{ "id": "...", "symbolId": "bed_double", "x": 100, "y": 90,
  "width": 1800, "height": 2000, "rotation": 0, "flipped": false }
```

`symbolId` 指向内置参数化图库（`BUILTIN_SYMBOLS`，10 个常用 + 5 个隐藏的竖向构件符号）
或后端 DXF 图块 UUID。数据里只存包围盒与变换，图元要自己重画或按包围盒近似。

### 3.8 texts

```json
{ "id": "...", "x": 60, "y": 70, "text": "主卧", "fontSize": 300, "rotation": 0 }
```

`(x, y)` 是文本框**左上角**锚点，`fontSize` 是毫米级字高，`text` 可含 `\n`（无自动换行）。

### 3.9 axisGrids（轴网）

```json
{ "id": "...", "schemeId": "...", "rotation": 0.0,
  "gridOffsetX": 0, "gridOffsetY": 0,
  "verticalAxes": [10, 22, 40], "horizontalAxes": [10, 22],
  "extent": { "minU": -500, "maxU": 900, "minV": -300, "maxV": 700 } }
```

命名与直觉相反，按 `AxisGridLayer` 的用法读：

- `horizontalAxes` = **竖线**（纵轴 ①②③）的 u 坐标；
- `verticalAxes` = **横线**（横轴 A B C）的 v 坐标；
- 轴位置是**轴网自身坐标系**下的像素坐标，落到世界坐标为

  ```
  X = gridOffsetX + u·cos(rotation) − v·sin(rotation)
  Y = gridOffsetY + u·sin(rotation) + v·cos(rotation)
  ```

- `extent` 是轴线的绘制范围（同坐标系）；缺省时由底图四角投影到轴网坐标得到，
  再缺省用 ±5000 px。

## 4. 语义不变量

1. **墙是链的成员**，不是一个孤立线段：只有“同厚度 + 同 curveMode”且节点度数为 2
   的相邻墙才会连成一条链；链级参数（曲线张力、转角样式）与链的缓冲几何绑定。
2. **房间由墙图决定**：面 = 墙体中线平面图的有界面；房间 = 一组边界墙 id。
   改墙不自动改房间记录，因此房间的 `wallIds` 可能过期。
3. **门窗是墙上的参数**，不是独立几何：位置是弧长比例，宽度沿弧长。
4. **竖向构件/家具是参数化符号**，记录里没有图元，只有参数与包围盒。
5. **只有 `exterior / interior / curtain_wall / brick_wall / railing` 有实体墙身**；
   `level_change` 只是标注。不同类型相接时按强度顺序修整：
   外墙 > 砖墙 > 幕墙 > 内墙（`WallBufferService.WALL_TRIM_ORDER`）。
6. `fromRecognition` / `sourceBoxId` 只是溯源标记，不影响几何。

## 5. 真实数据里见过的异常

读到这些不要惊讶，先按表处理再分析：

| 现象 | 后果 | 处理 |
|---|---|---|
| 零长墙 + 坐标重复的节点 | 缓冲几何为空；面提取时退化为一个点 | 保留记录、几何置空；报 `zero_length_wall` / `coincident_nodes` |
| 房间 `wallIds` 指向已删除的墙 | 面无法精确匹配 | 先精确匹配，再按 Jaccard ≥ 0.5 容差匹配；报 `room_dangling_wall_ids` |
| 两个房间指向同一个面 | 面积重复计算 | 去重后报 `room_face_shared`，不要直接求和 |
| `label` 含 U+FFFD | 语义丢失（不可逆） | 用 color → 预设名兜底 |
| `connectedWallIds` 与实际墙体不一致 | 依赖它会算错节点度数 | 永远从 `walls[]` 反推 |
| 洞口 `position·墙长 + width > 墙长` | 门窗画到墙外 | 报 `opening_exceeds_wall`；几何夹到墙长，`effective_width_mm` 是实际宽度 |
| 有底图但矩阵是单位阵 | mm 尺寸不可信 | 报 `uncalibrated`（error），拒绝出面积/工程量结论 |
