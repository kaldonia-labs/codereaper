"""Analyzer service — V8 coverage-driven dead code detection.

Responsible for Phase 2 of the pipeline:
1.  Load coverage data from all passes (collected via CDP during browser agent
    exploration)
2.  Merge per-function execution counts across passes
3.  Identify functions with 0 executions as dead-code candidates
4.  Fetch source only to resolve line numbers for human-readable output
5.  Produce Candidate list with risk scores

The sole signal is V8 precise coverage.  No AST parsing (SWC, Babel) or
regex-based function extraction is performed — V8 already reports
function-level granularity with names, byte ranges, and execution counts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from codereaper.core.config import Settings
from codereaper.core.storage import Storage
from codereaper.models.enums import AnalysisStatus, RiskScore
from codereaper.models.schemas import Candidate

logger = logging.getLogger("codereaper.analyzer")


class AnalyzerService:
    """Analyses V8 coverage data to find dead-code candidates."""

    def __init__(self, storage: Storage, settings: Settings) -> None:
        self._storage = storage
        self._settings = settings

    # ── Public API ───────────────────────────────────────────────────────

    async def analyze(self, scan_id: str) -> list[Candidate]:
        """Run the full analysis pipeline for a completed scan."""
        await self._storage.create_analysis(scan_id)
        await self._storage.update_analysis(
            scan_id, status=AnalysisStatus.MAPPING_COVERAGE,
        )

        try:
            # 1. Load all coverage passes
            coverage_passes = await self._storage.load_coverage(scan_id)
            if not coverage_passes:
                raise ValueError(f"No coverage data found for scan {scan_id}")

            # 2. Build a per-script, per-function merged view straight from
            #    the V8 coverage data.  Each entry keeps the *max* execution
            #    count observed across all passes — if a function was hit in
            #    any pass it is considered reachable.
            merged = self._merge_function_coverage(coverage_passes)

            # 3. Fetch source code (only for line-number resolution + risk
            #    heuristics — source is *not* parsed into an AST).
            script_urls = list(merged.keys())
            sources = await self._fetch_sources(script_urls)

            await self._storage.update_analysis(
                scan_id, status=AnalysisStatus.CROSS_REFERENCING,
            )

            # 4. Build candidate list from functions with 0 executions
            all_candidates: list[Candidate] = []
            files_analyzed = 0

            for script_url, functions in merged.items():
                source = sources.get(script_url, "")
                if not source:
                    # Can't resolve line numbers — skip
                    continue

                files_analyzed += 1

                for func in functions:
                    if func["max_count"] > 0:
                        continue  # Executed — not dead

                    name = func["name"] or f"<anonymous@{func['start']}>"
                    line_start = source[: func["start"]].count("\n") + 1
                    line_end = source[: func["end"]].count("\n") + 1
                    byte_size = func["end"] - func["start"]

                    risk = self._assess_risk(name, func, source)
                    func_id = _make_function_id(
                        script_url, name, func["start"], func["end"],
                    )

                    # Coverage pct for the whole script (for the evidence string)
                    script_cov = self._script_coverage_pct(merged[script_url])

                    candidate = Candidate(
                        function_id=func_id,
                        file_path=script_url,
                        name=name,
                        line_start=line_start,
                        line_end=line_end,
                        byte_size=byte_size,
                        risk_score=risk,
                        evidence=(
                            f"0 executions across {len(coverage_passes)} "
                            f"browser-agent exploration pass(es). "
                            f"Script coverage: {script_cov:.1f}%."
                        ),
                        execution_count=0,
                        dynamic_references=[],
                    )
                    all_candidates.append(candidate)

            # 5. Store results
            total_bytes = sum(c.byte_size for c in all_candidates)
            await self._storage.update_analysis(
                scan_id,
                status=AnalysisStatus.COMPLETED,
                candidates_json=json.dumps(
                    [c.model_dump() for c in all_candidates],
                ),
                total_candidates=len(all_candidates),
                total_reclaimable=total_bytes,
                files_analyzed=files_analyzed,
            )

            return all_candidates

        except Exception as exc:
            logger.exception("Analysis failed for scan %s", scan_id)
            await self._storage.update_analysis(
                scan_id,
                status=AnalysisStatus.FAILED,
                error=str(exc),
            )
            raise

    # ── V8 Coverage Merging ──────────────────────────────────────────────

    def _merge_function_coverage(
        self, passes: list[dict],
    ) -> dict[str, list[dict[str, Any]]]:
        """Merge V8 coverage across passes into per-script function lists.

        Returns::

            {
                script_url: [
                    {
                        "name": str,
                        "start": int,       # byte offset
                        "end": int,
                        "max_count": int,   # max execution count across passes
                        "is_block": bool,
                    },
                    ...
                ]
            }
        """
        # Key: (script_url, function_name, start, end) → max count
        func_map: dict[tuple[str, str, int, int], dict[str, Any]] = {}

        for pass_data in passes:
            for script in pass_data.get("result", []):
                url = script.get("url", "")
                if not self._should_include_script(url):
                    continue

                for func_cov in script.get("functions", []):
                    name = func_cov.get("functionName", "")
                    ranges = func_cov.get("ranges", [])
                    if not ranges:
                        continue

                    # The first range is the function's own range
                    primary = ranges[0]
                    start = primary["startOffset"]
                    end = primary["endOffset"]
                    count = primary["count"]

                    key = (url, name, start, end)
                    if key not in func_map:
                        func_map[key] = {
                            "name": name,
                            "start": start,
                            "end": end,
                            "max_count": count,
                            "is_block": func_cov.get("isBlockCoverage", False),
                        }
                    else:
                        # Keep highest count across passes
                        if count > func_map[key]["max_count"]:
                            func_map[key]["max_count"] = count

        # Group by script URL
        result: dict[str, list[dict[str, Any]]] = {}
        for (url, _name, _start, _end), info in func_map.items():
            result.setdefault(url, []).append(info)

        # Sort functions within each script by start offset
        for url in result:
            result[url].sort(key=lambda f: f["start"])

        return result

    @staticmethod
    def _should_include_script(url: str) -> bool:
        """Return True if a script URL should be analysed."""
        if not url:
            return False
        if url.startswith("data:"):
            return False
        # Keep http(s) and chrome-extension URLs
        if url.startswith("http") or url.startswith("chrome-extension://"):
            return True
        return False

    @staticmethod
    def _script_coverage_pct(functions: list[dict[str, Any]]) -> float:
        """Rough coverage % for a script based on its function list."""
        total_bytes = 0
        covered_bytes = 0
        for f in functions:
            size = f["end"] - f["start"]
            total_bytes += size
            if f["max_count"] > 0:
                covered_bytes += size
        return (covered_bytes / total_bytes * 100) if total_bytes > 0 else 0.0

    # ── Source Fetching ──────────────────────────────────────────────────

    async def _fetch_sources(
        self, urls: list[str],
    ) -> dict[str, str]:
        """Fetch the JS source for each script URL."""
        sources: dict[str, str] = {}
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True,
        ) as client:
            for url in urls:
                try:
                    if url.startswith("http"):
                        resp = await client.get(url)
                        if resp.status_code == 200:
                            sources[url] = resp.text
                    elif url.startswith("chrome-extension://"):
                        text = self._read_extension_source(url)
                        if text:
                            sources[url] = text
                except Exception as exc:
                    logger.warning("Failed to fetch source for %s: %s", url, exc)
        return sources

    @staticmethod
    def _read_extension_source(url: str) -> str:
        """Read source from an unpacked extension path."""
        parts = url.split("/", 4)
        if len(parts) > 4:
            rel_path = parts[4]
            for base in [Path("."), Path("./extension")]:
                candidate = base / rel_path
                if candidate.exists():
                    return candidate.read_text()
        return ""

    # ── Risk Assessment ──────────────────────────────────────────────────

    @staticmethod
    def _assess_risk(
        name: str,
        func: dict[str, Any],
        source: str,
    ) -> RiskScore:
        """Classify the risk of removing a function.

        Uses only lightweight heuristics on the function name.  No external
        tools or AST parsing.
        """
        if name.startswith("<anonymous"):
            return RiskScore.LOW

        # Event handlers / lifecycle methods -- medium risk
        handler_patterns = [
            r"^on[A-Z]",       # onClick, onChange
            r"^handle[A-Z]",   # handleSubmit
            r"^_on[A-Z]",
            r"Listener$",
            r"Callback$",
            r"^init",
            r"^setup",
            r"^destroy",
            r"^cleanup",
            r"^componentDid",
            r"^componentWill",
            r"^use[A-Z]",      # React hooks
        ]
        for pat in handler_patterns:
            if re.search(pat, name):
                return RiskScore.MEDIUM

        # Check if the name is referenced as a string elsewhere in the source
        # (dynamic dispatch, eval, etc.)
        if re.search(rf"""['"]{re.escape(name)}['"]""", source):
            return RiskScore.HIGH

        return RiskScore.LOW


# ── Module-level helpers ─────────────────────────────────────────────────


def _make_function_id(
    script_url: str, name: str, start: int, end: int,
) -> str:
    """Generate a deterministic ID for a function."""
    raw = f"{script_url}:{name}:{start}:{end}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
