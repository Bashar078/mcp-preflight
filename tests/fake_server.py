"""Minimal stdio MCP server for tests.
argv[1] = JSON list of tools, or a mode: 'ok' (one well-described tool), 'long' (one 1,025-char description),
'crash' (exit at startup with an ImportError), 'hang' (never answer)."""
import json, sys

if sys.argv[1] == "hang":  # like a server waiting on a network call before it answers initialize
    import time
    time.sleep(60)
if sys.argv[1] == "crash":
    sys.stderr.write("Traceback (most recent call last):\nImportError: cannot import name 'x' from 'y'\n")
    sys.exit(1)
PRESETS = {"ok": [{"name": "get_time", "description": "Returns the current time for an IANA timezone name.",
                   "inputSchema": {"type": "object", "properties": {}}}],
           "long": [{"name": "verbose", "description": "x" * 1025, "inputSchema": {"type": "object", "properties": {}}}]}
tools = PRESETS.get(sys.argv[1]) or json.loads(sys.argv[1])
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
              "serverInfo": {"name": "fake", "version": "1.0.0"}} if msg["method"] == "initialize" else {"tools": tools}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
