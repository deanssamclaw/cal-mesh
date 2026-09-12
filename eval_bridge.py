#!/usr/bin/env python3
"""eval_bridge.py — the grade -> work bridge, which is what makes drafts a loop instead of a log.

Before this existed, four separate pieces of code could each handle a human verdict -- drafts.py
could record one, the page could render one, export_judgment.py could project one -- and not one
of them turned a verdict into WORK. Two reviewers independently called the feature "a log, not a
learning loop" for exactly that reason. learn.grade_queue() is the join that closes it.

THE INVARIANT THIS SUITE EXISTS FOR: a grade is an OPINION, gap-ledger.json is a MEASUREMENT of
what happened on the radio, and the two are joined only at read time. If grading ever appended to
the ledger, the ledger would stop being a record of what the mesh did and start being a record of
what somebody thought about it -- and nothing downstream could tell the difference afterwards.

Run:  python3 eval_bridge.py     (exit 0 = pass; mutations included)
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILS = []


def expect(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def load(path=None):
    spec = importlib.util.spec_from_file_location(
        "learn_under_test", path or os.path.join(HERE, "learn.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DRAFTS = [
    {"draft_id": "A", "text": "Cal whats the temperature?", "draft": "Clear, 75F",
     "via": "model+weather"},
    {"draft_id": "B", "text": "Cal, hows the radio holding up?", "draft": "All good here",
     "via": "model"},
    {"draft_id": "C", "text": "Cal, latency test", "draft": "Copy: RSSI -31", "via": "sigreport"},
]
GRADES = {
    "A": {"verdict": "doer", "better": "75F clear, south wind 9", "by": "page",
          "ts": "2026-09-08T12:00:00+00:00"},
    "B": {"verdict": "wrong", "better": "Solid, no drops today", "by": "page",
          "ts": "2026-09-08T12:01:00+00:00"},
    "C": {"verdict": "good", "by": "page", "ts": "2026-09-08T12:02:00+00:00"},
}


def sandbox(m, grades=None, drafts=None):
    d = tempfile.mkdtemp(prefix="evalbridge-")
    m.GRADES = os.path.join(d, "draft-grades.json")
    m.DRAFTS = os.path.join(d, "drafts.jsonl")
    m.LEDGER_JSON = os.path.join(d, "gap-ledger.json")
    m.TRIAGE = os.path.join(d, "triage.json")
    with open(m.DRAFTS, "w", encoding="utf-8") as fh:
        for r in (drafts if drafts is not None else DRAFTS):
            fh.write(json.dumps(r) + "\n")
    json.dump(grades if grades is not None else GRADES,
              open(m.GRADES, "w", encoding="utf-8"))
    json.dump({"totals": {}, "clusters": {}, "schema": m.SCHEMA},
              open(m.LEDGER_JSON, "w", encoding="utf-8"))
    return d


mod = load()
BOX = sandbox(mod)

print("a verdict becomes work, and 'good' is not work")
q = mod.grade_queue({})
expect("two of three grades are work", len(q) == 2, [g["verdict"] for g in q])
expect("'good' is excluded", all(g["verdict"] != "good" for g in q))
expect("'doer' is included", any(g["verdict"] == "doer" for g in q))
expect("'wrong' is included", any(g["verdict"] == "wrong" for g in q))

print("\nthe queue speaks the distiller's language")
# If a grade keyed its work differently from the distiller, the two would never meet: the same
# ask would sit in the queue twice under two spellings and triaging one would not clear the
# other. Same normalize(), same namespace, one adjudication.
_a = [g for g in q if g["draft_id"] == "A"][0]
expect("the item is keyed by the distiller's own normalize()",
       _a["key"] == mod.normalize("Cal whats the temperature?"), _a["key"])
expect("it carries the corrected reply", _a["better"] == "75F clear, south wind 9", _a["better"])
expect("and what Cal actually said", _a["draft"] == "Clear, 75F", _a["draft"])
expect("and which arm said it", _a["via"] == "model+weather", _a["via"])

print("\nan item leaves the queue when the ask is TRIAGED, not when it is regraded")
_tr = {mod.normalize("Cal whats the temperature?"): {"oracle": "derivable", "armed": "2026-09-08"}}
q2 = mod.grade_queue(_tr)
expect("the triaged ask is marked done",
       [g for g in q2 if g["draft_id"] == "A"][0]["triaged"] is True)
expect("the untriaged one is not",
       [g for g in q2 if g["draft_id"] == "B"][0]["triaged"] is False)
expect("both are still listed, so the record of the verdict survives", len(q2) == 2)

print("\nan opinion never rewrites the measurement")
_before = open(mod.LEDGER_JSON, encoding="utf-8").read()
mod.grade_queue({})
mod.grade_queue(_tr)
expect("gap-ledger.json is untouched by reading grades",
       open(mod.LEDGER_JSON, encoding="utf-8").read() == _before)
expect("and no triage verdict is invented",
       not os.path.exists(mod.TRIAGE), mod.TRIAGE)

print("\nit degrades quietly rather than taking the loop down with it")
_d2 = sandbox(mod, grades={})
expect("no grades yet is an empty queue, not an error", mod.grade_queue({}) == [])
open(mod.GRADES, "w", encoding="utf-8").write("{ this is not json")
expect("a corrupt grades file is an empty queue", mod.grade_queue({}) == [])
json.dump(GRADES, open(mod.GRADES, "w", encoding="utf-8"))
os.remove(mod.DRAFTS)
_q3 = mod.grade_queue({})
expect("a grade whose draft is gone still counts as work", len(_q3) == 2, len(_q3))
expect("and keys as empty rather than guessing",
       all(g["key"] == "(empty)" for g in _q3), [g["key"] for g in _q3])

# --- mutations --------------------------------------------------------------------------------
print("\nmutations (each must be CAUGHT)")
SRC = open(os.path.join(HERE, "learn.py"), encoding="utf-8").read()
MUTANTS = {
    "'good' starts counting as work": (
        '        if v not in ("doer", "wrong", "harmful"):\n            continue',
        '        if v is None:\n            continue'),
    # Anchor updated 2026-09-12: the call site became cluster_key(), which keeps normalize()
    # for ordinary text and only changes the fallback so symbol-only asks stop sharing one
    # bucket. The mutation's MEANING is unchanged -- raw text instead of the distiller's key.
    "the queue stops speaking the distiller's language": (
        '        key = cluster_key(row.get("text", ""))',
        '        key = row.get("text", "") or "(empty)"'),
    "triaged items stop being marked done": (
        '            "triaged": bool(verdict(tr, key)),',
        '            "triaged": False,'),
}
for name, (old, new) in MUTANTS.items():
    if old not in SRC:
        expect("mutation anchor present: " + name, False, "anchor missing")
        continue
    d = tempfile.mkdtemp(prefix="bridgemut-")
    mp = os.path.join(d, "learn.py")
    open(mp, "w", encoding="utf-8").write(SRC.replace(old, new, 1))
    m2 = load(mp)
    sandbox(m2)
    if name == "'good' starts counting as work":
        caught = len(m2.grade_queue({})) != 2
    elif name == "the queue stops speaking the distiller's language":
        caught = ([g for g in m2.grade_queue({}) if g["draft_id"] == "A"][0]["key"]
                  != m2.normalize("Cal whats the temperature?"))
    else:
        caught = [g for g in m2.grade_queue(_tr) if g["draft_id"] == "A"][0]["triaged"] is not True
    expect("mutation caught: " + name, caught)

shutil.rmtree(BOX, ignore_errors=True)
shutil.rmtree(_d2, ignore_errors=True)

print()
if FAILS:
    print(f"eval_bridge: {len(FAILS)} FAILED — {FAILS}")
    sys.exit(1)
print("eval_bridge: all checks pass")
