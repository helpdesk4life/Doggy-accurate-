"""The privacy maths behind PROXIMITY_ALERT.

The frame carries a distance and a bearing instead of a coordinate. These tests
pin down both halves of that: the bearing points the right way, and the
quantisation is coarse enough that the reconstructed point is not the real one.
"""

import math

import pytest

from app import privacy


def offset(bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Displacement in metres as (east, north)."""
    radians = math.radians(bearing_deg)
    return distance_m * math.sin(radians), distance_m * math.cos(radians)


def test_bearing_due_north_is_zero():
    assert privacy.bearing_degrees(52.0, 4.0, 52.1, 4.0) == pytest.approx(0.0, abs=0.01)


def test_bearing_due_east_is_ninety():
    assert privacy.bearing_degrees(52.0, 4.0, 52.0, 4.1) == pytest.approx(90.0, abs=0.5)


def test_bearing_due_south_is_one_eighty():
    assert privacy.bearing_degrees(52.1, 4.0, 52.0, 4.0) == pytest.approx(180.0, abs=0.01)


def test_bearing_due_west_is_two_seventy():
    assert privacy.bearing_degrees(52.0, 4.1, 52.0, 4.0) == pytest.approx(270.0, abs=0.5)


def test_bearing_is_always_in_range():
    for lat in (-89.0, -45.0, 0.0, 45.0, 89.0):
        for lon in (-179.0, -90.0, 0.0, 90.0, 179.0):
            bearing = privacy.bearing_degrees(lat, lon, -lat, -lon)
            assert 0.0 <= bearing < 360.0


def test_distance_quantisation_snaps_to_the_grid():
    assert privacy.quantise_distance(12.0, 25) == 0.0
    assert privacy.quantise_distance(13.0, 25) == 25.0
    assert privacy.quantise_distance(100.0, 25) == 100.0
    assert privacy.quantise_distance(112.0, 25) == 100.0
    assert privacy.quantise_distance(113.0, 25) == 125.0


def test_bearing_quantisation_snaps_to_sectors():
    assert privacy.quantise_bearing(0.0, 30) == 0.0
    assert privacy.quantise_bearing(14.0, 30) == 0.0
    assert privacy.quantise_bearing(16.0, 30) == 30.0
    assert privacy.quantise_bearing(350.0, 30) == 0.0
    assert privacy.quantise_bearing(359.0, 30) == 0.0


def test_quantisation_keeps_the_error_in_the_tens_of_metres():
    """The reconstructed point must not land on the walker's doorstep.

    At 100 m a 30 degree sector is a ~52 m arc, so the worst case is tens of
    metres away rather than the few metres an exact pair would give.
    """
    true_bearing, true_distance = 47.0, 100.0
    quantised_bearing = privacy.quantise_bearing(true_bearing, 30)
    quantised_distance = privacy.quantise_distance(true_distance, 25)

    true_east, true_north = offset(true_bearing, true_distance)
    quantised_east, quantised_north = offset(quantised_bearing, quantised_distance)
    error = math.hypot(quantised_east - true_east, quantised_north - true_north)

    assert error > 20.0
    assert error < 60.0


def test_zero_quantum_disables_snapping():
    """The escape hatch for a development machine, documented in config.py."""
    assert privacy.quantise_distance(112.4, 0) == 112.4
    assert privacy.quantise_bearing(47.0, 0) == 47.0
