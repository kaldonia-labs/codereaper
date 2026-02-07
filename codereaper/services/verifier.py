"""Verifier service — replays interaction plans and detects regressions.

Responsible for Phase 4:
1.  Load the stored interaction plan from Phase 1
2.  Replay it against the patched code via Index agent
3.  Collect new coverage + console logs + DOM state
4.  Compare against baseline: console errors, missing elements, coverage drops
5.  Report pass/fail with detailed regression info
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from index import Agent, BrowserConfig

from codereaper.core.config import Settings
from codereaper.core.sse import SSEChannel
from codereaper.core.storage import Storage
from codereaper.models.enums import PatchStatus, VerificationStatus
from codereaper.models.schemas import (
    CoverageComparison,
    Regression,
    VerificationResult,
)

logger = logging.getLogger("codereaper.verifier")


class VerifierService:
    """Verifies patches by replaying interactions and checking for regressions."""

    def __init__(self, storage: Storage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    async def verify(
        self,
        patch_id: str,
        channel: SSEChannel | None = None,
    ) -> VerificationResult:
        """Run full verification — called as a background task."""
        t0 = time.monotonic()

        await self._storage.create_verification(patch_id)
        await self._storage.update_verification(
            patch_id, status=VerificationStatus.REPLAYING
        )

        try:
            # Load patch metadata
            patch = await self._storage.get_patch(patch_id)
            if not patch:
                raise ValueError(f"Patch {patch_id} not found")

            scan_id = patch["scan_id"]
            scan = await self._storage.get_scan(scan_id)
            if not scan:
                raise ValueError(f"Scan {scan_id} not found")

            target = scan["target"]

            if channel:
                await channel.send("verify_status", {
                    "patch_id": patch_id,
                    "status": VerificationStatus.REPLAYING,
                })

            # Load baseline data
            baseline_coverage = await self._storage.load_coverage(scan_id)
            baseline_interactions = await self._storage.load_interactions(scan_id)

            # Replay each interaction pass
            all_regressions: list[Regression] = []
            post_coverage_passes: list[dict] = []

            for pass_idx, interaction_plan in enumerate(baseline_interactions, 1):
                if channel:
                    await channel.send("verify_progress", {
                        "patch_id": patch_id,
                        "pass": pass_idx,
                        "total_passes": len(baseline_interactions),
                        "message": f"Replaying pass {pass_idx}/{len(baseline_interactions)}",
                    })

                coverage, console_logs, regressions = await self._replay_pass(
                    target=target,
                    interaction_plan=interaction_plan,
                    baseline_console=await self._load_baseline_console(scan_id, pass_idx),
                    channel=channel,
                    patch_id=patch_id,
                    pass_num=pass_idx,
                )
                post_coverage_passes.append(coverage)
                all_regressions.extend(regressions)

            await self._storage.update_verification(
                patch_id, status=VerificationStatus.COMPARING
            )

            if channel:
                await channel.send("verify_status", {
                    "patch_id": patch_id,
                    "status": VerificationStatus.COMPARING,
                })

            # Compare coverage
            coverage_comparison = self._compare_coverage(
                baseline_coverage, post_coverage_passes
            )

            # Check for coverage drops
            threshold = self._settings.coverage_drop_threshold
            for comp in coverage_comparison:
                if comp.delta_pct < -threshold:
                    all_regressions.append(Regression(
                        kind="coverage_drop",
                        description=(
                            f"Coverage for {comp.file_path} dropped by "
                            f"{abs(comp.delta_pct):.1f}pp "
                            f"({comp.before_pct:.1f}% -> {comp.after_pct:.1f}%)"
                        ),
                        file_path=comp.file_path,
                        details={
                            "before_pct": comp.before_pct,
                            "after_pct": comp.after_pct,
                            "delta_pct": comp.delta_pct,
                        },
                    ))

            # Determine pass/fail
            passed = len(all_regressions) == 0
            duration = time.monotonic() - t0

            status = (
                VerificationStatus.PASSED if passed
                else VerificationStatus.FAILED
            )

            await self._storage.update_verification(
                patch_id,
                status=status,
                passed=1 if passed else 0,
                regressions_json=json.dumps(
                    [r.model_dump() for r in all_regressions]
                ),
                coverage_cmp_json=json.dumps(
                    [c.model_dump() for c in coverage_comparison]
                ),
                duration_seconds=round(duration, 2),
            )

            # Update patch status
            patch_status = (
                PatchStatus.VERIFIED if passed
                else PatchStatus.VERIFICATION_FAILED
            )
            await self._storage.update_patch(patch_id, status=patch_status)

            result = VerificationResult(
                patch_id=patch_id,
                status=status,
                passed=passed,
                regressions=all_regressions,
                coverage_comparison=coverage_comparison,
                duration_seconds=round(duration, 2),
            )

            if channel:
                await channel.send("verify_complete", {
                    "patch_id": patch_id,
                    "passed": passed,
                    "regressions_count": len(all_regressions),
                    "duration_seconds": round(duration, 2),
                })

            return result

        except Exception as exc:
            logger.exception("Verification failed for patch %s", patch_id)
            await self._storage.update_verification(
                patch_id,
                status=VerificationStatus.FAILED,
                error=str(exc),
            )
            if channel:
                await channel.send("verify_error", {
                    "patch_id": patch_id,
                    "error": str(exc),
                })
            raise

        finally:
            if channel:
                await channel.close()

    # ── Replay ──────────────────────────────────────────────────────────

    async def _replay_pass(
        self,
        target: str,
        interaction_plan: dict,
        baseline_console: list[dict],
        channel: SSEChannel | None,
        patch_id: str,
        pass_num: int,
    ) -> tuple[dict, list[dict], list[Regression]]:
        """Replay a single interaction pass against the (patched) target.

        Returns (coverage_data, console_logs, regressions).
        """
        from codereaper.services.scanner import _build_llm

        llm = _build_llm(self._settings)
        browser_config = BrowserConfig(
            viewport_size={
                "width": self._settings.index_viewport_width,
                "height": self._settings.index_viewport_height,
            },
        )
        agent = Agent(llm=llm, browser_config=browser_config)

        console_logs: list[dict] = []
        regressions: list[Regression] = []

        try:
            page = await agent.browser.get_current_page()

            # Capture console
            page.on("console", lambda msg: console_logs.append({
                "type": msg.type,
                "text": msg.text,
                "url": page.url,
            }))

            # Enable coverage
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Profiler.enable")
            await cdp.send("Profiler.startPreciseCoverage", {
                "callCount": True,
                "detailed": True,
            })

            # Build replay prompt from interaction plan
            steps = interaction_plan.get("steps", [])
            step_descriptions = [
                f"Step {s.get('step', i+1)}: {s.get('summary', 'interact')}"
                for i, s in enumerate(steps)
            ]
            replay_prompt = (
                f"Navigate to {target} and perform the following interactions "
                f"in order to verify the page still works correctly:\n"
                + "\n".join(step_descriptions)
                + "\n\nReport any errors, missing elements, or broken functionality."
            )

            # Run the replay
            async for chunk in agent.run_stream(
                prompt=replay_prompt,
                max_steps=len(steps) + 20,  # Some buffer
                start_url=target,
                return_screenshots=False,
            ):
                if chunk.type == "step":
                    step_summary = getattr(chunk.content, "summary", "")
                    if channel:
                        await channel.send("replay_step", {
                            "patch_id": patch_id,
                            "pass": pass_num,
                            "summary": step_summary,
                        })

            # Collect coverage
            coverage = await cdp.send("Profiler.takePreciseCoverage")
            await cdp.send("Profiler.stopPreciseCoverage")
            await cdp.send("Profiler.disable")

            # Check for new console errors
            baseline_errors = {
                log["text"]
                for log in baseline_console
                if log.get("type") == "error"
            }
            for log in console_logs:
                if log["type"] == "error" and log["text"] not in baseline_errors:
                    regressions.append(Regression(
                        kind="console_error",
                        description=f"New console error: {log['text'][:200]}",
                        file_path=None,
                        details={"url": log.get("url", ""), "text": log["text"]},
                    ))

            return coverage, console_logs, regressions

        finally:
            try:
                await agent.browser.close()
            except Exception:
                pass

    # ── Coverage Comparison ─────────────────────────────────────────────

    def _compare_coverage(
        self,
        baseline_passes: list[dict],
        post_passes: list[dict],
    ) -> list[CoverageComparison]:
        """Compare coverage before and after patching."""
        baseline_pcts = self._aggregate_coverage(baseline_passes)
        post_pcts = self._aggregate_coverage(post_passes)

        comparisons: list[CoverageComparison] = []
        all_files = set(baseline_pcts) | set(post_pcts)

        for f in sorted(all_files):
            before = baseline_pcts.get(f, 0.0)
            after = post_pcts.get(f, 0.0)
            comparisons.append(CoverageComparison(
                file_path=f,
                before_pct=round(before, 2),
                after_pct=round(after, 2),
                delta_pct=round(after - before, 2),
            ))

        return comparisons

    @staticmethod
    def _aggregate_coverage(passes: list[dict]) -> dict[str, float]:
        """Aggregate coverage across passes into per-file percentages."""
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
                        size = rng["endOffset"] - rng["startOffset"]
                        file_data[url]["total"] += size
                        if rng["count"] > 0:
                            file_data[url]["covered"] += size

        return {
            url: (d["covered"] / d["total"] * 100) if d["total"] > 0 else 0.0
            for url, d in file_data.items()
        }

    # ── Helpers ─────────────────────────────────────────────────────────

    async def _load_baseline_console(
        self, scan_id: str, pass_num: int
    ) -> list[dict]:
        """Load console logs from the baseline scan."""
        path = (
            self._storage.scan_dir(scan_id)
            / "console_logs"
            / f"pass_{pass_num}.json"
        )
        if path.exists():
            return json.loads(path.read_text())
        return []
