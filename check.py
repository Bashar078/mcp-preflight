#!/usr/bin/env python3
"""MCP Preflight (minimal): three checks, nothing else.

  1. a tool description longer than 1,024 characters (OpenAI/Azure function calling reject it with HTTP 400)
  2. a tool with a missing or empty description
  3. the server does not start and answer tools/list (stderr tail is included)

Usage: python3 check.py --cmd "npx -y your-mcp-server@latest" [--md report.md] [--max-len 1024] [--timeout 180]
Exit codes: 0 = pass, 1 = description problems, 2 = server did not start.
Reuses snapshot() and the P002/P003 rules from preflight.py, which were audited out of sample (0 false positives).
"""
import argparse, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from preflight import snapshot  # noqa: E402


def run(cmd, timeout, retries):
    err = None
    for attempt in range(retries + 1):
        try:
            return snapshot(cmd, timeout=timeout), None
        except Exception as e:  # exited, timed out, or answered with an error
            err = f"{type(e).__name__}: {e}"
            if attempt < retries:
                time.sleep(10)  # one retry absorbs a transient registry/network failure
    return None, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cmd", required=True)
    ap.add_argument("--md")
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--retries", type=int, default=1)
    a = ap.parse_args()

    snap, err = run(a.cmd, a.timeout, a.retries)
    if snap is None:
        how = "exited before answering `tools/list`" if "server exited" in err else \
              f"did not answer `initialize` + `tools/list` within {a.timeout}s"
        last = next((l.strip() for l in reversed(err.splitlines()) if l.strip()), err)
        md = ["## MCP Preflight: the server did not start", "",
              f"`{a.cmd}` {how} ({a.retries + 1} attempt{'s' if a.retries else ''}).", "",
              "This can be a broken install (for example, a dependency released a breaking change), a missing "
              "environment variable, or a service the server needs at startup. The last output was:", "",
              "```", err[-1500:], "```"]
        print(f"::error title=MCP Preflight: server did not start::{last[:300]}")
        code = 2
    else:
        long_ = [t for t in snap["tools"] if len(t.get("description") or "") > a.max_len]
        empty = [t for t in snap["tools"] if not (t.get("description") or "").strip()]
        info = snap.get("serverInfo", {})
        md = [f"## MCP Preflight: {info.get('name', 'server')} {info.get('version', '')}", "",
              f"Started and listed **{len(snap['tools'])} tool{'' if len(snap['tools']) == 1 else 's'}**.", "",
              "| Check | Result |", "|---|---|",
              f"| Descriptions ≤ {a.max_len:,} chars | {'✅' if not long_ else f'❌ {len(long_)} tool(s)'} |",
              f"| Every tool has a description | {'✅' if not empty else f'❌ {len(empty)} tool(s)'} |",
              "| Starts and answers tools/list | ✅ |"]
        for t in long_:
            n = len(t["description"])
            md.append(f"\n- `{t['name']}`: description is {n:,} characters. OpenAI and Azure OpenAI function calling "
                      f"reject descriptions over {a.max_len:,} characters with HTTP 400, so clients that pass this tool "
                      f"through an OpenAI-compatible adapter fail the whole request.")
            print(f"::error title=MCP Preflight: description too long::{t['name']}: {n} chars (limit {a.max_len})")
        for t in empty:
            md.append(f"\n- `{t['name']}`: no description. Models choose tools by their descriptions.")
            print(f"::error title=MCP Preflight: missing description::{t['name']}")
        code = 1 if long_ or empty else 0
    text = "\n".join(md) + "\n"
    if a.md:
        open(a.md, "w").write(text)
    print(text, file=sys.stderr)
    sys.exit(code)


if __name__ == "__main__":
    main()
