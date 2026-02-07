"""CodeReaper REST API — FastAPI server."""


def main() -> None:
    """Entry point for the ``codereaper-api`` console script."""
    import uvicorn

    uvicorn.run(
        "codereaper.api.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
