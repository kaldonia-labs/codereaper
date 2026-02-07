"""Scanner service — orchestrates Index agent + V8 CDP coverage collection.

Responsible for Phase 1 of the pipeline:
1.  Launch Chromium via Index
2.  Enable V8 precise coverage via CDP
3.  Run N exploration passes with the Index agent
4.  Collect coverage data, interaction logs, console errors, network logs
5.  Persist all artifacts via Storage
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from index import Agent, BrowserConfig

from codereaper.core.config import Settings
from codereaper.core.sse import SSEChannel
from codereaper.core.storage import Storage
from codereaper.models.enums import ScanStatus

logger = logging.getLogger("codereaper.scanner")


def _build_llm(settings: Settings) -> Any:
    """Instantiate the LLM provider configured for the Index agent."""
    provider = settings.index_llm_provider.lower()
    model = settings.index_llm_model

    if provider == "gemini":
        from index import GeminiProvider
        return GeminiProvider(model=model)
    elif provider == "openai":
        from index import OpenAIProvider
        return OpenAIProvider(model=model)
    elif provider == "anthropic":
        from index import AnthropicProvider
        return AnthropicProvider(model=model)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")


class ScannerService:
    """Manages the full scan lifecycle for a single target."""

    def __init__(self, storage: Storage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    async def run_scan(
        self,
        scan_id: str,
        target: str,
        passes: int,
        max_steps: int,
        channel: SSEChannel | None = None,
        extension_path: str | None = None,
    ) -> None:
        """Execute the scan — called as a background task."""
        t0 = time.monotonic()
        try:
            await self._storage.update_scan(scan_id, status=ScanStatus.EXPLORING)
            if channel:
                await channel.send("scan_status", {"status": ScanStatus.EXPLORING, "scan_id": scan_id})

            total_interactions = 0

            for pass_num in range(1, passes + 1):
                logger.info("Scan %s: starting pass %d/%d", scan_id, pass_num, passes)
                if channel:
                    await channel.send("scan_progress", {
                        "scan_id": scan_id,
                        "pass": pass_num,
                        "total_passes": passes,
                        "message": f"Starting exploration pass {pass_num}/{passes}",
                    })

                coverage, interactions, console_logs, network_log = (
                    await self._run_single_pass(
                        scan_id=scan_id,
                        target=target,
                        max_steps=max_steps,
                        pass_num=pass_num,
                        channel=channel,
                        extension_path=extension_path,
                    )
                )

                # Persist artifacts
                await self._storage.store_coverage(scan_id, pass_num, coverage)
                await self._storage.store_interactions(scan_id, pass_num, interactions)
                await self._storage.store_console_logs(scan_id, pass_num, console_logs)
                await self._storage.store_network_log(scan_id, pass_num, network_log)

                total_interactions += len(interactions.get("steps", []))

                if channel:
                    await channel.send("pass_complete", {
                        "scan_id": scan_id,
                        "pass": pass_num,
                        "scripts_covered": len(coverage.get("result", [])),
                        "interactions_this_pass": len(interactions.get("steps", [])),
                    })

            # Finalise
            duration = time.monotonic() - t0
            plan_hash = self._storage.compute_interaction_plan_hash(scan_id)

            await self._storage.update_scan(
                scan_id,
                status=ScanStatus.COMPLETED,
                completed_at=__import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                duration_seconds=round(duration, 2),
                interaction_plan_hash=plan_hash,
                total_interactions=total_interactions,
            )

            if channel:
                await channel.send("scan_complete", {
                    "scan_id": scan_id,
                    "duration_seconds": round(duration, 2),
                    "total_interactions": total_interactions,
                })

        except Exception as exc:
            logger.exception("Scan %s failed", scan_id)
            await self._storage.update_scan(
                scan_id,
                status=ScanStatus.FAILED,
                error=str(exc),
            )
            if channel:
                await channel.send("scan_error", {
                    "scan_id": scan_id,
                    "error": str(exc),
                })
        finally:
            if channel:
                await channel.close()

    # ── Private ─────────────────────────────────────────────────────────

    async def _run_single_pass(
        self,
        scan_id: str,
        target: str,
        max_steps: int,
        pass_num: int,
        channel: SSEChannel | None,
        extension_path: str | None,
    ) -> tuple[dict, dict, list, list]:
        """Run one Index exploration pass with CDP coverage enabled.

        Returns (coverage_data, interaction_plan, console_logs, network_log).

        Key lifecycle:
        1. Initialise the agent browser (triggers Playwright launch)
        2. Navigate to the target URL manually
        3. Attach console / network listeners to the page
        4. Open a CDP session and enable V8 precise coverage
        5. Run the Index agent (close_context=False so the browser stays alive)
        6. After the agent finishes, collect coverage from the CDP session
        7. Manually close the browser
        """
        llm = _build_llm(self._settings)

        browser_config = BrowserConfig(
            viewport_size={
                "width": self._settings.index_viewport_width,
                "height": self._settings.index_viewport_height,
            },
        )

        agent = Agent(llm=llm, browser_config=browser_config)
        console_logs: list[dict] = []
        network_log: list[dict] = []
        interaction_steps: list[dict] = []

        try:
            # 1. Initialise browser and get the page handle
            page = await agent.browser.get_current_page()

            # 2. Navigate to the target so scripts load and we can instrument
            await agent.browser.goto(target)
            # Allow page to settle
            await asyncio.sleep(2)

            # Re-acquire page in case navigation opened a new one
            page = await agent.browser.get_current_page()

            # 3. Attach console listener
            page.on("console", lambda msg: console_logs.append({
                "type": msg.type,
                "text": msg.text,
                "url": page.url,
            }))

            # Attach network listener
            page.on("request", lambda req: network_log.append({
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
            }))

            # 4. Open a CDP session on the page and enable V8 precise coverage
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Profiler.enable")
            await cdp.send("Profiler.startPreciseCoverage", {
                "callCount": True,
                "detailed": True,
            })

            logger.info(
                "Scan %s pass %d: CDP coverage enabled on %s, starting agent",
                scan_id, pass_num, target,
            )

            # 5. Build exploration prompt
            prompt = (
                f"Maximize UI coverage on {target} by interacting "
                "with all visible controls, navigating all routes, triggering all "
                "states, opening modals/dropdowns, submitting forms, and exercising "
                "keyboard shortcuts. Explore thoroughly."
            )

            # Stream the agent execution.
            # - start_url=target so the agent gets proper initial browser state
            #   (it may re-navigate to the same URL, which is fine)
            # - close_context=False keeps the browser alive so we can collect
            #   coverage afterward.
            step_count = 0
            agent_error: str | None = None
            async for chunk in agent.run_stream(
                prompt=prompt,
                max_steps=max_steps,
                start_url=target,      # Agent navigates + captures initial state
                close_context=False,   # Keep browser alive for coverage
                return_screenshots=False,
            ):
                if chunk.type == "step":
                    step_count += 1
                    step_info = {
                        "step": step_count,
                        "summary": getattr(chunk.content, "summary", ""),
                        "url": page.url,
                    }
                    interaction_steps.append(step_info)

                    if channel and step_count % 5 == 0:
                        await channel.send("exploration_step", {
                            "scan_id": scan_id,
                            "pass": pass_num,
                            "step": step_count,
                            "max_steps": max_steps,
                            "summary": step_info["summary"],
                        })
                elif chunk.type == "error":
                    agent_error = str(chunk.content)
                    logger.warning(
                        "Scan %s pass %d: agent error — %s",
                        scan_id, pass_num, agent_error,
                    )

            if agent_error and step_count == 0:
                raise RuntimeError(f"Index agent failed before taking any steps: {agent_error}")

            # 6. Collect coverage from the still-open browser
            coverage_result = await cdp.send("Profiler.takePreciseCoverage")
            await cdp.send("Profiler.stopPreciseCoverage")
            await cdp.send("Profiler.disable")

            interaction_plan = {
                "scan_id": scan_id,
                "pass": pass_num,
                "target": target,
                "steps": interaction_steps,
                "total_steps": step_count,
            }

            return coverage_result, interaction_plan, console_logs, network_log

        finally:
            # 7. Always clean up the browser
            try:
                await agent.browser.close()
            except Exception:
                pass
