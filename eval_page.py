#!/usr/bin/env python3
"""Syntax check for the JavaScript embedded in the dashboard's page templates.

WHY THIS FILE EXISTS
--------------------
The pages are Python RAW triple-quoted strings (PAGE_V1, PAGE_V2), so a backslash written in the
source reaches the browser verbatim. On 2026-08-11 a backslash-escaped apostrophe was emitted
literally, which closed the JavaScript string early and made the whole page script a syntax error.

The failure is silent and total, and it defeats every cheap check:

  * the server still returns **HTTP 200** — the template is just text to Python
  * the HTML still **contains** every string you would grep for
  * nothing is logged, because nothing failed on the server side
  * only the *browser* refuses the script, and then NOTHING on the page renders

So "200 and the string is present" is not evidence the page works. This parses it.

Run:  python3 eval_page.py        (exit 0 = pass; skips cleanly if node is unavailable)
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dash_mod", os.path.join(HERE, "dashboard.py"))
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)          # safe: the server only starts under __main__

PAGES = [(n, getattr(dash, n)) for n in dir(dash) if re.fullmatch(r"PAGE_V\d+", n)]
SCRIPT_RE = re.compile(r"<script>(.*?)</script>", re.S)

failures = []
checked = 0

node = shutil.which("node")
if not node:
    print("SKIP: node not found — cannot parse the embedded JavaScript")
    sys.exit(0)

if not PAGES:
    print("FAIL: no PAGE_V* templates found in dashboard.py")
    sys.exit(1)

for name, html in sorted(PAGES):
    blocks = SCRIPT_RE.findall(html)
    if not blocks:
        failures.append(f"{name}: no <script> block found — did the template change shape?")
        continue
    for i, js in enumerate(blocks):
        checked += 1
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            # `node --check` parses without executing, so no browser API stubs are needed.
            f.write(js)
            path = f.name
        try:
            r = subprocess.run([node, "--check", path], capture_output=True, text=True)
            if r.returncode != 0:
                first = (r.stderr.strip().splitlines() or ["(no detail)"])
                detail = " / ".join(l.strip() for l in first[:4])
                failures.append(f"{name} script[{i}]: {detail}")
        finally:
            os.unlink(path)

# The specific hazard that caused this, called out by name so a failure explains itself rather
# than leaving the next person to rediscover why a raw string ate their escape.
for name, html in sorted(PAGES):
    for m in re.finditer(r"\\\\'", html):
        line = html[:m.start()].count("\n") + 1
        failures.append(
            f"{name} line ~{line}: literal \\\\' in a RAW template — this reaches the browser as a "
            f"backslash plus a quote that ENDS the string. Rewrite the sentence without the "
            f"apostrophe rather than trying to escape it.")

for f in failures:
    print("FAIL " + f)
print(f"\n{checked} script block(s) checked across {len(PAGES)} page template(s); "
      f"{len(failures)} problem(s)")
sys.exit(1 if failures else 0)
