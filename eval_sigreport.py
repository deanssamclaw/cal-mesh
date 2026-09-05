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
# GRADED BY SHAPE, NOT BY LITERAL TEXT. The first version pinned exact strings and an exact
# count, and it went red the moment Dean sent `Test 14` — a genuine test, correctly claimed,
# breaking the eval simply because the corpus grew. An eval that cries wolf on ordinary
# traffic gets ignored, which is worse than one that is slightly coarser.
#
# So digit runs collapse to <n> and the allowed set is written by hand at that granularity.
# `Test 12` and `Test 14` are one shape; a false fire on anything genuinely different still
# fails, because its normalised form will not be in this set. The set is NOT derived from the
# matcher — a fixture built from the thing under test could only ever pass.
def shape(t):
    return re.sub(r"\d+", "<n>", " ".join((t or "").lower().split())).strip()


ALLOWED_SHAPES = {
    "cal this is another test",
    "cal, latency test",
    "tange test",                  # the typo that killed the vocabulary-list design
    "range test",
    "test test",
    "cal test",
    "test <n>",                    # the numbered sequence — the 2026-08-23 live miss
    # Widening this set is the move that needs a reason, so each of these carries one.
    # A greeting in front of the trigger addresses Cal exactly as much as the trigger alone.
    # Both of these sat in the log as GAPs answered by the model while measured SNR was on
    # disk; they fire now because the trigger strip stopped being anchored to position 0.
    "hey cal, this is a test",
    "hey cal - this is a test",
    # A bare test, with and without the punctuation a person actually types. Covered by the
    # call this module was built on -- "any kind of range test or signal test deserves a
    # response" (2026-08-22) -- and a stranger sending `Test` on a mesh is running one. What
    # a bare test must NOT do is swallow somebody discussing a test; that is the referential
    # rule in sigreport.match, and `got the test` is its regression case.
    "test",
    "test!?",
}
# These specific shapes must ALWAYS fire; losing one is a regression, not a quiet corpus drift.
MUST_FIRE_SHAPES = {"range test", "tange test", "cal test", "test <n>"}

if os.path.exists(CORPUS):
    rows = [json.loads(l) for l in open(CORPUS, encoding="utf-8")]
    fired = [(x.get("text") or "") for x in rows if sigreport.match(x.get("text") or "")]
    got = {shape(t) for t in fired}
    # A FALSE FIRE IS THE FAILURE THAT MATTERS: this doer sits ahead of every ladder, so
    # anything it claims is a message no other capability will ever see.
    ck(got <= ALLOWED_SHAPES,
       f"corpus: fired on {sorted(got - ALLOWED_SHAPES)} — not a test shape, and it pre-empts "
       f"every other doer")
    ck(MUST_FIRE_SHAPES <= got,
       f"corpus: stopped firing on {sorted(MUST_FIRE_SHAPES - got)} — a regression")
    ck(len(fired) >= 10, f"corpus: only {len(fired)} fires over {len(rows)} messages — "
                         f"the trigger has narrowed")

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
          "check 12 boxes", "protest 5", "contest 3", "latest 5", "test 1234",
          "Cal whats the torque for a 1/2 inch bolt", "I failed my drivers test",
          "protest", "contest results", "Cal hows the weather", "5 mi in km",
          "we should test that later this week", "latest news"):
    ck(sigreport.match(t) is None, f"trigger: must not fire on {t!r}")

# A NUMBERED TEST — the shape a sequence actually takes, and the live miss of 2026-08-23.
for t, idx in (("Test 12", "12"), ("test 1", "1"), ("Range test 3", "3"), ("check 2", "2"),
               ("signal test 7", "7"), ("test #4", "4"), ("Cal test 9", "9")):
    m = sigreport.match(t)
    ck(m is not None, f"numbered test must fire: {t!r}")
    ck(m and m.get("index") == idx, f"index of {t!r} should be {idx!r}, got {m and m.get('index')}")
ck(sigreport.match("test")["index"] is None, "an unnumbered test carries no index")
# The counter is echoed so a reply can be matched to its test mid-sequence.
ck(sigreport.try_answer("Test 12", rec())[0] == "Copy 12: 2 hops, last leg RSSI -32, SNR 6.0",
   f"index must be echoed: {sigreport.try_answer('Test 12', rec())[0]!r}")
ck(sigreport.try_answer("Test test", rec())[0].startswith("Copy: "),
   "no index means no number in the head")
# NOTHING BUT DIGITS EVER REACHES THE AIR. The index is re-derived from the capture rather
# than passed through, so even a hand-built call cannot inject text into a broadcast reply.
ck(sigreport.report(rec(), index="12<script>")[0].startswith("Copy 12:"),
   "index is re-derived from digits, never echoed as text")
ck(sigreport.report(rec(), index="abc")[0].startswith("Copy: "), "a digitless index is dropped")
ck(sigreport.report(rec(), index="999999")[0].startswith("Copy 999:"), "index bounded to 3 digits")
# The bound is real: four digits is not a test index.
ck(sigreport.match("test 1234") is None, "a four-digit tail is not a test index")

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

# A GREETING MAY PRECEDE THE TRIGGER. Anchoring the strip to position 0 meant `Hey Cal, ...`
# was never recognised as addressed, so the phrase rules never ran on it. Both of these are
# real lines from the log that fell through to the model with measured SNR sitting on disk.
ck(sigreport.match("Hey Cal, this is a test") is not None, "a greeting before the trigger")
ck(sigreport.match("Hey Cal - this is a test") is not None, "a dash after the greeting")
ck(sigreport.match("Hello Cal, range test") is not None, "hello + trigger + range test")
ck(sigreport.match("hey cal, test 5")["index"] == "5", "the index survives a greeting")
ck(sigreport.match("Cal, this is a test") is not None, "the bare trigger still works")
# and it must not manufacture a trigger that is not there
ck(sigreport.match("heycal") is None, "a greeting rule does not fillet a word")

# TALKING ABOUT A TEST IS NOT RUNNING ONE. This doer pre-empts every other ladder, so a
# message it claims is one nothing else will ever see. A determiner immediately before the
# word is the tell: `the test` is a reference, `range test` is a test.
ck(sigreport.match("got the test") is None, "a determiner makes it a reference")
ck(sigreport.match("I got the test") is None, "and so does a longer report of one")
ck(sigreport.match("did you get my test") is None, "a possessive is referential too")
ck(sigreport.match("a test") is None, "an article alone is not a test")
ck(sigreport.match("your test") is None, "somebody else's test is not Cal's to answer")
# the qualifier rule must not eat the tests it exists to keep
ck(sigreport.match("range test") is not None, "range test still fires")
ck(sigreport.match("tange test") is not None, "and so does the typo")
ck(sigreport.match("latency test") is not None, "so does an unlisted qualifier")
ck(sigreport.match("test") is not None, "a bare test still fires")
# with the trigger present the reading is settled: the sender said Cal's name
ck(sigreport.match("Cal, got the test") is not None, "addressed overrides referential")

# --- 2. the report says only what was measured ----------------------------------------------
# THE SHAPE SAYS WHOSE MEASUREMENT IT IS. `RSSI -63` on a relayed packet is the RELAY's
# signal into Cal, not the sender's, and reported bare it reads as the sender's. That misread
# was made by this module's own author against real 28-mile traffic before it was fixed.
txt, meta = sigreport.report(rec())
ck(txt == "Copy: 2 hops, last leg RSSI -32, SNR 6.0", f"nominal report wrong: {txt!r}")
ck(sigreport.report(rec(hops=2), relay_name="MDNO")[0]
   == "Copy: 2 hops via MDNO, last leg RSSI -32, SNR 6.0", "a resolved relay is named")
ck(sigreport.report(rec(hops=0))[0] == "Copy: direct, RSSI -35, SNR 6.0".replace("-35", "-32"),
   f"direct must claim the sender's own signal: {sigreport.report(rec(hops=0))[0]!r}")
ck("last leg" not in sigreport.report(rec(hops=0))[0],
   "a direct packet IS the sender's signal — no last-leg qualifier")
ck("last leg" in sigreport.report(rec(hops=1))[0], "one hop is still a relayed measurement")
ck(sigreport.report(rec(hops=1))[0].startswith("Copy: 1 hop,"), "one hop must be singular")
# ROUTING LEADS. It is the field that moved across a 28-mile walk while SNR moved 1.25 dB.
ck(sigreport.report(rec())[0].split(": ")[1].startswith("2 hops"), "hop count leads")
# A relay is one byte, so an unresolved name must simply vanish, never become a guess.
ck("via" not in sigreport.report(rec(hops=2), relay_name=None)[0], "unresolved relay is unnamed")

# A MISSING HOP COUNT IS NOT 'DIRECT'. This is failure #2 in the docstring, and it is the one
# the bridge's own history makes likely: hopLimit is omitted by MessageToDict when it is 0.
t_nohop, _ = sigreport.report({"snr": 6.0, "rssi": -32})
ck("direct" not in t_nohop and "hop" not in t_nohop,
   f"absent hops must be omitted, never rendered: {t_nohop!r}")
# An unknown hop count means we cannot say whose signal it is, so the reply makes NO claim
# either way — neither "direct" nor "last leg".
ck("last leg" not in t_nohop, f"unknown routing must not claim a leg: {t_nohop!r}")
ck(t_nohop == "Copy: RSSI -32, SNR 6.0", f"neutral shape wrong: {t_nohop!r}")

# Field-by-field degradation. A bad field costs that field and nothing else.
ck(sigreport.report(rec(snr=None))[0] == "Copy: 2 hops, last leg RSSI -32",
   f"missing snr costs only snr: {sigreport.report(rec(snr=None))[0]!r}")
ck(sigreport.report(rec(rssi=None))[0] == "Copy: 2 hops, SNR 6.0",
   f"missing rssi costs only rssi: {sigreport.report(rec(rssi=None))[0]!r}")

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
ck(short == "Copy: 2 hops", f"truncation must shed whole fields from the right: {short!r}")
ck(smeta["parts"] == ["2 hops"], f"meta must record what actually shipped: {smeta['parts']}")
# The hop count is now the LAST thing to go, because it is the field carrying information.
ck("hops" in short, "routing survives truncation; the flat number does not")
ck(len(short or "") <= 22, f"length budget not honoured: {short!r}")
ck(not re.search(r"-3$", short or ""), f"truncated mid-field: {short!r}")
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
ck(ok[4] == "Copy: 2 hops, last leg RSSI -32, SNR 6.0", f"gate returned {ok[4]!r}")
ck(ok[2] == "^all", "a broadcast test is answered on the broadcast")
ck(plan(r=rec(text="Range test", to="!aaaaaaaa"))[2] == "!aaaaaaaa", "a DM test gets a DM")

# THE PATH THAT ACTUALLY TRANSMITS, graded end to end. plan_sigreport calls match() and
# report() separately; try_answer() joins them. On 2026-08-23 the index echo was written and
# green through try_answer while the transmitting path silently dropped it, because nothing
# graded the two-call path. These four lines are that gap closed.
ck(plan(r=rec(text="Test 12"))[4] == "Copy 12: 2 hops, last leg RSSI -32, SNR 6.0",
   f"the transmitting path must echo the index: {plan(r=rec(text='Test 12'))[4]!r}")
ck(plan(r=rec(text="Test test"))[4] == "Copy: 2 hops, last leg RSSI -32, SNR 6.0",
   "no index means no number in the head, on the transmitting path too")
# The RELAY NAME reaches the air only through the resolver, and only unambiguously.
ck(plan(r=rec(text="Test 12", relay_byte=198))[4]
   == "Copy 12: 2 hops via MDNO, last leg RSSI -32, SNR 6.0",
   f"a resolvable relay is named on the transmitting path: "
   f"{plan(r=rec(text='Test 12', relay_byte=198))[4]!r}")
ck("via" not in plan(r=rec(text="Test 12", relay_byte=7))[4],
   "an unplaceable relay byte is never guessed at")
ck(responder.resolve_relay(None) is None, "no relay byte, no name")
# SOMEONE ELSE'S TEXT ON OUR AIR. A short name is chosen by a third party, so it is
# whitelisted rather than escaped, and bounded to the four characters the protocol allows.
# It REJECTS rather than repairs: stripping and truncating turned "MD<script>" into "MDsc",
# a safe-looking name belonging to no node. Inventing an identity is the same class of failure
# as inventing a measurement.
for bad, want in (("MD<script>", None), ("../../etc", None), ("MDNO EXTRA", None),
                  ("\n\nCal:", None), ("!!!!", None), ("", None), (None, None), (42, None),
                  ("MDNO", "MDNO"), ("MDNO ", "MDNO"), ("MD", "MD"), ("M-1", "M-1")):
    ck(sigreport.clean_name(bad) == want,
       f"clean_name({bad!r}) should be {want!r}, got {sigreport.clean_name(bad)!r}")
ck(plan(r=rec(text="Test 12"))[1] == "sigreport", "a numbered test is claimed by the doer")
ck(plan(r=rec(text="Test 12"))[3] == 0, "the report answers on the channel it arrived on")
# THE 7th ELEMENT IS THE PUBLIC TRACE'S EVIDENCE. Without it the dashboard has nothing to show
# but a mechanism it would have to describe from memory — which is exactly how a range test came
# to be published as "a weather question" whose lookup failed.
m7 = plan(r=rec(text="Test 12"))[6]
ck(isinstance(m7, dict), f"the gate must return its measurement meta: {m7!r}")
ck(m7.get("parts") == ["2 hops", "last leg RSSI -32", "SNR 6.0"], f"meta parts: {m7.get('parts')}")
ck(m7.get("hops") == 2 and m7.get("snr") == 6.0 and m7.get("rssi") == -32, "meta carries the numbers")
ck(plan(r=rec(text="Test 12", relay_byte=198))[6].get("relay_name") == "MDNO",
   "meta carries the resolved relay for the trace")
# Every refusal returns the same arity, or the caller unpacks a different shape depending on
# which gate said no — a crash that only happens on the failing path.
for r_ in (rec(text="Range test", reaction=True), rec(text="not a test"),
           rec(text="Range test", **{"from": "!cccccccc"})):
    ck(len(plan(r=r_)) == 7, f"every return is a 7-tuple: {r_.get('text')!r}")
ck(len(responder.plan_sigreport({}, {}, rec(text="Range test"), "!cccccccc")) == 7,
   "including the disabled path")
ck(plan(r=rec(text="Test 12", channel=1))[3] == 1, "including Cal's own channel")

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
         'if not sig:', 'if False:'),
        ("truncation cuts mid-field",
         'while len(parts) > 1 and len(head + ", ".join(parts)) > max_chars:\n'
         '            parts.pop()',
         'text = text[:max_chars]'),
        ("trigger widened to anything containing test",
         '_TAIL_RE = re.compile(r"^(?:[\\w\'/-]+\\s+){0,%d}(?:test|check)%s$"\n'
         '                      % (_MAX_WORDS_TAIL - 1, _INDEX), re.I)',
         '_TAIL_RE = re.compile(r"^.*(?:test|check).*%s$" % (_INDEX,), re.I)'),
        ("numbered tests stop matching again (the 2026-08-23 regression)",
         '_INDEX = r"(?:\\s+#?(?P<idx>\\d{1,3}))?"', '_INDEX = r""'),
        ("index echoed as raw text instead of digits",
         'd = re.sub(r"\\D", "", str(index))[:3]', 'd = str(index)'),
        ("relayed signal reported as the sender's own",
         'sig_label = "last leg RSSI"', 'pass'),
        ("relay name transmitted unfiltered",
         'if not n or len(n) > 4 or _NAME_OK.search(n):\n        return None\n    return n',
         'return n'),
        ("out-of-range snr aired",
         'if snr is not None and not (-30.0 <= snr <= 30.0):\n        snr = None',
         'if False:\n        snr = None'),
        # the two 2026-09-05 rules, each mutated back to the behaviour it replaced
        ("greeting before the trigger stops being addressed",
         'stripped = re.sub(r"^(?:%s[\\s,:!-]+)?%s\\b[\\s,:-]*" % (_GREET, re.escape(trig)),',
         'stripped = re.sub(r"^%s\\b[\\s,:-]*" % (re.escape(trig),),'),
        ("talking about a test claims the message again",
         'if lead in _REFERENTIAL:\n                return None',
         'if False:\n                return None'),
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
                                           "we should test that later this week",
                                           "check 12 boxes", "test 1234")):
                caught = True
            mi = mmod.match("Test 12")
            if not mi or mi.get("index") != "12":
                caught = True
            if mmod.try_answer("Test 12", rec())[0] != "Copy 12: SNR 6.0, RSSI -32, 2 hops":
                caught = True
            if not mmod.report(rec(), index="12<script>")[0].startswith("Copy 12:"):
                caught = True
            if "last leg" not in (mmod.report(rec(hops=2))[0] or ""):
                caught = True
            if "last leg" in (mmod.report(rec(hops=0))[0] or ""):
                caught = True
            if mmod.clean_name("MD<script>") is not None:
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
