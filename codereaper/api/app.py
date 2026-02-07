"""CodeReaper -- FastAPI application entry point.

Run with:
    uvicorn codereaper.api.app:app --reload
    python -m codereaper.api
    codereaper-api
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from codereaper.core.config import get_settings
from codereaper.core.storage import Storage
from codereaper.models.schemas import HealthResponse
from codereaper.api.routers.patches import patches_router, scan_patches_router
from codereaper.api.routers.scans import router as scans_router

# ---------------------------------------------------------------------------
# Global state -- initialised during lifespan
# ---------------------------------------------------------------------------

app_storage: Storage = Storage()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("codereaper")

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup / shutdown hook -- initialise storage, run migrations."""
    global app_storage
    settings = get_settings()
    app_storage = Storage(settings=settings)
    await app_storage.init()
    logger.info(
        "CodeReaper started -- data_dir=%s, db=%s",
        settings.data_dir,
        settings.db_path,
    )
    yield
    await app_storage.close()
    logger.info("CodeReaper shut down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CodeReaper",
    description=(
        "AI-driven JavaScript dead code elimination. "
        "Scans websites and Chrome extensions, identifies unused code via "
        "V8 coverage + autonomous browser exploration, and produces "
        "verified unified diffs."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# -- CORS ------------------------------------------------------------------

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- Routers ---------------------------------------------------------------

app.include_router(scans_router)
app.include_router(scan_patches_router)
app.include_router(patches_router)

# -- Health Check ----------------------------------------------------------


@app.get("/api/v1/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    return HealthResponse()
