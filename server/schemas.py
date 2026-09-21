"""Typed request bodies for the FastAPI routes (was untyped `dict`, S1)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ArucoScaleRequest(BaseModel):
    side_m: float = 0.25
    dict_name: str = Field("auto", alias="dict")
    id: Optional[int] = None


class ManualEndpoint(BaseModel):
    image: str
    p1: tuple[float, float]
    p2: tuple[float, float]


class ManualScaleRequest(BaseModel):
    length_m: float
    a: ManualEndpoint
    b: ManualEndpoint


class MeasureRequest(BaseModel):
    polygon: list[tuple[float, float]]
    mode: Literal["photo", "ortho"] = "photo"
    image: Optional[str] = None
    dense: bool = True
    rim_px: float = 12.0
    rim_inner_px: Optional[float] = None
