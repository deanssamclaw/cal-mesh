#!/usr/bin/env python3
"""eval_policy.py -- the policy grader is a public claim about this node, so it gets tested.

Every rule is driven by a fixture that forces it, both ways (kept and broken). Mutations prove
the checks can fail. DM and Cal's-channel text must never appear in what the page publishes.

Run:  python3 evals/eval_policy.py               (exit 0 = pass)
      python3 evals/eval_policy.py --self-test   also proves the checks can FAIL
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(HERE) == "evals":
    HERE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import learn    # noqa: E402
import policy   # noqa: E402

FAILS = []


def ck(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}  {detail}")


NOW = datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)
OURS = "!cccccccc"
CAL_PKT = 900001                       # a packet Cal sent


def iso(m):
    return (NOW - timedelta(minutes=m)).isoformat()


def case(i, text, *, m=None, to="^all", ch=0, reply_to=None, reaction=None, reply=None,
         cap=None, gen=None):
    """One inbound message: its decision record and its inbox packet."""
    ts = iso(m if m is not None else 600 - i)
    d = {"ts": ts, "id": i, "from": "!aaaaaaaa", "to": to, "channel": ch, "text": text,
         "reply": reply, "capability": cap, "gen_status": gen}
    p = {"ts": ts, "id": i, "from": "!aaaaaaaa", "to": to, "channel": ch, "text": text,
         "reply_to": reply_to, "reaction": reaction}
    return d, p


SIG = dict(reply="I hear you on 2 hops", cap="sigreport", gen="fixed_sigreport")
GREET = dict(reply="Morning to you", cap="greeting", gen="fixed_greeting_ack")
WORDS = dict(reply="Sounds like a nice day out there", gen="ok")
WAVE = dict(reply="\U0001F44B", cap="greeting", gen="fixed_greeting_ack")

CASES = [
    # (label, case, rule, conforms?)
    ("tapback, silent", case(1, "\U0001F44D", reaction=True, reply_to=555), "P1", True),
    ("tapback answered", case(2, "\U0001F44D", reaction=True, reply_to=555, **SIG), "P1", False),
    ("wave on Cal's own message, mirrored", case(3, "\U0001F44B", reaction=True, reply_to=CAL_PKT, **WAVE), "P1", True),
    ("wave on Cal's own message, silent", case(4, "\U0001F44B", reaction=True, reply_to=CAL_PKT), "P1", True),
    ("wave on someone else's, mirrored", case(5, "\U0001F44B", reaction=True, reply_to=555, **WAVE), "P1", False),
    ("check-in answered", case(6, "Test from Raytown", **SIG), "P2", True),
    ("check-in ignored", case(7, "Test from Raytown"), "P2", False),
    ("keycap check-in ignored", case(8, "2️⃣"), "P2", False),
    ("hop report naming a station ignored", case(9, "87b3 1 hop"), "P2", False),
    ("short question check-in answered", case(10, "Radio check?", **SIG), "P2", True),
    ("report in another's test answered", case(11, "Received in OP", reply_to=555, **SIG), "P3", True),
    ("report in another's test ignored", case(12, "Received in OP", reply_to=555), "P3", False),
    ("chatter reply, silent", case(13, "ha yes the Chiefs looked great last night", reply_to=555), "P4", True),
    ("chatter reply, model words", case(14, "ha yes the Chiefs looked great last night", reply_to=555, **WORDS), "P4", False),
    ("open chatter, model words", case(15, "Beautiful sunrise over the lake this morning", **WORDS), "P4", False),
    ("open chatter, greeting doer", case(16, "Good morning mesh", **GREET), "P4", True),
    ("long question is not a check-in", case(17, "Does the channel order change how meshtastic behaves at all?"), "P4", True),
]
UNGRADED = [
    ("named: the ladder decides", case(30, "Cal what's 5 mi in km", **WORDS)),
    ("DM: the ladder decides", case(31, "private hello there", to=OURS, **WORDS)),
    ("Cal's channel: the ladder decides", case(32, "keyed channel words", ch=1, **WORDS)),
]


class Fixture:
    def __init__(self, cases):
        self.d = tempfile.mkdtemp(prefix="evalpolicy-")
        self.dec = os.path.join(self.d, "decisions.jsonl")
        self.inb = os.path.join(self.d, "inbox.jsonl")
        self.sent = os.path.join(self.d, "sent.jsonl")
        with open(self.dec, "w") as f, open(self.inb, "w") as g:
            for d, p in cases:
                f.write(json.dumps(d) + "\n")
                g.write(json.dumps(p) + "\n")
        with open(self.sent, "w") as f:
            f.write(json.dumps({"packet_id": CAL_PKT, "text": "Radio check here"}) + "\n")

    def __enter__(self):
        self.saved = (learn.DECISIONS, learn.INBOX, policy.SENT, learn.our_id, learn.cal_channel)
        learn.DECISIONS, learn.INBOX, policy.SENT = self.dec, self.inb, self.sent
        learn.our_id = lambda: OURS
        learn.cal_channel = lambda: 1
        return self

    def __exit__(self, *a):
        (learn.DECISIONS, learn.INBOX, policy.SENT, learn.our_id, learn.cal_channel) = self.saved
        shutil.rmtree(self.d, ignore_errors=True)


def run():
    del FAILS[:]
    for label, c, rule, good in CASES:
        with Fixture([c]):
            g = policy.grade(now=NOW, days=14)
        ck(f"[{label}] graded once", g["n"] == 1, g)
        ck(f"[{label}] under {rule}", g["by_rule"][rule]["n"] == 1, g["by_rule"])
        ck(f"[{label}] {'kept' if good else 'BROKEN'}", g["ok"] == (1 if good else 0), g)
    for label, c in UNGRADED:
        with Fixture([c]):
            g = policy.grade(now=NOW, days=14)
        ck(f"[{label}] counted, never broken", g["n"] == 1 and g["ok"] == 1, g)
        ck(f"[{label}] under no rule", all(v["n"] == 0 for v in g["by_rule"].values()), g["by_rule"])

    allc = [c for _, c, _, _ in CASES] + [c for _, c in UNGRADED]
    allc += [case(40, "Test from Gardner", m=60 * 24 * 20)]          # outside the window
    allc += [case(41, "secret dm test", to=OURS)]                     # DM text must never publish
    allc += [case(42, "keyed channel test 3 hops", ch=1)]
    with Fixture(allc):
        g = policy.grade(now=NOW, days=14)
    ck("window: an old message is not graded", g["n"] == len(allc) - 1, g["n"])
    blob = json.dumps(g)
    ck("DM text never published", "secret dm" not in blob and "private hello" not in blob)
    ck("Cal's-channel text never published", "keyed channel" not in blob)
    expected_bad = sum(1 for _, _, _, good in CASES if not good)
    ck("totals add up", g["bad"] == expected_bad and g["ok"] + g["bad"] == g["n"], g)
    ck("every rule is described", set(g["rules"]) == set(g["by_rule"]) == {"P1", "P2", "P3", "P4"})

    # malformed input never raises
    bad = [case(50, "Test 1")]
    with Fixture(bad) as fx:
        with open(fx.dec, "a") as f:
            f.write("[]\n7\n" + json.dumps({"ts": 5, "id": [1], "text": None}) + "\n")
        with open(fx.inb, "a") as f:
            f.write("[]\n" + json.dumps({"id": {"x": 1}, "ts": None}) + "\n")
        try:
            g = policy.grade(now=NOW, days=14)
            ok = True
        except Exception as e:
            g, ok = repr(e), False
    ck("malformed rows never raise", ok, g)

    # the page renders it: execute drawPolicy in node against real payload shapes
    node = shutil.which("node") or "/usr/local/opt/node@22/bin/node"
    if not os.path.exists(node):
        print("  SKIP no node on PATH -- the render execution check did not run")
    else:
        import importlib.util
        spec = importlib.util.spec_from_file_location("dash", os.path.join(HERE, "dashboard.py"))
        dash = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dash)
        cur = re.search(r"^CURRENT_PAGE = (PAGE_V\d+)", open(os.path.join(HERE, "dashboard.py")).read(),
                        re.M).group(1)
        page = getattr(dash, cur)
        ck("the live page has the policy section", 'id="lrn-policy"' in page)
        i = page.index("function drawPolicy")
        fn = page[i:page.index("\n}\n", i) + 3]
        for name, payload in (("graded", g), ("unreadable", None),
                              ("hostile text", {"n": 1, "ok": 0, "bad": 1, "days": 14, "ruled": "x",
                                                "rules": {"P2": "r"}, "by_rule": {"P2": {"n": 1, "bad": 1}},
                                                "examples": {"P2": [{"text": "<img src=x onerror=alert(1)>",
                                                                     "expected": "sigreport", "did": "silent"}]}})):
            d = tempfile.mkdtemp(prefix="evalpolicyjs-")
            js = os.path.join(d, "t.js")
            open(os.path.join(d, "p.json"), "w").write(json.dumps(payload))
            open(js, "w").write(
                "const P=require(" + json.dumps(os.path.join(d, "p.json")) + ");\n"
                "const N={};const $=s=>N[s]||(N[s]={innerHTML:''});\n"
                "const esc=s=>String(s).replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));\n"
                + fn + "\ndrawPolicy(P);console.log(JSON.stringify(N['#lrn-policy'].innerHTML));\n")
            r = subprocess.run([node, js], capture_output=True, text=True)
            html = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else ""
            ck(f"[render {name}] executes", r.returncode == 0, r.stderr[:200])
            ck(f"[render {name}] says something", len(html) > 0)
            ck(f"[render {name}] no NaN/undefined", not re.search(r"NaN|undefined", html), html[:200])
            ck(f"[render {name}] text is escaped", "<img" not in html, html[:200])
            shutil.rmtree(d, ignore_errors=True)
    return FAILS[:]


MUTATIONS = {
    "tapbacks graded as check-ins": lambda: setattr(policy, "expected",
        (lambda f: lambda d, p, o, s, c: f(d, dict(p, reaction=None), o, s, c))(policy.expected)),
    "every message conforms": lambda: setattr(policy, "conforms", lambda e, a, r="": True),
    "words never break P4": lambda: setattr(policy, "conforms",
        (lambda f: lambda e, a, r="": True if a == "words" else f(e, a, r))(policy.conforms)),
    "check-ins never recognised": lambda: setattr(policy, "checkin", lambda t: False),
    "wave exception removed": lambda: setattr(policy, "_WAVES", ()),
    "P1 silence loosened": lambda: setattr(policy, "conforms",
        (lambda f: lambda e, a, r="": True if (r == "P1" and a != "words") else f(e, a, r))(policy.conforms)),
    "DMs graded as public": lambda: setattr(learn, "cal_channel", lambda: -1) or setattr(
        policy, "expected", (lambda f: lambda d, p, o, s, c: f(dict(d, to="^all"), p, o, s, c))(policy.expected)),
}

if __name__ == "__main__":
    fails = run()
    for f in fails:
        print("  FAIL " + f)
    if "--self-test" in sys.argv:
        print("self-test -- each mutation must be CAUGHT:")
        keep = {n: getattr(policy, n) for n in ("expected", "conforms", "checkin", "_WAVES")}
        keep_cc = learn.cal_channel
        for name, mutate in MUTATIONS.items():
            mutate()
            caught = bool(run())
            print(f"  [{'CAUGHT' if caught else 'SURVIVED'}] {name}")
            if not caught:
                fails.append("self-test: " + name)
            for n, v in keep.items():
                setattr(policy, n, v)
            learn.cal_channel = keep_cc
    print(f"eval_policy: {len(fails)} failed")
    sys.exit(1 if fails else 0)
