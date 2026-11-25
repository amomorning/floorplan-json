"""Parity regression against the reference TypeScript implementation.

The expected numbers were produced by importing the EDITOR's own modules
(`buildPresentationWallRingsByType`, `extractRooms`, `roomAdjacency`,
`openingToWorldCoords`, `extractScale`) in floorplan-konva and dumping the
results for the sample file — i.e. they are the source's truth, not a
re-derivation of this port. Any drift in the Python reader shows up here.

Point FLOORPLAN_SAMPLE at any exported plan to re-run it; the default is the
real export shipped as `assets/reference-floorplan.json` (the synthetic sample
is 2 rooms and does not exercise the full pipeline).
"""

import math
import os
from pathlib import Path

import pytest

from floorplan_io import load_floorplan

DEFAULT_SAMPLE = Path(__file__).resolve().parent.parent / "assets" / "reference-floorplan.json"
SAMPLE = os.environ.get("FLOORPLAN_SAMPLE") or str(DEFAULT_SAMPLE)

pytestmark = pytest.mark.skipif(not Path(SAMPLE).exists(), reason="reference sample not available")


@pytest.fixture(scope="module")
def plan():
    return load_floorplan(SAMPLE)


def test_frame(plan):
    assert plan.frame.scale == pytest.approx(25.731177959342716, rel=1e-12)
    assert plan.frame.rotation_deg == pytest.approx(-12.462673205566341, abs=1e-9)
    assert plan.frame.calibrated is True


def test_wall_solids_match_the_editor(plan):
    area = lambda t, cut: plan.wall_solid(t, cut_openings=cut).area / 1e6
    assert area("exterior", True) == pytest.approx(10.970546, abs=1e-6)
    assert area("interior", True) == pytest.approx(5.682175, abs=1e-6)
    assert area("exterior", False) == pytest.approx(14.914740, abs=1e-6)
    assert area("interior", False) == pytest.approx(6.853415, abs=1e-6)
    assert area("railing", True) == pytest.approx(0.768863, abs=1e-6)


def test_faces_match_the_editor(plan):
    areas = sorted(round(f.area_m2, 6) for f in plan.faces)
    assert areas == [3.012263, 3.104544, 7.536889, 12.495449,
                     16.520263, 18.572581, 28.589562, 50.213369]
    assert len(plan.face_adjacency()) == 13


def test_opening_placement_matches_the_editor(plan):
    door = plan.openings["c1cf14b7-05f3-48c6-8fbe-5469935fc585"]
    assert door.start_mm == pytest.approx((5234.8807280511255, 8492.766945996595), abs=1e-6)
    assert door.end_mm == pytest.approx((6183.361161020743, 7966.996175941881), abs=1e-6)
    win = plan.openings["978eeb54-3afa-49c7-9c3a-87677022e478"]
    assert math.dist(win.start_mm, win.end_mm) == pytest.approx(
        math.hypot(*(a - b for a, b in zip(win.end_mm, win.start_mm))), abs=1e-9)


def test_data_quality_findings_are_stable(plan):
    codes = {i.code for i in plan.issues}
    assert {"zero_length_wall", "coincident_nodes", "room_dangling_wall_ids",
            "room_face_unmatched", "room_face_shared"} <= codes
    assert not [i for i in plan.issues if i.level == "error"]


def test_3d_slabs_are_stable(plan):
    """ifcModel: each z band subtracts only the openings that run through it."""
    ext = plan.wall_slabs("exterior")
    assert [(s.z0, s.z1) for s in ext] == [(0, 900), (900, 2100), (2100, 2400), (2400, 3000)]
    assert [round(s.area_mm2 / 1e6, 6) for s in ext] == [
        14.266743, 10.970546, 11.618543, 14.914740]   # top band = uncut envelope
    assert plan.wall_slabs("interior")[-1].area_mm2 / 1e6 == pytest.approx(6.853415, abs=1e-6)
