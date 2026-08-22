#!/usr/bin/env python3
"""eval_rate.py — what a reply costs, and who pays.

WHY THIS FILE EXISTS
--------------------
Two guardrails, aimed at two different things, and until 2026-08-22 both were billed to every
reply identically:

  * COOLDOWN_S paces AIRTIME. Every reply pays it. A private PSK'd channel does not help here
    and the reasoning is worth stating once: a channel is not a radio. Cal HT and the ~250 other
    nodes share one frequency slot, so encryption changes who may READ a packet, never who must
    WAIT while it transmits.
  * RATE_MAX guards COST and RUNAWAY LOOPS. A deterministic doer reply carries neither — no
    tokens, ~40 characters, and it can only fire on a question that already parsed, so it cannot
    invent a reason to keep talking. It was consuming the same budget as a 22-second model reply.

Measured on the live torque exchange of 2026-08-22: three replies burned three of five slots in
81 seconds, and two of them were 0 ms doer answers. The owner ran dry mid-conversation because
the limiter was charging for the wrong thing.

THE HONEST LIMIT, asserted below rather than argued away: the rate gate runs BEFORE planning, so
it cannot know what a message will cost. Once the window is full of model replies everything is
throttled, doers included. That is the conservative direction, and the alternative — moving the
rate gate below the doer ladder — would let a message reach the doers before anything had
established it was allowed to be answered at all.

Run:  python3 eval_rate.py                (exit 0 = pass)
      python3 eval_rate.py --self-test    also proves the checks can FAIL
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    import responder as R
except ModuleNotFoundError as e:
    print(f"SKIP: {e} — run with the bridge's interpreter "
          f"(~/.local/pipx/venvs/meshtastic/bin/python)")
    sys.exit(0)

A = "!aaaaaaaa"
B = "!bbbbbbbb"

failures, checked = [], 0


def check(label, got, want):
    global checked
    checked += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def cfg(**over):
    c = dict(R.DEFAULTS)
    c.update({"RATE_MAX": "5", "RATE_WINDOW_S": "600", "COOLDOWN_S": "8"})
    c.update(over)
    return c


def st():
    return {"last_reply_ts": 0, "per_sender": {}}


# ---------------------------------------------------------------- a doer reply costs no slot
s = st()
for i in range(20):
    R.charge(s, A, 1000 + i * 10, fixed=True)
check("20 doer replies fill no slots", len(s["per_sender"].get(A, [])), 0)
check("but each one paid the cooldown", s["last_reply_ts"], 1190)
# t=1200 is INSIDE the 600s window, so this proves the slots are empty rather than expired —
# checking at t=2000 would have passed even under the old rule, because every hit had aged out.
check("so the window still admits a model reply", R.rate_ok(cfg(), s, A, 1200)[0], True)

# ------------------------------------------------------------- a model reply costs one slot
s = st()
for i in range(5):
    R.charge(s, A, 1000 + i * 10, fixed=False)
check("5 model replies fill 5 slots", len(s["per_sender"][A]), 5)
check("and the window is now closed", R.rate_ok(cfg(), s, A, 1100)[1], "rate_limited")

# THE LIVE EXCHANGE. clarify (doer) -> answer (doer) -> "thanks" (model). Three replies,
# 81 seconds, and under the old rule it cost three of five slots.
s = st()
R.charge(s, A, 1000, fixed=True)     # "Which grade -- 2, 5 or 8?"
R.charge(s, A, 1026, fixed=True)     # "1/2-13 grade 5: 75 ft-lb dry, 57 lubricated"
R.charge(s, A, 1081, fixed=False)    # "Anytime friend, always happy to help"
check("the real exchange now costs ONE slot", len(s["per_sender"][A]), 1)

# ------------------------------------------------------------------- the cooldown is universal
s = st()
R.charge(s, A, 1000, fixed=True)
check("a doer reply starts the cooldown", R.rate_ok(cfg(), s, A, 1004)[1], "cooldown")
check("and it expires on schedule", R.rate_ok(cfg(), s, A, 1009)[0], True)
s = st()
R.charge(s, A, 1000, fixed=False)
check("a model reply starts it too", R.rate_ok(cfg(), s, A, 1004)[1], "cooldown")
# The cooldown is GLOBAL, not per sender — it paces the radio, and the radio is one radio.
s = st()
R.charge(s, A, 1000, fixed=False)
check("the cooldown applies across senders", R.rate_ok(cfg(), s, B, 1004)[1], "cooldown")

# ------------------------------------------------------------------- the window is per sender
s = st()
for i in range(5):
    R.charge(s, A, 1000 + i * 10, fixed=False)
check("one sender's budget is their own", R.rate_ok(cfg(), s, B, 1100)[0], True)
check("and the other is still spent", R.rate_ok(cfg(), s, A, 1100)[1], "rate_limited")

# --------------------------------------------------------------------------- the window slides
s = st()
for i in range(5):
    R.charge(s, A, 1000 + i, fixed=False)
check("full inside the window", R.rate_ok(cfg(), s, A, 1500)[1], "rate_limited")
check("clear once they age out", R.rate_ok(cfg(), s, A, 1000 + 601)[0], True)

# ------------------------------------------------------- THE HONEST LIMIT, stated as a test
# The gate cannot know what a message will cost, so a full window throttles doers too. Asserted
# so the limitation is a decision on the record rather than a surprise.
s = st()
for i in range(5):
    R.charge(s, A, 1000 + i * 10, fixed=False)
check("a full window throttles even a doer question", R.rate_ok(cfg(), s, A, 1100)[0], False)

# ------------------------------------------------------------------------------ degenerate state
s = {"last_reply_ts": 0}
R.charge(s, A, 1000, fixed=False)
check("a state with no per_sender is repaired, not crashed", s["per_sender"][A], [1000])
s = st()
R.charge(s, None, 1000, fixed=False)
check("an unknown sender is still billed somewhere", len(s["per_sender"]), 1)

# MUTATION-BOUNDARY — the self-test replays everything ABOVE this line under each mutation.
# ------------------------------------------------------------------------------- MUTATIONS
_ORIG_CHARGE = R.charge

MUTATIONS = [
    # The pre-2026-08-22 behaviour: every reply costs a slot.
    ("every reply charges the window (the old rule)",
     lambda: setattr(R, "charge", lambda st, sender, ts, fixed: (
         st.__setitem__("last_reply_ts", ts),
         st.setdefault("per_sender", {}).setdefault(sender, []).append(ts))[0])),
    # The over-correction: doers skip the cooldown too, so a burst of doer answers can key up
    # the radio with no pacing at all.
    ("doer replies skip the cooldown as well",
     lambda: setattr(R, "charge", lambda st, sender, ts, fixed: None if fixed else (
         st.__setitem__("last_reply_ts", ts),
         st.setdefault("per_sender", {}).setdefault(sender, []).append(ts))[0])),
    # Inverted: model replies stop being charged, which removes the guard entirely.
    ("the test is inverted, so only doers are charged",
     lambda: setattr(R, "charge", lambda st, sender, ts, fixed: (
         st.__setitem__("last_reply_ts", ts),
         st.setdefault("per_sender", {}).setdefault(sender, []).append(ts) if fixed else None)[0])),
]


def run_self_test():
    src = open(os.path.join(HERE, "eval_rate.py")).read()
    body = src[src.index("# ---", src.index('"""', 3)):src.index("# MUTATION-BOUNDARY")]
    survived = []
    for name, mutate in MUTATIONS:
        globals()["failures"], globals()["checked"] = [], 0
        mutate()
        try:
            exec(compile(body, "<mutation>", "exec"), globals())
        except Exception:
            pass
        caught = bool(globals()["failures"])
        R.charge = _ORIG_CHARGE
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
