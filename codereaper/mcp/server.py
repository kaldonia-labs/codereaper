"""CodeReaper MCP Server -- dead code elimination tools for Cursor.

Wraps the existing CodeReaper pipeline (browser agent scan, V8 coverage
analysis, patch generation) as MCP tools that Cursor can invoke to find
and suggest removal of dead JavaScript code.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP

from codereaper.core.config import Settings, get_settings
from codereaper.core.unbound_llm import unbound_ping as _unbound_ping
from codereaper.core.storage import Storage
from codereaper.models.enums import SafetyMode, ScanStatus
from codereaper.models.schemas import Candidate

# ---------------------------------------------------------------------------
# Heavy services are imported lazily to avoid loading the browser agent
# (lmnr-index / playwright) at import time.
# ---------------------------------------------------------------------------


def _get_scanner_cls():
    from codereaper.services.scanner import ScannerService

    return ScannerService


def _get_analyzer_cls():
    from codereaper.services.analyzer import AnalyzerService

    return AnalyzerService


def _get_patcher_cls():
    from codereaper.services.patcher import PatcherService

    return PatcherService


def _get_verifier_cls():
    from codereaper.services.verifier import VerifierService

    return VerifierService


# ---------------------------------------------------------------------------
# Logging -- stderr only (stdout is reserved for MCP JSON-RPC protocol)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("codereaper.mcp")

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "CodeReaper",
    instructions=(
        "CodeReaper finds dead JavaScript code in websites by launching an AI "
        "browser agent that autonomously explores the UI while collecting V8 "
        "code coverage data. Use 'find_dead_code' as the primary tool to scan "
        "a URL and receive a detailed report of unused functions. You can then "
        "suggest removals to the user based on the report, or use "
        "'generate_patches' to create unified diffs."
    ),
)

# ---------------------------------------------------------------------------
# Lazy-initialised singletons
# ---------------------------------------------------------------------------

_storage: Storage | None = None
_settings: Settings | None = None


async def _ensure_init() -> tuple[Storage, Settings]:
    """Initialise storage and settings on first use."""
    global _storage, _settings
    if _storage is None:
        _settings = get_settings()
        _storage = Storage(settings=_settings)
        await _storage.init()
        logger.info("Storage initialised (data_dir=%s)", _settings.data_dir)
    assert _settings is not None
    return _storage, _settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _url_to_local_path(url: str, source_dir: str) -> str | None:
    """Map a script URL (e.g. http://localhost:3000/app.js) to a local file."""
    if not source_dir:
        return None
    parsed = urlparse(url)
    url_path = parsed.path.lstrip("/")
    if not url_path:
        return None

    candidate = Path(source_dir) / url_path
    if candidate.exists():
        return str(candidate)

    parts = url_path.split("/")
    for i in range(1, len(parts)):
        sub = "/".join(parts[i:])
        candidate = Path(source_dir) / sub
        if candidate.exists():
            return str(candidate)

    return None


def _format_report(
    scan_id: str,
    target: str,
    candidates: list[Candidate],
    source_dir: str = "",
    passes: int = 1,
) -> str:
    """Build a human-readable + AI-parseable dead code report."""
    if not candidates:
        return (
            f"No dead code found for scan {scan_id} ({target}).\n"
            f"All detected JavaScript functions were executed during "
            f"{passes} exploration pass(es)."
        )

    total_bytes = sum(c.byte_size for c in candidates)
    unique_files = sorted({c.file_path for c in candidates})

    lines: list[str] = [
        "Dead Code Analysis Report",
        "=" * 60,
        f"Scan ID   : {scan_id}",
        f"Target    : {target}",
        f"Passes    : {passes}",
        f"Files     : {len(unique_files)}",
        f"Candidates: {len(candidates)}",
        f"Reclaimable bytes: {total_bytes:,}",
        "",
    ]

    by_file: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_file.setdefault(c.file_path, []).append(c)

    num = 0
    for file_path in sorted(by_file):
        file_candidates = sorted(by_file[file_path], key=lambda c: c.line_start)
        local = _url_to_local_path(file_path, source_dir) if source_dir else None

        lines.append("-" * 60)
        lines.append(f"File: {file_path}")
        if local:
            lines.append(f"Local path: {local}")
        file_bytes = sum(c.byte_size for c in file_candidates)
        lines.append(f"Dead functions: {len(file_candidates)} ({file_bytes:,} bytes)")
        lines.append("")

        for c in file_candidates:
            num += 1
            risk = (
                c.risk_score.upper()
                if isinstance(c.risk_score, str)
                else str(c.risk_score)
            )
            lines.append(
                f"  {num}. {c.name}  "
                f"[lines {c.line_start}-{c.line_end}, {c.byte_size:,} bytes, {risk} risk]"
            )
            lines.append(f"     {c.evidence}")

            if risk == "LOW":
                lines.append("     Recommendation: Safe to remove.")
            elif risk == "MEDIUM":
                lines.append(
                    "     Recommendation: Likely safe, but verify surrounding "
                    "code still works after removal."
                )
            else:
                lines.append(
                    "     Recommendation: May be dynamically referenced. "
                    "Review carefully before removing."
                )
            lines.append("")

    lines.append("=" * 60)
    lines.append("")
    lines.append("Next steps:")
    lines.append(
        f'  - Generate removal diffs: generate_patches(scan_id="{scan_id}")'
    )
    lines.append(
        "  - Safety modes: conservative (low-risk only), balanced "
        "(low+medium), aggressive (all)"
    )
    lines.append(
        "  - Or edit the files directly based on the candidates above."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def unbound_ping() -> str:
    """Ping the Unbound API (getunbound.ai). Returns the model reply; expect 'UNBOUND_OK' if the API key and model work."""
    return await _unbound_ping()


@mcp.tool()
async def find_dead_code(
    target: str,
    source_dir: str = "",
    passes: int = 1,
    max_steps_per_pass: int = 50,
) -> str:
    """Scan a website and find dead JavaScript code using an AI browser agent.

    This is the primary tool.  It launches a Chromium browser with an AI agent
    that autonomously explores the site (clicks buttons, navigates routes,
    opens modals, fills forms, triggers keyboard shortcuts) while V8 precise
    coverage tracks which JavaScript functions actually execute.

    Functions that receive zero executions across all exploration passes are
    reported as dead-code candidates with risk scores and removal
    recommendations.

    Args:
        target: URL to scan, e.g. "http://localhost:3000" or "https://example.com".
        source_dir: Optional absolute path to the local directory that contains
                    the JS source files.  When provided, script URLs are mapped
                    to local file paths in the report so Cursor can suggest edits
                    directly.
        passes: Number of exploration passes (more = better coverage, default 1).
        max_steps_per_pass: Max browser-agent steps per pass (default 50).

    Returns:
        Detailed report listing every dead-code candidate with file path, line
        range, byte size, risk score, and removal recommendation.
    """
    storage, settings = await _ensure_init()

    ScannerService = _get_scanner_cls()
    scanner = ScannerService(storage=storage, settings=settings)
    scan_id = str(uuid.uuid4())[:12]

    await storage.create_scan(
        scan_id=scan_id,
        target=target,
        passes=passes,
        max_steps=max_steps_per_pass,
    )

    logger.info(
        "Scan %s: target=%s passes=%d max_steps=%d",
        scan_id,
        target,
        passes,
        max_steps_per_pass,
    )

    await scanner.run_scan(
        scan_id=scan_id,
        target=target,
        passes=passes,
        max_steps=max_steps_per_pass,
    )

    scan = await storage.get_scan(scan_id)
    if scan is None or scan["status"] != ScanStatus.COMPLETED:
        error = (scan or {}).get("error", "Unknown error")
        return f"Scan failed: {error}"

    AnalyzerService = _get_analyzer_cls()
    analyzer = AnalyzerService(storage=storage, settings=settings)
    candidates = await analyzer.analyze(scan_id)

    return _format_report(scan_id, target, candidates, source_dir, passes)


@mcp.tool()
async def scan_website(
    target: str,
    passes: int = 1,
    max_steps_per_pass: int = 50,
) -> str:
    """Launch the AI browser agent to explore a website and collect V8 coverage.

    Use this when you want to run the scan separately from the analysis
    (e.g. to inspect scan status first).  After it completes, pass the
    returned scan_id to analyze_dead_code().

    Args:
        target: URL to scan.
        passes: Exploration passes (default 1).
        max_steps_per_pass: Max agent steps per pass (default 50).

    Returns:
        JSON with scan_id, status, timing, and interaction count.
    """
    storage, settings = await _ensure_init()
    ScannerService = _get_scanner_cls()
    scanner = ScannerService(storage=storage, settings=settings)

    scan_id = str(uuid.uuid4())[:12]
    await storage.create_scan(
        scan_id=scan_id,
        target=target,
        passes=passes,
        max_steps=max_steps_per_pass,
    )

    await scanner.run_scan(
        scan_id=scan_id,
        target=target,
        passes=passes,
        max_steps=max_steps_per_pass,
    )

    scan = await storage.get_scan(scan_id)
    return json.dumps(
        {
            "scan_id": scan_id,
            "status": scan["status"] if scan else "unknown",
            "target": target,
            "passes": passes,
            "duration_seconds": (scan or {}).get("duration_seconds"),
            "total_interactions": (scan or {}).get("total_interactions"),
            "error": (scan or {}).get("error"),
        },
        indent=2,
    )


@mcp.tool()
async def analyze_dead_code(
    scan_id: str,
    source_dir: str = "",
) -> str:
    """Analyze a completed scan to identify dead JavaScript functions.

    Reads V8 coverage data collected during the scan, merges execution
    counts across passes, and reports every function with zero executions.

    Args:
        scan_id: The scan_id returned by scan_website().
        source_dir: Optional local directory for URL-to-file mapping.

    Returns:
        Detailed dead-code report (same format as find_dead_code).
    """
    storage, settings = await _ensure_init()

    scan = await storage.get_scan(scan_id)
    if not scan:
        return f"Error: Scan '{scan_id}' not found."
    if scan["status"] != ScanStatus.COMPLETED:
        return (
            f"Error: Scan status is '{scan['status']}'; "
            f"it must be 'completed' before analysis."
        )

    AnalyzerService = _get_analyzer_cls()
    analyzer = AnalyzerService(storage=storage, settings=settings)
    candidates = await analyzer.analyze(scan_id)

    return _format_report(
        scan_id,
        scan["target"],
        candidates,
        source_dir,
        scan.get("passes", 1),
    )


@mcp.tool()
async def generate_patches(
    scan_id: str,
    safety_mode: str = "conservative",
) -> str:
    """Generate unified-diff patches that remove dead code.

    Creates a patch set based on the analysis results for the given scan.
    Each affected file gets a unified diff that replaces dead functions
    with a removal comment.

    Safety modes control which risk levels are included:
      - conservative: low-risk only (safest, default)
      - balanced: low + medium risk
      - aggressive: all candidates including high risk

    Args:
        scan_id: Scan ID from a completed analysis.
        safety_mode: One of "conservative", "balanced", "aggressive".

    Returns:
        Patch ID and unified diffs for every affected file.
    """
    storage, settings = await _ensure_init()
    PatcherService = _get_patcher_cls()
    patcher = PatcherService(storage=storage, settings=settings)

    try:
        mode = SafetyMode(safety_mode)
    except ValueError:
        return (
            f"Error: Invalid safety_mode '{safety_mode}'. "
            f"Valid options: conservative, balanced, aggressive."
        )

    try:
        patch_id, hunks = await patcher.generate_patches(
            scan_id=scan_id,
            safety_mode=mode,
        )
    except ValueError as exc:
        return f"Error: {exc}"

    parts: list[str] = [
        f"Patch ID: {patch_id}",
        f"Safety mode: {safety_mode}",
        f"Files affected: {len(hunks)}",
        "",
    ]

    for hunk in hunks:
        parts.append(f"--- {hunk.file_path} ---")
        parts.append(f"Risk: {hunk.risk_score}")
        parts.append(f"Functions removed: {len(hunk.candidates_removed)}")
        parts.append(f"Rationale:\n{hunk.rationale}")
        parts.append("")
        parts.append(hunk.unified_diff)
        parts.append("")

    parts.append(f'To apply this patch: apply_patch(patch_id="{patch_id}")')
    parts.append(f'To rollback later:   rollback_patch(patch_id="{patch_id}")')

    return "\n".join(parts)


@mcp.tool()
async def get_patch_diff(patch_id: str) -> str:
    """Retrieve the combined unified diff for a generated patch.

    Args:
        patch_id: Patch ID from generate_patches().

    Returns:
        Combined unified diff string.
    """
    storage, settings = await _ensure_init()
    PatcherService = _get_patcher_cls()
    patcher = PatcherService(storage=storage, settings=settings)

    try:
        diff = await patcher.get_combined_diff(patch_id)
        return diff if diff else "No diff content."
    except ValueError as exc:
        return f"Error: {exc}"


@mcp.tool()
async def apply_patch(patch_id: str) -> str:
    """Apply a generated patch to the source files on disk.

    Stores pre-patch snapshots so the change can be rolled back later.
    Review the diff (get_patch_diff) before applying.

    Args:
        patch_id: Patch ID from generate_patches().

    Returns:
        JSON with the list of modified file paths.
    """
    storage, settings = await _ensure_init()
    PatcherService = _get_patcher_cls()
    patcher = PatcherService(storage=storage, settings=settings)

    try:
        modified = await patcher.apply_patch(patch_id)
        return json.dumps(
            {"patch_id": patch_id, "status": "applied", "files_modified": modified},
            indent=2,
        )
    except ValueError as exc:
        return f"Error: {exc}"


@mcp.tool()
async def rollback_patch(patch_id: str) -> str:
    """Rollback a previously applied patch, restoring the original files.

    Args:
        patch_id: Patch ID to rollback.

    Returns:
        JSON with the list of restored file paths.
    """
    storage, settings = await _ensure_init()
    PatcherService = _get_patcher_cls()
    patcher = PatcherService(storage=storage, settings=settings)

    try:
        restored = await patcher.rollback_patch(patch_id)
        return json.dumps(
            {
                "patch_id": patch_id,
                "status": "rolled_back",
                "files_restored": restored,
            },
            indent=2,
        )
    except ValueError as exc:
        return f"Error: {exc}"


@mcp.tool()
async def verify_patch(patch_id: str) -> str:
    """Verify an applied patch by replaying the original interactions.

    Re-runs the browser agent with the same interaction plan used during the
    scan and checks for regressions: new console errors, missing elements,
    and coverage drops.

    Args:
        patch_id: Patch ID to verify.

    Returns:
        JSON verification result with pass/fail, regressions, and coverage
        comparison.
    """
    storage, settings = await _ensure_init()
    VerifierService = _get_verifier_cls()
    verifier = VerifierService(storage=storage, settings=settings)

    try:
        result = await verifier.verify(patch_id=patch_id)
        return json.dumps(result.model_dump(), indent=2, default=str)
    except ValueError as exc:
        return f"Error: {exc}"


@mcp.tool()
async def list_scans() -> str:
    """List recent scans with their status.

    Returns:
        JSON array of scan records (most recent first, up to 20).
    """
    storage, _ = await _ensure_init()
    assert storage._db is not None

    cur = await storage._db.execute(
        "SELECT scan_id, target, status, passes, "
        "duration_seconds, total_interactions, started_at "
        "FROM scans ORDER BY started_at DESC LIMIT 20"
    )
    rows = await cur.fetchall()
    scans = [dict(r) for r in rows]

    if not scans:
        return "No scans found. Use find_dead_code() or scan_website() to start one."

    return json.dumps(scans, indent=2, default=str)


@mcp.tool()
async def get_scan_status(scan_id: str) -> str:
    """Get detailed status and metadata for a specific scan.

    Args:
        scan_id: The scan ID to look up.

    Returns:
        JSON with full scan details.
    """
    storage, _ = await _ensure_init()
    scan = await storage.get_scan(scan_id)

    if not scan:
        return f"Error: Scan '{scan_id}' not found."

    return json.dumps(scan, indent=2, default=str)
