#!/usr/bin/env python3
"""eval_channel.py — a channel that means "this is for Cal", and the fail-open value that isn't.

WHY THIS FILE EXISTS
--------------------
On the shared channel the trigger word is the only thing separating "Cal, what's the torque"
from a sentence that merely mentions Cal, so it has to stay. Two other ways of dropping it were
designed and rejected first, and both are worth recording because they look reasonable:

  * Treat a Meshtastic REPLY to one of Cal's messages as addressing. The plumbing works, but
    long-press -> Reply -> type is MORE work than typing four characters, so it does not solve
    the problem it was proposed for. Measured: 10 of 10 messages from the owner in the log
    window carry no reply id, including the two this would have existed to catch.
  * A conversation window after Cal replies. On a shared channel this makes Cal answer messages
    meant for other humans, and it breaks the clarify follow-up outright — a chit-chat message
    landing in the window consumes the pending clarify, so the real answer that follows reaches
    the MODEL and gets an invented torque figure. Demonstrated by execution, not argument.

The channel carries the meaning in the transport instead. `p->channel` is assigned only after
the payload decrypts and parses to a valid Data with a known portnum (Router.cpp:482-515), so
the index is a cryptographic assertion that the sender holds a key we chose. `from` is cleartext
(RadioInterface.h:34-54) and a reply id is forgeable by anyone holding the public channel's
well-known PSK; the channel index is neither.

THE ONE FAILURE THAT MATTERS is fail-open. Channel 0 is the public channel. A disarmed value
that parses to 0 — an empty string, a typo, a missing key, `int("")` swallowed by a bare except
— would drop the trigger requirement for every one of ~250 nodes simultaneously, silently, and
in the direction where nothing looks broken. Hence -1, and hence most of this file.

Run:  python3 eval_channel.py                (exit 0 = pass)
      python3 eval_channel.py --self-test    also proves the checks can FAIL
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

OURS = "!c0000001"
STRANGER = "!e0000003"
ALLOWED = "!aaaaaaaa"   # already published in this repo; scrub-staged.sh rejects a NEW id

failures, checked = [], 0


def check(label, got, want):
    global checked
    checked += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def cfg(**over):
    c = dict(R.DEFAULTS)
    c.update({"RESPONDER_ENABLED": "true", "TRIGGER_WORD": "cal",
              "ALLOW_FROM": ALLOWED, "GREETING_ENABLED": "false"})
    c.update(over)
    return c


def rec(text, ch=0, to="^all", frm=ALLOWED):
    import datetime
    return {"from": frm, "to": to, "text": text, "channel": ch,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def state():
    """A throwaway rate-limit state. `rate_ok` indexes st["per_sender"] directly rather than
    with .get, so a bare {} raises — every call gets a fresh one, which also keeps each check
    independent of how many calls ran before it."""
    return {"per_sender": {}}


def gate(text, ch=0, to="^all", frm=ALLOWED, **over):
    """(ok, reason) from the real gate ladder."""
    ok, why, _dest, _ch = R.evaluate(cfg(**over), state(), rec(text, ch, to, frm), OURS, trace=[])
    return ok, why


# ---------------------------------------------------------------- cal_channel: parsing is a gate
# Everything unparseable must read as DISARMED. The direction that matters is that NOTHING
# accidentally becomes 0.
for value, want in [("-1", -1), ("1", 1), ("7", 7), (" 1 ", 1),
                    ("", -1), ("abc", -1), ("8", -1), ("99", -1), ("-2", -1),
                    ("1.5", -1), ("0x1", -1), ("true", -1), ("none", -1), (None, -1)]:
    c = cfg()
    if value is None:
        c.pop("CAL_CHANNEL", None)
    else:
        c["CAL_CHANNEL"] = value
    check(f"CAL_CHANNEL {value!r} parses", R.cal_channel(c), want)
check("a missing key is disarmed", R.cal_channel({}), -1)
check("ships disarmed", R.DEFAULTS["CAL_CHANNEL"], "-1")
check("and the shipped default parses to disarmed", R.cal_channel(dict(R.DEFAULTS)), -1)
# 0 is a LEGAL index and must be honoured when someone genuinely means it — the guard is that
# it can never be reached by accident, not that it is forbidden.
check("0 is legal when stated outright", R.cal_channel(cfg(CAL_CHANNEL="0")), 0)

# --------------------------------------------------------------------- disarmed changes nothing
# The whole point of shipping off: every existing behaviour is byte-identical until armed.
check("disarmed: trigger still required", gate("whats the torque")[1], "not_addressed")
check("disarmed: trigger still works", gate("cal whats the torque")[0], True)
check("disarmed: a DM still needs no trigger", gate("whats the torque", to=OURS)[0], True)
check("disarmed: another channel is not special",
      gate("whats the torque", ch=1)[1], "not_addressed")

# ------------------------------------------------------------------------------- armed on ch1
ARM = {"CAL_CHANNEL": "1"}
check("armed: no trigger needed on Cal's channel", gate("whats the torque", ch=1, **ARM)[0], True)
check("armed: the trigger still works there", gate("cal whats the torque", ch=1, **ARM)[0], True)
# The public channel is untouched. This is the assertion the whole design exists to hold.
check("armed: ch0 STILL requires the trigger",
      gate("whats the torque", ch=0, **ARM)[1], "not_addressed")
check("armed: ch0 with the trigger still works", gate("cal whats the torque", **ARM)[0], True)
check("armed: an unrelated channel is not addressed",
      gate("whats the torque", ch=2, **ARM)[1], "not_addressed")
# A missing channel field must not read as Cal's channel. It defaults to 0 upstream, which is
# only safe while Cal's channel is never 0 — assert the pair together.
check("armed on 0 would open the public channel",
      gate("whats the torque", ch=0, CAL_CHANNEL="0")[0], True)

# ------------------------------------------------------- the channel is NOT a trust escalation
# Addressing is the only thing it grants. Every gate above it still runs, in the same order.
check("armed: an off-list sender is still refused on Cal's channel",
      gate("whats the torque", ch=1, frm=STRANGER, **ARM)[1], "sender_not_allowed")
trace = []
R.evaluate(cfg(**ARM), state(), rec("whats the torque", ch=1, frm=STRANGER), OURS, trace=trace)
check("armed: sender_allowed is still checked BEFORE addressed",
      [g["gate"] for g in trace],
      ["not_self", "fresh", "responder_enabled", "sender_allowed"])
check("armed: the responder switch still wins",
      gate("whats the torque", ch=1, RESPONDER_ENABLED="false", **ARM)[1], "disabled")
check("armed: a stale message is still stale",
      R.evaluate(cfg(**ARM), state(), dict(rec("whats the torque", 1),
                 ts="2020-01-01T00:00:00+00:00"), OURS, trace=[])[1], "too_old")

# MUTATION-BOUNDARY — the self-test replays everything ABOVE this line under each mutation.
# ------------------------------------------------------------------------------- MUTATIONS
MUTATIONS = [
    # THE defect this file exists for: any spelling of the disarmed value that lands on 0.
    ("disarmed value parses to 0 (bare int with a fallback of 0)",
     lambda: setattr(R, "cal_channel", lambda cfg: (lambda v: v if isinstance(v, int) else 0)(
         int(cfg.get("CAL_CHANNEL", 0)) if str(cfg.get("CAL_CHANNEL", "0")).lstrip("-").isdigit()
         else 0))),
    # An unparseable value falling open instead of closed.
    ("unparseable value falls open to 0",
     lambda: setattr(R, "cal_channel", lambda cfg: _ORIG_CHAN(cfg) if str(
         cfg.get("CAL_CHANNEL", "-1")).lstrip("-").isdigit() else 0)),
    # The range check dropped, so "8" or "99" match a channel that cannot exist.
    ("range check removed",
     lambda: setattr(R, "cal_channel", lambda cfg: int(str(cfg.get("CAL_CHANNEL", "-1")).strip())
                     if str(cfg.get("CAL_CHANNEL", "-1")).strip().lstrip("-").isdigit() else -1)),
]

_ORIG_CHAN = R.cal_channel


def run_self_test():
    src = open(os.path.join(HERE, "eval_channel.py")).read()
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
        R.cal_channel = _ORIG_CHAN
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
