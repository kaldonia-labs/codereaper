"""Patcher service — generates, applies, and rolls back unified diffs.

Responsible for Phase 3 (and Phase 5 rollback):
1.  Filter candidates by safety mode / explicit IDs
2.  Generate unified diffs for each affected file
3.  Store pre-patch snapshots for rollback
4.  Apply / rollback diffs to the working copy
"""

from __future__ import annotations

import difflib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from codereaper.core.config import Settings
from codereaper.core.storage import Storage
from codereaper.models.enums import PatchStatus, RiskScore, SafetyMode
from codereaper.models.schemas import Candidate, PatchHunk

logger = logging.getLogger("codereaper.patcher")

# Risk thresholds per safety mode
_SAFETY_THRESHOLDS: dict[SafetyMode, set[RiskScore]] = {
    SafetyMode.CONSERVATIVE: {RiskScore.LOW},
    SafetyMode.BALANCED: {RiskScore.LOW, RiskScore.MEDIUM},
    SafetyMode.AGGRESSIVE: {RiskScore.LOW, RiskScore.MEDIUM, RiskScore.HIGH},
}


class PatcherService:
    """Generates and manages code patches."""

    def __init__(self, storage: Storage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    # ── Generate ────────────────────────────────────────────────────────

    async def generate_patches(
        self,
        scan_id: str,
        safety_mode: SafetyMode = SafetyMode.CONSERVATIVE,
        candidate_ids: list[str] | None = None,
    ) -> tuple[str, list[PatchHunk]]:
        """Generate unified diffs from analysis candidates.

        Returns (patch_id, list_of_hunks).
        """
        # Load analysis
        analysis = await self._storage.get_analysis(scan_id)
        if not analysis or not analysis.get("candidates_json"):
            raise ValueError(f"No analysis results for scan {scan_id}")

        candidates_raw = json.loads(analysis["candidates_json"])
        candidates = [Candidate(**c) for c in candidates_raw]

        # Filter by safety mode / explicit IDs
        allowed_risks = _SAFETY_THRESHOLDS[safety_mode]
        filtered: list[Candidate] = []
        for c in candidates:
            if candidate_ids and c.function_id not in candidate_ids:
                continue
            if c.risk_score in allowed_risks:
                filtered.append(c)

        if not filtered:
            raise ValueError(
                f"No candidates match safety_mode={safety_mode} "
                f"(total candidates: {len(candidates)})"
            )

        # Group by file
        by_file: dict[str, list[Candidate]] = {}
        for c in filtered:
            by_file.setdefault(c.file_path, []).append(c)

        # Generate hunks
        hunks: list[PatchHunk] = []
        total_bytes = 0

        for file_path, file_candidates in by_file.items():
            source = await self._fetch_source(file_path)
            if not source:
                logger.warning("Cannot fetch source for %s, skipping", file_path)
                continue

            # Sort candidates by line_start descending so removals don't shift offsets
            file_candidates.sort(key=lambda c: c.line_start, reverse=True)

            original_lines = source.splitlines(keepends=True)
            modified_lines = list(original_lines)

            removed_ids: list[str] = []
            rationale_parts: list[str] = []
            max_risk = RiskScore.LOW
            bytes_removed = 0

            for c in file_candidates:
                # Remove lines (0-indexed)
                start_idx = c.line_start - 1
                end_idx = c.line_end
                # Replace with a comment indicating removal
                removed_code = "".join(modified_lines[start_idx:end_idx])
                modified_lines[start_idx:end_idx] = [
                    f"/* [CodeReaper] Removed: {c.name} "
                    f"(0 executions, risk: {c.risk_score}) */\n"
                ]

                removed_ids.append(c.function_id)
                rationale_parts.append(
                    f"- {c.name} (L{c.line_start}-{c.line_end}): {c.evidence}"
                )
                bytes_removed += c.byte_size
                if _risk_level(c.risk_score) > _risk_level(max_risk):
                    max_risk = c.risk_score

            total_bytes += bytes_removed

            # Generate unified diff
            diff = difflib.unified_diff(
                original_lines,
                modified_lines,
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
                lineterm="",
            )
            unified = "\n".join(diff)

            # Build verification plan
            verification_plan = self._build_verification_plan(file_candidates)

            hunk = PatchHunk(
                file_path=file_path,
                unified_diff=unified,
                rationale="\n".join(rationale_parts),
                risk_score=max_risk,
                candidates_removed=removed_ids,
                verification_plan=verification_plan,
            )
            hunks.append(hunk)

        # Persist
        patch_id = str(uuid.uuid4())[:12]
        hunks_json = json.dumps([h.model_dump() for h in hunks])
        await self._storage.create_patch(
            patch_id=patch_id,
            scan_id=scan_id,
            safety_mode=safety_mode,
            hunks_json=hunks_json,
            total_files=len(hunks),
            total_bytes=total_bytes,
        )

        return patch_id, hunks

    # ── Apply ───────────────────────────────────────────────────────────

    async def apply_patch(self, patch_id: str) -> list[str]:
        """Apply a generated patch to the working copy.

        Stores pre-patch snapshots for rollback.
        Returns list of modified file paths.
        """
        patch = await self._storage.get_patch(patch_id)
        if not patch:
            raise ValueError(f"Patch {patch_id} not found")

        if patch["status"] == PatchStatus.APPLIED:
            raise ValueError(f"Patch {patch_id} is already applied")

        hunks_raw = json.loads(patch["hunks_json"])
        hunks = [PatchHunk(**h) for h in hunks_raw]

        modified_files: list[str] = []

        for hunk in hunks:
            source = await self._fetch_source(hunk.file_path)
            if source is None:
                logger.warning("Cannot read %s for patching", hunk.file_path)
                continue

            # Store snapshot before modification
            await self._storage.store_snapshot(patch_id, hunk.file_path, source)

            # Apply the modifications: re-derive modified content
            # (We regenerate rather than applying the diff directly for reliability)
            scan_id = patch["scan_id"]
            analysis = await self._storage.get_analysis(scan_id)
            candidates_raw = json.loads(analysis["candidates_json"])
            all_candidates = [Candidate(**c) for c in candidates_raw]

            # Get candidates for this file that are in this hunk
            file_candidates = [
                c for c in all_candidates
                if c.function_id in hunk.candidates_removed
            ]
            file_candidates.sort(key=lambda c: c.line_start, reverse=True)

            lines = source.splitlines(keepends=True)
            for c in file_candidates:
                start_idx = c.line_start - 1
                end_idx = c.line_end
                lines[start_idx:end_idx] = [
                    f"/* [CodeReaper] Removed: {c.name} "
                    f"(0 executions, risk: {c.risk_score}) */\n"
                ]

            modified = "".join(lines)
            self._write_source(hunk.file_path, modified)
            modified_files.append(hunk.file_path)

        await self._storage.update_patch(patch_id, status=PatchStatus.APPLIED)
        return modified_files

    # ── Rollback ────────────────────────────────────────────────────────

    async def rollback_patch(self, patch_id: str) -> list[str]:
        """Restore original files from stored snapshots."""
        patch = await self._storage.get_patch(patch_id)
        if not patch:
            raise ValueError(f"Patch {patch_id} not found")

        snapshot_files = await self._storage.list_snapshots(patch_id)
        restored: list[str] = []

        for file_path in snapshot_files:
            original = await self._storage.load_snapshot(patch_id, file_path)
            if original is not None:
                self._write_source(file_path, original)
                restored.append(file_path)

        await self._storage.update_patch(patch_id, status=PatchStatus.ROLLED_BACK)
        return restored

    # ── Combined Diff ───────────────────────────────────────────────────

    async def get_combined_diff(self, patch_id: str) -> str:
        """Return all hunks as a single combined diff string."""
        patch = await self._storage.get_patch(patch_id)
        if not patch:
            raise ValueError(f"Patch {patch_id} not found")

        hunks_raw = json.loads(patch["hunks_json"])
        hunks = [PatchHunk(**h) for h in hunks_raw]
        return "\n\n".join(h.unified_diff for h in hunks)

    # ── Private Helpers ─────────────────────────────────────────────────

    async def _fetch_source(self, file_path: str) -> str | None:
        """Fetch JS source — handles URLs and local paths."""
        if file_path.startswith("http"):
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                try:
                    resp = await client.get(file_path)
                    return resp.text if resp.status_code == 200 else None
                except Exception:
                    return None
        else:
            # Local file
            p = Path(file_path)
            return p.read_text() if p.exists() else None

    @staticmethod
    def _write_source(file_path: str, content: str) -> None:
        """Write modified source back to disk (local files only)."""
        p = Path(file_path)
        if p.exists() or not file_path.startswith("http"):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

    @staticmethod
    def _build_verification_plan(candidates: list[Candidate]) -> str:
        """Describe which interaction steps exercise surrounding code."""
        parts = []
        for c in candidates:
            parts.append(
                f"Verify L{c.line_start}-{c.line_end} ({c.name}): "
                f"exercise code paths surrounding this function, "
                f"ensure no new console errors or missing elements."
            )
        return "\n".join(parts)


def _risk_level(risk: RiskScore) -> int:
    """Numeric ordering for risk scores."""
    return {RiskScore.LOW: 0, RiskScore.MEDIUM: 1, RiskScore.HIGH: 2}[risk]
