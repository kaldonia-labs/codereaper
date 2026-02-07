"""CodeReaper MCP server — dead code elimination tools for Cursor."""


def main() -> None:
    """Entry point for ``codereaper`` and ``codereaper-mcp`` console scripts."""
    from codereaper.mcp.server import mcp

    mcp.run(transport="stdio")
