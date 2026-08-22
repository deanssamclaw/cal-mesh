#!/usr/bin/env python3
"""eval_sigreport.py — a range test is answered with what the radio measured, or not at all.

WHAT THIS FILE IS FOR. Three failures, in the order they would hurt.

  1. AN INVENTED NUMBER. The whole reason this is a doer is that the model, asked "how's the
     link holding up", answered "Link's solid and steady over here" with no access to a
     number. Every assertion about the report body is really one assertion: no value appears
     in the reply that was not in the record.
  2. A CLAIM DRESSED AS A MEASUREMENT. A missing hop count rendered as "direct" is the same
     failure wearing better clothes, so the absent cases are tested as hard as the present
     ones.
  3. STEALING SOMEBODY ELSE'S MESSAGE. This doer sits AHEAD of every ladder, which is a
     privilege no other capability has. The corpus replay below is what pays for it.

THE ORACLE IS THE REAL LOG, NOT MY IMAGINATION. Session 126's lesson was five mechanisms
built against sentences I invented while the real distribution sat in inbox.jsonl. So the
trigger is graded against every message this node has actually received: it must fire on the
tests and on nothing else, and the expected set is written out by hand below so that a change
to the matcher fails HERE rather than on the air.

Run:  python3 eval_sigreport.py                (exit 0 = pass)
      python3 eval_sigreport.py --self-test    (also mutate the module; each must be caught)
"""
import importlib.util
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# Imports resolve RELATIVE TO THIS FILE, never through a hardcoded ~/cal-mesh on sys.path.
# That hardcoded path is what once made every eval running from a scratch copy import the
# DEPLOYED module — proven by sabotage, and it left four eval files green against a module
# that returned a fixed string for every input.
sys.path.insert(0, HERE)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sigreport = _load("sigreport")
failures, checked = [], 0


def ck(cond, msg):
    global checked
    checked += 1
    if not cond:
        failures.append(msg)


REC = {"snr": 6.0, "rssi": -32, "hops": 2, "hop_start": 7, "hop_limit": 5,
       "from": "!aaaaaaaa", "to": "^all", "channel": 0, "reaction": False}


def rec(**kw):
    r = dict(REC)
    r.update(kw)
    return r


# --- 1. the trigger, graded against the real log --------------------------------------------
#
# EXPECTED SET WRITTEN OUT BY HAND. A test that recomputed this from the matcher could only
# ever pass — the fixture would be built from the thing under test. These nine strings were
# read out of inbox.jsonl and judged by eye as "this person is testing their radio".
CORPUS = os.path.join(HERE, "inbox.jsonl")
EXPECTED_FIRE = {
    "Cal this is another test",
    "Cal, latency test",
    "Tange test",
    "Range test",          # sent four times; the set collapses them, the count below does not
    "Test test",
    "Cal test",
}
EXPECTED_FIRE_COUNT = 9

if os.path.exists(CORPUS):
    rows = [json.loads(l) for l in open(CORPUS, encoding="utf-8")]
    fired = [(x.get("text") or "") for x in rows if sigreport.match(x.get("text") or "")]
    ck(len(fired) == EXPECTED_FIRE_COUNT,
       f"corpus: expected {EXPECTED_FIRE_COUNT} fires over {len(rows)} real messages, got {len(fired)}")
    got = {t.strip() for t in fired}
    ck(got == EXPECTED_FIRE,
       f"corpus: fired on the wrong set.\n    unexpected: {sorted(got - EXPECTED_FIRE)}"
       f"\n    missed:     {sorted(EXPECTED_FIRE - got)}")

    # THE PRE-EMPT IS ONLY SAFE IF NOTHING ELSE WANTED THESE MESSAGES. Checked against the
    # live modules rather than argued about, and skipped rather than faked if one is absent.
    try:
        calc, weather, sunmoon = _load("calc"), _load("weather"), _load("sunmoon")
        stolen = []
        for x in rows:
            t = x.get("text") or ""
            if not sigreport.match(t):
                continue
            if calc.try_answer(t, trigger="cal")[0]:
                stolen.append((t, "calc"))
            wm = weather.explain_weather_match(t)
            if wm and wm.get("via"):
                stolen.append((t, "weather"))
            sm = sunmoon.explain_match(t)
            if sm and sm.get("via"):
                stolen.append((t, "sunmoon"))
        ck(not stolen, f"corpus: sigreport pre-empts another doer on {stolen}")
    except Exception as e:                                     # noqa: BLE001
        ck(False, f"collision check could not run: {e!r}")
else:
    ck(False, "inbox.jsonl missing — the trigger has no oracle to be graded against")

# Shapes that must NEVER fire. Each is a sentence a human would plausibly send.
for t in ("the range test we ran yesterday failed", "Cal", "Good morning", "test the antenna",
          "Cal whats the torque for a 1/2 inch bolt", "I failed my drivers test",
          "protest", "contest results", "Cal hows the weather", "5 mi in km",
          "we should test that later this week", "latest news"):
    ck(sigreport.match(t) is None, f"trigger: must not fire on {t!r}")

# Shapes that must fire — the vocabulary this was built for, plus the typo family.
for t in ("Range test", "range test", "RANGE TEST", "Tange test", "test", "Cal test",
          "signal test", "radio check", "comms check", "mic check", "check check",
          "you copy?", "do you copy", "hows my copy", "signal report", "sig report",
          "this is a test", "Cal this is another test", "testing 1 2 3", "hows my signal",
          "anyone copy me", "Cal, latency test", "link test", "coverage check"):
    ck(sigreport.match(t) is not None, f"trigger: should fire on {t!r}")

# The trigger word is stripped only as a WHOLE WORD at the FRONT.
ck(sigreport.match("Calibration test") is not None, "a node named Calibration may still test")
ck(sigreport.match("Cal") is None, "a bare hail is not a test")
ck(sigreport.match("test cal") is None, "trigger stripped from the front only")

# --- 2. the report says only what was measured ----------------------------------------------
txt, meta = sigreport.report(rec())
ck(txt == "Copy: SNR 6.0, RSSI -32, 2 hops", f"nominal report wrong: {txt!r}")
ck(sigreport.report(rec(hops=0))[0].endswith("direct"), "hops 0 must read as direct")
ck(sigreport.report(rec(hops=1))[0].endswith("1 hop"), "one hop must be singular")

# A MISSING HOP COUNT IS NOT 'DIRECT'. This is failure #2 in the docstring, and it is the one
# the bridge's own history makes likely: hopLimit is omitted by MessageToDict when it is 0.
t_nohop, _ = sigreport.report({"snr": 6.0, "rssi": -32})
ck("direct" not in t_nohop and "hop" not in t_nohop,
   f"absent hops must be omitted, never rendered: {t_nohop!r}")

# Field-by-field degradation. A bad field costs that field and nothing else.
ck(sigreport.report(rec(snr=None))[0] == "Copy: RSSI -32, 2 hops", "missing snr costs only snr")
ck(sigreport.report(rec(rssi=None))[0] == "Copy: SNR 6.0, 2 hops", "missing rssi costs only rssi")

# JSON true converts to 1.0 and would ship as a plausible SNR. This exact shape aired once
# already, as a heat index of 34F, and it pointed the reassuring direction.
for bad in (True, False, "strong", "", [], {}, float("nan"), float("inf")):
    r2 = sigreport.report(rec(snr=bad))
    ck(r2[1]["snr"] is None, f"snr {bad!r} must be rejected, got {r2[1]['snr']!r}")

# Out-of-range values are instrument faults, not remarkable links.
ck(sigreport.report(rec(snr=99))[1]["snr"] is None, "an impossible SNR is dropped")
ck(sigreport.report(rec(rssi=40))[1]["rssi"] is None, "a positive RSSI is corrupt, not strong")

# Nothing measured at all -> silence, and a stated reason.
none_txt, none_meta = sigreport.report({"hops": 3})
ck(none_txt is None, "a report with no signal in it must refuse")
ck(none_meta["refused"] == "no_measurements", f"refusal reason: {none_meta['refused']!r}")

# TRUNCATION DROPS WHOLE FIELDS FROM THE RIGHT. A truncated "RSSI -3" is a different and
# better-looking measurement than "RSSI -32", which is the shape every wrong answer this
# codebase has aired.
short, smeta = sigreport.report(rec(), max_chars=22)
# The EXACT value, not a property. "len <= 22 and no dangling -3" is satisfied by returning
# None, so a mutation that truncates the string and then refuses the over-long result passed
# a property test while destroying the behaviour. Naming the answer is what makes it fail.
ck(short == "Copy: SNR 6.0", f"truncation must shed whole fields from the right: {short!r}")
ck(smeta["parts"] == ["SNR 6.0"], f"meta must record what actually shipped: {smeta['parts']}")
ck(len(short or "") <= 22, f"length budget not honoured: {short!r}")
ck(not re.search(r"-3$", short or ""), f"truncated mid-field: {short!r}")
ck("hop" not in (short or ""), "routing sheds before signal")
# One field still over budget is a refusal, not a shaved string.
ck(sigreport.report(rec(), max_chars=6)[0] is None, "an unfittable report refuses")

# The reply carries no value that was not in the record — the whole point, asserted directly.
for h in (0, 1, 2, 5, None):
    r3 = rec(hops=h) if h is not None else {"snr": 6.0, "rssi": -32}
    out, _ = sigreport.report(r3)
    for n in re.findall(r"-?\d+\.?\d*", out or ""):
        ck(n in {"6.0", "32", "-32", str(h), "1", "0", "2", "5"},
           f"report {out!r} contains {n!r}, which is not in the record")

# --- 3. the gates ---------------------------------------------------------------------------
responder = _load("responder")
CFG_ON = {"SIGREPORT_ENABLED": "true", "TRIGGER_WORD": "cal"}


def plan(cfg=None, r=None, st=None, ts=None):
    return responder.plan_sigreport(dict(CFG_ON, **(cfg or {})), st if st is not None else {},
                                    r if r is not None else rec(text="Range test"),
                                    "!cccccccc", ts=ts or time.time())


# DEFAULT OFF is the house gate, and it is asserted rather than assumed.
ck(responder.DEFAULTS["SIGREPORT_ENABLED"] == "false", "SIGREPORT_ENABLED must default to false")
off = responder.plan_sigreport({}, {}, rec(text="Range test"), "!cccccccc")
ck(off[0] is False and off[1] == "sigreport_disabled", f"disabled config must refuse: {off[1]}")

# The channel gate needs a quiet, fresh status file to pass; build one so the gates below are
# testing themselves rather than the fixture.
STATUS_BAK = responder.STATUS


def with_status(payload, age_s=0):
    p = os.path.join(HERE, ".eval_status.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    if age_s:
        os.utime(p, (time.time() - age_s, time.time() - age_s))
    responder.STATUS = p
    return p


QUIET = {"metrics": {"chUtil": 5.0}}
with_status(QUIET)
ok = plan()
ck(ok[0] is True and ok[1] == "sigreport", f"a clean range test should be answered: {ok[1]}")
ck(ok[4] == "Copy: SNR 6.0, RSSI -32, 2 hops", f"gate returned {ok[4]!r}")
ck(ok[2] == "^all", "a broadcast test is answered on the broadcast")
ck(plan(r=rec(text="Range test", to="!aaaaaaaa"))[2] == "!aaaaaaaa", "a DM test gets a DM")

# CHANNEL STATE: unknown must FAIL CLOSED, in every one of its flavours.
for name, payload, age in (("stale", QUIET, 9999),
                           ("absent util", {"metrics": {}}, 0),
                           ("util is a bool", {"metrics": {"chUtil": True}}, 0),
                           ("util is text", {"metrics": {"chUtil": "quiet"}}, 0),
                           ("busy", {"metrics": {"chUtil": 40.0}}, 0)):
    with_status(payload, age)
    g = plan()
    ck(g[0] is False and g[1].startswith("sigreport_channel_"),
       f"channel {name} must refuse, got {g[0]}/{g[1]}")
responder.STATUS = os.path.join(HERE, ".eval_missing.json")
ck(plan()[1] == "sigreport_channel_status_unreadable", "a missing status file fails closed")
with_status(QUIET)

# A tapback is not a test. Absent reads as a real message; anything unexpected refuses.
ck(plan(r=rec(text="Range test", reaction=True))[1] == "sigreport_is_reaction", "reaction refused")
ck(plan(r=rec(text="Range test", reaction="true"))[1] == "sigreport_is_reaction",
   "an unexpected reaction value must fail the SAFE way, not sail through")
r_norx = rec(text="Range test")
r_norx.pop("reaction")
ck(plan(r=r_norx)[0] is True, "a record with no reaction field is a real message")

# Cal never answers himself.
ck(plan(r=rec(text="Range test", **{"from": "!cccccccc"}))[1] == "self", "self refused")

# BUDGET IS SPENT ONLY WHEN THERE IS SOMETHING TO SAY. A record with no measurements must not
# consume a slot — otherwise a stream of unmeasured packets exhausts the day in silence.
st = {}
nm = responder.plan_sigreport(CFG_ON, st, {"text": "Range test", "from": "!bbbbbbbb",
                                           "to": "^all", "reaction": False}, "!cccccccc")
ck(nm[0] is False and nm[1].startswith("sigreport_"), f"no measurements must refuse: {nm[1]}")
ck(not st.get("sig_day"), "a refused report must not have spent budget")
ck([g["gate"] for g in nm[5]].index("has_measurements") < len(nm[5]),
   "has_measurements is gated before the budget")
ck("daily_budget" not in [g["gate"] for g in nm[5]],
   "the budget gate must not even be reached when nothing was measured")

# Cooldown and daily cap.
st = {}
now_ts = time.time()
responder.commit_sigreport(st, "!aaaaaaaa", ts=now_ts)
# Boundary read from DEFAULTS, so a retuned cooldown cannot silently stop being tested.
COOL = int(responder.DEFAULTS["SIGREPORT_SENDER_COOLDOWN_S"])
ck(plan(st=st, ts=now_ts + COOL - 1)[1] == "sigreport_sender_cooldown",
   "cooldown holds inside the window")
ck(plan(st=st, ts=now_ts + COOL + 1)[0] is True, "cooldown releases after the window")
# The value itself is a decision, not an accident: the real log carries two tests from one
# walking node 33 s apart, and both are worth answering.
ck(COOL <= 33, f"cooldown {COOL}s would swallow the 33 s gap observed in the live log")
st2 = {"sig_day": {time.strftime("%Y-%m-%d", time.gmtime(now_ts)): 20}}
ck(plan(st=st2, ts=now_ts)[1] == "sigreport_budget_spent", "daily cap holds")

for p in (os.path.join(HERE, ".eval_status.json"),):
    if os.path.exists(p):
        os.unlink(p)
responder.STATUS = STATUS_BAK

# --- 4. self-test: every mutation must be caught ---------------------------------------------
if "--self-test" in sys.argv:
    print("\n--- self-test: each mutation must be CAUGHT ---")
    src = open(os.path.join(HERE, "sigreport.py"), encoding="utf-8").read()
    MUTATIONS = [
        ("absent hops rendered as direct",
         'if hops is not None:', 'if True:'),
        ("bool accepted as a measurement",
         'if v is None or isinstance(v, bool):', 'if v is None:'),
        ("positive rssi aired",
         'if rssi is not None and rssi > 0:\n        rssi = None',
         'if False:\n        rssi = None'),
        ("no-measurement case answers anyway",
         'if not parts:', 'if False:'),
        ("truncation cuts mid-field",
         'while len(parts) > 1 and len("Copy: " + ", ".join(parts)) > max_chars:\n'
         '            parts.pop()',
         'text = text[:max_chars]'),
        ("trigger widened to anything containing test",
         'if _TAIL_RE.match(s):', "if 'test' in s:"),
        ("out-of-range snr aired",
         'if snr is not None and not (-30.0 <= snr <= 30.0):\n        snr = None',
         'if False:\n        snr = None'),
    ]
    import tempfile
    for name, orig, mut in MUTATIONS:
        if orig not in src:
            print(f"  ?? {name}: anchor not found — this control is stale")
            failures.append(f"self-test anchor missing: {name}")
            continue
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "sigreport.py"), "w", encoding="utf-8") as f:
            f.write(src.replace(orig, mut, 1))
        # The mutant is loaded directly by path and re-graded below. Re-running this whole
        # file against it would be circular: the file is what is being validated.
        mspec = importlib.util.spec_from_file_location("sig_mut", os.path.join(d, "sigreport.py"))
        mmod = importlib.util.module_from_spec(mspec)
        try:
            mspec.loader.exec_module(mmod)
        except Exception as e:                                 # noqa: BLE001
            print(f"  ok {name}: CAUGHT (module threw: {e!r})")
            continue
        caught = False
        try:
            if mmod.report(rec())[0] != "Copy: SNR 6.0, RSSI -32, 2 hops":
                caught = True
            if "hop" in (mmod.report({"snr": 6.0, "rssi": -32})[0] or ""):
                caught = True
            if mmod.report(rec(snr=True))[1]["snr"] is not None:
                caught = True
            if mmod.report(rec(rssi=40))[1]["rssi"] is not None:
                caught = True
            if mmod.report(rec(snr=99))[1]["snr"] is not None:
                caught = True
            if mmod.report({"hops": 3})[0] is not None:
                caught = True
            if mmod.report(rec(), max_chars=22)[0] != "Copy: SNR 6.0":
                caught = True
            if any(mmod.match(t) for t in ("the range test we ran yesterday failed",
                                           "I failed my drivers test", "protest",
                                           "we should test that later this week")):
                caught = True
        except Exception:                                      # noqa: BLE001
            caught = True
        print(f"  {'ok' if caught else 'XX'} {name}: {'CAUGHT' if caught else 'SURVIVED'}")
        if not caught:
            failures.append(f"MUTATION SURVIVED: {name}")

for f in failures:
    print("FAIL " + f)
print(f"\n{checked} assertion(s); {len(failures)} problem(s)")
sys.exit(1 if failures else 0)
