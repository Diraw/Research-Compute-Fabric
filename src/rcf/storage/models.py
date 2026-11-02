"""Storage plans describe exact objects, never globs or arbitrary root deletion."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FileEvidence(BaseModel):
    model_config = ConfigDict(extra="allow")
    path: str
    kind: str = "file"
    bytes: int = 0
    mtime_ns: int = 0
    device: str = ""
    inode: str = ""
    sha256: str | None = None
    allocated_bytes: int | None = None


class StorageSnapshot(BaseModel):
    model_config = ConfigDict(extra="allow")
    scan_id: str = ""
    project_id: str
    node_id: str
    root_id: str
    root: str
    complete: bool
    entries: list[FileEvidence] = Field(default_factory=list)
    errors: list[Any] = Field(default_factory=list)
    logical_bytes: int = 0
    file_count: int = 0
    allocated_bytes: int | None = None
    observed_at: str


class CleanupPlan(BaseModel):
    model_config = ConfigDict(extra="allow")
    plan_id: str
    action: Literal["cleanup", "adopt"]
    project_id: str
    node_id: str
    root_id: str
    root: str
    created_at: str
    expires_at: str
    revision: int
    status: str = "planned"
    kind: str
    entries: list[dict[str, Any]] = Field(default_factory=list)
    blocked: list[dict[str, Any]] = Field(default_factory=list)
    logical_bytes: int = 0
    estimated_reclaim_bytes: int | None = None
    reasons: list[str] = Field(default_factory=list)
