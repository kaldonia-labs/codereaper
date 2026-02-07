"""Pydantic v2 request / response models for CodeReaper API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from codereaper.models.enums import (
    AnalysisStatus,
    PatchStatus,
    RiskScore,
    SafetyMode,
    ScanStatus,
    VerificationStatus,
)


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


class ScanRequest(BaseModel):
    """Body for POST /api/v1/scans."""

    target: str = Field(
        ...,
        description="URL to scan, or local path to unpacked extension directory.",
    )
    extension_path: str | None = Field(
        default=None,
        description="Explicit path to unpacked Chrome extension (overrides target).",
    )
    passes: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of Index exploration passes to run.",
    )
    max_steps_per_pass: int = Field(
        default=100,
        ge=10,
        le=500,
        description="Maximum Index agent steps per exploration pass.",
    )


class CoverageSummary(BaseModel):
    """Per-file coverage percentage."""

    file_path: str
    covered_bytes: int
    total_bytes: int
    coverage_pct: float = Field(ge=0.0, le=100.0)


class ScanResponse(BaseModel):
    """Response for GET /api/v1/scans/{scanId}."""

    scan_id: str
    status: ScanStatus
    target: str
    passes: int
    started_at: datetime
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    coverage_summary: list[CoverageSummary] | None = None
    interaction_plan_hash: str | None = None
    total_interactions: int | None = None
    error: str | None = None


class ScanCreatedResponse(BaseModel):
    """202 Accepted response for POST /api/v1/scans."""

    scan_id: str
    status: ScanStatus = ScanStatus.PENDING
    stream_url: str


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    """Body for POST /api/v1/scans/{scanId}/analyze (currently empty)."""

    pass


class Candidate(BaseModel):
    """A single dead-code candidate."""

    function_id: str
    file_path: str
    name: str
    line_start: int
    line_end: int
    byte_size: int
    risk_score: RiskScore
    evidence: str
    execution_count: int = Field(
        default=0,
        description="Number of times this function was hit across all passes (0 = dead).",
    )
    dynamic_references: list[str] = Field(
        default_factory=list,
        description="String references / dynamic imports that may reach this function.",
    )


class AnalysisResponse(BaseModel):
    """Response for POST /api/v1/scans/{scanId}/analyze."""

    scan_id: str
    status: AnalysisStatus
    candidates: list[Candidate] = Field(default_factory=list)
    total_candidates: int = 0
    total_reclaimable_bytes: int = 0
    files_analyzed: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Patches
# ---------------------------------------------------------------------------


class PatchRequest(BaseModel):
    """Body for POST /api/v1/scans/{scanId}/patches."""

    safety_mode: SafetyMode = SafetyMode.CONSERVATIVE
    candidate_ids: list[str] | None = Field(
        default=None,
        description="Subset of candidate IDs to patch. None = all matching safety_mode.",
    )


class PatchHunk(BaseModel):
    """One unified diff hunk with metadata."""

    file_path: str
    unified_diff: str
    rationale: str
    risk_score: RiskScore
    candidates_removed: list[str]
    verification_plan: str = Field(
        default="",
        description="Which interaction steps exercise the surrounding retained code.",
    )


class PatchResponse(BaseModel):
    """Response for POST /api/v1/scans/{scanId}/patches."""

    patch_id: str
    scan_id: str
    status: PatchStatus
    safety_mode: SafetyMode
    hunks: list[PatchHunk] = Field(default_factory=list)
    total_files_modified: int = 0
    total_bytes_removed: int = 0
    created_at: datetime


class PatchDetailResponse(BaseModel):
    """Response for GET /api/v1/patches/{patchId}."""

    patch_id: str
    scan_id: str
    status: PatchStatus
    safety_mode: SafetyMode
    hunks: list[PatchHunk] = Field(default_factory=list)
    total_files_modified: int = 0
    total_bytes_removed: int = 0
    combined_diff: str = ""
    created_at: datetime


class ApplyRequest(BaseModel):
    """Body for POST /api/v1/patches/{patchId}/apply."""

    confirm: bool = Field(
        ...,
        description="Must be true to apply. Prevents accidental application.",
    )


class ApplyResponse(BaseModel):
    """Response for POST /api/v1/patches/{patchId}/apply."""

    patch_id: str
    status: PatchStatus
    files_modified: list[str]


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


class Regression(BaseModel):
    """A single regression detected during verification."""

    kind: str = Field(
        description="Type: 'console_error' | 'missing_element' | 'coverage_drop' | 'visual_diff'",
    )
    description: str
    file_path: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class CoverageComparison(BaseModel):
    """Before/after coverage for a single file."""

    file_path: str
    before_pct: float
    after_pct: float
    delta_pct: float


class VerificationResult(BaseModel):
    """Response for POST /api/v1/patches/{patchId}/verify."""

    patch_id: str
    status: VerificationStatus
    passed: bool
    regressions: list[Regression] = Field(default_factory=list)
    coverage_comparison: list[CoverageComparison] = Field(default_factory=list)
    duration_seconds: float | None = None
    error: str | None = None


class RollbackResponse(BaseModel):
    """Response for POST /api/v1/patches/{patchId}/rollback."""

    patch_id: str
    status: PatchStatus
    files_restored: list[str]


# ---------------------------------------------------------------------------
# SSE Events
# ---------------------------------------------------------------------------


class SSEEvent(BaseModel):
    """Generic SSE event payload."""

    event: str
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class HealthResponse(BaseModel):
    """Response for GET /api/v1/health."""

    status: str = "ok"
    version: str = "0.1.0"
