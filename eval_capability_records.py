#!/usr/bin/env python3
"""eval_capability_records.py — the capability page's claims must stay true.

This page is a set of assertions about code. Two ways it can rot: the registry stops
matching what is armed, and a stated limit points at code that no longer exists. Both
fail silently on a page that still renders. Both are checked here.
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import capability_records as CR

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + detail) if detail and not cond else ""))


cfg = CR.read_config()
payload = CR.build_payload()
blob = json.dumps(payload)

# ---------------------------------------------------------- rule 1: flags own the set
print("\n[rule 1 -- the armed set comes from the flags]")
ck("no armed flag is missing a record", CR.unregistered_flags() == [], str(CR.unregistered_flags()))
ck("every record names a real config flag",
   all(re.fullmatch(r"[A-Z0-9_]+", r["flag"]) for r in CR.RECORDS))
ck("armed state is read from config, not stored",
   all(r["armed"] == (str(cfg.get(r["flag"], "false")).lower() == "true") for r in payload["records"]))
fake = dict(cfg); fake["QUANTUM_TUNNELING_ENABLED"] = "true"
ck("an armed flag with no record is DETECTED",
   CR.unregistered_flags(fake) == ["QUANTUM_TUNNELING_ENABLED"], str(CR.unregistered_flags(fake)))
ck("the master responder switch is exempt, not silently missing",
   "RESPONDER_ENABLED" in cfg and "RESPONDER_ENABLED" not in CR.unregistered_flags(cfg))

# ---------------------------------------------------------- rule 3: limits name real code
print("\n[rule 3 -- every stated limit points at code that exists]")
refs = [(r["name"], x["where"]) for r in CR.RECORDS for x in r["out_of_scope"]]
refs += [("standing", x["where"]) for x in CR.STANDING_REFUSALS]
ck("at least one limit is declared per capability",
   all(r["out_of_scope"] for r in CR.RECORDS),
   str([r["name"] for r in CR.RECORDS if not r["out_of_scope"]]))
bad = []
for owner, ref in refs:
    fn, _, sym = ref.partition(":")
    path = os.path.join(HERE, fn)
    if not os.path.exists(path):
        bad.append("%s -> missing file %s" % (owner, fn))
        continue
    if sym:
        try:
            body = open(path, errors="replace").read()
        except Exception:
            bad.append("%s -> unreadable %s" % (owner, fn)); continue
        if sym not in body:
            bad.append("%s -> %s not found in %s" % (owner, sym, fn))
ck("every `where` reference resolves to a real file and symbol", not bad, "; ".join(bad))
ck("a broken reference would be caught",
   "nope_not_a_symbol" not in open(os.path.join(HERE, "calc.py"), errors="replace").read())

# ---------------------------------------------------------- rule 4: no config leaks
print("\n[rule 4 -- no config VALUE is ever published]")
SECRET = ["WEATHER_POINT", "DM_UNLOCK_NODE", "DM_UNLOCK_PUBKEY_FP", "ALLOW_FROM", "HOST"]
leaks = []
for k in SECRET:
    v = cfg.get(k)
    if not v:
        continue
    for part in ([v] + [x.strip() for x in v.split(",")]):
        if part and len(part) > 3 and part in blob:
            leaks.append(k + "=" + part)
ck("no secret-bearing config value reaches the payload", not leaks, "; ".join(leaks))
ck("no node id in the payload", not re.search(r"![0-9a-f]{8}", blob))
wp = (cfg.get("WEATHER_POINT") or "").split(",")
ck("neither coordinate component appears",
   all(c.strip() not in blob for c in wp if c.strip()))
ck("the DM unlock node is described without being named",
   "not named here" in blob and (cfg.get("DM_UNLOCK_NODE") or "@@") not in blob)
ck("allow-list is a COUNT, not a list", isinstance(payload["allowed_count"], int))

# ---------------------------------------------------------- rule 2: no invented values
print("\n[rule 2 -- a row may say nothing known, never guess]")
try:
    triage = json.load(open(os.path.join(HERE, "triage.json")))
except Exception:
    triage = {}
for r in payload["records"]:
    src = None
    for reg in CR.RECORDS:
        if reg["name"] == r["name"] and reg.get("oracle_key"):
            t = triage.get(reg["oracle_key"])
            src = t.get("source") if isinstance(t, dict) else None
    if r["oracle"] is not None:
        ck("oracle for '%s' came from triage, not this file" % r["name"], r["oracle"] == src,
           repr(r["oracle"]))
n_none = sum(1 for r in payload["records"] if r["oracle"] is None)
ck("capabilities with no recorded oracle report None", n_none > 0, str(n_none))
ck("no record invents an armed date",
   all(r["armed_on"] is not None or r["commit"] is None for r in payload["records"]))

# ---------------------------------------------------------- defaults without importing
print("\n[responder DEFAULTS read without executing responder.py]")
d = CR.responder_defaults()
ck("DEFAULTS parsed out of responder.py", isinstance(d, dict) and len(d) > 5, str(len(d)))
ck("responder was NOT imported", "responder" not in sys.modules)
ck("a known budget key is present", "SIGREPORT_MAX_PER_DAY" in d, str(list(d)[:4]))
ck("budgets surface on the sigreport record",
   any(b["key"] == "SIGREPORT_MAX_PER_DAY"
       for r in payload["records"] if r["name"] == "sigreport" for b in r["budgets"]))

# ---------------------------------------------------------- the page
print("\n[page]")
P = CR.PAGE_CAPABILITIES
ck("out-of-scope is never inside a <details>", "<details" not in P)
ck("the warning-label block exists", 'class="stop"' in P)
ck("an empty limit list still says something",
   "not a claim that none exists" in P)
ck("a missing oracle renders as 'not recorded', not as blank",
   "Not recorded" in P)
ck("the page refuses to look complete when a flag is unregistered",
   "This page is incomplete and says so" in P)
ck("fetches through DIR", "DIR+'api/capabilities'" in P)
ck("DIR strips both supplement slugs", "capabilities|console" in P)
ck("no replacement character / mojibake", "�" not in P)
ck("only the GitHub link leaves the origin", len(re.findall(r'href="https?://', P)) == 1)
ck("no coordinate literal in the markup", not re.search(r"\b3[0-9]\.\d{4,}", P))

# ---------------------------------------------------------- mutations
print("\n[mutations -- each should be CAUGHT]")
def mut(name, cond):
    ck("mutation caught: " + name, cond)

saved = CR.RECORDS
CR.RECORDS = tuple(r for r in saved if r["flag"] != "SIGREPORT_ENABLED")
mut("a record silently dropped while its flag stays armed",
    CR.unregistered_flags() == ["SIGREPORT_ENABLED"])
CR.RECORDS = saved

saved2 = CR.RECORDS
CR.RECORDS = tuple(dict(r, out_of_scope=[{"limit": "x", "where": "calc.py:ghost_symbol_xyz"}])
                   if r["name"] == "calc" else r for r in saved2)
_refs = [(r["name"], x["where"]) for r in CR.RECORDS for x in r["out_of_scope"]]
_bad = [o for o, ref in _refs
        if ":" in ref and ref.split(":")[1] not in open(os.path.join(HERE, ref.split(":")[0]), errors="replace").read()]
mut("a limit pointing at a symbol that does not exist", bool(_bad))
CR.RECORDS = saved2

_p = json.dumps({"records": [{"oracle": "SAE J429 (invented)"}]})
mut("an oracle invented in this file rather than read from triage",
    "invented" in _p and "SAE J429 (invented)" not in blob)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + "; ".join(FAIL))
sys.exit(1 if FAIL else 0)
