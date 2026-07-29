"""Unit tests for engine/diagrams.py (vector diagrams: PDF Drawing + SVG export)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

import diagrams as D  # noqa: E402
from reportlab.graphics.shapes import Drawing  # noqa: E402


def test_rack_elevation_drawing():
    d = D.rack_elevation(total_u=42, occupants=[(20, 8, "Camarero AI")])
    assert isinstance(d, Drawing)
    assert len(d.contents) > 20            # frame + U-lines + labels + occupant


def test_railkit_steps_drawing():
    d = D.railkit_steps()
    assert isinstance(d, Drawing) and d.contents


def test_motherboard_map_drawing():
    d = D.motherboard_map()
    assert isinstance(d, Drawing) and d.contents


def test_to_svg_string():
    svg = D.to_svg(D.rack_elevation(occupants=[(1, 2, "x")]))
    assert "<svg" in svg.lower()


def test_save_svg_file(tmp_path):
    p = str(tmp_path / "mb.svg")
    D.save_svg(D.motherboard_map(), p)
    assert os.path.isfile(p)
    assert "<svg" in open(p, encoding="utf-8").read().lower()


def test_cli(tmp_path):
    p = str(tmp_path / "rack.svg")
    assert D.main(["rack", p]) == 0
    assert os.path.isfile(p)
