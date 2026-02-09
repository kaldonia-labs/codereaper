"""CodeReaper MCP server — dead code elimination tools for Cursor."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def _cursor_mcp_path() -> Path:
    return Path(os.path.expanduser("~/.cursor/mcp.json"))


def _load_mcp_config(path: Path) -> dict:
    if not path.exists():
        return {"mcpServers": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(
            f"Warning: Could not parse {path}. "
            "Skipping automatic MCP config update.",
            file=sys.stderr,
        )
        return {}


def _has_codereaper_key(config: dict) -> bool:
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    entry = servers.get("codereaper")
    if not isinstance(entry, dict):
        return False
    env = entry.get("env")
    if not isinstance(env, dict):
        return False
    return bool(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"))


def _prompt_api_key() -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        import getpass

        key = getpass.getpass(
            "Enter Gemini API key (stored in ~/.cursor/mcp.json): "
        )
    except Exception:
        try:
            key = input("Enter Gemini API key (stored in ~/.cursor/mcp.json): ")
        except Exception:
            return None
    key = key.strip()
    return key or None


def _ensure_cursor_mcp_config() -> None:
    load_dotenv()
    path = _cursor_mcp_path()
    config = _load_mcp_config(path)

    if _has_codereaper_key(config):
        return

    api_key = _prompt_api_key()
    if not api_key:
        print(
            "Gemini API key is missing. Set GOOGLE_API_KEY or "
            "add it to ~/.cursor/mcp.json under mcpServers.codereaper.env.",
            file=sys.stderr,
        )
        return

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return

    entry = servers.get("codereaper")
    if not isinstance(entry, dict):
        entry = {}
        servers["codereaper"] = entry

    entry.setdefault("command", "codereaper")
    env = entry.get("env")
    if not isinstance(env, dict):
        env = {}
        entry["env"] = env

    env.setdefault("CODEREAPER_INDEX_LLM_PROVIDER", "gemini")
    env.setdefault("CODEREAPER_INDEX_LLM_MODEL", "gemini-3.0-flash")
    env["GEMINI_API_KEY"] = api_key
    env.setdefault("GOOGLE_API_KEY", api_key)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {path} with codereaper MCP config.", file=sys.stderr)


def main() -> None:
    """Entry point for ``codereaper`` and ``codereaper-mcp`` console scripts."""
    _ensure_cursor_mcp_config()
    from codereaper.mcp.server import mcp

    mcp.run(transport="stdio")
