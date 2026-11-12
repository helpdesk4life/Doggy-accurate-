from typing import Literal

from pydantic import BaseModel, Field


class LocationFrame(BaseModel):
    """Client -> server: one GPS fix."""

    type: Literal["location"] = "location"
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    accuracy: float = 0.0
    ts: int = 0


class NearbyWalker(BaseModel):
    """One neighbour, described relative to the walker receiving the frame.

    Deliberately carries no absolute position. A distance and a bearing are
    enough for the client to draw a marker, and neither reveals where the walker
    actually is. See ``app/privacy.py`` for the quantisation.
    """

    id: str
    name: str = ""
    distance_m: float
    bearing_deg: float


class ProximityAlert(BaseModel):
    type: Literal["PROXIMITY_ALERT"] = "PROXIMITY_ALERT"
    radius_m: float
    walkers: list[NearbyWalker]


class ProximityClear(BaseModel):
    type: Literal["PROXIMITY_CLEAR"] = "PROXIMITY_CLEAR"


class Welcome(BaseModel):
    type: Literal["welcome"] = "welcome"
    walker_id: str
    radius_m: float


class ErrorFrame(BaseModel):
    type: Literal["error"] = "error"
    message: str
