#!/usr/bin/env python3
"""eval_anatomy.py — the entity/activity split, and the invariants over it.

The load-bearing case is weather: the model is handed the fetched observation and NOT the
sender's message. If that ever silently becomes "the model got the message", this page
would draw a boundary that is not there. That case is asserted directly.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import anatomy as A

PASS, FAIL = [], []
def ck(n, c, d=""):
    (PASS if c else FAIL).append(n)
    print(("  ok   " if c else "  FAIL ") + n + (("  -- " + d) if d and not c else ""))

WX = {"text": "whats the weather", "capability": "weather", "model": "haiku-4-5",
      "obs_station": "KOJC", "obs_age_s": 900, "reply": "62F", "dest": "^all",
      "sanitize": {"redactions": 0}}
CALC = {"text": "Cal 5+5", "capability": "calc", "calc": {"handler": "arith"},
        "reply": "10", "dest": "!x", "sanitize": {"redactions": 0}, "gen_status": "fixed_calc"}
GEN = {"text": "hows the radio", "model": "haiku-4-5", "reply": "solid", "dest": "^all",
       "sanitize": {"redactions": 0}}

print("\n[the sentence this page exists to make visible]")
sp = A.spec_for(WX)
obs = [e for e in sp["entities"] if e["kind"] == "observation"]
san = [e for e in sp["entities"] if e["kind"] == "sanitized"]
ck("weather: an observation entity exists", len(obs) == 1)
ck("weather: the OBSERVATION crossed to the model", sp["crossed"] == [obs[0]["id"]], str(sp["crossed"]))
ck("weather: the sender's message did NOT cross", san[0]["id"] not in sp["crossed"])
ck("weather: the message is a dead end, not a defect",
   A.dead_ends(sp) == [san[0]["id"]] and A.check_spec(sp) == [], str(A.check_spec(sp)))
spg = A.spec_for(GEN)
ck("general model path: the message DOES cross (the contrast)",
   spg["crossed"] == [[e for e in spg["entities"] if e["kind"] == "sanitized"][0]["id"]], str(spg["crossed"]))

print("\n[no model means no boundary]")
spc = A.spec_for(CALC)
ck("calc: model_ran is False", spc["model_ran"] is False)
ck("calc: nothing crossed", spc["crossed"] == [])
ck("calc: no generate activity exists",
   not any(a["kind"] == "generate" for a in spc["activities"]))
ck("calc: chain is still coherent", A.check_spec(spc) == [], str(A.check_spec(spc)))

print("\n[closed vocabulary -- no capability inherits another's story]")
ck("every entity kind is declared",
   all(e["kind"] in A.ENTITY_KINDS for r in (WX, CALC, GEN) for e in A.spec_for(r)["entities"]))
ck("every activity kind is declared",
   all(a["kind"] in A.ACTIVITY_KINDS for r in (WX, CALC, GEN) for a in A.spec_for(r)["activities"]))
ck("an unknown kind is a DEFECT, not a fallback",
   "undeclared kind" in " ".join(A.check_spec(
       {"entities": [{"id": "1", "kind": "received", "label": "x"},
                     {"id": "2", "kind": "invented", "label": "y"}],
        "activities": [{"id": "a", "kind": "match", "label": "m", "used": ["1"], "produced": "2"}],
        "model_ran": False, "crossed": []})))

print("\n[invariants]")
def probs(spec): return " ".join(A.check_spec(spec))
ck("a model with nothing crossing is caught",
   "nothing named crossing" in probs(
     {"entities": [{"id": "1", "kind": "received", "label": "m"},
                   {"id": "2", "kind": "reply", "label": "r"}],
      "activities": [{"id": "a", "kind": "generate", "label": "g", "used": [], "produced": "2"}],
      "model_ran": True, "crossed": []}))
ck("a dangling used-id is caught",
   "does not exist" in probs(
     {"entities": [{"id": "1", "kind": "received", "label": "m"}],
      "activities": [{"id": "a", "kind": "match", "label": "m", "used": ["9"], "produced": None}],
      "model_ran": False, "crossed": []}))
ck("two generations are caught",
   "more than one generation" in probs(
     {"entities": [{"id": "1", "kind": "received", "label": "m"},
                   {"id": "2", "kind": "reply", "label": "r"}],
      "activities": [{"id": "a", "kind": "generate", "label": "g", "used": ["1"], "produced": "2"},
                     {"id": "b", "kind": "generate", "label": "g", "used": ["1"], "produced": "2"}],
      "model_ran": True, "crossed": ["1"]}))
ck("a chain not starting at the received message is caught",
   "does not begin" in probs(
     {"entities": [{"id": "1", "kind": "reply", "label": "r"}],
      "activities": [], "model_ran": False, "crossed": []}))
ck("an entity produced by nothing is caught",
   "produced by nothing" in probs(
     {"entities": [{"id": "1", "kind": "received", "label": "m"},
                   {"id": "2", "kind": "computed", "label": "c"}],
      "activities": [{"id": "a", "kind": "match", "label": "m", "used": ["1", "2"], "produced": None}],
      "model_ran": False, "crossed": []}))
ck("a dead end is NOT reported as a problem",
   "went nowhere" not in probs(A.spec_for(WX)) and A.check_spec(A.spec_for(WX)) == [])

print("\n[page]")
P = A.PAGE_ANATOMY
# assert against whitespace-normalised markup: the sentence is line-wrapped in the source
# and an exact-substring check on it fails for a reason that has nothing to do with meaning.
import re as _re
PN = _re.sub(r"\s+", " ", P)
ck("the check's limit is stated, not hidden", "does not contradict itself" in PN)
ck("the page never claims a chain is verified", "coherent, never verified" in P)
ck("an undemonstrated weather claim is flagged", "is not demonstrated on this page" in P)
ck("fetches through DIR", "DIR+'api/anatomy'" in P)
ck("DIR strips all three supplement slugs", "anatomy|capabilities|console" in P)
ck("no mojibake", "�" not in P)
ck("no external asset host", P.count('href="https') == 0)

print("\n[gate ladders reach the page]")
import dashboard as dash
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.py")).read()
for k in ("dm_unlock_gates", "dm_longer_gates", "sigreport_gates", "greeting_gates"):
    ck("%s is whitelisted through to the trace" % k, '"%s"' % k in src)
# `via` is a real third field and only ever takes "trigger" or "channel" -- verified over
# every decision on disk. The property worth asserting is not the key set but that no VALUE
# is an identifier, so widen the allowlist and check the values directly.
_ent = [x for r in A.build_anatomy()["records"] for l in r["gates"].values() for x in l]
ck("gate entries carry only known keys",
   all(set(x) <= {"gate", "pass", "via"} for x in _ent),
   str({k for x in _ent for k in x}))
ck("no gate value is an identifier",
   not any(_re.search(r"![0-9a-f]{6}|[0-9a-f]{16}", str(v))
           for x in _ent for v in x.values()))
ck("no node id in the anatomy payload",
   "!" not in json.dumps([r["gates"] for r in A.build_anatomy()["records"]]))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL: print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
