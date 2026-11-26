#!/usr/bin/env python3
"""eval_health.py — the health strip is a public claim about this node, so it gets tested.

`learn.audit()` and `learn.health()` exist because a loop that runs on a schedule and reports
its own numbers can be wrong in three different ways that all look identical from outside, and
one of them shipped: three answered range tests sat in the published build queue as unanswered
gaps for six days while every figure on the page was internally consistent.

So the checks here are mostly NEGATIVE. It is easy to write a health function that returns
FRESH; the only thing worth testing is whether it can return anything else, and whether it says
the right thing when it does. Every state is driven by a fixture that forces it, and the drift
check is exercised by putting the classifier back into the exact shape that caused the original
defect and demanding the audit notices.

Run:  python3 evals/eval_health.py                (exit 0 = pass)
      python3 evals/eval_health.py --self-test    also proves the checks can FAIL
"""
import os, sys, re, json, tempfile, importlib.util
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(HERE) == "evals":   # the repo root; a copy run from a temp dir is its own
    HERE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import learn

_spec = importlib.util.spec_from_file_location("dash", os.path.join(HERE, "dashboard.py"))
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)

FAILS = []
def ck(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILS.append(name)

NOW = datetime(2026, 8, 31, 18, 0, 0, tzinfo=timezone.utc)
def iso(dt): return dt.isoformat()

# --------------------------------------------------------------------------------------
# Fixtures. Every path learn.py reads is a module global, so a temp directory can stand in
# for the live one without the live one being touched -- these checks must never depend on,
# or disturb, the deployed ledger.
# --------------------------------------------------------------------------------------
class Fixture:
    """A throwaway decisions log + bank + run history, wired into learn's globals."""
    def __init__(self, records, runs, banked=None, watermark=None, drafts=None, inbox=None,
                 grades=None):
        self.d = tempfile.mkdtemp(prefix="evalhealth-")
        self.dec = os.path.join(self.d, "decisions.jsonl")
        # drafts / inbox / grades are OPTIONAL and absent by default -- the live drafts file on
        # rflab holds a month of failed model calls, and a fixture that read it would report
        # FAILING for every case below.
        self.drafts = os.path.join(self.d, "drafts.jsonl")
        self.inbox = os.path.join(self.d, "inbox.jsonl")
        self.grades = os.path.join(self.d, "draft-grades.json")
        for path, rows in ((self.drafts, drafts), (self.inbox, inbox)):
            if rows is not None:
                with open(path, "w") as f:
                    for r in rows:
                        f.write(json.dumps(r) + "\n")
        if grades is not None:
            json.dump(grades, open(self.grades, "w"))
        self.hist = os.path.join(self.d, "learn-history.jsonl")
        self.led = os.path.join(self.d, "gap-ledger.json")
        self.st = os.path.join(self.d, "learn-state.json")
        with open(self.dec, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        with open(self.hist, "w") as f:
            for t in runs:
                f.write(json.dumps({"ts": t, "processed": 1, "untriaged": 3, "armed": 0}) + "\n")
        wm = watermark if watermark is not None else (records[-1]["ts"] if records else "")
        json.dump({"last_ts": wm, "runs": len(runs)}, open(self.st, "w"))
        if banked is None:
            banked = {}
            our = learn.our_id()
            own = learn.cal_channel()
            for r in records:
                if r["ts"] > wm:
                    continue
                b = learn.classify(r, our)
                banked[b] = banked.get(b, 0) + 1
                k = b + "_" + learn.stream(r, our, own)
                banked[k] = banked.get(k, 0) + 1
        json.dump({"totals": banked, "clusters": {}, "schema": learn.SCHEMA}, open(self.led, "w"))

    def __enter__(self):
        self.saved = (learn.DECISIONS, learn.HISTORY, learn.LEDGER_JSON, learn.STATE,
                      learn.DRAFTS, learn.INBOX, learn.GRADES)
        learn.DECISIONS, learn.HISTORY = self.dec, self.hist
        learn.LEDGER_JSON, learn.STATE = self.led, self.st
        learn.DRAFTS, learn.INBOX, learn.GRADES = self.drafts, self.inbox, self.grades
        return self

    def __exit__(self, *a):
        (learn.DECISIONS, learn.HISTORY, learn.LEDGER_JSON, learn.STATE,
         learn.DRAFTS, learn.INBOX, learn.GRADES) = self.saved


def rec(ts, text="hello there", gen="ok", cap=None, to=None, ch=None):
    # No `from` field. Nothing on the audit or health path reads the sender, and the staged-set
    # scrub rightly blocks any node-id shape that has not been published before -- a fixture is
    # not a reason to put one in a public repo.
    r = {"ts": iso(ts), "text": text, "reply": "some reply", "gen_status": gen,
         "matched": True, "replied": True}
    if cap: r["capability"] = cap
    if to: r["to"] = to
    if ch is not None: r["channel"] = ch
    return r

FRESH_RECS = [rec(NOW - timedelta(hours=3)), rec(NOW - timedelta(hours=2), text="another ask")]
FRESH_RUNS = [iso(NOW - timedelta(hours=24)), iso(NOW - timedelta(hours=1))]

print("_fmt_age")
ck("sub-hour reads in minutes", learn._fmt_age(0.5) == "30 min", learn._fmt_age(0.5))
ck("hours to one decimal", learn._fmt_age(2.0) == "2.0 h", learn._fmt_age(2.0))
ck("past two days reads in days", learn._fmt_age(60) == "2.5 d", learn._fmt_age(60))
ck("None is not zero", learn._fmt_age(None) == "?", learn._fmt_age(None))

print("\naudit — the drift check")
with Fixture(FRESH_RECS, FRESH_RUNS):
    a = learn.audit()
    ck("a bank built by the current code is clean", a["stale"] == 0 and a["ok"], a)
    ck("audited counts the folded records", a["audited"] == 2, a["audited"])

# THE ONE THAT MATTERS. Put the classifier back to its pre-fix shape over real live records and
# demand the audit sees it. A drift check that cannot reproduce the defect it was written for
# is decoration.
sig = [rec(NOW - timedelta(hours=5), text="Test 14", gen="fixed_sigreport", cap="sigreport"),
       rec(NOW - timedelta(hours=4), text="Test 15", gen="fixed_sigreport", cap="sigreport"),
       rec(NOW - timedelta(hours=3), text="Test 16", gen="fixed_sigreport", cap="sigreport")]
with Fixture(sig, FRESH_RUNS):
    clean = learn.audit()
    ck("sigreport readbacks bank as answered", clean["stale"] == 0, clean)
    saved = learn.DOER_CAPS
    try:
        learn.DOER_CAPS = saved - {"sigreport"}          # the pre-session-136 classifier
        drift = learn.audit()
    finally:
        learn.DOER_CAPS = saved
    ck("reverting the fix is DETECTED", drift["stale"] == 3, drift)
    ck("drift is not silently 'ok'", drift["ok"] is False, drift["ok"])
    ck("the delta names the buckets that moved", "HIT" in drift["delta"], drift["delta"])

# Records newer than the watermark are legitimately unbanked. Counting them would fire every
# day between the responder writing and the job running -- a false alarm on a schedule.
with Fixture(FRESH_RECS + [rec(NOW - timedelta(minutes=5), text="arrived after the run")],
             FRESH_RUNS, watermark=iso(NOW - timedelta(hours=2))) as fx:
    a = learn.audit()
    ck("unfolded records do not read as drift", a["stale"] == 0, a)
    ck("and they are not audited", a["audited"] == 2, a["audited"])

with Fixture([], FRESH_RUNS, banked={}, watermark=""):
    a = learn.audit()
    ck("no bank yields None, never 0", a["stale"] is None, a)

print("\nhealth — the state machine")
def state_for(recs, runs, mutate=False, **kw):
    with Fixture(recs, runs, **kw):
        saved = learn.DOER_CAPS
        try:
            if mutate:
                learn.DOER_CAPS = saved - {"sigreport"}
            return learn.health(now=NOW)
        finally:
            learn.DOER_CAPS = saved

h = state_for(FRESH_RECS, FRESH_RUNS)
ck("FRESH when the loop ran and input flows", h["state"] == "FRESH", h["state"])
ck("FRESH carries no flags", h["flags"] == [], h["flags"])
ck("run age is reported", 0.9 < h["run_age_h"] < 1.1, h["run_age_h"])
ck("next run is projected 24 h on", h["next_expected"] is not None)

h = state_for(FRESH_RECS, [iso(NOW - timedelta(hours=40))])
ck("LATE when no run inside the threshold", h["state"] == "LATE", h["state"])
ck("LATE names the run, not the input", "run" in h["reason"], h["reason"])

h = state_for([rec(NOW - timedelta(hours=60))], [iso(NOW - timedelta(hours=1))])
ck("STALLED when input dries up", h["state"] == "STALLED", h["state"])
ck("STALLED says the loop is still running", "running" in h["reason"], h["reason"])

# FAILING (2026-09-21): the loop is healthy but the responder's model replies are failing.
fails = FRESH_RECS + [rec(NOW - timedelta(minutes=30 - i), text=f"ask {i}", gen="gen_rc1:") for i in range(3)]
h = state_for(fails, FRESH_RUNS)
ck("FAILING when the last 3 model replies failed", h["state"] == "FAILING", h["state"])
ck("FAILING names the replies, not the loop", "model replies" in h["reason"] and "gen_rc1" in h["reason"], h["reason"])
ck("FAILING reports the trailing count", h["gen"]["recent_failed"] == 3, h["gen"])
leak = [rec(NOW - timedelta(minutes=10 - i), text=f"x{i}", gen="gen_rc1:Usage limit reached for acct 12345") for i in range(3)]
h = state_for(FRESH_RECS + leak, FRESH_RUNS)
ck("FAILING publishes the code, never the stderr tail",
   "gen_rc1" in h["reason"] and "acct" not in h["reason"] and "Usage" not in json.dumps(h["gen"]), h["reason"])
h = state_for(fails[:-1], FRESH_RUNS)
ck("two failures are not FAILING", h["state"] == "FRESH", h["state"])
h = state_for(fails + [rec(NOW - timedelta(minutes=1), text="later", gen="ok")], FRESH_RUNS)
ck("one success clears it", h["state"] == "FRESH" and h["gen"]["recent_failed"] == 0, h["gen"])
h = state_for(fails + [rec(NOW - timedelta(minutes=1), text="fixed", gen="fixed_calc", cap="calc")], FRESH_RUNS)
ck("a fixed reply is not a model attempt and does not clear it", h["state"] == "FAILING", h["state"])
h = state_for([r for r in fails if r["gen_status"] != "ok"] and
              [rec(NOW - timedelta(hours=60))] + [rec(NOW - timedelta(hours=59, minutes=i), gen="gen_rc1:") for i in range(3)],
              FRESH_RUNS)
ck("STALLED outranks FAILING, both flagged", h["state"] == "STALLED" and "FAILING" in h["flags"], h)
saved_run = learn.GEN_FAIL_RUN
try:
    learn.GEN_FAIL_RUN = 10**9                     # mutation: the check can never fire
    h = state_for(fails, FRESH_RUNS)
    ck("mutation: a disabled FAILING check is visible as FRESH here", h["state"] == "FRESH", h["state"])
finally:
    learn.GEN_FAIL_RUN = saved_run
ck("FAILING is in the closed set", "FAILING" in learn.STATES)

# FAILING from the DRAFTS (2026-11-25): the CLI login on rflab expired 2026-10-24 and every
# model draft failed for a month, while the responder had not tried a model reply since 09-23.
def drow(i, gen="ok", via="model", ts=None, **kw):
    t = ts or (NOW - timedelta(hours=6, minutes=-i))
    r = {"ts": iso(t), "draft_id": f"{iso(t)}:{1000 + i}", "text": f"msg {i}", "via": via,
         "gen_status": gen, "draft": None if gen != "ok" else "a draft"}
    r.update(kw)
    return r
dfail = [drow(0)] + [drow(i, gen="gen_rc1:") for i in range(1, 4)]
h = state_for(FRESH_RECS, FRESH_RUNS, drafts=dfail)
ck("FAILING when the last 3 model DRAFTS failed", h["state"] == "FAILING", h["state"])
ck("drafts FAILING names the drafts", "drafted replies" in h["reason"], h["reason"])
ck("drafts FAILING leaves the responder count alone", h["gen"]["recent_failed"] == 0, h["gen"])
h = state_for(FRESH_RECS, FRESH_RUNS, drafts=dfail[:-1])
ck("two failed drafts are not FAILING", h["state"] == "FRESH", h["state"])
h = state_for(FRESH_RECS, FRESH_RUNS, drafts=dfail + [drow(9, via="greeting")])
ck("a doer draft does not clear a failing model", h["state"] == "FAILING", h["state"])
h = state_for(FRESH_RECS, FRESH_RUNS, drafts=dfail + [drow(9)])
ck("one good model draft clears it", h["state"] == "FRESH", h["state"])
h = state_for(FRESH_RECS, FRESH_RUNS)
ck("no drafts file: not checked, not FAILING", h["drafts_gen"] is None and h["state"] == "FRESH", h)
leakd = [drow(i, gen="gen_rc1:Failed to authenticate: OAuth session acct-SECRET") for i in range(3)]
h = state_for(FRESH_RECS, FRESH_RUNS, drafts=leakd)
ck("drafts FAILING publishes the code, never the CLI text",
   "gen_rc1" in h["reason"] and "OAuth" not in json.dumps(h) and "SECRET" not in json.dumps(h), h["reason"])

# STARVED (2026-11-25): input flowed for 56 days -- greetings and radio checks -- and nobody asked
# Cal anything. Every fixture keeps input RECENT so STALLED cannot be what fires.
greet = [rec(NOW - timedelta(hours=k), text="good morning", gen="fixed_greeting_ack", cap="greeting")
         for k in (5, 3, 1)]
sigs = [rec(NOW - timedelta(hours=k), text="test", gen="fixed_sigreport", cap="sigreport") for k in (4, 2)]
old_ask = [rec(NOW - timedelta(days=40), text="how far is it to town")]
h = state_for(old_ask + greet, FRESH_RUNS)
ck("STARVED when the last ask is older than STARVE_D", h["state"] == "STARVED", h["state"])
ck("STARVED reason says nobody asked", "nobody has asked" in h["reason"], h["reason"])
ck("STARVED reports the age", 39.5 < h["ask"]["age_d"] < 40.5, h["ask"])
h = state_for(old_ask + greet + sigs, FRESH_RUNS)
ck("signal reports are not asks", h["state"] == "STARVED", h["state"])
h = state_for(old_ask + greet + [rec(NOW - timedelta(hours=2), text="5 mi in km",
                                     gen="fixed_calc", cap="calc")], FRESH_RUNS)
ck("a doer HIT is an ask", h["state"] == "FRESH", h["state"])
h = state_for([rec(NOW - timedelta(days=25), text="how far")] + greet, FRESH_RUNS)
ck("25 days is not STARVED", h["state"] == "FRESH", h["state"])
h = state_for([rec(NOW - timedelta(days=40), text="hi", gen="fixed_greeting_ack", cap="greeting")]
              + greet, FRESH_RUNS)
ck("no ask in the retained log: STARVED as a lower bound",
   h["state"] == "STARVED" and h["ask"]["in_log"] is False and "retained log" in h["reason"], h)
h = state_for(old_ask + greet, FRESH_RUNS, drafts=dfail)
ck("FAILING outranks STARVED, both flagged",
   h["state"] == "FAILING" and "STARVED" in h["flags"], h["flags"])
saved_sd = learn.STARVE_D
try:
    learn.STARVE_D = 10**9
    h = state_for(old_ask + greet, FRESH_RUNS)
    ck("mutation: a disabled STARVED check is visible as FRESH here", h["state"] == "FRESH", h["state"])
finally:
    learn.STARVE_D = saved_sd
ck("STARVED is in the closed set", "STARVED" in learn.STATES)

print("\noverheard -- what the mesh asked that nobody answered")
def pkt(i, text, days_ago=1.0, to="^all", ch=0, reply_to=None, reaction=None):
    return {"id": i, "ts": iso(NOW - timedelta(days=days_ago)), "text": text, "to": to,
            "channel": ch, "reply_to": reply_to, "reaction": reaction}
INB = [pkt(101, "anyone hear me from the east side?"),
       pkt(102, "nice morning on the mesh"),
       pkt(103, "what antenna are you running?"),
       pkt(104, "a 5 dBi on the roof", reply_to=103),
       pkt(105, "how far have you gotten?"),
       pkt(106, "\U0001F44D", reply_to=105, reaction=True),
       pkt(107, "how is the weather there?", to="!bbbbbbbb"),
       pkt(108, "is the repeater up?"),
       pkt(109, "who is on tonight?", days_ago=20)]
def odraft(p, **kw):
    r = {"ts": p["ts"], "draft_id": f"{p['ts']}:{p['id']}", "text": p["text"], "via": "model",
         "gen_status": "ok", "draft": "x"}
    r.update(kw)
    return r
DR = [odraft(INB[0]), odraft(INB[1], doers=["calc"]), odraft(INB[2]), odraft(INB[4], gen_status="gen_rc1:"),
      odraft(INB[6]), odraft(INB[7], sent_reply="Yes, it is"), odraft(INB[8])]
with Fixture(FRESH_RECS, FRESH_RUNS, drafts=DR, inbox=INB,
             grades={DR[0]["draft_id"]: {"verdict": "good", "ts": iso(NOW - timedelta(days=3))}}):
    o = learn.overheard(now=NOW)
ck("public, silent, inside the window: 4 heard", o["heard"] == 4, o)
ck("3 question-shaped", o["questions"] == 3, o)
ck("a threaded reply counts as answered; a tapback does not", o["unthreaded"] == 2, o)
keys = [q["text"] for q in o["top"]]
ck("the answered question is not listed", "what antenna are you running?" not in keys, keys)
ck("the tapped-back question IS listed", "how far have you gotten?" in keys, keys)
ck("a DM is never listed", all("weather" not in k for k in keys), keys)
ck("a message Cal answered is not counted", all("repeater" not in k for k in keys), keys)
ck("outside the window is not counted", all("tonight" not in k for k in keys), keys)
ck("doer_could counts a silent message a doer had", o["doer_could"] == 1, o)
ck("failed model drafts are counted", o["drafts_failed"] == 1, o)
# 4 heard: 101 graded, 105 failed -> 102 and 103 are the two waiting.
ck("graded drafts are not 'to grade'", o["ungraded"] == 2, o)
ck("last grade is reported", (o["last_grade"] or "").startswith(iso(NOW - timedelta(days=3))[:10]), o)
with Fixture(FRESH_RECS, FRESH_RUNS):
    o = learn.overheard(now=NOW)
ck("no drafts: zeros, not an error", o["heard"] == 0 and o["top"] == [], o)

print("\nmalformed input never takes the health check down (review 2026-11-25)")
BAD_DRAFTS = DR + [[], 3, "str", {"ts": 12345, "draft_id": 7, "text": ["x"], "via": None},
                   {"ts": iso(NOW), "draft_id": f"{iso(NOW)}:101", "text": 42, "via": "model",
                    "gen_status": 5}]
BAD_INBOX = INB + [[], {"id": [1], "ts": 99, "reply_to": {"a": 1}, "text": None},
                   {"id": 555, "ts": iso(NOW), "reply_to": [101]}]
BAD_DEC = [[], 7, {"ts": 12345, "matched": True}]
with Fixture(FRESH_RECS, FRESH_RUNS, drafts=BAD_DRAFTS, inbox=BAD_INBOX,
             grades={"x": {"ts": 7}, "y": [1]}) as fx:
    # Appended AFTER the fixture banks the good records: the fixture's own banking is test
    # scaffolding and is not what is under test here.
    with open(fx.dec, "a") as f:
        for r in BAD_DEC:
            f.write(json.dumps(r) + "\n")
    with open(fx.dec, "ab") as f:
        f.write(b'{"ts": "\xff", "matched": true}\n')
    with open(fx.drafts, "ab") as f:
        f.write(b'{"ts": "\xff\xfe", "text": "bad utf8"}\n')
    try:
        h = learn.health(now=NOW); hok = True
    except Exception as e:
        h, hok = repr(e), False
    ck("health() survives malformed rows", hok and h["state"] in learn.STATES, h)
    try:
        o = learn.overheard(now=NOW); ook = True
    except Exception as e:
        o, ook = repr(e), False
    ck("overheard() survives malformed rows", ook, o)
    ck("and still counts the good rows", ook and o["questions"] == 3 and o["unthreaded"] == 2, o)
    _md = learn.LEDGER_MD
    learn.LEDGER_MD = os.path.join(fx.d, "gap-ledger.md")      # never the live ledger
    try:
        learn.render_md({"totals": {}, "clusters": {}}, {"runs": 1, "last_ts": ""}, {}); rok = True
    except Exception as e:
        rok = repr(e)
    finally:
        learn.LEDGER_MD = _md
    ck("render_md() survives malformed rows", rok is True, rok)

print("\na drafts file nobody is writing says nothing about the model")
with Fixture(FRESH_RECS, FRESH_RUNS, drafts=dfail) as fx:
    old = NOW.timestamp() - (learn.DRAFTS_STALE_H + 1) * 3600
    os.utime(fx.drafts, (old, old))
    h = learn.health(now=NOW)
ck("stale drafts file: not checked, not FAILING", h["drafts_gen"] is None and h["state"] == "FRESH", h)
with Fixture(FRESH_RECS, FRESH_RUNS, drafts=dfail) as fx:
    fresh = NOW.timestamp() - 3600
    os.utime(fx.drafts, (fresh, fresh))
    h = learn.health(now=NOW)
ck("fresh drafts file with failures: FAILING", h["state"] == "FAILING", h["state"])

h = state_for(sig, FRESH_RUNS, mutate=True)
ck("DRIFT when the bank disagrees with the code", h["state"] == "DRIFT", h["state"])
ck("DRIFT reports how many records", h["stale"] == 3, h["stale"])

# Precedence, and the reason it exists: a dead job makes every number under it old, so naming
# the drift while the job itself is stopped would point at the wrong repair.
h = state_for(sig, [iso(NOW - timedelta(hours=40))], mutate=True)
ck("LATE outranks DRIFT for the chip", h["state"] == "LATE", h["state"])
ck("but BOTH are flagged, so neither is hidden",
   set(h["flags"]) >= {"LATE", "DRIFT"}, h["flags"])

with Fixture(FRESH_RECS, FRESH_RUNS) as fx:
    learn.HISTORY = os.path.join(fx.d, "does-not-exist.jsonl")
    learn.LEDGER_JSON = os.path.join(fx.d, "also-missing.json")
    h = learn.health(now=NOW)
ck("unreadable artefacts yield UNKNOWN", h["state"] == "UNKNOWN", h["state"])
ck("UNKNOWN never reads as healthy", "unknown" in h["reason"].lower(), h["reason"])

print("\nthe closed set, and reasons that name their own mechanism")
seen = set()
for st in learn.STATES:
    r = learn._health_reason(st, 99.0, 99.0, {"stale": 3})
    ck(f"{st} has a reason", bool(r) and r not in seen, r)
    seen.add(r)
ck("an unrecognised state says so rather than defaulting",
   "unrecognised" in learn._health_reason("BOGUS", 1, 1, {}),
   learn._health_reason("BOGUS", 1, 1, {}))
ck("the fall-through is not the FRESH sentence",
   learn._health_reason("BOGUS", 1, 1, {}) != learn._health_reason("FRESH", 1, 1, {}))

print("\nthe page contract")
L = dash.build_learning()
ck("build_learning publishes health", "health" in L, sorted(L)[:4])
ck("live health is in the closed set",
   (L["health"] or {}).get("state") in learn.STATES, (L["health"] or {}).get("state"))
ck("history is published for the plot", isinstance(L.get("history"), list))
ck("overheard is published", "overheard" in L, sorted(L))

# Grade whatever "/" actually serves. Naming a template here means the suite keeps grading
# the page it was written against long after that page is retired to /old-N, which is the
# quiet way an eval stops covering what ships.
_cur = re.search(r"^CURRENT_PAGE = (PAGE_V\d+)",
                 open(os.path.join(HERE, "dashboard.py")).read(), re.M).group(1)
v5 = getattr(dash, _cur)
ck("every state has a chip class in the JS map",
   all(st in v5 for st in learn.STATES), "missing a state in the chip map")
for cls in ("lchip ok", "lchip bad", "lchip unknown"):
    sel = "." + cls.replace(" ", ".")
    ck(f"CSS defines {sel}", f"#pane-learn .{cls.replace(' ', '.')}" in v5, sel)
# Every id paintAges writes to must be created by drawHealth, or the ages silently stop moving
# while the strip goes on looking authoritative.
for ident in ("lrn-agerun", "lrn-agein", "lrn-agenext"):
    ck(f"{ident} is both painted and created",
       v5.count(ident) >= 2, f"{ident} appears {v5.count(ident)}×")
ck("the strip container exists", 'id="lrn-health"' in v5)
ck("the plot container exists", 'id="lrn-spark"' in v5)
ck("ages are kept out of the render signature",
   "run_age_h" not in v5.split("const lsig=")[1].split("\n")[0])

print("\nthe renderers, EXECUTED")
# Reading the markup a function would build is not the same as building it. eval_render makes
# this argument for the Python side; the strip and the plot are drawn in the browser, so they
# are executed here in node against real payload shapes. Skipped, loudly, when node is absent --
# a check that quietly does not run is worse than one that is not written.
import shutil, subprocess as _sp, re as _re
_node = shutil.which("node") or "/usr/local/opt/node@22/bin/node"
if not os.path.exists(_node):
    print("  SKIP no node on PATH — the render execution checks did not run")
else:
    _fns = []
    for _n in ("function ageTxt", "function untilTxt", "function drawHealth", "function drawSpark",
               "function drawOverheard", "function daystamp"):
        _i = v5.index(_n); _j = v5.index("\n}\n", _i) + 3
        _fns.append(v5[_i:_j])
    _dir = tempfile.mkdtemp(prefix="evalrender-")
    _hjs = os.path.join(_dir, "h.js")
    _cases = {
        "live-shaped, healthy": {
            "health": {"state": "FRESH", "flags": [], "reason": "all good", "run_age_h": 1.0,
                       "input_age_h": 0.5, "next_expected": iso(NOW + timedelta(hours=23)),
                       "stale": 0, "audited": 147},
            "history": [{"ts": iso(NOW - timedelta(hours=24 * (5 - i))), "processed": i + 1,
                         "untriaged": 20 + i} for i in range(5)],
            "expect_late": 0},
        "health unreadable": {
            "health": None,
            "history": [{"ts": iso(NOW - timedelta(hours=24 * (3 - i))), "processed": 2,
                         "untriaged": 9} for i in range(3)],
            "expect_late": 0},
        "a run was missed": {
            "health": {"state": "LATE", "flags": ["LATE"], "reason": "no run in 45 h",
                       "run_age_h": 45.0, "input_age_h": 2.0, "next_expected": None,
                       "stale": 0, "audited": 10},
            "history": [{"ts": "2026-08-20T11:15:00+00:00", "processed": 5, "untriaged": 20},
                        {"ts": "2026-08-21T11:15:00+00:00", "processed": 3, "untriaged": 21},
                        {"ts": "2026-08-23T09:00:00+00:00", "processed": 9, "untriaged": 25}],
            "expect_late": 1},
        "starved, drafts failing": {
            "health": {"state": "STARVED", "flags": ["FAILING", "STARVED"], "reason": "nobody asked",
                       "run_age_h": 1.0, "input_age_h": 0.2, "next_expected": None,
                       "stale": 0, "audited": 10,
                       "gen": {"recent_failed": 0, "fail_after": 3},
                       "drafts_gen": {"recent_failed": 344, "fail_after": 3},
                       "ask": {"age_d": 56.9, "in_log": True}},
            "overheard": {"days": 14, "heard": 219, "questions": 12, "unthreaded": 12, "doer_could": 4,
                          "drafts_failed": 216, "top": [{"text": "How far have you been able to go?",
                                                        "n": 1, "last": iso(NOW)}]},
            "history": [{"ts": iso(NOW - timedelta(hours=24 * (2 - i))), "processed": 3,
                         "untriaged": 1} for i in range(2)],
            "expect_late": 0, "expect_warn": True},
    }
    for _name, _c in _cases.items():
        json.dump(_c, open(os.path.join(_dir, "p.json"), "w"))
        open(_hjs, "w").write(
            "const L=require(" + json.dumps(os.path.join(_dir, "p.json")) + ");\n"
            "const N={};const el=i=>N[i]||(N[i]={innerHTML:'',textContent:''});\n"
            "const $=s=>el(s.replace('#',''));\n"
            "const esc=s=>String(s).replace(/[&<>\"]/g,c=>"
            "({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));\n"
            + "\n".join(_fns) +
            "\ndrawHealth(L.health); drawSpark(L.history); drawOverheard(L.overheard);\n"
            "const H=N['lrn-health'].innerHTML,S=N['lrn-spark'].innerHTML+N['lrn-overheard'].innerHTML;\n"
            "console.log(JSON.stringify({chip:/lchip/.test(H),"
            "bad:/NaN|undefined|Infinity/.test(H+S),"
            "bars:(S.match(/<rect /g)||[]).length,"
            "pts:((S.match(/points=\"([^\"]+)\"/)||[])[1]||'').split(' ').filter(Boolean).length,"
            "late:(S.match(/sb late/g)||[]).length,"
            "warn:/lchip warn/.test(H),ovh:N['lrn-overheard'].innerHTML.length}));\n")
        _r = _sp.run([_node, _hjs], capture_output=True, text=True)
        if _r.returncode != 0:
            ck(f"renderers execute [{_name}]", False, _r.stderr[:300]); continue
        _o = json.loads(_r.stdout.strip().splitlines()[-1])
        _n_runs = len(_c["history"])
        ck(f"[{_name}] a chip is drawn", _o["chip"], _o)
        ck(f"[{_name}] no NaN or undefined reaches the page", not _o["bad"], _o)
        ck(f"[{_name}] one bar per run", _o["bars"] == _n_runs, _o)
        ck(f"[{_name}] the queue line has a point per run", _o["pts"] == _n_runs, _o)
        ck(f"[{_name}] late runs marked: {_c['expect_late']}",
           _o["late"] == _c["expect_late"], _o)
        ck(f"[{_name}] amber chip only for STARVED", _o["warn"] == _c.get("expect_warn", False), _o)
        ck(f"[{_name}] the overheard panel always says something", _o["ovh"] > 0, _o)

# --- gen_status is PUBLIC: nothing the CLI prints may reach it (review 2026-09-24) -------------
# 0a807d4 kept the stderr tail out of the health strip, but run_claude still returned it inside
# gen_status, and dashboard.correlate copies gen_status onto every message's public trace.
print("\n== gen_status carries a code, never CLI output ==")
import types, subprocess as _sp
import responder as _R
_real_run, _real_log = _R.subprocess.run, _R.log
_R.log = lambda *a, **k: None
try:
    _R.subprocess.run = lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout="Usage limit reached for org acct-SECRET", stderr="acct-SECRET auth")
    _, st = _R.run_claude(dict(_R.DEFAULTS), "hi")
    ck("a failing CLI: code only", st.startswith("gen_rc1") and "acct" not in st and "SECRET" not in st, st)
    def _boom(*a, **k):
        # The path must be IN the repr: FileNotFoundError(2, msg, path) drops the filename from
        # its repr, so the first version of this check passed with the leak put back.
        raise ValueError("bad exec /Users/someone/secret/path/claude")
    _R.subprocess.run = _boom
    _, st = _R.run_claude(dict(_R.DEFAULTS), "hi")
    ck("an exception: type only", st.startswith("gen_exc:") and "/Users" not in st and "secret" not in st, st)
finally:
    _R.subprocess.run, _R.log = _real_run, _real_log

# ---------------------------------------------------------------------------------------
if "--self-test" in sys.argv:
    print("\nself-test — each mutation must FAIL a check above")
    # Each mutation is applied in a CHILD interpreter through sitecustomize, so the checks
    # above run against a genuinely broken learn.py rather than against a patched copy this
    # process could accidentally un-patch. A mutation that leaves the suite green is a check
    # that was never testing anything.
    import subprocess
    MUTANTS = {
        "audit blind to a reverted fix":
            "learn.audit = lambda *a, **k: {'ok': True, 'stale': 0, 'audited': 2,"
            " 'banked': 2, 'delta': {}, 'reason': ''}\n",
        "every reason identical":
            "learn._health_reason = lambda *a, **k: 'everything is fine'\n",
        "STARVED never fires":
            "learn.STARVE_D = 10**9\n",
        "drafts never read":
            "learn._drafts_gen_health = lambda now: None\n",
        "tapbacks count as answers":
            "_o = learn.overheard\n"
            "def _ov(*a, **k):\n"
            "    r = _o(*a, **k); r['unthreaded'] = 0; return r\n"
            "learn.overheard = _ov\n",
        "health always FRESH":
            "learn.health = lambda now=None: {'state':'FRESH','flags':[],'reason':'fine',"
            "'stale':0,'audited':0,'run_age_h':0,'input_age_h':0,'next_expected':None,"
            "'last_run':None,'last_input':None,'runs':0,'delta':{}}\n",
    }
    for name, patch in MUTANTS.items():
        shim = tempfile.mkdtemp()
        open(os.path.join(shim, "sitecustomize.py"), "w").write(
            f"import sys\nsys.path.insert(0, {HERE!r})\nimport learn\n" + patch)
        r = subprocess.run([sys.executable, os.path.abspath(__file__)],
                           capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=shim))
        print(f"  {'ok  ' if r.returncode != 0 else 'FAIL'} mutation caught: {name}")
        if r.returncode == 0:
            FAILS.append("self-test: " + name)

print()
if FAILS:
    print(f"eval_health: {len(FAILS)} FAILED — {FAILS}")
    sys.exit(1)
print("eval_health: all checks pass")
