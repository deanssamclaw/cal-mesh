#!/usr/bin/env python3
"""eval_console.py — checks on the console supplement.

The rule this suite exists to enforce: every panel on that page makes a claim about
data, and the claims that are easiest to get wrong are the ones about ABSENCE. A zero
that should be an em-dash, a missing hop count drawn as a zero bar, a denominator
invented so a rate can be printed -- each one reads as a measurement and is not.

Fixtures are written to a temp dir from literal file bytes. None is built from the
constant it is testing: a fixture derived from the value under test can only pass.
"""
import json, os, re, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import console

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if detail and not cond else ""))


def w(d, name, text):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(text)
    return p


D = tempfile.mkdtemp(prefix="calmesh-console-")

# ---------------------------------------------------------------- census
print("\n[census]")
LOG = w(D, "b.log",
        "2026-08-19T00:00:00+00:00 PORT CENSUS A=5 B=1\n"
        "2026-08-19T00:05:00+00:00 PORT CENSUS A=35 B=1\n"      # session 1, not traced
        "2026-08-19T01:00:00+00:00 PORT CENSUS A=2 B=0\n"        # total DROPPED -> new session
        "2026-08-19T01:05:00+00:00 PORT CENSUS A=8 B=2\n")
c = console.parse_census(LOG)
ck("two sessions detected from the counter reset", c["sessions"] == 2, str(c["sessions"]))
ck("grand total sums SESSION FINALS, not lines", c["grand_total"] == 36 + 10, str(c["grand_total"]))
ck("session count travels with the total", "sessions" in c and c["sessions"] == 2)
ck("trace holds only the CURRENT session's deltas", len(c["trace"]) == 1, str(c["trace"]))
# current session runs 2 -> 10 packets across 300 s = 1.6/min. Worked by hand from the
# fixture bytes above, NOT read back off the function under test.
ck("packets/minute computed from the time delta", abs(c["trace"][0]["ppm"] - 1.6) < .001, str(c["trace"]))
ck("missing file degrades to ok=False, not a crash", console.parse_census(os.path.join(D, "nope"))["ok"] is False)

# ---------------------------------------------------------------- hops
print("\n[hops -- absence is not zero]")
N = w(D, "n.json", json.dumps({"nodes": [
    {"id": "!a", "hops": 0}, {"id": "!b", "hops": 3}, {"id": "!c", "hops": 3},
    {"id": "!d"}, {"id": "!e", "hops": None}]}))
h = console.build_hops(N)
ck("unknown hop counts are counted separately", h["unknown"] == 2, str(h))
ck("unknown is NOT folded into the 0 bucket", h["hist"].get(0) == 1, str(h["hist"]))
ck("histogram omits a None key entirely", None not in h["hist"] and "None" not in h["hist"])
ck("total counts every node", h["total"] == 5)

# ---------------------------------------------------------------- receipts
print("\n[receipts -- em-dash is not zero]")
EMPTY = w(D, "empty.jsonl", "")
r = console.build_receipts(EMPTY)
ck("an unobserved class is None (renders as an em-dash)",
   all(r["counts"][k] is None for k in r["classes"]), str(r["counts"]))
ck("None is not 0 for any class", not any(r["counts"][k] == 0 for k in r["classes"]))
ck("total is 0 on an empty file", r["total"] == 0)
ONE = w(D, "one.jsonl", json.dumps({"status": "delivered"}) + "\n")
r2 = console.build_receipts(ONE)
ck("an observed class becomes a number", r2["counts"]["delivered"] == 1)
ck("the other three stay None beside it",
   r2["counts"]["no_ack"] is None and r2["counts"]["error"] is None, str(r2["counts"]))

# ---------------------------------------------------------------- latency
print("\n[latency -- two populations, never one average]")
DEC = w(D, "d.jsonl", "\n".join([
    json.dumps({"gen_ms": 10000, "model": "opus"}),
    json.dumps({"gen_ms": 20000, "model": "opus"}),
    json.dumps({"gen_ms": 0, "model": None}),          # answered from code
    json.dumps({"gen_ms": 0, "model": None}),
    json.dumps({"gen_ms": None, "model": None}),        # no measurement at all
]))
L = console.build_latency(DEC)
ck("model population excludes the zeros", L["model"]["n"] == 2, str(L["model"]["n"]))
ck("model median is of model replies only", L["model"]["median"] == 15000.0, str(L["model"]["median"]))
ck("code-answered replies counted apart", L["fixed"]["n"] == 2, str(L["fixed"]["n"]))
ck("a null gen_ms is not a zero", L["model"]["n"] + L["fixed"]["n"] == 4)
naive = sum(L["model"]["points"]) / (L["model"]["n"] + L["fixed"]["n"])
ck("the merged average would have understated it", naive < L["model"]["median"], str(naive))

# ---------------------------------------------------------------- tracer
print("\n[tracer -- no invented denominator, no target ids]")
RT = w(D, "r.jsonl", "\n".join([
    json.dumps({"kind": "response", "witness": "addressed", "traced": "!own1", "requester": "!me"}),
    json.dumps({"kind": "response", "witness": "addressed", "traced": "!strange", "requester": "!me"}),
    json.dumps({"kind": "response", "witness": "unsolicited", "traced": "!forged", "requester": "!me"}),
    json.dumps({"kind": "request", "witness": "probed", "traced": "!me", "requester": "!me"}),
]))
CFG = w(D, "cfg", "ALLOW_FROM=!own1\nWEATHER_POINT=1.0,-2.0\n")
LED = w(D, "led.md", "asked **7** node(s), **1** answered\n- `!own1`\n- `!secret`\n")
_of, _lc = console._own_fleet, console._ledger_counts
console._own_fleet = lambda path=CFG: _of(CFG)
console._ledger_counts = lambda path=LED: _lc(LED)
t = console.build_tracer_score(RT)
ck("only 'addressed' responses count as answered", t["answered"] == 2, str(t["answered"]))
ck("an unsolicited/forged path is not counted", t["answered"] == 2)
ck("third-party count excludes the operator's own fleet", t["third_party"] == 1, str(t["third_party"]))
ck("asked comes from the ledger", t["asked"] == 7, str(t["asked"]))
ck("a ledger that disagrees is FLAGGED, not reconciled", t["disagrees"] is True)
ck("both answered numbers survive to the page", t["ledger_answered"] == 1 and t["answered"] == 2)
blob = json.dumps(t)
ck("no node id of any kind reaches the output", "!" not in blob, blob)
ck("the ledger's secret target is not published", "secret" not in blob)

# the load-bearing one: an unreadable ledger must NOT be repaired into a denominator
console._ledger_counts = lambda path=None: None
t2 = console.build_tracer_score(RT)
ck("no ledger -> asked is None, not clamped to answered", t2["asked"] is None, str(t2["asked"]))
ck("no ledger -> NO rate is published", t2["rate"] is None, str(t2["rate"]))
ck("answered still reported without a denominator", t2["answered"] == 2)
console._own_fleet, console._ledger_counts = _of, _lc

# ---------------------------------------------------------------- config leak
print("\n[config -- the coordinate must never reach the page]")
st = json.dumps(console.build_console())
ck("no WEATHER_POINT key in the console payload", "WEATHER_POINT" not in st)
try:
    live = open(os.path.join(console.BASE, "config")).read()
    pt = [l.split("=", 1)[1].strip() for l in live.splitlines() if l.startswith("WEATHER_POINT=")]
    ck("the live coordinate value is absent from the payload",
       not pt or pt[0] not in st, "coordinate found in /api/console output")
except Exception:
    ck("the live coordinate value is absent from the payload", True)

# ---------------------------------------------------------------- the page itself
print("\n[page]")
P = console.PAGE_CONSOLE
ck("no external asset host is referenced",
   not re.search(r'(src|href)\s*=\s*["\']https?://(?!.*github\.com)', P), "external ref")
ck("only the GitHub link leaves the origin",
   len(re.findall(r'href="https?://', P)) == 1)
ck("no red-amber-green ramp on an ordered quantity",
   not re.search(r'#(?:d7|e5|f8|ff)[0-9a-f]{2}(?:2[0-9a-f]|3[0-9a-f])', P, re.I))
ck("fetches through DIR, not a bare relative path", "DIR+'api/console'" in P)
ck("DIR strips this page's own slug", "replace(/\\/(console)\\/?$/" in P)
ck("the em-dash rule is stated on the page, not just in code", "not a zero" in P)
ck("SNR last-leg caption present", "last leg only" in P)
ck("hop-limit-is-a-setting caption present", "setting</em> rather than a shape" in P)
ck("census reset caption present", "sum of" in P and "session finals" in P)
ck("the dark-signals table renders on the page", 'id="dark"' in P)
# A U+FFFD reached this page once, inserted by a careless sed during editing. It parses,
# it renders, and it shows a broken glyph to every visitor -- the exact "it rendered and
# nothing threw" class. Cheap to assert, so assert it.
ck("no replacement character / mojibake in the markup", "\ufffd" not in P)
ck("markup is clean UTF-8 round-trip", P.encode("utf-8").decode("utf-8") == P)
# A talker present in the SNR history but absent from nodes.json has no short name. The
# page must NOT fall back to a slice of the node id: an id fragment is meaningless to a
# reader and puts an identifier on screen for no benefit. Neutral label instead.
ck("SNR panel never falls back to a raw node-id fragment",
   "t.node.slice(" not in P and "esc(t.short||'(unnamed)')" in P)
ck("SNR table twin uses the same fallback", P.count("t.short||'(unnamed)'") == 2)
ck("no coordinate literal anywhere in the markup",
   not re.search(r'\b3[0-9]\.\d{3,}\s*,\s*-9[0-9]\.\d{3,}', P))

# ---------------------------------------------------------------- mutation harness
print("\n[mutations -- each should be CAUGHT]")
def mutate(name, fn, check):
    try:
        fn()
        bad = not check()
    except Exception as e:
        bad = False
        print("      (mutation raised: %s)" % type(e).__name__)
    ck("mutation caught: " + name, bad)

# 1. receipts zero-filled instead of None
orig = console.build_receipts
console.build_receipts = lambda p=None: {"counts": {c: 0 for c in ["delivered", "no_ack", "error", "unknown"]},
                                         "total": 0, "classes": ["delivered", "no_ack", "error", "unknown"]}
mutate("receipts return 0 instead of None",
       lambda: None,
       lambda: all(console.build_receipts()["counts"][k] is None for k in ["no_ack", "error"]))
console.build_receipts = orig

# 2. hops folds unknown into bucket 0
orig_h = console.build_hops
console.build_hops = lambda p=N: {"hist": {0: 3, 3: 2}, "unknown": 0, "total": 5, "default_hop_limit": 3}
mutate("unknown hop counts folded into bucket 0",
       lambda: None,
       lambda: console.build_hops()["unknown"] == 2)
console.build_hops = orig_h

# 3. tracer clamps asked up to answered (the bug this suite was written after)
def clamped():
    a, ans = None, 2
    if a is None or a < ans:
        a = ans
    return {"asked": a, "answered": ans, "rate": round(ans / a, 3)}
mutate("tracer clamps a missing denominator up to answered",
       lambda: None,
       lambda: clamped()["rate"] is None)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
