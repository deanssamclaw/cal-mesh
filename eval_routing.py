#!/usr/bin/env python3
"""Offline regression eval for the bridge's packet-capture layer.

WHY THIS FILE EXISTS
--------------------
Both bugs it guards were invisible to inspection and produced *plausible* output, which is the
failure mode that gets shipped. Neither crashed, neither logged an error, and the public page
rendered a confident sentence about each. They were found only by comparing what Cal recorded
against what a second, independent receiver recorded off the same air.

  1. `hop_limit` is decremented by every relay, so hops = hop_start - hop_limit. The radio
     library builds its packet dict with `google.protobuf.json_format.MessageToDict`, which
     OMITS proto3 scalars equal to their default. A packet that consumed its ENTIRE hop budget
     therefore has hop_limit 0 -> key absent -> the old `isinstance(hl, int)` guard recorded
     None. The most-relayed packets were exactly the ones being discarded, and the dashboard
     explained the resulting blank as "this message predates routing capture" — a false claim
     about the message's history, on a public page.

  2. `fromId` is resolved through the library's node database and is None for a node we have
     not yet received a NodeInfo from — while the raw `from` field carries that node's NUMBER
     the whole time. The ID therefore goes missing precisely at FIRST CONTACT. That is not a
     corner case for this project: an unknown sender's opening message IS the population the
     unknown-sender tier is designed around, and it would have been blind to it.

The cases below build REAL protobuf packets and convert them with the REAL library, so the
"key is missing" condition is produced by the library rather than asserted by hand. A hand-made
dict would have passed the old buggy code too — which is the whole lesson.

Run:  ~/.local/pipx/venvs/meshtastic/bin/python eval_routing.py     (exit 0 = pass)
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("bridge_mod", os.path.join(HERE, "bridge.py"))
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

try:
    from meshtastic.protobuf import mesh_pb2
    from google.protobuf.json_format import MessageToDict
except ImportError:
    print("SKIP: meshtastic not importable — run with the venv python")
    sys.exit(0)

BROADCAST = 0xFFFFFFFF
CHECKS = []


def check(name, got, want):
    CHECKS.append((name, got == want, got, want))


def as_dict(hop_start, hop_limit):
    """A packet dict built the way the library actually builds one."""
    p = mesh_pb2.MeshPacket()
    p.hop_start = hop_start
    p.hop_limit = hop_limit
    return MessageToDict(p)


# --- hops_taken --------------------------------------------------------------------------
# The load-bearing case is hop_limit=0: assert the library really does drop the key, so this
# eval fails loudly if a future library version changes that instead of silently passing.
check("library omits hop_limit when it is 0", "hopLimit" in as_dict(3, 0), False)
check("library keeps hop_limit when nonzero", "hopLimit" in as_dict(3, 1), True)

check("direct (3/3) -> 0 hops",              bridge.hops_taken(as_dict(3, 3))[0], 0)
check("FULL budget (3/0) -> 3 hops",         bridge.hops_taken(as_dict(3, 0))[0], 3)
check("FULL budget (7/0) -> 7 hops",         bridge.hops_taken(as_dict(7, 0))[0], 7)
check("partial (3/1) -> 2 hops",             bridge.hops_taken(as_dict(3, 1))[0], 2)
check("no routing info (0/0) -> None",       bridge.hops_taken(as_dict(0, 0))[0], None)
check("no hop fields at all -> None",        bridge.hops_taken({})[0], None)
# Never invent a 0: "direct — heard straight from the sender" must not be printed on a guess.
check("hop_limit > hop_start is nonsense, not 0", bridge.hops_taken({"hopStart": 3, "hopLimit": 9})[0], None)
check("non-int hop_start -> None",           bridge.hops_taken({"hopStart": "3", "hopLimit": 0})[0], None)
check("recorded hop_limit is the resolved 0", bridge.hops_taken(as_dict(3, 0))[2], 0)

# --- node_id -----------------------------------------------------------------------------
check("unresolved sender falls back to the packet's nodenum",
      bridge.node_id(0xBA0CC0C0, None), "!ba0cc0c0")
check("resolved id passes through untouched",
      bridge.node_id(0x11CFAAFD, "!11cfaafd"), "!11cfaafd")
check("broadcast nodenum never becomes !ffffffff",
      bridge.node_id(BROADCAST, None), "^all")
check("zero nodenum -> None", bridge.node_id(0, None), None)
check("missing nodenum -> None", bridge.node_id(None, None), None)
check("fallback id is lowercase 8-hex, same shape the library emits",
      bridge.node_id(0x000000FF, None), "!000000ff")

# --- integration: the two bugs together, through the real on_receive ----------------------
# A first-contact node whose message also used its whole hop budget — the exact packet shape
# that lost BOTH fields before this fix.
import json
import tempfile

_tmp = tempfile.mkdtemp(prefix="cal-mesh-eval-")
bridge.INBOX = os.path.join(_tmp, "inbox.jsonl")
bridge.SNR_HIST = os.path.join(_tmp, "snr.jsonl")

p = as_dict(3, 0)
p["from"] = 0xBA0CC0C0
p["to"] = BROADCAST
p["toId"] = "^all"          # library resolves broadcast fine; the SENDER is what it loses
p["decoded"] = {"portnum": "TEXT_MESSAGE_APP", "text": "Hi"}
p["rxSnr"], p["rxRssi"] = 6.0, -56
bridge.on_receive(packet=p)

def line_count(path):
    """Missing file counts as zero, not a crash — a regression that stops writing a file must
    report as a failed CHECK, not as a traceback that hides every check after it."""
    try:
        return len([l for l in open(path).read().splitlines() if l.strip()])
    except FileNotFoundError:
        return 0


rec = json.loads(open(bridge.INBOX).read().strip())
check("on_receive records the first-contact sender", rec["from"], "!ba0cc0c0")
check("on_receive records the full hop count",       rec["hops"], 3)
check("on_receive records hop_start",                rec["hop_start"], 3)
check("SNR sample kept for an unresolved sender",    line_count(bridge.SNR_HIST), 1)

# --- report ------------------------------------------------------------------------------
passed = sum(1 for _, ok, _, _ in CHECKS if ok)
for name, ok, got, want in CHECKS:
    if not ok:
        print(f"FAIL  {name}\n        got={got!r} want={want!r}")
print(f"\n{passed}/{len(CHECKS)} checks passed")
sys.exit(0 if passed == len(CHECKS) else 1)
