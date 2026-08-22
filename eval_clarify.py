#!/usr/bin/env python3
"""eval_clarify.py — answering a doer's own question, without grafting answers onto the wrong one.

WHY THIS FILE EXISTS
--------------------
Live, 2026-08-21T22:13 CDT:

    you   Cal whats the torque for a 1/2 inch bolt
    Cal   Which grade -- 2, 5 or 8? Torque changes by more than 2x.     (0 ms, doer)
    you   Cal 5
    Cal   Received loud and clear, what do you need?                    (22.7 s, MODEL)
    you   Cal whats the torque for a 1/2 inch bolt grade 5?
    Cal   1/2-13 grade 5: 75 ft-lb dry, 57 lubricated                   (0 ms, doer)

A doer that asks a question has committed to a two-turn exchange, and only the first turn was
built. The answer to its own question fell through the entire ladder to the model, and the asker
had to retype the whole thing — the exact friction the clarify existed to remove.

THE RISK IS THE OPPOSITE ONE. Splicing text from an earlier message into a later one is how a
confident wrong answer gets built out of two innocent messages. Four things hold it down, and
each has assertions below:

  * The message is tried ALONE FIRST. A complete question always wins over being read as
    somebody's leftover answer.
  * The splice is kept only if it parses to a REAL ANSWER. The doer adjudicates; there is no
    heuristic about what a follow-up "looks like" that could be wrong.
  * The pending clarify is ONE-SHOT and TTL-bounded, so a stale answer cannot be grafted onto a
    question several exchanges later.
  * It is keyed per sender. One node's answer can never complete another node's question.

And one mechanical trap that made the first implementation produce nothing at all: a bare "5" is
not self-describing. The doer names the SLOT it is asking for (`want="grade"`) and the splice
inserts it, because "... bolt 5" parses as no grade while "... bolt grade 5" parses. The trigger
word has to come out of both halves too — it leads every message here, so a plain join buries it
mid-sentence where the grade regex needs digits.

Run:  python3 eval_clarify.py                (exit 0 = pass)
      python3 eval_clarify.py --self-test    also proves the checks can FAIL
"""
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import responder as R
    import calc as C
except ModuleNotFoundError as e:
    print(f"SKIP: {e} — run with the bridge's interpreter "
          f"(~/.local/pipx/venvs/meshtastic/bin/python)")
    sys.exit(0)

A = "!aaaaaaaa"
B = "!bbbbbbbb"
ASK = "Cal whats the torque for a 1/2 inch bolt"

failures, checked = [], 0


def check(label, got, want):
    global checked
    checked += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def cfg(**kw):
    c = dict(R.DEFAULTS)
    c.update({"RESPONDER_ENABLED": "true", "CALC_ENABLED": "true", "TRIGGER_WORD": "cal",
              "CLARIFY_FOLLOWUP_ENABLED": "true"})
    c.update(kw)
    return c


def plan(text, pending=None, **kw):
    return R.plan_response(cfg(**kw), A, text, pending=pending)


# ------------------------------------------------------------------ the exchange that failed
p1 = plan(ASK)
check("turn 1 asks for the grade", "Which grade" in (p1["fixed_reply"] or ""), True)
check("turn 1 records what it asked for", (p1["clarify_pending"] or {}).get("want"), "grade")
check("turn 1 remembers the ORIGINAL, not its own question",
      (p1["clarify_pending"] or {}).get("text"), ASK)

p2 = plan("Cal 5", pending=p1["clarify_pending"])
check("turn 2 answers it", p2["fixed_reply"], "1/2-13 grade 5: 75 ft-lb dry, 57 lubricated")
check("turn 2 is a doer, not the model", p2["mode"], "fixed")
check("turn 2 records where the words came from",
      (p2["calc_meta"] or {}).get("resolved_from", {}).get("text"), ASK)

# The same two turns with the switch off must behave exactly as they did live: no splice.
p2_off = plan("Cal 5", pending=p1["clarify_pending"], CLARIFY_FOLLOWUP_ENABLED="false")
check("disabled: no splice", p2_off["fixed_reply"], None)
check("ships disabled", R.DEFAULTS["CLARIFY_FOLLOWUP_ENABLED"], "false")

# ------------------------------------------------------------- a complete question wins outright
# This is what keeps the feature from hijacking ordinary traffic: a message that answers on its
# own is never reinterpreted as somebody's leftover fragment.
p = plan("Cal 5 mi in km", pending=p1["clarify_pending"])
check("a complete question is answered as itself", p["fixed_reply"], "5 mi = 8.0467 km")
check("and is not spliced", (p["calc_meta"] or {}).get("resolved_from"), None)
p = plan("Cal whats the torque for a 3/8 grade 8 bolt", pending=p1["clarify_pending"])
check("a complete question of the SAME kind wins too",
      "3/8-16 grade 8" in (p["fixed_reply"] or ""), True)

# ------------------------------------------------------- a splice that does not resolve is dropped
# "9" is not a grade. The splice must not be kept, and no number may be aired.
p = plan("Cal 9", pending=p1["clarify_pending"])
check("an answer that does not parse yields no doer reply", p["fixed_reply"], None)
check("and airs no torque figure", "ft-lb" in str(p["fixed_reply"]), False)
# An unrelated follow-up must not be forced into the old question.
p = plan("Cal thanks", pending=p1["clarify_pending"])
check("an unrelated follow-up is not spliced", (p["calc_meta"] or {}).get("resolved_from"), None)

# -------------------------------------------------------------------------- the splice mechanics
sp = R.splice_followup({"text": ASK, "want": "grade"}, "Cal 5", "cal")
check("trigger word gone from both halves", "cal" in sp.lower(), False)
check("named slot inserted before the answer", sp.endswith("grade 5"), True)
check("whitespace collapsed", "  " in sp, False)
# No named slot: the answer is already self-describing and a plain join is right.
sp2 = R.splice_followup({"text": "Cal torque for a grade 5 bolt", "want": None}, "Cal 1/2 inch", "cal")
check("no slot named: plain join", sp2, "torque for a grade 5 bolt 1/2 inch")
check("that join actually parses", "1/2-13 grade 5" in (C.try_answer(sp2)[0] or ""), True)

# --------------------------------------------------------------- one-shot, TTL, and per sender
st = {}
R.put_pending(st, A, {"text": ASK, "want": "grade"})
check("stored under the sender", A in st["clarify"], True)
first = R.take_pending(st, A, 180)
check("first read returns it", (first or {}).get("text"), ASK)
check("second read is empty — ONE SHOT", R.take_pending(st, A, 180), None)

R.put_pending(st, A, {"text": ASK, "want": "grade"})
st["clarify"][A]["ts"] = time.time() - 181
check("expired never returns", R.take_pending(st, A, 180), None)
R.put_pending(st, A, {"text": ASK, "want": "grade"})
st["clarify"][A]["ts"] = time.time() - 179
check("just inside the TTL still returns", (R.take_pending(st, A, 180) or {}).get("text"), ASK)
# A corrupt timestamp must read as ancient, never as now.
R.put_pending(st, A, {"text": ASK, "want": "grade"})
st["clarify"][A]["ts"] = "recently"
check("corrupt timestamp expires", R.take_pending(st, A, 180), None)

R.put_pending(st, A, {"text": ASK, "want": "grade"})
check("another node cannot take it", R.take_pending(st, B, 180), None)
check("and it is still there for its owner", (R.take_pending(st, A, 180) or {}).get("text"), ASK)

# Bounded: one entry per node ever asked something would otherwise grow with the mesh.
st2 = {}
for i in range(40):
    R.put_pending(st2, f"!{i:08x}", {"text": ASK, "want": "grade"})
check("table bounded", len(st2["clarify"]) <= 16, True)

# MUTATION-BOUNDARY — the self-test replays everything ABOVE this line under each mutation.
# ------------------------------------------------------------------------------- MUTATIONS
_ORIG_SPLICE = R.splice_followup
_ORIG_TAKE = R.take_pending

MUTATIONS = [
    ("splice keeps the trigger word in the middle",
     lambda: setattr(R, "splice_followup",
                     lambda pending, followup, trig:
                     (pending.get("text", "") + " " + (pending.get("want") or "")
                      + " " + followup).strip())),
    ("splice drops the named slot (a bare '5' means nothing)",
     lambda: setattr(R, "splice_followup",
                     lambda pending, followup, trig:
                     _ORIG_SPLICE({"text": pending.get("text", ""), "want": None},
                                  followup, trig))),
    ("pending is not consumed — a stale answer can splice later",
     lambda: setattr(R, "take_pending",
                     lambda st, sender, ttl: (st.get("clarify") or {}).get(sender))),
    ("TTL ignored",
     lambda: setattr(R, "take_pending",
                     lambda st, sender, ttl: (st.get("clarify") or {}).pop(sender, None))),
]


def run_self_test():
    src = open(os.path.join(HERE, "eval_clarify.py")).read()
    # Sliced on an explicit sentinel, not on a run of dashes. Counting dashes by eye got it
    # wrong on the first try: the slice kept the file's own exit block, which re-ran the whole
    # eval inside the mutation and then raised SystemExit — which is a BaseException, so the
    # `except Exception` below did not catch it and the harness died after one mutation while
    # reporting a plausible-looking failure list.
    start = src.index("# ---", src.index('"""', 3))
    body = src[start:src.index("# MUTATION-BOUNDARY")]
    survived = []
    for name, mutate in MUTATIONS:
        globals()["failures"], globals()["checked"] = [], 0
        mutate()
        try:
            exec(compile(body, "<mutation>", "exec"), globals())
        except Exception:
            pass
        caught = bool(globals()["failures"])
        R.splice_followup, R.take_pending = _ORIG_SPLICE, _ORIG_TAKE
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
