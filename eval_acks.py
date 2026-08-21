#!/usr/bin/env python3
"""Eval for delivery receipts (ROUTING_APP) in bridge.py.

WHY THIS FILE EXISTS
--------------------
Before this branch, `sendText` was fire-and-forget: the TX line in bridge.log was written
after the call returned, so it recorded that the bridge handed a packet to the radio over TCP
and nothing more. A real delivery receipt DID arrive — it was counted in the port census as a
ROUTING_APP tick and discarded — so the only way to answer "did it arrive" was to watch a
counter change between two five-minute censuses. That is how "we called sendText" got read as
"it arrived", out loud, on 2026-08-21.

Reading the receipt is not the hard part. Not over-reading it is. THREE outcomes share one
portnum, and the firmware makes two of them look alike:

  * `from == dest`, no error -> DELIVERED. Every NONE ack the firmware emits sits behind an
    isToUs guard (ReliableRouter.cpp:95-129, NextHopRouter.cpp:73), so this one, and only this
    one, is the destination saying it has the packet.
  * `from == US`, no error -> IMPLICIT, and this is the trap. Our own radio manufactures a
    NONE ack the instant it overhears ANY node rebroadcast our packet
    (ReliableRouter.cpp:40-57, "Generate implicit ack"). It proves a relay repeated us. It
    proves nothing whatsoever about the destination — and at the Python layer it is identical
    to a real receipt in every field except `from`.
  * `from == US`, MAX_RETRANSMIT -> FAILED, generated locally by our own retransmit timer
    giving up (NextHopRouter.cpp:284, guarded by isFromUs).

And one decoding point that is the OPPOSITE of the trap this repo has been bitten by three
times. `error_reason` sits in Routing's `variant` oneof, so its presence IS tracked: a receipt
carries the literal string "NONE" rather than omitting the field the way bare proto3 scalars
(hopLimit, pkiEncrypted) do. Confirmed against mesh_pb2 and against the live receipt on
2026-08-21. The classifier still accepts absent as success — an absent errorReason means the
oneof carried another arm, not an error — but "NONE" is the shape that actually arrives, and a
reader who assumes the usual omission rule here would be assuming wrong.

The mutation section is the point of the file. A classifier that ignores `from` passes every
happy-path assertion here and silently reports "delivered" for a packet the destination never
heard, which is a worse failure than the one this branch set out to fix — it is confidently
wrong instead of merely blind.

Run:  python3 eval_acks.py                (exit 0 = pass)
      python3 eval_acks.py --self-test    also proves the checks can FAIL
"""
import importlib.util
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# bridge.py opens a radio only under main(); importing it is safe. It does import the
# meshtastic package at module scope, so run this under the same interpreter the bridge uses.
_spec = importlib.util.spec_from_file_location("bridge_mod", os.path.join(HERE, "bridge.py"))
bridge = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(bridge)
except ModuleNotFoundError as e:
    print(f"SKIP: {e} — run with the bridge's interpreter "
          f"(~/.local/pipx/venvs/meshtastic/bin/python)")
    sys.exit(0)

# Placeholder ids only. This repo is public and Cal HT's real id has deliberately never been
# in it; scrub-staged.sh enforces that at `git add` time.
OURS = "!aaaaaaaa"        # us — the node that sent the wantAck message
DEST = "!bbbbbbbb"        # the node we addressed it to
OTHER = "!cccccccc"       # some third node on the channel

ORIG_RING = bridge.ACK_RING     # captured before any mutation can move it
BURST = 48                      # comfortably more than the shipped ring
ORIG_TTL = bridge.ACK_TTL_S     # ditto for the TTL

failures, checked = [], 0


def check(label, got, want):
    global checked
    checked += 1
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


class FakePkt:
    def __init__(self, pid):
        self.id = pid


def reset():
    bridge.PENDING_ACK[:] = []


# ---------------------------------------------------------------- classify_ack: the 3 outcomes
check("destination answered -> delivered",
      bridge.classify_ack(None, DEST, DEST, OURS)[0], "delivered")
check("our own radio, no error -> implicit, NOT delivered",
      bridge.classify_ack(None, OURS, DEST, OURS)[0], "implicit")
check("our own radio, MAX_RETRANSMIT -> failed",
      bridge.classify_ack("MAX_RETRANSMIT", OURS, DEST, OURS)[0], "failed")
check("third party, no error -> relayed, NOT delivered",
      bridge.classify_ack(None, OTHER, DEST, OURS)[0], "relayed")

# "NONE" is what the wire carries (oneof presence, see the header); None and "" are defensive.
# All three must agree, and none of them may turn an implicit ack into a delivered one.
for spelling in (None, "", "NONE"):
    check(f"success spelling {spelling!r} from dest -> delivered",
          bridge.classify_ack(spelling, DEST, DEST, OURS)[0], "delivered")
    check(f"success spelling {spelling!r} from us -> implicit",
          bridge.classify_ack(spelling, OURS, DEST, OURS)[0], "implicit")

# Any error at all is a failure, whoever it came from — an error from the destination is still
# not a delivery.
for err in ("MAX_RETRANSMIT", "NO_RESPONSE", "NO_CHANNEL", "PKI_UNKNOWN_PUBKEY", "TIMEOUT"):
    check(f"error {err} -> failed", bridge.classify_ack(err, DEST, DEST, OURS)[0], "failed")

# Degenerate inputs must never read as delivered. An unknown `from` or an unknown dest is the
# state right after a restart, and "delivered" is the one answer it must not produce.
check("unknown sender -> not delivered", bridge.classify_ack(None, None, DEST, OURS)[0] != "delivered", True)
check("unknown dest -> not delivered", bridge.classify_ack(None, DEST, None, OURS)[0] != "delivered", True)
check("unknown ours, from dest -> still delivered",
      bridge.classify_ack(None, DEST, DEST, None)[0], "delivered")

# ------------------------------------------------------------------------ remember_ack / match
reset()
pid = bridge.remember_ack(FakePkt(4242), DEST, "hello")
check("remember returns the packet id", pid, 4242)
check("one pending", len(bridge.PENDING_ACK), 1)
check("match finds it", (bridge.match_ack(4242) or {}).get("dest"), DEST)
check("match is id-specific", bridge.match_ack(4243), None)
check("non-int id never matches", bridge.match_ack("4242"), None)
check("None id never matches", bridge.match_ack(None), None)

# sendText can return None (or something without an id) — that must not enter the table, or a
# later receipt would match a send we cannot identify.
reset()
check("no packet -> nothing remembered", bridge.remember_ack(None, DEST, "x"), None)
check("packet without int id -> nothing remembered", bridge.remember_ack(FakePkt("nope"), DEST, "x"), None)
check("table still empty", len(bridge.PENDING_ACK), 0)

# The ring is bounded. A bridge that runs for weeks must not accumulate one entry per send.
# The loop count is a CONSTANT, not derived from ACK_RING: a mutation that inflates the ring
# would otherwise inflate this loop with it and the test would hang rather than fail.
reset()
for i in range(BURST):
    bridge.remember_ack(FakePkt(1000 + i), DEST, "x")
check("ring bounded", len(bridge.PENDING_ACK), ORIG_RING)
check("oldest evicted", bridge.match_ack(1000), None)
check("newest kept", (bridge.match_ack(1000 + BURST - 1) or {}).get("dest"), DEST)

# TTL: a receipt that arrives after we gave up is not that send's receipt. Matching it would
# attach a delivery to the wrong message — and on a public page, publish it.
# The ages below are built from ORIG_TTL, never from bridge.ACK_TTL_S. Deriving them from the
# live constant makes the assertion move with the mutation: inflate the TTL and the fake age
# inflates too, the entry still reads as expired, and a bridge with NO expiry at all passes.
# That is not a hypothetical — it is what this file did on its first run.
reset()
bridge.remember_ack(FakePkt(777), DEST, "old")
bridge.PENDING_ACK[0]["ts"] = time.time() - ORIG_TTL - 1
check("expired never matches", bridge.match_ack(777), None)
check("expired is dropped, not left to rot", len(bridge.PENDING_ACK), 0)

reset()
bridge.remember_ack(FakePkt(778), DEST, "fresh")
bridge.PENDING_ACK[0]["ts"] = time.time() - ORIG_TTL + 5
check("just inside TTL still matches", (bridge.match_ack(778) or {}).get("id"), 778)

# A corrupt timestamp must expire the entry, not preserve it forever.
reset()
bridge.remember_ack(FakePkt(779), DEST, "corrupt")
bridge.PENDING_ACK[0]["ts"] = "not-a-time"
check("corrupt ts expires", bridge.match_ack(779), None)

# ------------------------------------------------------------------------------- MUTATIONS
# Each mutation is a plausible way to write this wrong. Every one must be CAUGHT by the
# assertions above — a mutation that survives means the assertion above it is decorative.
MUTATIONS = [
    # The one that matters: ignore `from` and call every non-error receipt a delivery. This is
    # the natural implementation, it passes every "did we read the receipt" test, and it
    # reports success for packets the destination never heard.
    ("classifier ignores `from`",
     lambda: setattr(bridge, "classify_ack",
                     lambda err, frm, dest, ours: (("failed", err) if err not in (None, "", "NONE")
                                                   else ("delivered", "x")))),
    # Absent errorReason read as unknown rather than as success — the proto3 omission trap.
    ("absent error read as unknown",
     lambda: setattr(bridge, "classify_ack",
                     lambda err, frm, dest, ours: ("unknown", "x") if err is None
                     else (("failed", err) if err != "NONE" else ("delivered", "x")))),
    # No TTL: stale sends match forever.
    ("TTL removed", lambda: setattr(bridge, "ACK_TTL_S", 10 ** 9)),
    # No bound: the table grows without limit.
    ("ring unbounded", lambda: setattr(bridge, "ACK_RING", 10 ** 6)),
]


def run_self_test():
    """Re-run every assertion under each mutation; each must produce at least one failure."""
    import copy
    src = open(os.path.join(HERE, "eval_acks.py")).read()
    body = src.split("# ---------------------------------------------------------------- classify_ack")[1]
    body = body.split("# ------------------------------------------------------------------------------- MUTATIONS")[0]
    body = "# ---" + body
    survived = []
    for name, mutate in MUTATIONS:
        saved = {"classify_ack": bridge.classify_ack, "ACK_TTL_S": bridge.ACK_TTL_S,
                 "ACK_RING": bridge.ACK_RING}
        globals()["failures"] = []
        globals()["checked"] = 0
        mutate()
        try:
            exec(compile(body, "<mutation>", "exec"), globals())
        except Exception:
            pass
        caught = bool(globals()["failures"])
        for k, v in saved.items():
            setattr(bridge, k, v)
        print(f"  {'CAUGHT ' if caught else 'SURVIVED'} {name}"
              + (f" ({len(globals()['failures'])} assertions failed)" if caught else ""))
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
        survived = run_self_test()
        if survived:
            print(f"FAIL: {len(survived)} mutation(s) survived: {survived}")
            sys.exit(1)
        print(f"all {len(MUTATIONS)} mutations caught")
