"""Declarative progress contracts; an ETA number alone is not evidence."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProgressContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["file", "log_json"] = "file"
    mode: Literal["steps", "convergence", "phases"] = "steps"
    completion_condition: str = Field(min_length=1, max_length=500)
    total: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    total_param: str | None = None
    heartbeat_timeout_seconds: int = Field(default=120, ge=5, le=86400)
    stall_timeout_seconds: int = Field(default=300, ge=10, le=86400)
    start_grace_seconds: int = Field(default=60, ge=1, le=3600)
    phase_weights: dict[str, float] = Field(default_factory=dict)
    require_calibration: bool = False

    @field_validator("completion_condition", "total_param")
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError("progress fields may not be blank")
        return value.strip() if value else value

    @field_validator("phase_weights")
    @classmethod
    def weights_valid(cls, values):
        if any(not name or not math.isfinite(weight) or weight <= 0 for name, weight in values.items()):
            raise ValueError("phase weights must have names and finite positive weights")
        if values and not math.isclose(sum(values.values()), 1.0, abs_tol=1e-6):
            raise ValueError("phase weights must sum to 1")
        return values

    @model_validator(mode="after")
    def fixed_work_defined(self):
        if self.mode == "steps" and self.total is None and self.total_param is None:
            raise ValueError("steps progress needs total or total_param")
        return self
