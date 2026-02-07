# CodeReaper

AI-driven JavaScript dead code elimination for websites and Chrome extensions.

CodeReaper scans a target URL or unpacked Chrome extension, autonomously explores the UI via the [Index](https://github.com/lmnr-ai/index) browser agent, collects V8 precise coverage data, and produces verified unified diffs that safely remove dead code.

## Install

### pip / pipx (recommended)

```bash
pip install codereaper
playwright install chromium
```

Or with zero-install via pipx:

```bash
pipx run codereaper          # starts MCP server directly
```

### npm / npx

```bash
npx codereaper               # auto-detects uvx/pipx/python
```

### From source

```bash
git clone https://github.com/kaldonia-labs/codereaper.git
cd codereaper
pip install -e .
playwright install chromium
```

## Configure

Create a `.env` file in the project root (or set environment variables):

```env
# LLM for Index browser agent
CODEREAPER_INDEX_LLM_PROVIDER=gemini
CODEREAPER_INDEX_LLM_MODEL=gemini-2.5-pro-preview-05-06
GOOGLE_API_KEY=your-key-here

# Or use OpenAI / Anthropic
# CODEREAPER_INDEX_LLM_PROVIDER=openai
# CODEREAPER_INDEX_LLM_MODEL=gpt-4o
# OPENAI_API_KEY=your-key-here

# Storage (optional, defaults shown)
CODEREAPER_DATA_DIR=./data
CODEREAPER_DB_PATH=./data/codereaper.db
```

## Cursor MCP Integration

CodeReaper runs as an MCP (Model Context Protocol) server that Cursor invokes to scan websites, find dead code, and suggest what to delete.

### Add to Cursor

Add one of these to your `.cursor/mcp.json`:

**If installed via pip:**

```json
{
  "mcpServers": {
    "codereaper": {
      "command": "codereaper"
    }
  }
}
```

**If using pipx (zero-install):**

```json
{
  "mcpServers": {
    "codereaper": {
      "command": "pipx",
      "args": ["run", "codereaper"]
    }
  }
}
```

**If using npx (zero-install):**

```json
{
  "mcpServers": {
    "codereaper": {
      "command": "npx",
      "args": ["-y", "codereaper"]
    }
  }
}
```

**From source (development):**

```json
{
  "mcpServers": {
    "codereaper": {
      "command": "python3",
      "args": ["-m", "codereaper.mcp"],
      "cwd": "/absolute/path/to/codereaper"
    }
  }
}
```

Restart Cursor after editing `mcp.json`.

### Usage in Cursor

Ask the assistant to find dead code:

> "Find dead JavaScript code on http://localhost:3000"

The assistant calls `find_dead_code` which:
1. Launches a browser with an AI agent that explores the site
2. Collects V8 code coverage showing which functions executed
3. Returns a report of every function with zero executions

Provide a local source directory so the report maps URLs to local files:

> "Find dead code on http://localhost:3000, source is in ./test_site"

### MCP Tools

| Tool | Description |
|------|-------------|
| `find_dead_code` | Full pipeline: scan + analyze. Returns dead-code report with file paths, line numbers, risk scores, and removal recommendations. |
| `scan_website` | Scan only (no analysis). Returns scan_id for later use. |
| `analyze_dead_code` | Analyze a completed scan. Takes scan_id. |
| `generate_patches` | Generate unified diffs to remove dead code (conservative / balanced / aggressive). |
| `get_patch_diff` | Retrieve the combined diff for a patch. |
| `apply_patch` | Apply a patch to source files (stores snapshots for rollback). |
| `verify_patch` | Re-run the browser agent to check for regressions after patching. |
| `rollback_patch` | Restore original files from pre-patch snapshots. |
| `list_scans` | List recent scans. |
| `get_scan_status` | Get detailed status of a scan. |

## Architecture

```
Cursor / AI assistant
        |
   MCP Server (stdio)
   (codereaper.mcp)
        |
   Services Layer
   (scanner, analyzer, patcher, verifier)
        |               |
  Index Browser Agent  V8 CDP Coverage
  (autonomous UI       (precise per-function
   exploration)         execution counts)
```

## Pipeline

| Phase | Description |
|-------|-------------|
| 1. Scan & Explore | Launch Index agent to autonomously interact with all UI surfaces while collecting V8 coverage |
| 2. Analyze | Map coverage ranges to functions, classify executed vs. unexecuted, detect dynamic references |
| 3. Patch | Generate unified diffs with rationale and risk scores |
| 4. Verify | Replay the original interaction plan against patched code, detect regressions |
| 5. Rollback | Restore original files from pre-patch snapshots |

## Project Structure

```
codereaper/
├── __main__.py              # python -m codereaper -> MCP server
├── mcp/                     # MCP server for Cursor
│   ├── __init__.py          # main() entry point
│   ├── __main__.py          # python -m codereaper.mcp
│   └── server.py            # FastMCP tool definitions
├── services/                # Business logic
│   ├── scanner.py           # Index agent orchestration + CDP coverage
│   ├── analyzer.py          # Coverage mapping + dead code detection
│   ├── patcher.py           # Diff generation + application + rollback
│   └── verifier.py          # Replay + regression detection
├── models/
│   ├── schemas.py           # Pydantic request/response models
│   └── enums.py             # Status, risk, safety enums
├── core/
│   ├── config.py            # Settings via pydantic-settings
│   └── storage.py           # SQLite + filesystem artifact storage
└── tests/

bin/cli.mjs                  # npx wrapper (spawns uvx/pipx/python)
pyproject.toml               # Python packaging (pip install codereaper)
package.json                 # npm packaging (npx codereaper)
```

## Tech Stack

- **MCP Server**: FastMCP
- **Validation**: Pydantic v2
- **Storage**: aiosqlite + filesystem
- **Browser**: Playwright (via Index agent)
- **Coverage**: V8 CDP Profiler domain
- **Diff**: Python `difflib`
