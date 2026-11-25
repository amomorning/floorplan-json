"""Tests for the report layer: SVG escaping, PNG framing, digest totals."""

import xml.etree.ElementTree as ET

import pytest

from floorplan_io import load_floorplan
from floorplan_report import summarize, write_png, write_svg
from test_floorplan_io import apartment

SVG = "{http://www.w3.org/2000/svg}"


def _png_size(path):
    head = path.read_bytes()[:33]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def test_svg_escapes_room_labels(tmp_path):
    data = apartment()
    data["rooms"]["r1"]["label"] = "A&B <主卧>"
    plan = load_floorplan(data)

    out = tmp_path / "plan.svg"
    write_svg(plan, out)

    root = ET.parse(out).getroot()          # invalid XML if the label is raw
    texts = [node.text for node in root.iter(f"{SVG}text")]
    assert "A&B <主卧>" in texts


def test_png_follows_the_plan_aspect_not_a_fixed_sheet(tmp_path):
    plan = load_floorplan(apartment())      # 5000 x 3000 mm interior
    out = tmp_path / "plan.png"
    write_png(plan, out, dpi=60)

    width, height = _png_size(out)
    # The plan is ~1.67:1; the old fixed 14x10 sheet forced 1.4:1.
    assert 1.5 < width / height < 1.9


def test_digest_reports_both_per_room_and_per_face_area():
    data = apartment()
    data["rooms"]["r1b"] = {"id": "r1b", "wallIds": data["rooms"]["r1"]["wallIds"]}
    digest = summarize(load_floorplan(data))["rooms"]

    assert digest["count"] == 3
    assert digest["faces"] == 2
    assert digest["total_area_m2"] == pytest.approx(22.5)
    assert digest["total_face_area_m2"] == pytest.approx(15.0)
