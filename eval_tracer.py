#!/usr/bin/env python3
"""eval_tracer.py — the probe campaign asks the right node, and never too often.

THE FAILURES THIS GUARDS, in the order they would hurt.

  1. SPENDING AIRTIME NOBODY AGREED TO. This is the only loop in cal-mesh that initiates
     transmission on its own schedule, on a shared channel, about nodes that never asked us
     anything. Every budget and exclusion below is that concern.
  2. A CAMPAIGN THAT SILENTLY NARROWS. A ranker that quietly drops most of the mesh would
     probe the same six nodes forever and report a response rate that describes its own
     habits. Every exclusion is therefore explained and counted, and the exclusions are
     tested by instance rather than asserted as an invariant.
  3. LYING ABOUT WHO ANSWERED. The responder map predicts our own probes, so an overheard
     third-party response must not be counted as evidence that a node answers US.

Run:  python3 eval_tracer.py               (exit 0 = pass)
      python3 eval_tracer.py --self-test   (also mutate the module; each must be caught)
"""
import importlib.util
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _load(name, path=None):
    spec = importlib.util.spec_from_file_location(name, path or os.path.join(HERE, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tracer = _load("tracer")
failures, checked = [], 0
NOW = 1_787_500_000.0
OURS = "!cccccccc"


def ck(cond, msg):
    global checked
    checked += 1
    if not cond:
        failures.append(msg)


def cfg(**kw):
    c = dict(tracer.DEFAULTS)
    c["TRACER_ENABLED"] = "true"
    c.update({k: str(v) for k, v in kw.items()})
    return c


def node(nid, heard_ago=60, hops=2, short=None):
    return {"id": nid, "short": short or nid[-4:], "hops": hops, "lastHeard": NOW - heard_ago}


def state(probes):
    return {"probes": [{"dest": d, "ts": t} for d, t in probes]}


def route(traced, ts, requester=OURS, kind="response"):
    from datetime import datetime, timezone
    return {"kind": kind, "requester": requester, "traced": traced,
            "ts": datetime.fromtimestamp(ts, timezone.utc).isoformat()}


A, B, C = "!aaaaaaaa", "!bbbbbbbb", "!deadbeef"

# --- the gate ---------------------------------------------------------------------------
ck(tracer.DEFAULTS["TRACER_ENABLED"] == "false", "TRACER_ENABLED must default to false")
off = tracer.plan([node(A)], {}, [], OURS, dict(tracer.DEFAULTS), now=NOW)
ck(off[0] is None and off[1] == "tracer_disabled", f"disabled must refuse: {off[1]}")
ck(off[2] == [] and off[3] == [], "a disabled loop must not even rank")

# --- budget, read from the bridge's own record -------------------------------------------
# Derived from the bridge's probe list on purpose: a counter of our own would be a second
# ledger for one quantity, and the two would drift the first time a probe was queued but not
# sent, or sent but not recorded.
day = NOW - (NOW % 86400)
full = state([(A, day + i) for i in range(12)])
ck(tracer.spent_today(full, NOW) == 12, "today's probes counted from the bridge record")
ck(tracer.spent_today(state([(A, day - 10)]), NOW) == 0, "yesterday's probes do not count")
p = tracer.plan([node(B)], full, [], OURS, cfg(), now=NOW)
ck(p[0] is None and p[1].startswith("budget_spent"), f"cap must hold: {p[1]}")
ck(tracer.plan([node(B)], full, [], OURS, cfg(TRACER_MAX_PER_DAY=20), now=NOW)[0] is not None,
   "a raised cap releases it")

# --- exclusions, each tested by instance --------------------------------------------------
# A universal-sounding "we exclude the unreachable" covers only the cases actually written
# under it, and they are usually the ones already right.
CASES = [
    ("stale", node(A, heard_ago=9999), "a node not heard in the window"),
    ("never_heard", {"id": A, "short": "x", "hops": 1}, "a node with no lastHeard at all"),
    ("too_far", node(A, hops=6), "a node beyond the probe's reach"),
]
for why, n, desc in CASES:
    t, reason, ranked, skipped = tracer.plan([n], {}, [], OURS, cfg(), now=NOW)
    ck(t is None, f"{desc} must not be probed")
    ck(any(s["why"] == why and s["node"] == A for s in skipped),
       f"{desc} must be EXPLAINED as {why!r}, got {skipped}")
# lastHeard as a bool is not a timestamp.
ck(tracer.plan([{"id": A, "lastHeard": True, "hops": 1}], {}, [], OURS, cfg(), now=NOW)[0] is None,
   "a bool lastHeard is not a measurement")
# TOO FAR IS AN EXCLUSION, NOT A REFUSAL. A node that cannot be reached has not declined; if
# it were counted silent it would enter the responder map as a routing fact wearing a
# behavioural label.
t, _, _, sk = tracer.plan([node(A, hops=9)], state([(A, NOW - 100)]), [], OURS, cfg(), now=NOW)
ck(any(s["why"] == "too_far" for s in sk), "unreachable is excluded before it is judged")
# Cal never probes himself.
ck(tracer.plan([node(OURS)], {}, [], OURS, cfg(), now=NOW)[0] is None, "never probe ourselves")
# A malformed id is never queued.
for bad in ("!nothex01", "aaaaaaa1", "!aaa", None, 42, "!aaaaaaaaextra"):
    ck(tracer.plan([{"id": bad, "lastHeard": NOW - 10, "hops": 1}], {}, [], OURS, cfg(),
                   now=NOW)[0] is None, f"malformed id {bad!r} must never be a target")

# --- priority --------------------------------------------------------------------------
nodes = [node(A), node(B), node(C)]
hist = state([(B, NOW - 5000), (C, NOW - 100)])
t, _, ranked, _ = tracer.plan(nodes, hist, [], OURS, cfg(), now=NOW)
ck(t["node"] == A, f"an unasked node comes first — discovery is the point; got {t['node']}")
order = [r["node"] for r in ranked]
ck(order.index(B) < order.index(C), "among asked nodes, the longest-waiting goes first")
# A known responder whose probe record has been TRIMMED is not a discovery candidate.
routes_old = [route(A, NOW - 99999)]
t2, _, ranked2, _ = tracer.plan(nodes, hist, routes_old, OURS, cfg(), now=NOW)
ck(t2["node"] != A or t2["tier"] == 1,
   "a node we know answered is not 'never probed' just because the record was trimmed")
ck([r for r in ranked2 if r["node"] == A][0]["tier"] == 1, "trimmed responder ranks as tier 1")

# --- freshness and backoff ---------------------------------------------------------------
fresh = [route(A, NOW - 60)]
t3, _, _, sk3 = tracer.plan([node(A)], state([(A, NOW - 120)]), fresh, OURS, cfg(), now=NOW)
ck(t3 is None and any(s["why"] == "path_fresh" for s in sk3),
   "a path we just measured is not re-measured")
ck(tracer.plan([node(A)], state([(A, NOW - 120)]), [route(A, NOW - 99999)], OURS, cfg(),
               now=NOW)[0] is not None, "a stale path is refreshed")
# Three silences and it stops asking — then the retry window lets it back in.
sil = state([(A, NOW - 300), (A, NOW - 200), (A, NOW - 100)])
t4, _, _, sk4 = tracer.plan([node(A)], sil, [], OURS, cfg(), now=NOW)
ck(t4 is None and any(s["why"].startswith("silent_x") for s in sk4),
   f"a node that ignores us is left alone: {sk4}")
old_sil = state([(A, NOW - 9 * 86400), (A, NOW - 9 * 86400), (A, NOW - 9 * 86400)])
ck(tracer.plan([node(A)], old_sil, [], OURS, cfg(), now=NOW)[0] is not None,
   "a long-silent node rejoins the population — it may simply have been switched off")

# --- the responder map -------------------------------------------------------------------
# It predicts OUR probes, so somebody else's traceroute is not evidence about us.
theirs = [route(A, NOW - 100, requester="!ffffffff")]
ck(tracer.responders(theirs, OURS) == {}, "a third party's response is not our evidence")
ck(tracer.responders([route(A, NOW - 100, kind="request")], OURS) == {},
   "a request is not a response")
ck(set(tracer.responders([route(A, NOW - 100)], OURS)) == {A}, "our own response counts")
ck(tracer.responders([{"kind": "response", "requester": OURS, "traced": "bogus",
                       "ts": "x"}], OURS) == {}, "a malformed row is dropped, not guessed")

# --- the queue entry ---------------------------------------------------------------------
d = tempfile.mkdtemp()
path = tracer.enqueue(d, {"node": A, "why": "never probed"}, now=NOW)
files = os.listdir(d)
ck(len(files) == 1, f"exactly one queue entry per run, got {files}")
ck(not any(f.endswith(".tmp") for f in files), "the write is atomic — no .tmp left behind")
ck(json.load(open(path))["dest"] == A, "the entry names the node")
ck(A.lstrip("!") in os.path.basename(path), "the filename is legible in the queue directory")

# --- the queue depth cap: the second half of the budget --------------------------------
# spent_today() counts probes SENT. While the bridge holds for a busy channel, nothing is
# spent and entries pile up, so the daily cap alone would let a backlog build that all goes
# out at one per measurement window once the air clears.
qd = tempfile.mkdtemp()
ck(tracer.pending(qd) == (0, set()), "an empty queue is empty")
ck(tracer.pending(os.path.join(qd, "nope"))[0] == 0, "a missing queue directory is not a crash")
tracer.enqueue(qd, {"node": A, "why": "x"}, now=NOW)
ck(tracer.pending(qd)[0] == 1 and tracer.pending(qd)[1] == {A}, "one entry, one node")
# A NODE ALREADY WAITING IS NOT A CANDIDATE. The ranker cannot see the queue by itself.
t5, r5, ranked5, sk5 = tracer.plan([node(A), node(B)], {}, [], OURS, cfg(), now=NOW, queue_dir=qd)
ck(t5 and t5["node"] == B, f"a queued node is not re-queued; got {t5 and t5['node']}")
ck(any(s2["why"] == "already_queued" and s2["node"] == A for s2 in sk5),
   "and the exclusion is explained")
tracer.enqueue(qd, {"node": B, "why": "x"}, now=NOW + 1)
t6, r6, _, _ = tracer.plan([node(A), node(B), node(C)], {}, [], OURS, cfg(), now=NOW,
                           queue_dir=qd)
ck(t6 is None and r6.startswith("queue_backed_up"), f"depth cap must hold: {r6}")
ck(tracer.plan([node(C)], {}, [], OURS, cfg(TRACER_MAX_QUEUED=9), now=NOW,
               queue_dir=qd)[0] is not None, "a raised depth cap releases it")
# An unreadable entry still occupies the queue.
with open(os.path.join(qd, "junk"), "w") as fh:
    fh.write("not json")
ck(tracer.pending(qd)[0] == 3, "a malformed entry still counts toward depth")
# Without a queue_dir the cap is simply not applied — callers that do not pass one are not
# silently told the queue is full.
ck(tracer.plan([node(A)], {}, [], OURS, cfg(), now=NOW)[0] is not None,
   "no queue_dir means no depth claim")

if "--self-test" in sys.argv:
    print("\n--- self-test: each mutation must be CAUGHT ---")
    src = open(os.path.join(HERE, "tracer.py"), encoding="utf-8").read()
    MUTATIONS = [
        ("default becomes enabled", '"TRACER_ENABLED": "false"', '"TRACER_ENABLED": "true"'),
        ("daily budget ignored", "if used >= cap:", "if False:"),
        ("stale nodes probed anyway", "if age > heard_within:", "if False:"),
        ("unreachable counted as silent", 'skip(nid, "too_far")', "pass"),
        ("fresh paths re-probed", "if answered is not None and now - answered < refresh:",
         "if False:"),
        ("silent nodes hammered forever", "if silences >= silent_after:", "if False:"),
        ("third-party responses counted as ours",
         'if r.get("kind") != "response" or r.get("requester") != ours:',
         'if r.get("kind") != "response":'),
        ("discovery priority inverted", 'tier, why = 0, "never probed"',
         'tier, why = 9, "never probed"'),
        ("queue depth cap ignored", "if queued_n >= depth:", "if False:"),
        ("a queued node is queued again",
         'ranked = [r for r in ranked if r["node"] not in queued_ids]', "pass"),
        ("queue write no longer atomic", "os.replace(tmp, path)",
         "os.rename(tmp, path) if False else __import__('shutil').copyfile(tmp, path)"),
    ]
    for name, orig, mut in MUTATIONS:
        if orig not in src:
            print(f"  ?? {name}: anchor not found — this control is stale")
            failures.append(f"self-test anchor missing: {name}")
            continue
        md = tempfile.mkdtemp()
        mp = os.path.join(md, "tracer.py")
        with open(mp, "w", encoding="utf-8") as f:
            f.write(src.replace(orig, mut, 1))
        try:
            m = _load("tracer_mut", mp)
        except Exception as e:                                 # noqa: BLE001
            print(f"  ok {name}: CAUGHT (threw: {e!r})")
            continue
        caught = False
        try:
            if m.DEFAULTS["TRACER_ENABLED"] != "false":
                caught = True
            if m.plan([node(A)], {}, [], OURS, dict(m.DEFAULTS), now=NOW)[0] is not None:
                caught = True
            if m.plan([node(B)], full, [], OURS, cfg(), now=NOW)[0] is not None:
                caught = True
            if m.plan([node(A, heard_ago=9999)], {}, [], OURS, cfg(), now=NOW)[0] is not None:
                caught = True
            if not any(s["why"] == "too_far"
                       for s in m.plan([node(A, hops=9)], {}, [], OURS, cfg(), now=NOW)[3]):
                caught = True
            if m.plan([node(A)], state([(A, NOW - 120)]), fresh, OURS, cfg(), now=NOW)[0]:
                caught = True
            if m.plan([node(A)], sil, [], OURS, cfg(), now=NOW)[0] is not None:
                caught = True
            if m.responders(theirs, OURS) != {}:
                caught = True
            tt = m.plan(nodes, hist, [], OURS, cfg(), now=NOW)[0]
            if not tt or tt["node"] != A:
                caught = True
            d2 = tempfile.mkdtemp()
            m.enqueue(d2, {"node": A, "why": "x"}, now=NOW)
            if any(f.endswith(".tmp") for f in os.listdir(d2)):
                caught = True
            m.enqueue(d2, {"node": B, "why": "x"}, now=NOW + 1)
            if m.plan([node(C)], {}, [], OURS, cfg(), now=NOW, queue_dir=d2)[0] is not None:
                caught = True
            d3 = tempfile.mkdtemp()
            m.enqueue(d3, {"node": A, "why": "x"}, now=NOW)
            tq = m.plan([node(A), node(B)], {}, [], OURS, cfg(), now=NOW, queue_dir=d3)[0]
            if not tq or tq["node"] != B:
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
