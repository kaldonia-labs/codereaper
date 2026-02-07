"""Patch + Verify + Rollback endpoints.

POST /api/v1/scans/{scanId}/patches       -- generate patch proposals
GET  /api/v1/patches/{patchId}            -- retrieve patch details
POST /api/v1/patches/{patchId}/apply      -- apply patch (requires confirm)
POST /api/v1/patches/{patchId}/verify     -- verify patch via replay
GET  /api/v1/patches/{patchId}/verify/stream -- SSE verification progress
POST /api/v1/patches/{patchId}/rollback   -- rollback to pre-patch state
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from codereaper.core.config import Settings, get_settings
from codereaper.core.sse import SSEChannel, sse_response
from codereaper.core.storage import Storage
from codereaper.models.enums import PatchStatus, ScanStatus, VerificationStatus
from codereaper.models.schemas import (
    ApplyRequest,
    ApplyResponse,
    PatchDetailResponse,
    PatchHunk,
    PatchRequest,
    PatchResponse,
    RollbackResponse,
    VerificationResult,
)
from codereaper.services.patcher import PatcherService
from codereaper.services.verifier import VerifierService

# Two routers: one nested under /scans, one top-level /patches
scan_patches_router = APIRouter(prefix="/api/v1/scans", tags=["patches"])
patches_router = APIRouter(prefix="/api/v1/patches", tags=["patches"])

# In-memory map of patch_id -> SSEChannel for active verifications
_verify_channels: dict[str, SSEChannel] = {}


# -- Dependency Injection --------------------------------------------------


def _get_storage() -> Storage:
    from codereaper.api.app import app_storage

    return app_storage


def _get_patcher(
    storage: Storage = Depends(_get_storage),
    settings: Settings = Depends(get_settings),
) -> PatcherService:
    return PatcherService(storage=storage, settings=settings)


def _get_verifier(
    storage: Storage = Depends(_get_storage),
    settings: Settings = Depends(get_settings),
) -> VerifierService:
    return VerifierService(storage=storage, settings=settings)


# -- Scan Patches Router ---------------------------------------------------


@scan_patches_router.post(
    "/{scan_id}/patches",
    response_model=PatchResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate patch proposals from analysis",
)
async def create_patches(
    scan_id: str,
    body: PatchRequest,
    storage: Storage = Depends(_get_storage),
    patcher: PatcherService = Depends(_get_patcher),
) -> PatchResponse:
    # Verify scan exists and has been analyzed
    scan = await storage.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail=f"Scan {scan_id} not found")
    if scan["status"] != ScanStatus.COMPLETED:
        raise HTTPException(
            status_code=409,
            detail="Scan must be completed before generating patches",
        )

    analysis = await storage.get_analysis(scan_id)
    if not analysis or not analysis.get("candidates_json"):
        raise HTTPException(
            status_code=409,
            detail="Scan has not been analyzed yet. Run POST /analyze first.",
        )

    patch_id, hunks = await patcher.generate_patches(
        scan_id=scan_id,
        safety_mode=body.safety_mode,
        candidate_ids=body.candidate_ids,
    )

    patch = await storage.get_patch(patch_id)
    return PatchResponse(
        patch_id=patch_id,
        scan_id=scan_id,
        status=PatchStatus(patch["status"]),
        safety_mode=body.safety_mode,
        hunks=hunks,
        total_files_modified=len(hunks),
        total_bytes_removed=sum(
            sum(
                c.byte_size
                for c in _resolve_candidates(analysis, h.candidates_removed)
            )
            for h in hunks
        ),
        created_at=datetime.fromisoformat(patch["created_at"]),
    )


# -- Patches Router --------------------------------------------------------


@patches_router.get(
    "/{patch_id}",
    response_model=PatchDetailResponse,
    summary="Retrieve patch details and diffs",
)
async def get_patch(
    patch_id: str,
    storage: Storage = Depends(_get_storage),
    patcher: PatcherService = Depends(_get_patcher),
) -> PatchDetailResponse:
    patch = await storage.get_patch(patch_id)
    if not patch:
        raise HTTPException(status_code=404, detail=f"Patch {patch_id} not found")

    hunks_raw = json.loads(patch["hunks_json"]) if patch.get("hunks_json") else []
    hunks = [PatchHunk(**h) for h in hunks_raw]
    combined = await patcher.get_combined_diff(patch_id)

    return PatchDetailResponse(
        patch_id=patch["patch_id"],
        scan_id=patch["scan_id"],
        status=PatchStatus(patch["status"]),
        safety_mode=patch["safety_mode"],
        hunks=hunks,
        total_files_modified=patch.get("total_files", 0),
        total_bytes_removed=patch.get("total_bytes", 0),
        combined_diff=combined,
        created_at=datetime.fromisoformat(patch["created_at"]),
    )


@patches_router.post(
    "/{patch_id}/apply",
    response_model=ApplyResponse,
    summary="Apply patch to working copy",
)
async def apply_patch(
    patch_id: str,
    body: ApplyRequest,
    storage: Storage = Depends(_get_storage),
    patcher: PatcherService = Depends(_get_patcher),
) -> ApplyResponse:
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm must be true to apply the patch",
        )

    patch = await storage.get_patch(patch_id)
    if not patch:
        raise HTTPException(status_code=404, detail=f"Patch {patch_id} not found")

    if patch["status"] == PatchStatus.APPLIED:
        raise HTTPException(
            status_code=409,
            detail=f"Patch {patch_id} is already applied",
        )

    modified = await patcher.apply_patch(patch_id)

    return ApplyResponse(
        patch_id=patch_id,
        status=PatchStatus.APPLIED,
        files_modified=modified,
    )


@patches_router.post(
    "/{patch_id}/verify",
    response_model=VerificationResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Verify patch via interaction replay",
)
async def verify_patch(
    patch_id: str,
    background_tasks: BackgroundTasks,
    storage: Storage = Depends(_get_storage),
    verifier: VerifierService = Depends(_get_verifier),
) -> VerificationResult:
    patch = await storage.get_patch(patch_id)
    if not patch:
        raise HTTPException(status_code=404, detail=f"Patch {patch_id} not found")

    if patch["status"] not in (PatchStatus.APPLIED, PatchStatus.GENERATED):
        raise HTTPException(
            status_code=409,
            detail=f"Patch status is '{patch['status']}', must be 'applied' or 'generated'",
        )

    # Create SSE channel
    channel = SSEChannel()
    _verify_channels[patch_id] = channel

    # Run verification in background
    background_tasks.add_task(
        verifier.verify,
        patch_id=patch_id,
        channel=channel,
    )

    return VerificationResult(
        patch_id=patch_id,
        status=VerificationStatus.PENDING,
        passed=False,
    )


@patches_router.get(
    "/{patch_id}/verify/stream",
    summary="Stream verification progress via SSE",
)
async def stream_verification(patch_id: str):
    channel = _verify_channels.get(patch_id)
    if not channel:
        raise HTTPException(
            status_code=404,
            detail=f"No active verification stream for patch {patch_id}",
        )
    return sse_response(channel.subscribe())


@patches_router.post(
    "/{patch_id}/rollback",
    response_model=RollbackResponse,
    summary="Rollback to pre-patch state",
)
async def rollback_patch(
    patch_id: str,
    storage: Storage = Depends(_get_storage),
    patcher: PatcherService = Depends(_get_patcher),
) -> RollbackResponse:
    patch = await storage.get_patch(patch_id)
    if not patch:
        raise HTTPException(status_code=404, detail=f"Patch {patch_id} not found")

    restored = await patcher.rollback_patch(patch_id)

    return RollbackResponse(
        patch_id=patch_id,
        status=PatchStatus.ROLLED_BACK,
        files_restored=restored,
    )


# -- Helpers ---------------------------------------------------------------


def _resolve_candidates(analysis: dict, candidate_ids: list[str]) -> list:
    """Resolve candidate objects from IDs for byte counting."""
    from codereaper.models.schemas import Candidate

    all_raw = json.loads(analysis.get("candidates_json", "[]"))
    all_candidates = [Candidate(**c) for c in all_raw]
    return [c for c in all_candidates if c.function_id in candidate_ids]
