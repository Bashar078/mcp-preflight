# MCP Preflight

A GitHub Action that fails CI when your MCP server:

1. **won't start**: it exits or never answers `tools/list`;
2. **has a tool with no description**; or
3. **has a tool description longer than 1,024 characters**, which OpenAI and Azure OpenAI function
   calling reject with HTTP 400 (`string_above_max_length`).

That's all it checks. No scores, no style opinions.

## Why these three
- **Long descriptions break OpenAI-based clients.** Any agent that passes your tools to OpenAI or Azure OpenAI
  function calling, for example through an SDK adapter, gets an error for the *whole request*, not just your tool.
  In October 2026 we checked 87 public MCP servers (deprecated packages excluded): **35 had at least one description
  over the limit.**
- **Missing descriptions leave models guessing.** Models pick tools by their descriptions.
- **Servers break without code changes.** A dependency release can break a fresh install of a server whose own code
  hasn't changed. We found published servers that had failed every fresh install for weeks to months, with no
  matching issue upstream. A weekly run against your *published* package catches this within a week.

Accuracy: on 28 servers the checks had never been tuned on, we reviewed all 78 description findings by hand and
found 0 false positives.

## Usage
```yaml
name: MCP Preflight
on:
  pull_request:
  schedule:
    - cron: "17 6 * * 1"   # weekly: catches dependency releases that break a fresh install

jobs:
  build:            # the code in this PR
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-node@v7
        with: {node-version: 22}
      - run: npm ci && npm run build
      - uses: Bashar078/mcp-preflight@v0
        with:
          command: node dist/index.js

  published:        # what users get today from a clean install
    runs-on: ubuntu-latest
    steps:
      - uses: Bashar078/mcp-preflight@v0
        with:
          command: npx -y your-mcp-server@latest
```
For a Python server, install uv first (for example `pipx install uv`) and use `command: uvx your-mcp-server`.

The `build` job tests your lockfile. Only the `published` job resolves dependencies the way a new user does, so
keep it if you want to catch breakage from upstream releases.

| Input | Default | |
|---|---|---|
| `command` | (required) | Starts your server over stdio |
| `max-length` | `1024` | Longest allowed description |
| `timeout` | `180` | Seconds to wait for `tools/list`, including package install |
| `report-only` | `false` | Write the report, never fail the job |

| Output | |
|---|---|
| `exit-code` | `0` pass · `1` description problems · `2` server did not start (set even when the step fails) |

## Output
A job summary like this, plus an annotation for each problem:

> **MCP Preflight: sequential-thinking-server 2.0.0**
>
> Started and listed **1 tool**.
>
> | Check | Result |
> |---|---|
> | Descriptions ≤ 1,024 chars | ❌ 1 tool(s) |
> | Every tool has a description | ✅ |
> | Starts and answers tools/list | ✅ |
>
> - `sequentialthinking`: description is 2,781 characters. OpenAI and Azure OpenAI function calling reject
>   descriptions over 1,024 characters with HTTP 400, so clients that pass this tool through an OpenAI-compatible
>   adapter fail the whole request.

## When "did not start" isn't a bug
The Action reports the server's last output and doesn't guess the cause. In our scans, fewer than half of
startup failures were broken installs. The rest needed something the test environment didn't have:
- **Credentials checked at startup.** Pass a test key through `env:`. Listing tools shouldn't need a real one;
  if yours does, consider deferring the check to the first tool call.
- **A service at startup** (database, Docker daemon, model download). Start it in the job, or use `report-only: true`.

## Limits
stdio servers only (no Streamable HTTP yet). Reads `tools/list` only. Needs `python3` on the runner (preinstalled on
GitHub-hosted Linux runners). Tested on GitHub-hosted `ubuntu-latest` (Python 3.12.3, October 2026); not yet tested on
macOS or Windows runners. No dependencies beyond the Python standard library.
