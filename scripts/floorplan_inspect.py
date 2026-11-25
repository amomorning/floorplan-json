"""交互式读图工具：把导出的平面图 JSON 变成面积表、图纸和 3D 模型。

不需要记参数，直接跑：

    uv run python scripts/floorplan_inspect.py

会先列出可读的文件，再问你要做什么（摘要 / 平面图 / GeoJSON / 3D / 讲解）。
每个动作都会打印它调用的 shapely / trimesh 图元以及对应的几何算法，
菜单第 5 项把所有步骤一次列全——这是给建筑同学的"代码对照表"。

脚本里用的话，给一个路径即可，只打印摘要：

    uv run python scripts/floorplan_inspect.py data/plan.json
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import floorplan_report as report  # noqa: E402
from floorplan_io import load_floorplan  # noqa: E402

ACTIONS = {
    "1": ("摘要：房间面积表（墙中线 / 净面积）", "digest"),
    "2": ("平面图：写 PNG + SVG", "plan"),
    "3": ("GeoJSON：丢进 QGIS 叠图", "geojson"),
    "4": ("3D：写 GLB（墙身 + 房间体块）", "glb"),
    "5": ("讲解：上面每步用了什么图元和算法", "explain"),
}

# 每类输出涉及的步骤，键对应 report.PIPELINE
STEPS = {
    "digest": ("scale", "wall", "face", "area"),
    "plan": ("wall", "body", "trim", "opening", "face"),
    "geojson": ("body", "opening", "face"),
    "glb": ("body", "opening", "extrude"),
    "explain": ("scale", "wall", "body", "trim", "opening", "face", "area", "extrude"),
}


# ── 小工具：中文对齐 + 提问 ────────────────────────────────────────────────

def _width(text: str) -> int:
    """显示宽度：中日韩字符占两列。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, columns: int) -> str:
    return text + " " * max(0, columns - _width(text))


def ask(prompt: str, default: str | None = None) -> str | None:
    """读一行输入；EOF（管道/非交互）返回 None，让调用方直接退出。"""
    try:
        answer = input(prompt).strip()
    except EOFError:
        print()
        return None
    return answer or default


# ── 选文件 / 选动作 ────────────────────────────────────────────────────────

def candidates() -> list[Path]:
    """默认去找 ./data、./assets 和技能自带的样例。"""
    here = Path(__file__).resolve().parent.parent
    found: list[Path] = []
    for folder in (Path.cwd() / "data", Path.cwd() / "assets", here / "assets"):
        if folder.is_dir():
            found.extend(sorted(folder.glob("*.json")))
    return list(dict.fromkeys(p.resolve() for p in found))


def choose_plan(plans: list[Path]) -> Path | None:
    """返回要读的文件；None = 退出。也可以直接输入一个路径。"""
    while True:
        if plans:
            print("\n可读的平面图：")
            for i, path in enumerate(plans, 1):
                print(f"  {i}) {path.name}   <{path.parent}>")
        print("  0) 退出")
        prompt = (f"选文件 [1-{len(plans)}]，或粘贴一个 .json 路径：" if plans
                  else "粘贴一个 .json 路径：")
        answer = ask(prompt, "1" if plans else None)
        if answer is None or answer == "0":
            return None
        if plans and answer.isdigit() and 1 <= int(answer) <= len(plans):
            return plans[int(answer) - 1]
        path = Path(answer).expanduser()
        if path.is_file():
            return path
        print(f"  找不到 {path}，再试一次。")


def choose_action() -> str | None:
    """返回动作编号；None = 换文件。"""
    print("\n要做什么？")
    for key, (label, _) in ACTIONS.items():
        print(f"  {key}) {label}")
    print("  0) 换文件 / 退出")
    while True:
        answer = ask("选择 [1]：", "1")
        if answer is None or answer == "0":
            return None
        if answer in ACTIONS:
            return answer
        print("  没有这一项，再试一次。")


# ── 输出动作 ──────────────────────────────────────────────────────────────

def show_digest(plan, path: Path | None = None) -> None:
    """打印一份人看的摘要（不是 JSON）。"""
    digest = report.summarize(plan)
    frame = digest["frame"]
    title = f"{path.name}  " if path else ""
    print(f"\n{title}比例尺 {frame['scale_mm_per_px']:.2f} mm/px"
          f"   网格转角 {frame['rotation_deg']:+.1f}°")
    if not frame["calibrated"]:
        print("  ! 底图未标定（imageTransform 是单位阵）：面积与长度都不可信")

    print("  墙体")
    for wtype, row in digest["walls"]["by_type"].items():
        print(f"    {_pad(wtype, 14)}{row['count']:3d} 段   "
              f"{row['length_m']:7.1f} m 实心 {row['solid_area_m2']:7.2f} m²")
    print("  洞口")
    for otype, row in digest["openings"]["by_type"].items():
        print(f"    {_pad(otype, 14)}{row['count']:3d} 个   "
              f"合计 {row['total_width_m']:7.2f} m  平均 {row['mean_width_mm']:6.0f} mm")

    rooms = digest["rooms"]
    print(f"  房间（{rooms['matched']}/{rooms['count']} 条记录匹配到面）")
    shared: dict[str, list[str]] = {}
    for room in plan.rooms.values():
        if room.face is not None:
            shared.setdefault(room.face.id, []).append(room.id)
    for room in plan.rooms.values():
        label = room.label_text or room.id[:8]
        if room.face is None:
            print(f"    {_pad(label, 14)}    —          边界不闭合（墙可能已删）")
            continue
        note = "  <- 与另一条同面，别重复计" if len(shared[room.face.id]) > 1 else ""
        print(f"    {_pad(label, 14)}{room.area_m2:7.2f} m²  净 {room.net_area_m2:7.2f} m²"
              f"  {room.match or ''}{note}")
    print(f"  面合计（去重）{sum(face.area_m2 for face in plan.faces):.2f} m²"
          f"   房间记录求和 {rooms['total_area_m2']:.2f} m²")
    print(f"  问题：{digest['issues']['errors']} error / "
          f"{digest['issues']['warnings']} warning")


def explain(kind: str) -> None:
    """打印这一步用到的图元 + 几何算法（内容在 report.PIPELINE）。"""
    print("  用到的图元与算法：")
    for key in STEPS[kind]:
        label, call, why = report.PIPELINE[key]
        print(f"    {_pad(label, 12)}{call}")
        print(f"                {why}")


def do_action(plan, key: str, outdir: Path, path: Path | None = None) -> None:
    kind = ACTIONS[key][1]
    if kind == "digest":
        show_digest(plan, path)
        return
    if kind == "explain":
        for step in STEPS["explain"]:
            label, call, why = report.PIPELINE[step]
            print(f"  {_pad(label, 12)}{call}")
            print(f"              {why}")
        return

    outdir.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "plan":
            paths = [outdir / "plan.png", outdir / "plan.svg"]
            report.write_png(plan, paths[0])
            report.write_svg(plan, paths[1])
        elif kind == "geojson":
            paths = [outdir / "plan.geojson"]
            report.write_geojson(plan, paths[0])
        else:                                        # glb
            paths = [outdir / "plan.glb"]
            report.write_glb(plan, paths[0])
    except ImportError as exc:
        print(f"  跳过：{exc}")
        return
    for path in paths:
        print(f"  写了 {path}")
    explain(kind)


# ── 入口 ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None, *, outdir: Path | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):          # Windows 管道默认 GBK，中文会崩
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    if args:                                         # 脚本用法：只出摘要
        show_digest(load_floorplan(args[0]), Path(args[0]))
        return 0

    plans = candidates()
    if not plans:
        print("没找到平面图 JSON。把文件放进 ./data/ 再跑，或直接粘贴路径。")
    while True:
        path = choose_plan(plans)
        if path is None:
            print("再见。")
            return 0
        plan = load_floorplan(path)
        print(f"\n已读取 {path.name}：比例尺 {plan.frame.scale:.2f} mm/px，"
              f"{len(plan.walls)} 段墙，{len(plan.rooms)} 条房间记录")
        if path not in plans:
            plans.append(path)
        target = Path(outdir) if outdir else Path.cwd() / "out" / path.stem
        while True:
            action = choose_action()
            if action is None:
                break
            do_action(plan, action, target, path)


if __name__ == "__main__":
    raise SystemExit(main())
