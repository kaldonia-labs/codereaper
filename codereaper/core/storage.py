"""Artifact storage backed by SQLite (metadata) + filesystem (blobs).

Tables
------
scans          – scan metadata, status, timestamps
analyses       – analysis results per scan
patches        – patch metadata, status
patch_hunks    – individual diff hunks per patch
verifications  – verification results per patch

Filesystem layout under ``data_dir``::

    data/
    ├── scans/{scan_id}/
    │   ├── coverage/        # raw V8 coverage JSON per pass
    │   ├── interactions/    # interaction plan JSON per pass
    │   ├── console_logs/    # console logs per pass
    │   └── network_logs/    # network log per pass
    ├── patches/{patch_id}/
    │   ├── diffs/           # unified diff files
    │   └── snapshots/       # pre-patch file backups
    └── codereaper.db
"""

from __future__ import annotations

import json
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from codereaper.core.config import Settings, get_settings
from codereaper.models.enums import (
    AnalysisStatus,
    PatchStatus,
    RiskScore,
    SafetyMode,
    ScanStatus,
    VerificationStatus,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id         TEXT PRIMARY KEY,
    target          TEXT NOT NULL,
    extension_path  TEXT,
    passes          INTEGER NOT NULL DEFAULT 3,
    max_steps       INTEGER NOT NULL DEFAULT 100,
    status          TEXT NOT NULL DEFAULT 'pending',
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    duration_seconds REAL,
    interaction_plan_hash TEXT,
    total_interactions INTEGER,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    scan_id             TEXT PRIMARY KEY REFERENCES scans(scan_id),
    status              TEXT NOT NULL DEFAULT 'pending',
    candidates_json     TEXT,  -- JSON array of Candidate dicts
    total_candidates    INTEGER DEFAULT 0,
    total_reclaimable   INTEGER DEFAULT 0,
    files_analyzed      INTEGER DEFAULT 0,
    error               TEXT
);

CREATE TABLE IF NOT EXISTS patches (
    patch_id        TEXT PRIMARY KEY,
    scan_id         TEXT NOT NULL REFERENCES scans(scan_id),
    status          TEXT NOT NULL DEFAULT 'generated',
    safety_mode     TEXT NOT NULL DEFAULT 'conservative',
    hunks_json      TEXT,  -- JSON array of PatchHunk dicts
    total_files     INTEGER DEFAULT 0,
    total_bytes     INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    patch_id            TEXT PRIMARY KEY REFERENCES patches(patch_id),
    status              TEXT NOT NULL DEFAULT 'pending',
    passed              INTEGER,  -- 0/1
    regressions_json    TEXT,
    coverage_cmp_json   TEXT,
    duration_seconds    REAL,
    error               TEXT
);
"""


class Storage:
    """Async storage layer combining SQLite and filesystem artifacts."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._db_path = self._settings.db_path
        self._data_dir = self._settings.data_dir
        self._db: aiosqlite.Connection | None = None

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def init(self) -> None:
        """Initialise the database and filesystem directories."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        (self._data_dir / "scans").mkdir(exist_ok=True)
        (self._data_dir / "patches").mkdir(exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA_SQL)
        await self._db.commit()

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()

    # ── Scans ───────────────────────────────────────────────────────────

    async def create_scan(
        self,
        scan_id: str,
        target: str,
        passes: int,
        max_steps: int,
        extension_path: str | None = None,
    ) -> None:
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO scans
               (scan_id, target, extension_path, passes, max_steps, status, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (scan_id, target, extension_path, passes, max_steps, ScanStatus.PENDING, now),
        )
        await self._db.commit()
        # Create filesystem dirs
        scan_dir = self._data_dir / "scans" / scan_id
        for sub in ("coverage", "interactions", "console_logs", "network_logs"):
            (scan_dir / sub).mkdir(parents=True, exist_ok=True)

    async def update_scan(self, scan_id: str, **fields: Any) -> None:
        assert self._db is not None
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [scan_id]
        await self._db.execute(
            f"UPDATE scans SET {sets} WHERE scan_id = ?", vals
        )
        await self._db.commit()

    async def get_scan(self, scan_id: str) -> dict[str, Any] | None:
        assert self._db is not None
        cur = await self._db.execute("SELECT * FROM scans WHERE scan_id = ?", (scan_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    # ── Scan Artifacts ──────────────────────────────────────────────────

    def scan_dir(self, scan_id: str) -> Path:
        return self._data_dir / "scans" / scan_id

    async def store_coverage(self, scan_id: str, pass_num: int, data: Any) -> None:
        path = self.scan_dir(scan_id) / "coverage" / f"pass_{pass_num}.json"
        path.write_text(json.dumps(data, default=str))

    async def load_coverage(self, scan_id: str) -> list[dict]:
        """Load all coverage passes for a scan."""
        cov_dir = self.scan_dir(scan_id) / "coverage"
        results = []
        for p in sorted(cov_dir.glob("pass_*.json")):
            results.append(json.loads(p.read_text()))
        return results

    async def store_interactions(self, scan_id: str, pass_num: int, data: Any) -> None:
        path = self.scan_dir(scan_id) / "interactions" / f"pass_{pass_num}.json"
        path.write_text(json.dumps(data, default=str))

    async def load_interactions(self, scan_id: str) -> list[dict]:
        int_dir = self.scan_dir(scan_id) / "interactions"
        results = []
        for p in sorted(int_dir.glob("pass_*.json")):
            results.append(json.loads(p.read_text()))
        return results

    async def store_console_logs(self, scan_id: str, pass_num: int, data: Any) -> None:
        path = self.scan_dir(scan_id) / "console_logs" / f"pass_{pass_num}.json"
        path.write_text(json.dumps(data, default=str))

    async def store_network_log(self, scan_id: str, pass_num: int, data: Any) -> None:
        path = self.scan_dir(scan_id) / "network_logs" / f"pass_{pass_num}.json"
        path.write_text(json.dumps(data, default=str))

    def compute_interaction_plan_hash(self, scan_id: str) -> str:
        """SHA-256 of the concatenated interaction plans."""
        int_dir = self.scan_dir(scan_id) / "interactions"
        h = hashlib.sha256()
        for p in sorted(int_dir.glob("pass_*.json")):
            h.update(p.read_bytes())
        return h.hexdigest()[:16]

    # ── Analysis ────────────────────────────────────────────────────────

    async def create_analysis(self, scan_id: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO analyses (scan_id, status) VALUES (?, ?)",
            (scan_id, AnalysisStatus.PENDING),
        )
        await self._db.commit()

    async def update_analysis(self, scan_id: str, **fields: Any) -> None:
        assert self._db is not None
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [scan_id]
        await self._db.execute(
            f"UPDATE analyses SET {sets} WHERE scan_id = ?", vals
        )
        await self._db.commit()

    async def get_analysis(self, scan_id: str) -> dict[str, Any] | None:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM analyses WHERE scan_id = ?", (scan_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    # ── Patches ─────────────────────────────────────────────────────────

    async def create_patch(
        self,
        patch_id: str,
        scan_id: str,
        safety_mode: str,
        hunks_json: str,
        total_files: int,
        total_bytes: int,
    ) -> None:
        assert self._db is not None
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO patches
               (patch_id, scan_id, status, safety_mode, hunks_json,
                total_files, total_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                patch_id, scan_id, PatchStatus.GENERATED,
                safety_mode, hunks_json, total_files, total_bytes, now,
            ),
        )
        await self._db.commit()
        # Create patch filesystem dirs
        patch_dir = self._data_dir / "patches" / patch_id
        (patch_dir / "diffs").mkdir(parents=True, exist_ok=True)
        (patch_dir / "snapshots").mkdir(parents=True, exist_ok=True)

    async def update_patch(self, patch_id: str, **fields: Any) -> None:
        assert self._db is not None
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [patch_id]
        await self._db.execute(
            f"UPDATE patches SET {sets} WHERE patch_id = ?", vals
        )
        await self._db.commit()

    async def get_patch(self, patch_id: str) -> dict[str, Any] | None:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM patches WHERE patch_id = ?", (patch_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_patches_for_scan(self, scan_id: str) -> list[dict[str, Any]]:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM patches WHERE scan_id = ?", (scan_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    def patch_dir(self, patch_id: str) -> Path:
        return self._data_dir / "patches" / patch_id

    async def store_snapshot(self, patch_id: str, file_path: str, content: str) -> None:
        """Back up a file before patching for rollback."""
        safe_name = file_path.replace("/", "__").replace("\\", "__")
        snap = self.patch_dir(patch_id) / "snapshots" / safe_name
        snap.write_text(content)

    async def load_snapshot(self, patch_id: str, file_path: str) -> str | None:
        safe_name = file_path.replace("/", "__").replace("\\", "__")
        snap = self.patch_dir(patch_id) / "snapshots" / safe_name
        return snap.read_text() if snap.exists() else None

    async def list_snapshots(self, patch_id: str) -> list[str]:
        """Return original file paths that have snapshots."""
        snap_dir = self.patch_dir(patch_id) / "snapshots"
        if not snap_dir.exists():
            return []
        return [
            f.name.replace("__", "/")
            for f in snap_dir.iterdir()
            if f.is_file()
        ]

    # ── Verification ────────────────────────────────────────────────────

    async def create_verification(self, patch_id: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO verifications (patch_id, status) VALUES (?, ?)",
            (patch_id, VerificationStatus.PENDING),
        )
        await self._db.commit()

    async def update_verification(self, patch_id: str, **fields: Any) -> None:
        assert self._db is not None
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [patch_id]
        await self._db.execute(
            f"UPDATE verifications SET {sets} WHERE patch_id = ?", vals
        )
        await self._db.commit()

    async def get_verification(self, patch_id: str) -> dict[str, Any] | None:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT * FROM verifications WHERE patch_id = ?", (patch_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None
