#!/usr/bin/env python3
"""Eval for the passive traceroute harvest in bridge.py.

WHY THIS FILE EXISTS
--------------------
`capture_route` reads a protobuf decoded by the meshtastic library from a packet that anybody
on the channel can transmit, and writes what it finds to a file the dashboard will draw. Three
things about that decoding are counter-intuitive, and every one of them has a matching defect
already in this repo's history:

  * `MessageToDict` OMITS an empty repeated field. A DIRECT path — the commonest case — arrives
    with no `route` key at all. Reading absent as "unknown" is the exact shape of the hop-count
    bug that shipped twice on this bridge (`hops_taken`, and its docstring).
  * The firmware marks a hop it could not name with `0xFFFFFFFF`, which is ALSO the broadcast
    address. `node_id()` renders that as `^all`, so a relay nobody could identify would be
    published as a broadcast — a wrong claim about who carried a message, on a public page.
  * SNR is quarter-dB in a SIGNED BYTE, and `-128` means unknown. It is also what a real
    -32.0 dB clamps to; the firmware does not disambiguate, so neither can we.

And one that is not about decoding at all: a traceroute RESPONSE travels from the traced node
back to the requester, so `from` and `to` are the reverse of what a message's own diagram
means by them. Getting that backwards draws every path in the wrong direction, and it would
look entirely plausible.

Run:  python3 eval_routes.py                (exit 0 = pass)
      python3 eval_routes.py --self-test    also proves the checks can FAIL
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

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

# Fixtures use ONLY the ids scrub-staged.sh recognises as placeholders. That is not a style
# preference: this repo is public, and Cal HT's real node id has deliberately never been in it.
# The first draft of this file used the real one and the scrub stopped the commit, which is
# exactly the job it exists for — an id published here is one that can be tied to a node on the
# air and correlated with the signal readings the dashboard already publishes.
REQ = "!aaaaaaaa"         # the node that asked
TRACED = "!bbbbbbbb"      # the node that answered
R1, R2 = 0xcccccccc, 0xdeadbeef      # two relays in between
UNKNOWN_NODE = 0xFFFFFFFF
UNKNOWN_SNR = -128

failures, checked = [], 0


def pkt(frm, to, tr, request_id=None, snr=6.0, rssi=-41, pid=1234):
    """A packet shaped the way the meshtastic library hands one to on_receive: the protobuf
    already parsed into decoded['traceroute'] as a MessageToDict, camelCase, empty repeated
    fields ABSENT rather than empty."""
    d = {"portnum": "TRACEROUTE_APP", "traceroute": tr}
    if request_id is not None:
        d["requestId"] = request_id
    return {"from": frm, "to": to, "id": pid, "rxSnr": snr, "rxRssi": rssi, "decoded": d}


def num(nid):
    return int(nid[1:], 16)


def run(packet, ours=None):
    """Call capture_route with ROUTES pointed at a scratch file, and return the record.

    `ours` defaults to None -- Cal's id unresolved -- so a test must state explicitly when it
    means "this one was addressed to us"."""
    d = packet["decoded"]
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as f:
        path = f.name
    old = bridge.ROUTES
    bridge.ROUTES = path
    try:
        rec = bridge.capture_route(packet, d, ours)
        written = [json.loads(l) for l in open(path) if l.strip()]
    finally:
        bridge.ROUTES = old
        os.unlink(path)
    # what is RETURNED and what is WRITTEN must be the same thing — a dashboard reads the file,
    # not the return value, and an eval that only checks the return value would not notice.
    if len(written) != 1 or written[0] != rec:
        failures.append(("the record written to disk differs from the one returned",
                         rec, written))
    return rec


def check(why, got, want):
    global checked
    checked += 1
    if got != want:
        failures.append((why, got, want))


def check_true(why, got):
    global checked
    checked += 1
    if not got:
        failures.append((why, got, True))


# ---------------------------------------------------------------------------------------
# 1. A DIRECT path. MessageToDict omits the empty route entirely — this is the common case.
# ---------------------------------------------------------------------------------------
r = run(pkt(num(TRACED), num(REQ), {"snrTowards": [24], "snrBack": [20]}, request_id=7))
check("direct path: orientation — the responder is the node that was traced",
      (r["requester"], r["traced"]), (REQ, TRACED))
check("direct path: the chain is requester -> traced with nothing in between",
      r["path"], [REQ, TRACED])
check("direct path: an absent 'route' key is EMPTY, never unknown", r["route"], [])
check("direct path: one link, not zero and not None", r["links"], 1)
check("direct path: quarter-dB decodes to dB", r["snr_towards"], [6.0])
check_true("direct path: one SNR entry for one link is COMPLETE", r["snr_towards_complete"])

# ---------------------------------------------------------------------------------------
# 2. A two-hop path, both directions, addressed to somebody else.
# ---------------------------------------------------------------------------------------
r = run(pkt(num(TRACED), num(REQ),
            {"route": [R1, R2], "snrTowards": [24, 20, -12],
             "routeBack": [R2, R1], "snrBack": [-8, 16, 28]}, request_id=8))
check("two hops: the full chain includes both endpoints",
      r["path"], [REQ, "!cccccccc", "!deadbeef", TRACED])
check("two hops: three links for two intermediate relays", r["links"], 3)
check("two hops: SNR is per LINK and negative values survive",
      r["snr_towards"], [6.0, 5.0, -3.0])
check("two hops: the return path is recorded separately, not merged",
      r["route_back"], ["!deadbeef", "!cccccccc"])
check("two hops: a packet addressed to another node is labelled overheard",
      r["witness"], "overheard")

# ---------------------------------------------------------------------------------------
# 3. THE TRAP: an unnamed hop is 0xFFFFFFFF, which is also the broadcast address.
# ---------------------------------------------------------------------------------------
r = run(pkt(num(TRACED), num(REQ),
            {"route": [R1, UNKNOWN_NODE], "snrTowards": [24, UNKNOWN_SNR, 20]}, request_id=9))
check("unnamed hop is a HOLE, not a node", r["route"], ["!cccccccc", None])
check_true("unnamed hop is never rendered as the broadcast address",
           "^all" not in [h for h in r["route"] if h] and "^all" not in
           [h for h in r["path"] if h])
check("unknown SNR is a hole, not a number", r["snr_towards"], [6.0, None, 5.0])
check("an unnamed hop still occupies its position in the chain",
      r["path"], [REQ, "!cccccccc", None, TRACED])

# ---------------------------------------------------------------------------------------
# 4. A REQUEST in flight carries a half-built route and is not a path.
# ---------------------------------------------------------------------------------------
r = run(pkt(num(REQ), num(TRACED), {"route": [R1]}))
check("a request is not a response", r["kind"], "request")
check("a half-built route is not presented as a path", r["path"], None)
check("a request has no link count", r["links"], None)
check("a request's requester is its sender", (r["requester"], r["traced"]), (REQ, TRACED))
check_true("a request's SNR arrays are never called complete",
           not r["snr_towards_complete"] and not r["snr_back_complete"])

# ---------------------------------------------------------------------------------------
# 5. Incomplete SNR arrays are flagged, not padded.
# ---------------------------------------------------------------------------------------
r = run(pkt(num(TRACED), num(REQ), {"route": [R1, R2], "snrTowards": [24]}, request_id=10))
check_true("an SNR array shorter than the link count is NOT complete",
           not r["snr_towards_complete"])
check("a short SNR array is left short, never padded to fit", r["snr_towards"], [6.0])
check_true("an absent snrBack is not complete either", not r["snr_back_complete"])

# ---------------------------------------------------------------------------------------
# 6. Addressed to us.
# ---------------------------------------------------------------------------------------
# A response addressed to us is, by definition, one we asked for.
r = run(pkt(num(TRACED), num(REQ), {"snrTowards": [24]}, request_id=11), ours=REQ)
check("a response addressed to Cal is labelled addressed", r["witness"], "addressed")
r = run(pkt(num(TRACED), num(REQ), {"snrTowards": [24]}, request_id=11), ours=None)
check("with our own id unresolved, a path is NOT claimed as addressed to us",
      r["witness"], "overheard")
r = run(pkt(num(TRACED), num(REQ), {"snrTowards": [24]}, request_id=11), ours=TRACED)
check("a path merely passing our way is not ours", r["witness"], "overheard")

# ---------------------------------------------------------------------------------------
# 7. A real -32.0 dB encodes to -128 as well. The firmware cannot tell them apart, so this
#    reads it as unknown — recorded here so the ambiguity is a decision, not an accident.
# ---------------------------------------------------------------------------------------
r = run(pkt(num(TRACED), num(REQ), {"snrTowards": [-128]}, request_id=12))
check("-32.0 dB is indistinguishable from unknown and is read as unknown",
      r["snr_towards"], [None])

# ---------------------------------------------------------------------------------------
# 8. Nothing crashes on a packet with no traceroute payload at all.
# ---------------------------------------------------------------------------------------
p = pkt(num(TRACED), num(REQ), {}, request_id=13)
del p["decoded"]["traceroute"]
r = run(p)
check("a traceroute packet with no payload still records a chain", r["path"], [REQ, TRACED])
check("...with an empty route", r["route"], [])


# ---------------------------------------------------------------------------------------
# 9. The SEND guards. This is the only code in cal-mesh that puts a traceroute on the air, and
#    a traceroute is the most airtime Cal can spend for the least payload. Every guard is
#    checked against a fake interface that records calls instead of transmitting.
# ---------------------------------------------------------------------------------------
class FakeIface:
    def __init__(self):
        self.sent = []

    def sendData(self, data, **kw):
        self.sent.append(kw)


def drain(cfg, queued, state=None):
    """Run one drain pass in a scratch directory. Returns (sends, state-after)."""
    with tempfile.TemporaryDirectory() as td:
        q = os.path.join(td, "traceroute")
        os.makedirs(q)
        os.makedirs(os.path.join(td, "sent"))
        for i, dest in enumerate(queued):
            open(os.path.join(q, f"{i:03d}"), "w").write(dest)
        stp = os.path.join(td, "traceroute-state.json")
        if state is not None:
            json.dump(state, open(stp, "w"))
        oq, ost, osent = bridge.TR_QUEUE, bridge.TR_STATE, bridge.SENT
        bridge.TR_QUEUE, bridge.TR_STATE, bridge.SENT = q, stp, os.path.join(td, "sent")
        f = FakeIface()
        try:
            bridge.drain_traceroute(f, cfg)
            after = json.load(open(stp)) if os.path.exists(stp) else {}
        finally:
            bridge.TR_QUEUE, bridge.TR_STATE, bridge.SENT = oq, ost, osent
        return f.sent, after


ON = {"TRACEROUTE_ENABLED": "true", "TRACEROUTE_MIN_GAP_S": 180}

sent, _ = drain({}, [TRACED])
check("send: DISABLED by default — an absent config key must not transmit", sent, [])
sent, _ = drain({"TRACEROUTE_ENABLED": "false"}, [TRACED])
check("send: explicitly disabled does not transmit", sent, [])

sent, st = drain(ON, [TRACED])
check("send: enabled with an empty state transmits exactly once", len(sent), 1)
check("send: it goes to the queued destination", sent[0].get("destinationId"), TRACED)
check_true("send: it asks for a response, or nothing ever comes back",
           sent[0].get("wantResponse") is True)
check_true("send: the state records the send, on disk", bool(st.get("last_ts")))

sent, _ = drain(ON, [TRACED, REQ, "!cccccccc"])
check("send: at most ONE per pass, however many are queued", len(sent), 1)


def drain_twice(cfg, queued):
    """Two consecutive passes against ONE persistent state file — the shape a repeat really
    takes. The single-pass check above cannot see a missing interval stamp, because the stamp
    only matters on the NEXT pass."""
    with tempfile.TemporaryDirectory() as td:
        q = os.path.join(td, "traceroute")
        os.makedirs(q)
        os.makedirs(os.path.join(td, "sent"))
        for i, dest in enumerate(queued):
            open(os.path.join(q, f"{i:03d}"), "w").write(dest)
        stp = os.path.join(td, "traceroute-state.json")
        oq, ost, osent = bridge.TR_QUEUE, bridge.TR_STATE, bridge.SENT
        bridge.TR_QUEUE, bridge.TR_STATE, bridge.SENT = q, stp, os.path.join(td, "sent")
        f = FakeIface()
        try:
            bridge.drain_traceroute(f, cfg)
            bridge.drain_traceroute(f, cfg)
        finally:
            bridge.TR_QUEUE, bridge.TR_STATE, bridge.SENT = oq, ost, osent
        return f.sent


sent = drain_twice(ON, [TRACED, REQ])
check("send: a second pass immediately after a send is blocked by the stamp it left",
      len(sent), 1)

import time as _t
sent, _ = drain(ON, [TRACED], state={"last_ts": _t.time() - 10})
check("send: the on-disk interval blocks a probe sent 10 s ago", sent, [])
sent, _ = drain(ON, [TRACED], state={"last_ts": _t.time() - 999})
check("send: and allows one after the interval has passed", len(sent), 1)

# The whole reason the limiter is on disk: an in-memory one dies with the process, and this
# bridge restarts on every dropped link — the same event that resets the firmware's own 30 s
# window. Reloading the module must NOT forget that we just transmitted.
st_recent = {"last_ts": _t.time() - 10}
sent, _ = drain(ON, [TRACED], state=st_recent)
sent2, _ = drain(ON, [TRACED], state=st_recent)
check("send: the interval survives a restart, because it is read from disk every pass",
      (sent, sent2), ([], []))

sent, _ = drain(ON, ["^all"])
check("send: a broadcast traceroute is refused, not silently sent", sent, [])
sent, _ = drain(ON, ["!ffffffff"])
check("send: the broadcast NUMBER is refused too", sent, [])
sent, _ = drain(ON, [""])
check("send: an empty destination is refused", sent, [])

sent, _ = drain(ON, [])
check("send: an empty queue transmits nothing", sent, [])

sent, _ = drain({**ON, "TRACEROUTE_HOP_LIMIT": 2}, [TRACED])
check("send: the hop limit is configurable and is passed through",
      sent[0].get("hopLimit"), 2)

# A refused destination must not consume the interval — otherwise one bad queue entry buys
# silence until the next window for no transmission at all.
sent, st = drain(ON, ["^all"])
check_true("send: a refusal does not stamp the interval", not st.get("last_ts"))


# ---------------------------------------------------------------------------------------
if "--self-test" in sys.argv:
    print("\n--- self-test: each mutation must be CAUGHT ---")
    import re
    src = open(os.path.join(HERE, "bridge.py")).read()
    MUTATIONS = [
        ("the unnamed-hop marker is resolved through node_id, so it becomes '^all'",
         "out.append(None if n == UNKNOWN_NODE else node_id(n, None))",
         "out.append(node_id(n, None))"),
        ("the response's from/to are read as a message's would be",
         '"requester": to if is_response else frm,\n           "traced":    frm if is_response else to,',
         '"requester": frm,\n           "traced":    to,'),
        ("unknown SNR is passed through as a real -32 dB reading",
         "out.append(None if v == UNKNOWN_SNR else v / 4.0)",
         "out.append(v / 4.0)"),
        ("quarter-dB is treated as dB",
         "out.append(None if v == UNKNOWN_SNR else v / 4.0)",
         "out.append(None if v == UNKNOWN_SNR else float(v))"),
        ("an incomplete SNR array is called complete anyway",
         '"snr_towards_complete": len(snr_t) == len(route) + 1 if is_response else False,',
         '"snr_towards_complete": is_response,'),
        ("a half-built request route is presented as a path",
         "path = ([to] + route + [frm]) if is_response else None",
         "path = [to] + route + [frm]"),
        ("the enabled flag defaults to ON",
         'if str(cfg.get("TRACEROUTE_ENABLED", "false")).lower() != "true":',
         'if str(cfg.get("TRACEROUTE_ENABLED", "true")).lower() != "true":'),
        ("the interval is read once into memory instead of from disk each pass",
         'st = read_json_file(TR_STATE, {}) or {}',
         'st = {}'),
        ("a broadcast traceroute is allowed through",
         'if not dest or dest in ("^all", "!ffffffff"):',
         'if not dest and dest in ("^all", "!ffffffff"):'),
        ("the interval stamp is never written, so every pass transmits again",
         '        st["last_ts"] = time.time()',
         '        pass'),
    ]
    bad = 0
    for why, old, new in MUTATIONS:
        if src.count(old) != 1:
            print(f"  !! could not apply ({src.count(old)} matches): {why}")
            bad += 1
            continue
        # Run the WHOLE eval against a mutated copy, in its own directory. Mutating the real
        # bridge.py in place and restoring it would leave the live file broken if this crashed
        # mid-run, and the bridge is a service.
        with tempfile.TemporaryDirectory() as td:
            open(os.path.join(td, "bridge.py"), "w").write(src.replace(old, new))
            shutil.copy(os.path.join(HERE, "eval_routes.py"), td)
            out = subprocess.run([sys.executable, "eval_routes.py"], cwd=td,
                                 capture_output=True, text=True, timeout=120)
        # A mutation is CAUGHT only by a real assertion failure. A crash, an import error or a
        # SKIP is the eval failing to run, which is not the same thing and must not read as a
        # pass — that is how a green suite ends up testing nothing.
        r = out.returncode == 1 and "failed" in out.stdout and "passed" in out.stdout
        detail = "" if r else f"  [rc={out.returncode} {out.stdout.strip().splitlines()[-1:]}]"
        print(f"  {'ok' if r else '!!'} {why}: {'CAUGHT' if r else 'SURVIVED'}{detail}")
        if not r:
            bad += 1
    if bad:
        print(f"\n{bad} mutation(s) not caught — those checks are decorative")
        sys.exit(1)

print()
if failures:
    for why, got, want in failures:
        print(f"  FAIL {why}\n       got:  {got!r}\n       want: {want!r}")
    print(f"\neval_routes: {checked - len(failures)} passed, {len(failures)} failed")
    sys.exit(1)
print(f"eval_routes: {checked} passed, 0 failed")
