# floorplan-json

`floorplan-konva` 导出的平面图是**参数化**数据：JSON 里存节点、墙、门窗参数、房间边界
id，**不存几何**。本仓库把这堆参数还原成几何与图结构——比例尺、墙身（斜接拐角、跨类型
修整、门窗退洞）、房间面、房间邻接、按洞口高度分段的 3D 截面——口径与编辑器（TypeScript
源码）逐位对齐，并用真实导出的回归数字锁住。

JSON 由在线平台产生：**<https://konva.archialgo.com/>**（画平面图 + AI 识别 + 出图）。

技能正文见 [SKILL.md](SKILL.md)；字段说明书见 [references/protocol.md](references/protocol.md)。

## 数据从哪来

在 <https://konva.archialgo.com/> 上：

1. 导入底图（扫描件 / 照片 / 图纸截图）。
2. **标定比例尺**——平台把它写成相似变换矩阵 `imageTransform`（`a,b,c,d`，mm/px）。
   这是全流程唯一的口径来源；矩阵是单位阵就等于没标定。
3. 沿底图绘出，或让 AI 识别出节点 / 墙 / 门窗 / 房间。
4. 导出 JSON（`ExportService.exportToJSON()`）：文件名形如
   `floorplan-1795589871235.json`，`version: "1.0.0"`，顶层九张实体表
   （空表是 `{}`，不是 `null`，不是数组）。

本技能**只读**这个文件：不回写、不连平台、不联网。

拿到文件先看 `plan.frame.calibrated` 与 `plan.issues`：有底图但矩阵是单位阵的文件
（issue `uncalibrated`）比例尺未知，mm 尺寸和像素位置不在同一尺度上，**不要**出任何
面积或长度结论，回平台重新标定再导出。

## 安装

```bash
git clone git@github.com:amomorning/floorplan-json.git ~/.claude/skills/floorplan-json
cd ~/.claude/skills/floorplan-json && uv sync            # 核心只依赖 shapely
uv sync --extra png                                      # 出 PNG 时才要 matplotlib
uv sync --extra glb                                      # 出 GLB 时才要 trimesh
```

脚本是就地运行的（`pyproject.toml` 里 `package = false`），不需要构建或安装；把
`scripts/floorplan_io.py` 拷进任何项目即可当库用。

## 用法

```bash
uv run pytest -q                                        # 40 个用例
uv run python scripts/floorplan_inspect.py              # 交互：列 ./data/*.json → 选动作
uv run python scripts/floorplan_inspect.py plan.json    # 脚本用法：只打印摘要
```

菜单：`1` 摘要表，`2` 平面图 PNG+SVG，`3` GeoJSON，`4` GLB，`5` 流水线讲解；
产物写到 `out/<文件名>/`。

```python
from floorplan_io import load_floorplan
import floorplan_report as report

plan = load_floorplan("floorplan-*.json")   # 路径（str/Path）/ JSON 字符串 / dict
print(report.summarize(plan))               # 计数、面积、邻接、连通、问题清单
print(plan.issues)                          # error / warning / info，带 entity 与 message

plan.wall_solid("exterior")                 # 真实墙身截面（mm²，已退洞）
plan.rooms["<id>"].area_m2                  # 墙中线围合面积（与编辑器侧栏一致）
plan.rooms["<id>"].net_area_m2              # 扣墙厚后的净面积
plan.face_adjacency()                       # 面-面邻接：(面A, 面B, 共墙 id, 共墙长度)
plan.room_adjacency()                       # 房间对邻接：+ 可通行门（同面房间会交叉相乘）
plan.wall_slabs("exterior")                 # 3D 分段截面
plan.raw                                    # 未进读入器的实体（柱 / 家具 / 轴网 / …）

report.write_svg(plan, "plan.svg")          # 还有 write_png / write_geojson / write_glb
```

`plan.raw` 里的柱、家具、文字、轴网、竖向构件是自足参数，不进读入器；
`references/recipes.md` §10–§13 每项十几行配方。

## 文件

| 路径 | 内容 |
|---|---|
| `SKILL.md` | 技能正文：四步读法、九条硬性约定、体检、验证、常见追问 |
| `references/protocol.md` | 数据协议：字段 / 单位 / 语义 / 异常 |
| `references/algorithms.md` | 算法复刻：公式 + 参数 + 实测对账 |
| `references/recipes.md` | 分析配方：面积、邻接、泡泡图、工程量、掩膜、疏散、3D |
| `scripts/floorplan_io.py` | 读入器：九条约定 + 体检（1374 行，只依赖 shapely） |
| `scripts/floorplan_report.py` | 摘要 + SVG/PNG/GeoJSON/GLB + `PIPELINE` 图元对照表 |
| `scripts/floorplan_inspect.py` | 交互式 CLI：选文件 → 选动作 → 打印用到的图元 |
| `assets/sample-floorplan.json` | 合成样例（2 房间 / 7 墙 / 3 洞口） |
| `assets/reference-floorplan.json` | 真实导出（回归对账用的默认样本） |
| `tests/` | 40 个用例：核心算法、配方、CLI、SVG/PNG、与 TS 源码的回归对账 |

## 怎么确认自己的实现是对的

三条互不依赖的检查，全过才算对齐：

1. **自洽**：`wall_solid(type, cut_openings=False).area` ≥ `wall_solid(type).area`；
   `band − solid` 约等于门窗洞口的截面积之和。
2. **回归**：`uv run pytest -q tests/test_reference_parity.py`（缺省样本
   `assets/reference-floorplan.json`，可用环境变量 `FLOORPLAN_SAMPLE` 换文件），期望
   `scale=25.731177959342716`、外墙 14.914740 / 10.970546 m²、8 个面、13 对相邻面。
   这些数字直接取自 TypeScript 源码的输出，不是本实现的再推导。
3. **3D**：`plan.wall_slabs(type)` 的 z 分界等于洞口高度值（0/900/2100/2400/3000），
   最顶一层等于未退洞的实心带面积；按 recipe §13 拉伸后每个 mesh 应 `is_watertight`。

## 已知的坑（详情见 SKILL.md）

- 位置是**底图像素**，尺寸是**毫米**，不要统一乘比例尺；y 要翻成向上。
- 房间 `wallIds` 可能过期：精确匹配 → Jaccard ≥ 0.5 → 否则丢弃并记 issue。
- 两个房间指向同一个面（`room_face_shared`）时，房间行面积会重复计；一层面积用
  `summarize()["rooms"]["total_face_area_m2"]`（按面去重）。
- 洞口可能越过墙端：几何已夹到墙长，`opening.effective_width_mm` 是实际可用宽度，
  `clamped` 标记两者不一致。
- `connectedWallIds` 会过期，节点度数永远从 `walls[]` 反推。
- 房间名可能是 U+FFFD（上游丢字节，不可逆）或 GBK-as-cp1252 乱码；`room.label_text`
  会自动复原后者、前者退回 color → 预设语义。
