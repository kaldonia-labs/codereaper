#!/usr/bin/env python3
"""One-off script to run find_dead_code: scan URL + analyze and print report."""
import asyncio
import sys
import uuid
from pathlib import Path

# Package root = directory containing this script (outer codereaper)
_pkg_root = Path(__file__).resolve().parent
sys.path.insert(0, str(_pkg_root))

from codereaper.core.config import get_settings
from codereaper.core.storage import Storage
from codereaper.models.enums import ScanStatus


async def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765"
    source_dir = sys.argv[2] if len(sys.argv) > 2 else ""
    passes = 1
    max_steps_per_pass = 30

    settings = get_settings()
    storage = Storage(settings=settings)
    await storage.init()

    from codereaper.services.scanner import ScannerService
    from codereaper.services.analyzer import AnalyzerService
    from codereaper.mcp.server import _format_report

    scan_id = str(uuid.uuid4())[:12]
    await storage.create_scan(
        scan_id=scan_id,
        target=target,
        passes=passes,
        max_steps=max_steps_per_pass,
    )

    print(f"Scan {scan_id}: target={target} passes={passes} max_steps={max_steps_per_pass}", file=sys.stderr)
    scanner = ScannerService(storage=storage, settings=settings)
    await scanner.run_scan(
        scan_id=scan_id,
        target=target,
        passes=passes,
        max_steps=max_steps_per_pass,
    )

    scan = await storage.get_scan(scan_id)
    if scan is None or scan["status"] != ScanStatus.COMPLETED:
        error = (scan or {}).get("error", "Unknown error")
        print(f"Scan failed: {error}", file=sys.stderr)
        sys.exit(1)

    analyzer = AnalyzerService(storage=storage, settings=settings)
    candidates = await analyzer.analyze(scan_id)
    report = _format_report(scan_id, target, candidates, source_dir, passes)
    print(report)


if __name__ == "__main__":
    asyncio.run(main())
