#!/usr/bin/env node

/**
 * CodeReaper MCP server launcher for npx / Cursor.
 *
 * Resolution order:
 *   1. uvx  (fastest — zero-install via uv)
 *   2. pipx (zero-install via pipx)
 *   3. python3 / python  (requires codereaper to be pip-installed)
 */

import { execSync, spawn } from "node:child_process";

function commandExists(cmd) {
  try {
    execSync(`${cmd} --version`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function resolve() {
  // 1. uvx (uv tool runner)
  if (commandExists("uvx")) {
    return { cmd: "uvx", args: ["codereaper"] };
  }

  // 2. pipx
  if (commandExists("pipx")) {
    return { cmd: "pipx", args: ["run", "codereaper"] };
  }

  // 3. Direct python (package must be installed)
  for (const py of ["python3", "python"]) {
    if (commandExists(py)) {
      return { cmd: py, args: ["-m", "codereaper.mcp"] };
    }
  }

  console.error(
    "Error: Could not find uvx, pipx, or python3.\n" +
      "Install one of:\n" +
      "  - uv:   curl -LsSf https://astral.sh/uv/install.sh | sh\n" +
      "  - pipx: pip install pipx\n" +
      "  - Or:   pip install codereaper"
  );
  process.exit(1);
}

const { cmd, args } = resolve();
const child = spawn(cmd, args, { stdio: "inherit" });

child.on("error", (err) => {
  console.error(`Failed to start ${cmd}: ${err.message}`);
  process.exit(1);
});

child.on("exit", (code) => {
  process.exit(code ?? 1);
});
