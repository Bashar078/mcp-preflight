# Changelog

## v0.1.0 (first release)

MCP Preflight fails CI when an MCP server:
- **won't start**: it exits or never answers `tools/list`. The job shows the server's last output and the error line as an annotation;
- **has a tool with no description**; or
- **has a tool description longer than 1,024 characters**, which OpenAI and Azure OpenAI function calling reject with HTTP 400.

Also in this release:
- `report-only` input, and an `exit-code` output (`0` / `1` / `2`) for custom handling.
- One automatic retry of server startup, so a transient install failure doesn't fail the job.
- Python standard library only; works with any server you can start from the command line (npx, uvx, node, python).

Tested on GitHub-hosted `ubuntu-latest` runners against real servers: one that passes, one with a 2,781-character
description, and two published servers that currently fail a fresh install. All four produced the expected result.
Before release, the description checks were run on 87 public MCP servers; on the 28 servers they had never been tuned
on, all 78 findings were reviewed by hand with 0 false positives.

Known limits: stdio servers only; checks `tools/list` only; not yet tested on macOS or Windows runners.
