"""CLI: friendly digest, interactive menu, written files, teaching output."""

from __future__ import annotations

import json
import math

import pytest

import floorplan_inspect as cli
from test_floorplan_io import apartment


def _write(tmp_path, data=None, name="plan.json"):
    path = tmp_path / name
    path.write_text(json.dumps(data or apartment(), ensure_ascii=False), encoding="utf-8")
    return path


def _feed(monkeypatch, answers):
    """答案用完后抛 EOFError —— 交互循环应当据此退出，不挂起。"""
    queue = list(answers)

    def fake_input(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def _interactive(tmp_path, monkeypatch, answers, data=None):
    """把 plan 放进 ./data/，喂入交互答案，返回退出码。"""
    (tmp_path / "data").mkdir(exist_ok=True)
    _write(tmp_path / "data", data)
    monkeypatch.chdir(tmp_path)
    _feed(monkeypatch, answers)
    return cli.main([], outdir=tmp_path / "out")


def test_path_argument_prints_friendly_digest(tmp_path, capsys):
    src = _write(tmp_path)
    assert cli.main([str(src)]) == 0
    out = capsys.readouterr().out
    assert "比例尺 25.00 mm/px" in out
    assert "卧室" in out and "客厅" in out
    assert "7.50 m²" in out


def test_uncalibrated_scan_is_called_out(tmp_path, capsys):
    data = apartment()
    data["imageTransform"] = {"a": 1, "b": 0, "c": 0, "d": 1, "e": 0, "f": 0}
    assert cli.main([str(_write(tmp_path, data))]) == 0
    assert "未标定" in capsys.readouterr().out


def test_interactive_menu_writes_png_svg_and_glb(tmp_path, monkeypatch, capsys):
    pytest.importorskip("matplotlib")
    pytest.importorskip("trimesh")
    assert _interactive(tmp_path, monkeypatch, ["1", "2", "4", "0"]) == 0

    out = capsys.readouterr().out
    assert (tmp_path / "out" / "plan.png").stat().st_size > 3_000
    assert (tmp_path / "out" / "plan.svg").read_text(encoding="utf-8").startswith("<svg")
    assert (tmp_path / "out" / "plan.glb").stat().st_size > 1_000
    assert "buffer" in out and "extrude_polygon" in out


def test_explain_lists_primitives(tmp_path, monkeypatch, capsys):
    assert _interactive(tmp_path, monkeypatch, ["1", "5", "0"]) == 0
    out = capsys.readouterr().out
    for primitive in ("LineString", "buffer", "difference", "unary_union",
                      "polygonize", "polygon.area", "extrude_polygon"):
        assert primitive in out, primitive


def test_no_stdin_exits_quietly(tmp_path, monkeypatch, capsys):
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    _feed(monkeypatch, [])
    assert cli.main([]) == 0
    assert "再见。" in capsys.readouterr().out
