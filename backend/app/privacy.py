"""Coordinate-free proximity payloads.

A ``PROXIMITY_ALERT`` must never carry another walker's absolute position. The
server sends a distance and a bearing instead, both snapped to a coarse grid, so
the client can place a marker relative to itself without ever learning where that
walker actually is.

The quanta are the privacy budget. At 100 m a 30 degree bearing sector is a
~52 m arc and a 25 m distance bucket is +/-12.5 m, so the marker lands on the
right street rather than the right doorstep. At 500 m the arc is ~260 m.

Both quanta are configurable, because the right trade-off depends on how dense
the walking area is. Tightening them makes the map more accurate and the
position more identifiable; they move together.
"""

import math

DISTANCE_QUANTUM_M = 25.0
BEARING_QUANTUM_DEG = 30.0


def bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, in [0, 360)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_lambda = math.radians(lon2 - lon1)

    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        delta_lambda
    )
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def quantise_distance(meters: float, quantum: float = DISTANCE_QUANTUM_M) -> float:
    """Snap a distance to the nearest bucket, so it cannot be used as a rangefinder."""
    if quantum <= 0:
        return round(meters, 1)
    return round(meters / quantum) * quantum


def quantise_bearing(degrees: float, quantum: float = BEARING_QUANTUM_DEG) -> float:
    """Snap a bearing to the nearest sector, so it cannot be used as a compass fix."""
    if quantum <= 0:
        return round(degrees, 1) % 360.0
    return (round(degrees / quantum) * quantum) % 360.0
