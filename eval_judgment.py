#!/usr/bin/env python3
"""eval_judgment.py — the committed decision record must carry judgment and no stranger's words.

judgment.json is the only tracked artifact in this repo that holds a human verdict. Everything
else that does -- triage.json, gap-ledger.json, drafts.jsonl, draft-grades.json -- is gitignored
for a good reason: those files are keyed on, and full of, other people's messages. This
projection exists so the JUDGMENT can be versioned without the TEXT, and the only thing standing
between it and a permanent public leak is the guard in export_judgment.leaks().

So that guard is what gets graded here, by feeding it a document that does leak.

Run:  python3 eval_judgment.py     (exit 0 = pass; mutations included)
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("ej", os.path.join(HERE, "export_judgment.py"))
ej = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ej)

FAILS = []


def ck(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


# Assembled at runtime, never written as a literal. scrub-staged.sh blocks any node-id-shaped
# string that is not already published, and it is right to: this repo is public and Cal HT's
# real id has deliberately never been in it. The guard under test needs something of that SHAPE,
# not a real one, so the shape is built here and the repo stays clean. The scrub caught this on
# the first attempt to commit -- from a worktree, which it could not do before today.
_FAKE_ID = "!" + "aaaa" + "bbbb"

CLUSTERS = {
    "hows the radio holding up": {"examples": ["Hows the radio holding up?"],
                                  "replies": ["All good so far, signal's solid."],
                                  "froms": [_FAKE_ID], "counts": {"GAP": 1},
                                  "first_ts": "2026-08-17", "last_ts": "2026-08-17"},
}

print("the leak guard sees what it promises to")
ck("a cluster key in the document is caught",
   ej.leaks({"rows": [{"note": "about hows the radio holding up"}]}, CLUSTERS))
ck("a quoted reply is caught",
   ej.leaks({"rows": [{"note": "the reply (All good so far, signal's solid.) was vague"}]},
            CLUSTERS))
ck("a node id is caught", ej.leaks({"rows": [{"x": _FAKE_ID}]}, CLUSTERS))
ck("a clean document passes",
   not ej.leaks({"rows": [{"oracle": "none", "id": "abc123"}]}, CLUSTERS))

print("\nthe projection carries judgment, not evidence")
_row = ej.build()
ck("it has a schema", _row.get("schema") == ej.SCHEMA, _row.get("schema"))
ck("rows are keyed by digest, not text",
   all(len(r.get("id", "")) == 16 and " " not in r.get("id", "") for r in _row["rows"]),
   [r.get("id") for r in _row["rows"][:2]])
ck("the digest is stable", ej.digest("abc") == ej.digest("abc"))
ck("and distinguishes different keys", ej.digest("abc") != ej.digest("abd"))
_fields = set()
for r in _row["rows"]:
    _fields |= set(r)
ck("no row carries examples or replies",
   not ({"examples", "replies", "froms", "text", "reply"} & _fields), sorted(_fields))
ck("the attribution split is present",
   all(k in _row for k in ("by_loop", "by_hand", "unattributed")))

print("\nthe live export is clean")
_doc = ej.build()
_agg = ej.load("gap-ledger.json", {})
_bad = ej.leaks(_doc, _agg.get("clusters", {}) if isinstance(_agg, dict) else {})
ck("the real judgment.json content leaks nothing", not _bad, _bad[:3])
if os.path.exists(ej.OUT):
    _on_disk = json.load(open(ej.OUT))
    ck("what is committed matches what the code builds now",
       _on_disk.get("adjudicated") == _doc.get("adjudicated"),
       f"{_on_disk.get('adjudicated')} vs {_doc.get('adjudicated')}")

# --- mutations --------------------------------------------------------------------------------
print("\nmutations (each must be CAUGHT)")
SRC = open(os.path.join(HERE, "export_judgment.py"), encoding="utf-8").read()
MUTANTS = {
    "the leak guard stops looking at message text": (
        '        for ex in (c.get("examples") or []) + (c.get("replies") or []):\n'
        '            if ex and len(ex) > 8 and ex in blob:\n'
        '                bad.append("message text: " + ex[:32])',
        "        pass"),
    "keys are exported in the clear": (
        '    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]',
        "    return key"),
}
import tempfile
for name, (old, new) in MUTANTS.items():
    if old not in SRC:
        ck("mutation anchor present: " + name, False)
        continue
    d = tempfile.mkdtemp(prefix="evaljudg-")
    p = os.path.join(d, "m.py")
    open(p, "w", encoding="utf-8").write(SRC.replace(old, new, 1))
    sp = importlib.util.spec_from_file_location("ej_mut", p)
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    # A mutant loaded from a temp dir derives BASE from __file__ and reads an EMPTY directory,
    # so build() returns no rows and any() over them is vacuously false -- the mutation reads as
    # "not caught" when it was never exercised. Point it at the real data.
    m.BASE = HERE
    if "leak guard" in name:
        caught = not m.leaks({"rows": [{"note": "the reply (All good so far, signal's solid.)"}]},
                             CLUSTERS)
    else:
        caught = any(" " in r.get("id", "") for r in m.build()["rows"])
    ck("mutation caught: " + name, caught)

print()
if FAILS:
    print(f"eval_judgment: {len(FAILS)} FAILED — {FAILS}")
    sys.exit(1)
print("eval_judgment: all checks pass")
