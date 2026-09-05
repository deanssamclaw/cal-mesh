#!/usr/bin/env python3
"""eval_correlate.py — a decision trace belongs to exactly one message, and the page must never
show it under another.

The dashboard binds each inbound message to the responder's decision about it. That binding used
to be sender + text + nearest timestamp, walked one record at a time, and it had a hole: two
identical messages from one node where only the SECOND was evaluated. The first, scanning for
its nearest unconsumed candidate, would take the second's decision, and the page would render a
complete gate ladder for a message that never got one while the message that did showed "no
trace recorded". Both wrong, silently, with no way for a reader to tell.

Two fixes, and this grades both. The responder now stamps the packet id on every decision, which
is the radio's own identifier and the only exact key available; and the text+time fallback that
still has to serve pre-id records is now globally nearest — closest pair first across the whole
set, rather than first-come-first-served.

The mutants are loaded IN-PROCESS, deliberately. Copying a mutated dashboard.py into a temp
directory and running it there makes `import console` fail, and a mutation "caught" by an
ImportError is a check that proves nothing.

Run:  python3 eval_correlate.py                (exit 0 = pass)
      python3 eval_correlate.py --self-test    also proves the checks can FAIL
"""
import os, sys, json, tempfile, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DASH = os.path.join(HERE, "dashboard.py")
_spec = importlib.util.spec_from_file_location("dash", DASH)
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)

FAILS = []
def ck(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def T(sec):
    return "2026-01-01T00:%02d:%02d+00:00" % (sec // 60, sec % 60)


def msg(t, ident, text="cal test", frm="!aa"):
    return {"ts": T(t), "from": frm, "text": text, "id": ident}


def dec(t, text="cal test", frm="!aa", reply="R", ident=None):
    d = {"ts": T(t), "from": frm, "text": text, "reply": reply, "matched": True}
    if ident is not None:
        d["id"] = ident
    return d


def replies(mod, inbox, decisions):
    """{inbox id: paired reply or None}, which is what the page would actually render."""
    return {r["id"]: (d["reply"] if d else None)
            for r, d in mod._pair_decisions(inbox, decisions)}


# --- the defect, in the shape it actually had -----------------------------------------------
# Pre-id records: the only key is (from, text), and these two are identical under it.
THEFT_IN = [msg(0, 1), msg(50, 2)]
THEFT_DEC = [dec(51, reply="OWNED BY 2")]

print("the theft case (id-less records, the historical shape)")
g = replies(dash, THEFT_IN, THEFT_DEC)
ck("the unevaluated message is given NO trace", g[1] is None, g[1])
ck("the decision lands on the message it was made about", g[2] == "OWNED BY 2", g[2])

print("\nthe exact join, once the responder stamps the id")
g = replies(dash, THEFT_IN, [dict(THEFT_DEC[0], id=2)])
ck("the decision pairs by packet id", g[2] == "OWNED BY 2", g[2])
ck("the other message still gets nothing", g[1] is None, g[1])

print("\nthe id outranks a nearer-in-time neighbour")
# 8 is 1s from the decision, 7 is 3s away -- but the decision says it belongs to 7.
g = replies(dash, [msg(0, 7), msg(2, 8)], [dec(3, reply="SEVEN", ident=7)])
ck("proximity does not override the id", g[8] is None, g[8])
ck("the id's owner keeps it", g[7] == "SEVEN", g[7])

print("\nan id reused by a different node does not cross-pair")
g = [(r["from"], d["reply"] if d else None) for r, d in dash._pair_decisions(
    [msg(0, 5, "hi", "!aa"), msg(1, 5, "hi", "!bb")],
    [dec(1, "hi", "!bb", reply="BEE", ident=5)])]
ck("the other node gets nothing", g[0][1] is None, g)
ck("the sending node gets its own", g[1][1] == "BEE", g)

print("\nthe shape of the result")
out = dash._pair_decisions([msg(0, 1), msg(50, 2)], THEFT_DEC)
ck("one entry per inbound record, in order", [r["id"] for r, _ in out] == [1, 2],
   [r["id"] for r, _ in out])
ck("an unpaired record yields None, not a fabricated stub",
   any(d is None for _, d in out))
ck("a decision outside the window is never claimed",
   replies(dash, [msg(0, 1)], [dec(400)])[1] is None)
ck("mixed id / no-id decisions both pair",
   replies(dash, [msg(0, 1), msg(200, 2)],
           [dec(201, reply="B"), dec(1, reply="A", ident=1)]) == {1: "A", 2: "B"})

print("\nduplicate groups keep their order")
g = replies(dash, [msg(0, 1), msg(10, 2), msg(20, 3)],
            [dec(1, reply="one"), dec(11, reply="two"), dec(21, reply="three")])
ck("i-th message pairs with the i-th decision",
   g == {1: "one", 2: "two", 3: "three"}, g)

print("\nthe responder stamps it")
rsrc = open(os.path.join(HERE, "responder.py")).read()
ck("the packet id is written onto the decision record",
   '"id": rec.get("id")' in rsrc)
ck("it is on the single dict both record_decision sites share",
   rsrc.count('"id": rec.get("id")') == 1)

print("\nthe live corpus, if it is on this box")
inbox_p = os.path.join(HERE, "inbox.jsonl")
dec_p = os.path.join(HERE, "decisions.jsonl")
if not (os.path.exists(inbox_p) and os.path.exists(dec_p)):
    # gitignored operational state; absent in a worktree or a fresh clone
    print("  skip  no inbox.jsonl / decisions.jsonl here")
else:
    inb = [json.loads(l) for l in open(inbox_p) if l.strip()]
    dcs = [json.loads(l) for l in open(dec_p) if l.strip()]
    pairs = dash._pair_decisions(inb, dcs)
    paired = [(r, d) for r, d in pairs if d is not None]
    ck("every decision on disk is consumed", len(paired) == len(dcs),
       f"{len(paired)}/{len(dcs)}")
    ck("no pair exceeds the window",
       all(abs(dash._epoch(r["ts"]) - dash._epoch(d["ts"])) <= 300 for r, d in paired))
    ck("no inbound record is paired twice",
       len({id(d) for _, d in paired}) == len(paired))


# --- mutation: prove the checks above can fail ----------------------------------------------
def load_mutant(edits):
    """Apply one or more source edits and import the result. Several mutations only model a real
    defect if they land at BOTH ends of a key -- breaking the build side alone stops everything
    pairing, which is a different bug that happens to look caught."""
    src = open(DASH).read()
    for old, new in edits:
        if old not in src:
            return None
        src = src.replace(old, new, 1)
    tmp = tempfile.mkdtemp(prefix="evalcorr-")
    p = os.path.join(tmp, "dash_mut.py")
    open(p, "w").write(src)
    spec = importlib.util.spec_from_file_location("dash_mut", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)          # sys.path still has HERE, so siblings import
    return m


MUTANTS = {
    # the original defect, restored: per-record greedy instead of globally nearest
    "pairing walks record-by-record again": (
        [("    edges.sort(key=lambda e: (e[0], e[1], e[2]))",
          "    edges.sort(key=lambda e: (e[1], e[0], e[2]))")],
        lambda m: replies(m, THEFT_IN, THEFT_DEC)[1] is not None),
    # the id is collected but never used to join
    "the packet id is ignored": (
        [('            by_id.setdefault((d.get("from"), did), []).append(d)',
          "            pass")],
        lambda m: replies(m, [msg(0, 7), msg(2, 8)],
                          [dec(3, reply="SEVEN", ident=7)])[8] is not None),
    # the sender drops out of the id key at BOTH ends, so two nodes sharing an id cross-pair
    "the id key forgets the sender": (
        [('            by_id.setdefault((d.get("from"), did), []).append(d)',
          "            by_id.setdefault(did, []).append(d)"),
         ('            for d in by_id.get((r.get("from"), rid), ()):',
          "            for d in by_id.get(rid, ()):")],
        lambda m: [(r["from"], d["reply"] if d else None) for r, d in m._pair_decisions(
            [msg(0, 5, "hi", "!aa"), msg(1, 5, "hi", "!bb")],
            [dec(1, "hi", "!bb", reply="BEE", ident=5)])][0][1] is not None),
    # the window stops bounding anything
    "the time window is unbounded": (
        [("            if dt <= window:", "            if True:")],
        lambda m: replies(m, [msg(0, 1)], [dec(400)])[1] is not None),
}

print("\nmutations (in-process; an ImportError must not stand in for a catch)")
for name, (edits, probe) in MUTANTS.items():
    m = load_mutant(edits)
    if m is None:
        ck(f"mutation anchor present: {name}", False, "anchor missing")
        continue
    try:
        caught = probe(m)
    except Exception as e:
        caught, name = False, name + f" (probe raised {e!r})"
    ck(f"mutation caught: {name}", caught)

print()
if FAILS:
    print(f"eval_correlate: {len(FAILS)} FAILED — {FAILS}")
    sys.exit(1)
print("eval_correlate: all checks pass")
