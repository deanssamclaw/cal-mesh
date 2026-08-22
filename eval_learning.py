#!/usr/bin/env python3
"""eval_learning.py — the learning panel is a public claim about this node, so it gets tested.

WHY THIS FILE EXISTS
--------------------
`build_learning` publishes the distiller's state: what Cal still cannot answer, what got built
for it, and which commit did the building. Three ways to get that wrong, and each one makes the
page say something untrue about the work:

  * Counting corrections only where a CLUSTER exists. The wavelength doer was corrected after
    arming for a question that never appeared as a gap — it was always answered, just answered
    in centimetres with no cut length. Counting per-cluster drops exactly the corrections that
    happened to capabilities nobody had filed a complaint about, which is most of them.
  * Publishing a commit without saying whether it is pushed. A sha that exists only on this Mac
    is work nobody else can see, and "armed, commit abc1234" reads as shipped.
  * Leaking node ids. Clusters carry a `froms` list of every node that asked. The exchange
    streams publish message TEXT deliberately; nothing on this page needs to publish who asked
    what, and a field that is merely unused today is one paste away from being published.

Run:  python3 eval_learning.py                (exit 0 = pass)
      python3 eval_learning.py --self-test    also proves the checks can FAIL
"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dash_mod", os.path.join(HERE, "dashboard.py"))
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)

# Placeholder ids only — this repo is public and scrub-staged.sh enforces it.
NODE_A = "!aaaaaaaa"

failures, checked = [], 0


def check(label, got, want):
    global checked
    checked += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


ARMED_ASK = "torque for a half inch grade 5 bolt"
OPEN_ASK = "how many amps can 14 gauge copper carry"

LEDGER = {"schema": 2, "totals": {"GAP": 4}, "clusters": {
    ARMED_ASK: {"counts": {"GAP": 2}, "dm_counts": {}, "examples": [], "replies": [],
                "froms": [NODE_A], "first_ts": "2026-08-21T18:00:00Z",
                "last_ts": "2026-08-21T18:44:00Z",
                "last_by_bucket": {"GAP": "2026-08-21T18:44:00Z"}, "generic_smell": False},
    OPEN_ASK: {"counts": {"GAP": 2}, "dm_counts": {"GAP": 1}, "examples": [], "replies": [],
               "froms": [NODE_A], "first_ts": "2026-08-21T19:00:00Z",
               "last_ts": "2026-08-21T19:30:00Z",
               "last_by_bucket": {"GAP": "2026-08-21T19:30:00Z"}, "generic_smell": False},
}}

TRIAGE = {
    ARMED_ASK: {"oracle": "derivable", "source": "SAE J429", "armed": "2026-08-21T18:49:00Z",
                "commit": "4cc9920", "pushed": True, "found_by": "manual"},
    # A correction on a doer that never appeared as a cluster at all.
    "doer:wavelength": {"oracle": "derivable", "armed": "2026-08-19T00:00:00Z",
                        "commit": "4259e6c", "pushed": True, "found_by": "manual",
                        "corrections": [{"ts": "2026-08-21T17:00:00Z",
                                         "what": "gated on the literal word 'antenna'"}]},
}

# TWO runs, with DIFFERENT numbers and in file order (oldest first). One record cannot tell
# "newest" from "oldest" apart, and the first version of this fixture had exactly one — while
# build_learning read the wrong end of tail_jsonl, which returns newest-first.
HISTORY = [{"ts": "2026-08-20T06:15:00Z", "new_gaps": 9, "untriaged": 9, "armed": 0,
            "recurred": 0, "corrections": 0, "by_loop": 0, "by_hand": 0},
           {"ts": "2026-08-21T19:22:00Z", "new_gaps": 2, "untriaged": 1, "armed": 1,
            "recurred": 0, "corrections": 1, "by_loop": 0, "by_hand": 2}]


def with_fixtures(ledger=None, triage=None, history=None):
    d = tempfile.mkdtemp()
    lp, tp, hp = (os.path.join(d, n) for n in ("l.json", "t.json", "h.jsonl"))
    json.dump(ledger if ledger is not None else LEDGER, open(lp, "w"))
    json.dump(triage if triage is not None else TRIAGE, open(tp, "w"))
    with open(hp, "w") as f:
        for h in (history if history is not None else HISTORY):
            f.write(json.dumps(h) + "\n")
    dash.LEDGER, dash.TRIAGE, dash.LHISTORY = lp, tp, hp
    return dash.build_learning()


L = with_fixtures()

# ---------------------------------------------------------------- the three sections separate
check("armed section holds only armed", [a["ask"] for a in L["armed"]], [ARMED_ASK])
check("queue holds only untriaged", [q["ask"] for q in L["untriaged"]], [OPEN_ASK])
check("an armed ask is not also in the queue",
      OPEN_ASK in [a["ask"] for a in L["armed"]], False)

# ------------------------------------------------------- corrections survive having no cluster
check("correction with no cluster still published",
      [c["ask"] for c in L["corrections"]], ["doer:wavelength"])
check("its text comes through",
      "antenna" in L["corrections"][0]["what"], True)

# --------------------------------------------------------------- the commit, and its honesty
a = L["armed"][0]
check("commit published", a["commit"], "4cc9920")
check("pushed state published", a["pushed"], True)
unpushed = with_fixtures(triage={ARMED_ASK: dict(TRIAGE[ARMED_ASK], pushed=False)})
check("a local-only commit says so", unpushed["armed"][0]["pushed"], False)
nocommit = with_fixtures(triage={ARMED_ASK: {"oracle": "derivable",
                                             "armed": "2026-08-21T18:49:00Z"}})
check("no commit recorded is not invented", nocommit["armed"][0]["commit"], None)

# -------------------------------------------------------------------- recurrence, on GAP only
L2 = with_fixtures(triage={ARMED_ASK: dict(TRIAGE[ARMED_ASK], armed="2026-08-21T10:00:00Z")})
check("gap after arming flags recurrence", L2["armed"][0]["recurred"], True)
check("gap before arming does not", L["armed"][0]["recurred"], False)

# ------------------------------------------------------ the scoreboard reads the NEWEST run
# tail_jsonl returns newest-FIRST, so the last element of what it hands back is the oldest
# record in the window. Reading it looks right forever while the numbers happen to be equal,
# and goes quietly stale the moment they move.
check("scoreboard reads the newest run", L["scoreboard"]["untriaged"], 1)
check("last_run is the newest timestamp", L["last_run"], "2026-08-21T19:22:00Z")
check("corrections count from the newest run", L["scoreboard"]["corrections"], 1)
check("history is chronological for plotting",
      [h["ts"] for h in L["history"]], ["2026-08-20T06:15:00Z", "2026-08-21T19:22:00Z"])

# --------------------------------------------------------------------------------- PRIVACY
blob = json.dumps(with_fixtures())
check("node ids never reach the page", NODE_A in blob, False)
check("the word froms never reaches the page", "froms" in blob, False)

# ------------------------------------------------------------------------- degenerate inputs
empty = with_fixtures(ledger={}, triage={}, history=[])
check("no ledger: no crash, empty queue", empty["untriaged"], [])
check("no history: scoreboard still renders", empty["scoreboard"]["untriaged"], 0)
check("no history: armed count falls back to the ledger", empty["scoreboard"]["armed"], 0)
# A triage file that is a list, or holds junk values, must not take the panel down.
junk = with_fixtures(triage={ARMED_ASK: "derivable"})
check("a malformed verdict is not a verdict", [q["ask"] for q in junk["untriaged"]],
      [OPEN_ASK, ARMED_ASK])

# MUTATION-BOUNDARY — the self-test replays everything ABOVE this line under each
# mutation. Sliced on this sentinel rather than on a run of dashes: a miscounted dash
# run keeps the file's own exit block, so the first mutation re-runs the whole eval and
# raises SystemExit — not an Exception, so it escapes the handler and kills the harness
# after one mutation while printing a plausible-looking list of CAUGHTs.
# ------------------------------------------------------------------------------- MUTATIONS
MUTATIONS = [
    ("corrections gathered per-cluster (drops doers that never gapped)",
     lambda: setattr(dash, "build_learning", _percluster_corrections)),
    ("pushed state dropped from the payload",
     lambda: setattr(dash, "build_learning", _no_pushed)),
    ("froms carried through to the page",
     lambda: setattr(dash, "build_learning", _leaks_froms)),
    ("scoreboard reads the oldest run in the window",
     lambda: setattr(dash, "build_learning", _oldest_run)),
]

_ORIG = dash.build_learning


def _percluster_corrections(top=6, runs=20):
    out = _ORIG(top, runs)
    tr = dash.read_json(dash.TRIAGE, {}) or {}
    clusters = (dash.read_json(dash.LEDGER, {}) or {}).get("clusters", {})
    out["corrections"] = [{"ask": k, "ts": c.get("ts"), "what": c.get("what")}
                          for k in clusters if isinstance(tr.get(k), dict)
                          for c in tr[k].get("corrections", [])]
    return out


def _no_pushed(top=6, runs=20):
    out = _ORIG(top, runs)
    for a in out["armed"]:
        a["pushed"] = True
    return out


def _leaks_froms(top=6, runs=20):
    out = _ORIG(top, runs)
    clusters = (dash.read_json(dash.LEDGER, {}) or {}).get("clusters", {})
    for q in out["untriaged"]:
        q["froms"] = clusters.get(q["ask"], {}).get("froms", [])
    return out


def _oldest_run(top=6, runs=20):
    out = _ORIG(top, runs)
    h = dash.tail_jsonl(dash.LHISTORY, runs)
    last = h[-1] if h else {}
    out["scoreboard"] = {k: last.get(k, 0) for k in out["scoreboard"]}
    out["last_run"] = last.get("ts")
    return out


def run_self_test():
    src = open(os.path.join(HERE, "eval_learning.py")).read()
    body = src[src.index("L = with_fixtures()"):src.index("# MUTATION-BOUNDARY")]
    survived = []
    for name, mutate in MUTATIONS:
        globals()["failures"], globals()["checked"] = [], 0
        mutate()
        try:
            exec(compile(body, "<mutation>", "exec"), globals())
        except Exception:
            pass
        caught = bool(globals()["failures"])
        dash.build_learning = _ORIG
        print(f"  {'CAUGHT  ' if caught else 'SURVIVED'} {name}"
              + (f" ({len(globals()['failures'])} failed)" if caught else ""))
        if not caught:
            survived.append(name)
    return survived


if __name__ == "__main__":
    if failures:
        print(f"FAIL {len(failures)}/{checked}")
        for f in failures:
            print("  " + f)
        sys.exit(1)
    print(f"PASS {checked} checks")
    if "--self-test" in sys.argv:
        print("mutations (each MUST be caught):")
        s = run_self_test()
        if s:
            print(f"FAIL: {len(s)} mutation(s) survived: {s}")
            sys.exit(1)
        print(f"all {len(MUTATIONS)} mutations caught")
