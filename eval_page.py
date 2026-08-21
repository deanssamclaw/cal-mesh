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

# --- selectors must resolve against the markup they were written for ------------------------
# The v5 build queue shipped with 15 stylesheet rules anchored to `#learning` — the id of the
# card the content had just been MOVED OUT OF. Every rule matched nothing and the tab rendered
# completely unstyled, and not one eval here noticed: they execute the render functions, check
# the JSON, and audit colours over markup they build themselves. Nothing resolved an id in the
# stylesheet against the page.
#
# Two directions, because both fail silently:
#   CSS -> markup   a rule anchored to a missing id styles nothing.
#   script -> markup  $('#x') on a missing id returns null; the write vanishes, or throws and
#                     takes the rest of the paint with it.
#
# Current page only. A retired page is frozen, and holding it to a rule written later would
# force an edit to a record of what the page WAS.
CUR_NAME = None
_m = re.search(r"^CURRENT_PAGE = (PAGE_V\d+)", open(os.path.join(HERE, "dashboard.py")).read(), re.M)
if _m:
    CUR_NAME = _m.group(1)
    cur = dict(PAGES).get(CUR_NAME, "")
    ids = set(re.findall(r'id="([\w-]+)"', cur))
    style = "\n".join(re.findall(r"<style>(.*?)</style>", cur, re.S))
    checked += 1
    # A hex colour is not a selector. `#fff8c5` and `#ffffff` are values, and reading them as
    # ids is how this check first reported 100+ imaginary failures.
    HEX = re.compile(r"^[0-9a-fA-F]{3,8}$")
    css_ids = {sel for sel in re.findall(r"#([\w-]+)", style) if not HEX.match(sel)}
    for sel in sorted(css_ids - ids):
        failures.append(f"{CUR_NAME} stylesheet targets #{sel}, which is not in the markup — "
                        f"the rule matches nothing and whatever it was meant to style is bare")
    script = "\n".join(re.findall(r"<script>(.*?)</script>", cur, re.S))
    # Only ids the page addresses directly. Anything built into a template string is created at
    # render time and cannot be resolved here.
    js_ids = set(re.findall(r"""\$\(['"]#([\w-]+)['"]\)""", script))
    for sel in sorted(js_ids - ids):
        failures.append(f"{CUR_NAME} script writes to #{sel}, which is not in the markup — "
                        f"the write goes nowhere")

# --- version promotion: a published /old-N link must always mean the same page --------------
# Promotion is four edits in three places (a new PAGE_Vn, CURRENT_PAGE, RETIRED_PAGES, the
# footer) and nothing enforced any of them. Getting it half-right serves the old page from "/"
# or renumbers a slot someone has already linked to, and both look fine from the machine that
# made the change.
SRC = open(os.path.join(HERE, "dashboard.py")).read()
m = re.search(r"^CURRENT_PAGE = (PAGE_V(\d+))", SRC, re.M)
if not m:
    failures.append("CURRENT_PAGE is not a plain `PAGE_Vn` assignment — the evals resolve it "
                    "from source and cannot follow anything cleverer")
else:
    cur_name, cur_n = m.group(1), int(m.group(2))
    retired = dict(re.findall(r'"(old-\d+)": (PAGE_V\d+)', SRC))
    checked += 1
    if cur_name in retired.values():
        failures.append(f"{cur_name} is served at / AND retired at an old-N slot")
    # Every version below the current one must be retired, at the slot matching its number.
    for n in range(1, cur_n):
        want, got = f"PAGE_V{n}", retired.get(f"old-{n}")
        if got != want:
            failures.append(f"old-{n} should be {want}, found {got or 'nothing'} — an old-N slot "
                            f"is permanent and must never be renumbered")
    if len(retired) != cur_n - 1:
        failures.append(f"{len(retired)} retired page(s) for a current v{cur_n}; "
                        f"expected {cur_n - 1}")
    # The footer is the only version label a reader ever sees. A promoted page still wearing the
    # old number is the single most likely thing to be missed, because everything else works.
    cur_html = dict(PAGES).get(cur_name, "")
    if f"cal-mesh dashboard v{cur_n}" not in cur_html:
        failures.append(f"{cur_name} footer does not say 'cal-mesh dashboard v{cur_n}'")
    if cur_n > 1 and f'href="old-{cur_n - 1}"' not in cur_html:
        failures.append(f"{cur_name} footer does not link back to old-{cur_n - 1}")

for f in failures:
    print("FAIL " + f)
print(f"\n{checked} script block(s) checked across {len(PAGES)} page template(s); "
      f"{len(failures)} problem(s)")
sys.exit(1 if failures else 0)
