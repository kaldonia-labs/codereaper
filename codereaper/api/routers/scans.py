"""Scan + Analyze endpoints.

POST /api/v1/scans           -- start a new scan
GET  /api/v1/scans/{scanId}  -- get scan status & results
GET  /api/v1/scans/{scanId}/stream  -- SSE progress stream
POST /api/v1/scans/{scanId}/analyze -- analyze dead-code candidates
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from codereaper.core.config import Settings, get_settings
from codereaper.core.sse import SSEChannel, sse_response
from codereaper.core.storage import Storage
from codereaper.models.enums import AnalysisStatus, ScanStatus
from codereaper.models.schemas import (
    AnalysisResponse,
    AnalyzeRequest,
    Candidate,
    CoverageSummary,
    ScanCreatedResponse,
    ScanRequest,
    ScanResponse,
)
from codereaper.services.analyzer import AnalyzerService
from codereaper.services.scanner import ScannerService

router = APIRouter(prefix="/api/v1/scans", tags=["scans"])

# In-memory map of scan_id -> SSEChannel for active scans
_scan_channels: dict[str, SSEChannel] = {}


# -- Dependency Injection --------------------------------------------------


def _get_storage() -> Storage:
    """Return the global Storage singleton (set during app lifespan)."""
    from codereaper.api.app import app_storage

    return app_storage


def _get_scanner(
    storage: Storage = Depends(_get_storage),
    settings: Settings = Depends(get_settings),
) -> ScannerService:
    return ScannerService(storage=storage, settings=settings)


def _get_analyzer(
    storage: Storage = Depends(_get_storage),
    settings: Settings = Depends(get_settings),
) -> AnalyzerService:
    return AnalyzerService(storage=storage, settings=settings)


# -- Endpoints -------------------------------------------------------------


@router.post(
    "",
    response_model=ScanCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a new scan",
)
async def create_scan(
    body: ScanRequest,
    background_tasks: BackgroundTasks,
    storage: Storage = Depends(_get_storage),
    scanner: ScannerService = Depends(_get_scanner),
) -> ScanCreatedResponse:
    scan_id = str(uuid.uuid4())[:12]

    await storage.create_scan(
        scan_id=scan_id,
        target=body.target,
        passes=body.passes,
        max_steps=body.max_steps_per_pass,
        extension_path=body.extension_path,
    )

    # Create SSE channel for this scan
    channel = SSEChannel()
    _scan_channels[scan_id] = channel

    # Launch scan in background
    background_tasks.add_task(
        scanner.run_scan,
        scan_id=scan_id,
        target=body.target,
        passes=body.passes,
        max_steps=body.max_steps_per_pass,
        channel=channel,
        extension_path=body.extension_path,
    )

    return ScanCreatedResponse(
        scan_id=scan_id,
        status=ScanStatus.PENDING,
        stream_url=f"/api/v1/scans/{scan_id}/stream",
    )


@router.get(
    "/{scan_id}",
    response_model=ScanResponse,
    summary="Get scan status and results",
)
async def get_scan(
    scan_id: str,
    storage: Storage = Depends(_get_storage),
) -> ScanResponse:
    scan = await storage.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail=f"Scan {scan_id} not found")

    # Build coverage summary if scan is completed
    coverage_summary = None
    if scan["status"] == ScanStatus.COMPLETED:
        coverage_passes = await storage.load_coverage(scan_id)
        coverage_summary = _build_coverage_summary(coverage_passes)

    return ScanResponse(
        scan_id=scan["scan_id"],
        status=ScanStatus(scan["status"]),
        target=scan["target"],
        passes=scan["passes"],
        started_at=datetime.fromisoformat(scan["started_at"]),
        completed_at=(
            datetime.fromisoformat(scan["completed_at"])
            if scan.get("completed_at")
            else None
        ),
        duration_seconds=scan.get("duration_seconds"),
        coverage_summary=coverage_summary,
        interaction_plan_hash=scan.get("interaction_plan_hash"),
        total_interactions=scan.get("total_interactions"),
        error=scan.get("error"),
    )


@router.get(
    "/{scan_id}/stream",
    summary="Stream scan progress via SSE",
)
async def stream_scan(scan_id: str):
    channel = _scan_channels.get(scan_id)
    if not channel:
        raise HTTPException(
            status_code=404,
            detail=f"No active stream for scan {scan_id}",
        )
    return sse_response(channel.subscribe())


@router.post(
    "/{scan_id}/analyze",
    response_model=AnalysisResponse,
    summary="Analyze dead-code candidates",
)
async def analyze_scan(
    scan_id: str,
    body: AnalyzeRequest | None = None,
    storage: Storage = Depends(_get_storage),
    analyzer: AnalyzerService = Depends(_get_analyzer),
) -> AnalysisResponse:
    # Verify scan exists and is completed
    scan = await storage.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail=f"Scan {scan_id} not found")
    if scan["status"] != ScanStatus.COMPLETED:
        raise HTTPException(
            status_code=409,
            detail=f"Scan is in status '{scan['status']}', must be 'completed' to analyze",
        )

    candidates = await analyzer.analyze(scan_id)

    return AnalysisResponse(
        scan_id=scan_id,
        status=AnalysisStatus.COMPLETED,
        candidates=candidates,
        total_candidates=len(candidates),
        total_reclaimable_bytes=sum(c.byte_size for c in candidates),
        files_analyzed=len({c.file_path for c in candidates}),
    )


# -- Helpers ---------------------------------------------------------------


def _build_coverage_summary(passes: list[dict]) -> list[CoverageSummary]:
    """Aggregate V8 coverage passes into per-file summaries."""
    file_data: dict[str, dict[str, int]] = {}

    for pass_data in passes:
        for script in pass_data.get("result", []):
            url = script.get("url", "")
            if not url:
                continue
            if url not in file_data:
                file_data[url] = {"total": 0, "covered": 0}

            for func_cov in script.get("functions", []):
                for rng in func_cov.get("ranges", []):
                    size = rng.get("endOffset", 0) - rng.get("startOffset", 0)
                    file_data[url]["total"] += size
                    if rng.get("count", 0) > 0:
                        file_data[url]["covered"] += size

    summaries = []
    for url, data in sorted(file_data.items()):
        total = data["total"]
        covered = data["covered"]
        pct = (covered / total * 100) if total > 0 else 0.0
        summaries.append(
            CoverageSummary(
                file_path=url,
                covered_bytes=covered,
                total_bytes=total,
                coverage_pct=round(pct, 2),
            )
        )

    return summaries
