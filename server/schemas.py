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
    """`multiview=True` (photo mode) measures the fused region of the job's
    accepted outlines instead of `polygon`/`image`; `indicative=True` allows
    that below the 4 viewpoints x 2 photos requirement. Both default off, so
    a pre-multi-view client's body means exactly what it always did."""
    polygon: list[tuple[float, float]] = []
    mode: Literal["photo", "ortho"] = "photo"
    image: Optional[str] = None
    dense: bool = True
    rim_px: float = 12.0
    rim_inner_px: Optional[float] = None
    multiview: bool = False
    indicative: bool = False


class AnnotationRequest(BaseModel):
    """One photo's outline of the landslide region, in stored-photo px.
    `skipped` needs no polygon; `draft` is saved but not used for fusion."""
    polygon: list[tuple[float, float]] = Field(default_factory=list, max_length=2000)
    status: Literal["draft", "accepted", "skipped"] = "accepted"
    source: Literal["manual", "suggested", "auto"] = "manual"


class CaptureSessionRequest(BaseModel):
    """S2 (GUIDED_CAPTURE_PLAN.md §9.2): starts a `capturing` job."""
    marker_side_m: float = 0.5
    marker_dict: str = "auto"
    has_pre_event_dem: Optional[Literal["yes", "no", "not_sure"]] = None


class CapturePhotoMeta(BaseModel):
    """Form field `meta` on `PUT .../capture/photos/{uuid}` — a JSON string
    of this shape. `arm` is client-assigned (S3 builds the UI that sets it;
    S2 just stores and uses whatever the caller sends)."""
    seq: int
    arm: str
    client: dict = Field(default_factory=dict)


class CaptureFinalizeRequest(BaseModel):
    override: bool = False


class CaptureRoiRequest(BaseModel):
    """S5C (GUIDED_CAPTURE_PLAN.md §12c.8): the tapped slide outline, in the
    ROI photo's own original-resolution pixel coordinates."""
    photo_uuid: str
    polygon: list[tuple[float, float]]


class CapturePreviewRequest(BaseModel):
    """S5C (§12c.8): optional body on `POST .../capture/preview`. Backward
    compatible — an empty body (S4's original contract) defaults to a full
    run."""
    roi_only: bool = False
