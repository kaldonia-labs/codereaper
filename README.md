# CodeReaper

CodeReaper is an AI-driven MCP tool for Cursor that finds and removes dead JavaScript by exploring real UIs and capturing V8 coverage.

## Key Features

- Autonomous UI exploration via the Index browser agent
- V8 precise coverage to identify zero-execution functions
- Risk scoring and removal recommendations
- Patch generation and optional verification replay
- MCP integration for Cursor workflows

## Quick Install

```bash
pip install codereaper
playwright install chromium
codereaper
```

When you first run `codereaper`, it prompts for your Gemini API key and saves it in your global `~/.cursor/mcp.json` so Cursor can invoke it later.

## Alternative Install

```bash
# pipx
pipx run codereaper

# npm
npx codereaper
```

## Prerequisites

- Python 3.11+
- Playwright Chromium (installed via `playwright install chromium`)
- A Gemini API key (or OpenAI / Anthropic if you change providers)

## Quick Start

1. Install and run `codereaper` (it updates `~/.cursor/mcp.json`)
2. Restart Cursor
3. Ask the assistant:
   > "Find dead JavaScript code on http://localhost:3000"

## Usage

Command:

```bash
codereaper
```

Example scan with local source mapping:

> "Find dead code on http://localhost:3000, source is in ./test_site"

## MCP Tools

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

## Troubleshooting

- If the browser doesn’t open, install Chromium: `playwright install chromium`
- If the scan fails with key errors, ensure `GEMINI_API_KEY` exists in `~/.cursor/mcp.json`
- If local pages don’t load, confirm your dev server is running and reachable
- If Gemini rate limits hit, retry after the quota window resets

## Update

- 02-09-2026: v0.2.3 release

## Issues & Feedback

Open an issue with steps to reproduce and logs if possible. Feedback and suggestions are welcome.
