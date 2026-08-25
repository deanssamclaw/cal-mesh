#!/usr/bin/env python3
"""Runtime eval for build_decision_stats() — it EXECUTES the aggregator over fixture records.

WHY THIS FILE EXISTS
--------------------
`/api/stats` is a public endpoint and nothing on any page renders it, which is exactly how it
carried a wrong number for weeks without anyone seeing it. The guard was `if ms is not None`,
so a reply written by code — which logs `gen_ms: 0`, meaning NO MODEL RAN — was averaged into
model latency as an infinitely fast model call. Measured on the live log: 11,499 ms published
against a true 13,985 ms, 18% low, and drifting further with every doer armed.

The failure mode is the one this repo keeps meeting: a number that quietly asserts a mechanism
it cannot stand behind. A zero is a measurement; "no model ran" is an absence; averaging the
second into the first makes the page look faster the more work it does without a model.

Run:  python3 eval_stats.py              (exit 0 = pass)
      python3 eval_stats.py --self-test  also proves the checks can FAIL
"""
import importlib.util
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def load(src=None):
    """Import dashboard.py (optionally a mutated copy) as a module.

    The mutated copy MUST be written next to dashboard.py: the module resolves paths relative
    to its own file. It is the caller's job to remove it, and the caller has to be able to do
    that even when exec_module raises -- an earlier version returned the path only on success,
    so a mutation that failed to import left a full copy of dashboard.py in the repo, where
    `git add -A` would have swept it in."""
    path = os.path.join(HERE, "dashboard.py")
    if src is not None:
        fh = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=HERE,
                                         prefix="ZZ_eval_stats_mutant_")
        fh.write(src)
        fh.close()
        path = fh.name
    spec = importlib.util.spec_from_file_location("dash_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, (path if src is not None else None)


# Records shaped as responder.py writes them. The two 0 ms rows are the whole point: they are
# real shapes from the live log, not invented ones.
FIXTURE = [
    # a model reply: the only population the average is about
    {"ts": "2026-08-25T01:00:00+00:00", "matched": True, "gen_ms": 14000,
     "gen_status": "ok", "model": "claude-haiku-4-5-20251001"},
    {"ts": "2026-08-25T01:01:00+00:00", "matched": True, "gen_ms": 12000,
     "gen_status": "ok", "model": "claude-haiku-4-5-20251001"},
    # answered from code. gen_ms 0 means no model ran; it is not a fast model.
    {"ts": "2026-08-25T01:02:00+00:00", "matched": True, "gen_ms": 0, "gen_status": "fixed_calc"},
    {"ts": "2026-08-25T01:03:00+00:00", "matched": True, "gen_ms": 0,
     "gen_status": "fixed_forecast_refused"},
    # answered from code with no duration logged at all
    {"ts": "2026-08-25T01:04:00+00:00", "matched": True, "gen_ms": None,
     "gen_status": "fixed_greeting_ack"},
    # predates gen_status AND carries no model: unknown provenance, must not be guessed either way
    {"ts": "2026-08-25T01:05:00+00:00", "matched": True, "gen_ms": 9000},
    # a skip still counts as a skip
    {"ts": "2026-08-25T01:06:00+00:00", "matched": False, "reason": "sender_not_allowed"},
]


def stats_for(mod):
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for r in FIXTURE:
        fh.write(json.dumps(r) + "\n")
    fh.close()
    mod.DECISIONS = fh.name
    try:
        return mod.build_decision_stats()
    finally:
        os.unlink(fh.name)


def check(mod):
    """Returns a list of failures. Every one names the defect it descends from."""
    out = []
    day = stats_for(mod)["days"][0]

    if day["avg_gen_ms"] != 13000:
        out.append(f"avg_gen_ms is {day['avg_gen_ms']}, expected 13000 — the average must be "
                   "over the two model replies only. Including the 0 ms code replies gives "
                   "6500 and including the unattributed row gives 11667; both are the "
                   "published-latency defect in a different disguise.")
    if day["min_gen_ms"] == 0:
        out.append("min_gen_ms is 0 — a code reply reached the model distribution. min is the "
                   "first place a non-event shows up and the last place anyone looks.")
    if day["model_replies"] != 2:
        out.append(f"model_replies is {day['model_replies']}, expected 2")
    if day["from_code"] != 3:
        out.append(f"from_code is {day['from_code']}, expected 3 — a reply written by code must "
                   "be COUNTED, not merely excluded, or the page implies it never happened")
    if day["unattributed"] != 1:
        out.append(f"unattributed is {day['unattributed']}, expected 1 — a record that predates "
                   "gen_status and names no model is unknown. Guessing it either way is the "
                   "assertion this endpoint is not entitled to make.")
    if day["replied"] != 6:
        out.append(f"replied is {day['replied']}, expected 6 — the reply count is every reply, "
                   "whoever wrote it; only the LATENCY is model-only")
    if day["skipped"] != 1:
        out.append(f"skipped is {day['skipped']}, expected 1")
    return out


mod, _ = load()
failures = check(mod)

if "--self-test" in sys.argv:
    SRC = open(os.path.join(HERE, "dashboard.py")).read()
    MUTATIONS = [
        # the code exactly as it shipped
        # The mutation must be VALID CODE that restores the old BEHAVIOUR. A first attempt
        # produced an IndentationError and the harness reported CAUGHT — caught by the parser,
        # which proves nothing about the assertions. The trailing `if` is kept so the `elif`
        # chain below it still binds, isolating the averaging defect and nothing else.
        ("every duration is treated as a model duration",
         '                    if _ran_a_model(r):\n'
         '                        if ms is not None:\n'
         '                            e["gen_ms"].append(ms)',
         '                    if ms is not None:\n'
         '                        e["gen_ms"].append(ms)\n'
         '                    if _ran_a_model(r):\n'
         '                        pass'),
        ("an unknown-provenance record is assumed to be a model reply",
         '    return r.get("gen_status") is None and bool(r.get("model"))',
         '    return r.get("gen_status") is None'),
        ("code replies are excluded but not counted",
         '                        e["from_code"] += 1', '                        pass'),
    ]
    print("--- self-test: each mutation must be CAUGHT ---")
    for name, orig, mut in MUTATIONS:
        if SRC.count(orig) != 1:
            failures.append(f"MUTATION ANCHOR MISSING: {name} (found {SRC.count(orig)})")
            continue
        mpath = None
        try:
            # Bind the path BEFORE anything can raise, so `finally` can always remove it.
            mmod, mpath = load(SRC.replace(orig, mut, 1))
            caught = bool(check(mmod))
        except Exception as e:                                    # noqa: BLE001
            # A mutation caught by the PARSER proves nothing about the assertions. Say which
            # kind of catch this was, so a syntactically broken mutation cannot masquerade as
            # a check doing its job -- that happened once and read as green.
            caught = True
            print(f"  ?? {name}: raised {type(e).__name__} — caught by the interpreter, NOT by "
                  f"the checks. Rewrite the mutation as valid code.")
            failures.append(f"MUTATION NOT EXERCISED: {name} raised {type(e).__name__}")
        else:
            print(f"  {'ok' if caught else 'XX'} {name}: {'CAUGHT' if caught else 'SURVIVED'}")
            if not caught:
                failures.append(f"MUTATION SURVIVED: {name} — the checks above cannot detect it")
        finally:
            if mpath:
                os.unlink(mpath)
                for c in (mpath + "c",):
                    if os.path.exists(c):
                        os.unlink(c)

for f in failures:
    print(f"FAIL {f}")
n = 7
print(f"\n{n} assertion(s) over the aggregator; {len(failures)} problem(s)")
sys.exit(1 if failures else 0)
