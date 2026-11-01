"""Offline tests for minimal/check.py using a fake stdio server. Run: python3 -m unittest discover -s tests"""
import json, os, shlex, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# Works in the prototype layout (minimal/check.py) and the release layout (check.py at the repo root).
CHECK = next(p for p in (os.path.join(ROOT, "minimal", "check.py"), os.path.join(ROOT, "check.py")) if os.path.exists(p))
FAKE = os.path.join(HERE, "fake_server.py")


def run(arg, timeout=20):
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(FAKE)} {shlex.quote(arg)}"
    p = subprocess.run([sys.executable, CHECK, "--cmd", cmd, "--retries", "0", "--timeout", str(timeout), "--md", os.devnull],
                       capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def tool(name, desc):
    return {"name": name, "description": desc, "inputSchema": {"type": "object", "properties": {}}}


class Minimal(unittest.TestCase):
    def test_pass(self):
        code, out, _ = run(json.dumps([tool("ok", "Returns the current time in a given timezone.")]))
        self.assertEqual(code, 0); self.assertNotIn("::error", out)

    def test_long_description(self):
        code, out, _ = run(json.dumps([tool("big", "x" * 1025), tool("ok", "fine")]))
        self.assertEqual(code, 1); self.assertIn("description too long::big: 1025 chars", out)

    def test_exactly_1024_passes(self):
        self.assertEqual(run(json.dumps([tool("edge", "x" * 1024)]))[0], 0)

    def test_missing_and_blank_descriptions(self):
        code, out, _ = run(json.dumps([tool("a", ""), tool("b", "   "), {"name": "c", "inputSchema": {"type": "object"}}]))
        self.assertEqual(code, 1); self.assertEqual(out.count("missing description"), 3)

    def test_crash_reports_last_error_line(self):
        code, out, _ = run("crash")
        self.assertEqual(code, 2)
        self.assertIn("server did not start::ImportError: cannot import name 'x' from 'y'", out)


    def test_hang_reports_timeout_not_crash(self):
        code, out, err = run("hang", timeout=3)
        self.assertEqual(code, 2); self.assertIn("did not answer `initialize` + `tools/list` within 3s", err)


if __name__ == "__main__":
    unittest.main()
