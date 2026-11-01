#!/usr/bin/env python3
"""MCP Preflight: grade an MCP server's tool surface before you ship or install it.

  snapshot  launch a stdio MCP server, call tools/list, save JSON
  check     grade a snapshot (portability, risky params, token footprint, drift vs baseline)
  run       snapshot + check in one go

Examples:
  python3 preflight.py run --cmd "npx -y @modelcontextprotocol/server-memory" --out snaps/memory.json
  python3 preflight.py check snaps/fs-2.0.json --baseline snaps/fs-0.6.json --md report.md

Stdlib only. Token counts are estimates (chars/4 of the JSON the client sends to the model).
"""
import argparse, json, os, queue, re, shlex, signal, subprocess, sys, threading, time

PROTOCOL = "2025-06-18"
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")  # OpenAI function-name rule, the strictest common limit
# OpenAI/Azure OpenAI reject longer descriptions with HTTP 400 "Expected a string with maximum length 1024"
# (modelcontextprotocol/servers#799, community.openai.com/t/951951).
DESC_MAX = 1024
# Risk-surface inventory (info only): a url param on a browser tool or sql on a DB tool is the tool's purpose,
# and a schema cannot show whether the server validates it. Reported so installers can see the surface.
RISKY_PARAM = re.compile(r"(^|_)(url|uri|endpoint|host|cmd|command|shell|script|code|sql|expr|expression)s?($|_)", re.I)
PATHY_PARAM = re.compile(r"(^|_)(path|paths|file|filename|dir|directory|query|regex|pattern)s?($|_)", re.I)
# Directive patterns from published tool-poisoning examples. Bare mentions of .env/~/.ssh are NOT flagged:
# filesystem/search tools legitimately name them (false positive found in a real search tool, 2026-10-23).
POISON = re.compile(r"ignore (all |any )?(the )?(previous|prior|above) instructions|"
                    r"(do not|don't|never) (tell|inform|mention|reveal|disclose)[^.\n]{0,30}\buser|"
                    r"without (telling|informing|notifying) the user|"
                    r"before using this tool,? (you must )?(read|send|call|pass)|"
                    r"(read|send|include|pass|upload|attach)\b[^.\n]{0,60}(~/\.ssh|id_rsa|\.aws/credentials|mcp\.json)", re.I)
# Param names a model can use correctly without a description.
SELF_EXPLANATORY = {"path", "paths", "url", "uri", "query", "q", "limit", "offset", "page", "page_size", "per_page",
                    "id", "name", "title", "body", "owner", "repo", "branch", "content", "text", "message",
                    "timeout", "cursor", "email", "username", "file_path", "filepath", "repo_path", "directory",
                    "source", "destination", "branch_name"}
# Weight caps. Footprint is a cost to users, not a defect, so size alone cannot fail a server.
CATEGORY_CAP = {"T": 20}
WEAK_KEYWORDS = {"anyOf", "oneOf", "allOf", "$ref", "$defs", "definitions", "patternProperties", "if", "then", "not"}
SEVERITY_COST = {"error": 15, "warn": 5, "info": 0}
RULE_CAP = 30  # one noisy rule can't sink the grade on its own


# ------------------------------------------------------------------ snapshot
def snapshot(cmd, env=None, timeout=180):
    # New session so npx/uvx and the node/python child they spawn can be killed together.
    p = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, bufsize=1, env={**os.environ, **(env or {})}, start_new_session=True)
    stderr_tail, lines = [], queue.Queue()
    err_reader = threading.Thread(target=lambda: [stderr_tail.append(l) for l in p.stderr], daemon=True)
    err_reader.start()
    threading.Thread(target=lambda: ([lines.put(l) for l in p.stdout], lines.put(None)), daemon=True).start()
    deadline = time.time() + timeout

    def send(msg):
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()

    def recv(want_id):
        while time.time() < deadline:
            try:
                line = lines.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if line is None:
                try:  # let the process finish and the reader drain, so the final exception line is included
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                err_reader.join(timeout=2)
                raise RuntimeError("server exited: " + "".join(stderr_tail[-20:]).strip()[-1500:])
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # some servers log to stdout; skip non-JSON lines
            if msg.get("id") == want_id:
                if "error" in msg:
                    raise RuntimeError(f"rpc error: {msg['error']}")
                return msg["result"]
        raise TimeoutError("no response before timeout")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "mcp-preflight", "version": "0.1"}}})
        init = recv(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools, cursor, rid = [], None, 2
        while True:
            send({"jsonrpc": "2.0", "id": rid, "method": "tools/list", "params": {"cursor": cursor} if cursor else {}})
            res = recv(rid)
            tools += res.get("tools", [])
            cursor, rid = res.get("nextCursor"), rid + 1
            if not cursor:
                break
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()  # reap, or every snapshot leaves a zombie
    return {"cmd": cmd, "serverInfo": init.get("serverInfo", {}), "protocolVersion": init.get("protocolVersion"),
            "instructions": init.get("instructions"), "tools": tools,
            "taken_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ------------------------------------------------------------------ rules
def tokens(obj):
    return len(json.dumps(obj, separators=(",", ":"))) // 4


def walk_keywords(schema, found):
    if isinstance(schema, dict):
        found.update(k for k in schema if k in WEAK_KEYWORDS)
        for v in schema.values():
            walk_keywords(v, found)
    elif isinstance(schema, list):
        for v in schema:
            walk_keywords(v, found)
    return found


def has_description(ps):
    if not isinstance(ps, dict):
        return False
    return bool(ps.get("description")) or any(has_description(b) for k in ("anyOf", "oneOf") for b in ps.get(k) or [])


def needs_doc(ps):
    """True when a model has nothing but the name to go on: no description, enum, default, format or pattern,
    and not a boolean/number (whose names are usually enough). Audited on 30 sampled params, 2026-10-23."""
    if not isinstance(ps, dict) or has_description(ps):
        return False
    if any(k in ps for k in ("enum", "const", "default", "format", "pattern", "examples", "$ref", "properties")):
        return False
    branches = [ps] + [b for k in ("anyOf", "oneOf") for b in ps.get(k) or [] if isinstance(b, dict)]
    types = {b.get("type") for b in branches if b.get("type")} - {"null"}
    if types and types <= {"boolean", "number", "integer"}:
        return False
    items = ps.get("items") if isinstance(ps.get("items"), dict) else {}
    return not any(k in items for k in ("enum", "format", "pattern"))


def check_tool(t, F):
    raw_desc = t.get("description") or ""
    name, desc = t.get("name", ""), raw_desc.strip()
    schema = t.get("inputSchema") or {}
    props = schema.get("properties") or {}

    if not NAME_RE.match(name):
        F("error", "P001", name, "tool name not portable (OpenAI function calling requires ^[a-zA-Z0-9_-]{1,64}$)")
    if not desc:
        F("error", "P002", name, "missing description: models cannot choose this tool reliably")
    elif len(desc) < 25:
        F("info", "P002", name, f"description only {len(desc)} chars: fine for obvious verbs, otherwise say when to use it")
    if len(raw_desc) > DESC_MAX:
        F("error", "P003", name, f"description {len(raw_desc)} chars > {DESC_MAX}: rejected with HTTP 400 when passed "
                                 "to OpenAI/Azure OpenAI function calling (e.g. via an agent SDK adapter)")
    if schema.get("type") != "object":
        if set(schema) <= {"$schema"}:
            # Seen on 3 published packages in 2026-10; all were fixed by pinning zod@3.
            F("error", "P005", name, "inputSchema is empty ({$schema} only), so clients see no parameters. Typical cause: "
                                     "zod v4 resolved under zod-to-json-schema v3 / an old MCP TS SDK; pin zod@3 or upgrade the SDK")
        else:
            F("error", "P005", name, "inputSchema.type must be 'object'")
    weak = walk_keywords(schema, set())
    if weak:
        F("info", "P004", name, f"schema uses {sorted(weak)}: flattened or rejected by some function-calling clients")

    ambiguous = [k for k, v in props.items() if k.lower() not in SELF_EXPLANATORY and isinstance(v, dict)
                 and (needs_doc(v) or has_description(v))]
    unclear = [k for k in ambiguous if needs_doc(props[k])]
    if unclear:
        F("info", "S003", name, f"ambiguous params without description, enum, default or format: {unclear[:5]}")
    for pname, ps in props.items():
        if not isinstance(ps, dict):
            continue
        if ps.get("type") not in ("string", None) or any(k in ps for k in ("enum", "pattern", "maxLength", "format", "const")):
            continue
        if RISKY_PARAM.search(pname):
            F("info", "S001", name, f"param '{pname}' accepts a free-form URL/command/code string: confirm the server "
                                    "validates it (private IPs, shell metacharacters, write SQL)")
        elif PATHY_PARAM.search(pname):
            F("info", "S002", name, f"param '{pname}' is an unconstrained path/pattern string: document allowed roots")
    if POISON.search(desc + json.dumps(props)):
        F("error", "S004", name, "description contains instruction-like text (possible tool poisoning)")

    tk = tokens(t)  # measure before annotating
    t["_doc"] = (len(ambiguous) - len(unclear), len(ambiguous))  # (documented, total) ambiguous params
    if tk > 800:
        F("warn", "T002", name, f"~{tk} tokens for one tool definition (sent on every turn)")
    return tk


def type_set(ps):
    """JSON types a param accepts, unioned across anyOf/oneOf branches."""
    if not isinstance(ps, dict):
        return set()
    t = ps.get("type")
    out = {t} if isinstance(t, str) else set(t or [])
    for k in ("anyOf", "oneOf"):
        for b in ps.get(k) or []:
            out |= type_set(b)
    return out


def check_drift(cur, base, F):
    old = {t["name"]: t for t in base["tools"]}
    new = {t["name"]: t for t in cur["tools"]}
    for n in sorted(old.keys() - new.keys()):
        F("error", "D001", n, "tool removed since baseline (breaking for existing prompts/agents)")
    for n in sorted(new.keys() - old.keys()):
        F("info", "D002", n, "tool added since baseline")
    for n in sorted(old.keys() & new.keys()):
        o, c = old[n], new[n]
        if (o.get("description") or "") != (c.get("description") or ""):
            F("warn", "D003", n, "description changed: review the new text (descriptions steer the model, and "
                                 "tool-poisoning changes arrive this way)")
        os_, cs = o.get("inputSchema") or {}, c.get("inputSchema") or {}
        op, cp = os_.get("properties") or {}, cs.get("properties") or {}
        if not op and not os_.get("type"):
            F("info", "D000", n, "baseline schema was empty: param-level drift not comparable")
            continue
        for k in sorted(op.keys() - cp.keys()):
            F("error", "D004", n, f"param '{k}' removed (breaking)")
        for k in sorted(set(cs.get("required") or []) - set(os_.get("required") or [])):
            F("error", "D005", n, f"param '{k}' became required (breaking)")
        for k in sorted(op.keys() & cp.keys()):
            ot, nt = type_set(op[k]), type_set(cp[k])
            if not ot or not nt or ot == nt:
                continue
            change = f"param '{k}' type {sorted(ot)} -> {sorted(nt)}"
            if ot <= nt:  # e.g. string -> string|null: every old call still validates
                F("info", "D006", n, change + " (widened, compatible)")
            elif ot - nt == {"number"} and "integer" in nt:
                F("warn", "D006", n, change + " (narrowed: fractional values now rejected)")
            else:
                F("error", "D006", n, change + " (breaking: previously valid values rejected)")


def check(snap, baseline=None):
    findings = []
    F = lambda sev, code, tool, msg: findings.append({"severity": sev, "code": code, "tool": tool, "msg": msg})
    tools = [dict(t) for t in snap["tools"]]  # check_tool annotates; don't mutate the caller's snapshot
    per_tool = {t.get("name", "?"): check_tool(t, F) for t in tools}
    documented = sum(t["_doc"][0] for t in tools)
    ambiguous = sum(t["_doc"][1] for t in tools)
    if ambiguous >= 5 and documented / ambiguous < 0.5:
        # Info only: two blind audits (30 params each) found 57% then 73% false positives for any name-based
        # judgement of "needs a description" (context makes `sheet`, `old_string`, wrapper objects obvious).
        F("info", "S005", "*", f"{documented}/{ambiguous} params with no enum, default or format have a description; "
                               "add descriptions where the name alone is ambiguous")
    total = sum(per_tool.values())
    n = len(snap["tools"])
    if total > 10000:
        F("error", "T001", "*", f"~{total} tokens of tool schemas loaded every turn")
    elif total > 4000:
        F("warn", "T001", "*", f"~{total} tokens of tool schemas loaded every turn")
    if n > 20:
        F("warn", "T003", "*", f"{n} tools: tool-selection accuracy drops as tool count grows; consider splitting/toolsets")
    if baseline:
        before = len(findings)
        check_drift(snap, baseline, F)
        changed = [f for f in findings[before:] if f["code"] != "D000"]
        v_old, v_new = baseline.get("serverInfo", {}).get("version"), snap.get("serverInfo", {}).get("version")
        if changed and v_old == v_new:
            F("error", "D007", "*", f"{len(changed)} tool-surface changes but serverInfo.version unchanged ({v_new}): "
                                    "clients cannot detect this release from the handshake")

    cost = {}
    for f in findings:
        cost[f["code"]] = min(RULE_CAP, cost.get(f["code"], 0) + SEVERITY_COST[f["severity"]])
    by_cat = {}
    for code, c in cost.items():
        by_cat[code[0]] = by_cat.get(code[0], 0) + c
    score = max(0, 100 - sum(min(c, CATEGORY_CAP.get(cat, 100)) for cat, c in by_cat.items()))
    grade = next(g for g, lo in (("A", 90), ("B", 80), ("C", 70), ("D", 60), ("F", 0)) if score >= lo)
    risk = sorted({f["tool"] for f in findings if f["code"] == "S001"})
    return {"server": snap["serverInfo"], "tools": n, "tokens_total": total, "tokens_per_tool": per_tool,
            "doc_coverage": [documented, ambiguous],
            "risk_surface": risk,
            "score": score, "grade": grade, "findings": findings,
            "counts": {s: sum(f["severity"] == s for f in findings) for s in SEVERITY_COST}}


def to_md(r, title=None):
    s = r["server"]
    out = [f"## {title or s.get('name', '?')} {s.get('version', '')}: grade **{r['grade']}** ({r['score']}/100)", "",
           f"- Tools: {r['tools']} · schema footprint ≈ {r['tokens_total']} tokens/turn",
           f"- Findings: {r['counts']['error']} error · {r['counts']['warn']} warn · {r['counts']['info']} info",
           f"- Risk surface (not graded): {len(r.get('risk_surface', []))} tools take free-form URL/command/code input", ""]
    if r["findings"]:
        out += ["| sev | rule | tool | finding |", "|---|---|---|---|"]
        order = {"error": 0, "warn": 1, "info": 2}
        for f in sorted(r["findings"], key=lambda f: (order[f["severity"]], f["code"], f["tool"])):
            out.append(f"| {f['severity']} | {f['code']} | `{f['tool']}` | {f['msg'].replace('|', '/')} |")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ cli
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd_name", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--cmd", required=True); s.add_argument("--out", required=True)
    s.add_argument("--env", action="append", default=[], help="KEY=VALUE, repeatable (e.g. dummy API keys)")
    c = sub.add_parser("check"); c.add_argument("snap"); c.add_argument("--baseline"); c.add_argument("--md")
    c.add_argument("--json", action="store_true"); c.add_argument("--min-grade", default="F")
    c.add_argument("--fail-on", default="", help="comma-separated rule codes; exit 1 if any fires at error level")
    r = sub.add_parser("run"); r.add_argument("--cmd", required=True); r.add_argument("--out", required=True)
    r.add_argument("--baseline"); r.add_argument("--md"); r.add_argument("--min-grade", default="F")
    r.add_argument("--env", action="append", default=[], help="KEY=VALUE, repeatable")
    r.add_argument("--fail-on", default="", help="comma-separated rule codes; exit 1 if any fires at error level")
    a = ap.parse_args()

    if a.cmd_name in ("snapshot", "run"):
        snap = snapshot(a.cmd, dict(e.split("=", 1) for e in a.env))
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        json.dump(snap, open(a.out, "w"), indent=2)
        print(f"snapshot: {len(snap['tools'])} tools -> {a.out}", file=sys.stderr)
        if a.cmd_name == "snapshot":
            return
        a.snap, a.json = a.out, False
    snap = json.load(open(a.snap))
    res = check(snap, json.load(open(a.baseline)) if a.baseline else None)
    md = to_md(res)
    if a.md:
        open(a.md, "w").write(md)
    print(json.dumps(res, indent=2) if a.json else md)
    # CI gates: grade floor, and/or specific rules that must not fire at error level
    gate = {c.strip() for c in a.fail_on.split(",") if c.strip()}
    hits = [f for f in res["findings"] if f["code"] in gate and f["severity"] == "error"]
    for f in hits:
        print(f"::error title=MCP Preflight {f['code']}::{f['tool']}: {f['msg']}", file=sys.stderr)
    if hits or "ABCDF".index(res["grade"]) > "ABCDF".index(a.min_grade):
        sys.exit(1)


if __name__ == "__main__":
    main()
