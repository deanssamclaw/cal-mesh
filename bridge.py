#!/usr/bin/env python3
"""Cal's Meshtastic bridge — the single owner of the Cal HT radio.

Receives text messages  -> appends to ~/cal-mesh/inbox.jsonl
Sends text messages     -> drop a file into ~/cal-mesh/outbox/ (plain text = broadcast
                           on ch0, or JSON {"text","dest","channel","wantAck"})
Transport (serial|tcp)  -> read from ~/cal-mesh/config
Always-on               -> launchd (com.cal.mesh-bridge), single-instance via flock.

Also emits, for the dashboard:
    status.json  bridge + radio state (heartbeat every loop)
    sent.jsonl   structured log of every message sent (with metadata)
    nodes.json   neighbors currently heard (refreshed periodically)

Design note: a serial/TCP link has exactly one owner. This process IS that owner.
While it runs, do NOT run `meshtastic --port ...` against Cal HT — send via the outbox.
"""
import os, sys, time, json, glob, fcntl, threading, traceback, base64, hashlib
from datetime import datetime, timezone

BASE     = os.path.expanduser("~/cal-mesh")
INBOX    = os.path.join(BASE, "inbox.jsonl")
OUTBOX   = os.path.join(BASE, "outbox")
SENT     = os.path.join(BASE, "sent")
SENT_LOG = os.path.join(BASE, "sent.jsonl")
STATUS   = os.path.join(BASE, "status.json")
NODES    = os.path.join(BASE, "nodes.json")
SNR_HIST = os.path.join(BASE, "snr-history.jsonl")
ROUTES   = os.path.join(BASE, "routes.jsonl")
TR_QUEUE = os.path.join(BASE, "traceroute")
TR_STATE = os.path.join(BASE, "traceroute-state.json")
CONFIG   = os.path.join(BASE, "config")
LOCK     = os.path.join(BASE, "bridge.lock")

from pubsub import pub
import meshtastic, meshtastic.serial_interface, meshtastic.tcp_interface

START = time.time()


def now(): return datetime.now(timezone.utc).isoformat()
def log(m): print(f"{now()} {m}", flush=True)


def read_json_file(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, allow_nan=False)
    os.replace(tmp, path)


def load_config():
    cfg = {"TRANSPORT": "serial", "PORT": "", "HOST": "Meshtastic.local"}
    if os.path.exists(CONFIG):
        for ln in open(CONFIG):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


lost = threading.Event()
COUNTS = {"rx": 0, "tx": 0}
POS_SEEN = {"n": None}   # last logged count of position-reporting neighbours (see write_nodes)
CURRENT = {"iface": None}   # the live interface; guards against stale lost-events
SNR_APPENDS = {"n": 0}


def append_snr(node, snr):
    """Record a real per-packet SNR measurement (not a repeated snapshot)."""
    try:
        with open(SNR_HIST, "a") as f:
            f.write(json.dumps({"ts": int(time.time()), "node": node, "snr": snr}) + "\n")
        SNR_APPENDS["n"] += 1
        if SNR_APPENDS["n"] % 200 == 0:
            trim_snr()
    except Exception as e:
        log("append_snr err: " + repr(e))


def trim_snr(keep=2000):
    trim_file(SNR_HIST, keep)


def trim_file(path, keep):
    """Cap a jsonl file to its last `keep` lines. Safe for append-only files with no
    external byte-offset reader (NOT inbox.jsonl — the responder tracks an offset there)."""
    try:
        with open(path) as f:
            lines = f.readlines()
        if len(lines) > keep:
            with open(path + ".tmp", "w") as f:
                f.writelines(lines[-keep:])
            os.replace(path + ".tmp", path)
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"trim err {path}: {e!r}")


BROADCAST_NUM = 0xffffffff


def node_id(num, given):
    """A node's '!hex' id, falling back to the node NUMBER the packet always carries.

    The library resolves fromId/toId through its node database (`_nodeNumToId`), which returns
    None for a node we have not yet received a NodeInfo from — even though the raw `from` field
    in the same packet holds that node's number the whole time. So the id goes missing exactly
    at FIRST CONTACT, which is the moment we most want it: a stranger's opening message is by
    definition from a node not yet in the database. Measured live 2026-08-11: 'Hi' recorded
    from=None at 13:57:32; the sender's NodeInfo arrived 11 minutes later and it resolved to
    !ba0cc0c0 (rflab, which already knew the node, logged the id correctly at the time).

    '!%08x' is the same mapping the library uses, so a fallback id is indistinguishable from a
    resolved one — and, like every id on this mesh, still unauthenticated and spoofable."""
    if given:
        return given
    if not isinstance(num, int) or num <= 0:
        return None
    return "^all" if num == BROADCAST_NUM else f"!{num:08x}"


def hops_taken(packet):
    """(hops, hop_start, hop_limit) — how far a packet actually travelled, or None if unknown.

    hop_limit is decremented by each relay, so hops = hop_start - hop_limit. The trap: the
    library builds its packet dict with `MessageToDict`, which OMITS proto3 scalars equal to
    their default. A packet that used its ENTIRE hop budget has hop_limit 0, so the key simply
    is not there — and a naive `isinstance(hl, int)` guard reads the most-relayed packets as
    "no routing data", identical to a packet that carried none. Verified against the library:
    hop_start=3/hop_limit=0 yields dict keys ['hopStart'] only.

    hop_start is the discriminator. A sender that populates it is a sender whose hop_limit is
    meaningful, so a missing hop_limit *there* means 0. With hop_start itself absent or 0 we
    genuinely know nothing, and must return None rather than a made-up 0, which would render
    as "direct — heard straight from the sender" and be a lie."""
    hs = packet.get("hopStart")
    hl = packet.get("hopLimit")
    if not isinstance(hs, int) or hs <= 0:
        return None, hs, hl                      # no routing information at all
    if hl is None:
        hl = 0                                   # omitted by MessageToDict == fully relayed
    if not isinstance(hl, int) or not 0 <= hl <= hs:
        return None, hs, packet.get("hopLimit")  # nonsense — say unknown, don't guess
    return hs - hl, hs, hl


def pubkey_fp(pub):
    """Short, stable fingerprint of a node's public key, or None.

    The library hands this back as raw bytes or as base64 depending on path, so normalize to
    bytes first — comparing a str to bytes silently never matches, which for an auth signal
    would fail OPEN-looking (always 'no key') rather than loudly.
    """
    if not pub:
        return None
    if isinstance(pub, str):
        try:
            pub = base64.b64decode(pub)
        except Exception:
            return None
    if not isinstance(pub, (bytes, bytearray)):
        return None
    return hashlib.sha256(bytes(pub)).hexdigest()[:16]


UNKNOWN_NODE = 0xFFFFFFFF   # NODENUM_BROADCAST, what the firmware inserts for a hop it
                            # could not decrypt or name (TraceRouteModule.cpp:367)
UNKNOWN_SNR  = -128         # INT8_MIN (TraceRouteModule.cpp:376)


def _route_ids(nums):
    """Node numbers -> ids, with the firmware's 'I could not name this hop' marker kept as a
    hole rather than resolved. 0xFFFFFFFF is ALSO the broadcast address, so node_id() would
    render it '^all' — a relay that could not be identified would appear as a broadcast."""
    out = []
    for n in nums or []:
        out.append(None if n == UNKNOWN_NODE else node_id(n, None))
    return out


def _route_snrs(vals):
    """Quarter-dB int8 -> dB. -128 means unknown (TraceRouteModule.cpp:376) — but a real
    -32.0 dB encodes to -128 as well and the firmware does not disambiguate the two, so this
    reads it as unknown and that ambiguity is recorded rather than hidden."""
    out = []
    for v in vals or []:
        out.append(None if v == UNKNOWN_SNR else v / 4.0)
    return out


def _num(v, default):
    """A number from a config string or a JSON field, or the default. Never raises.

    Both sources are unvalidated: config is a text file a human edits, and traceroute-state
    is a file on disk. A bare float() on either escapes drain_traceroute into main()'s
    connection loop, which closes the interface and reconnects -- forever, because nothing
    ever repairs the file. Measured: a state file containing {"last_ts": "abc"} put the real
    main() into a permanent 60 s reconnect cycle with no message RX and no outbox TX. A
    traceroute limiter must never be able to take the radio down."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):   # NaN and infinities
        return default
    return f


def channel_util(iface):
    """Channel utilization percent as the radio measures it, or None if it cannot be read.

    None is a real answer here and it means DO NOT SEND -- see drain_traceroute. Read live from
    the interface rather than from status.json, which is written on a 30 s cycle and would let
    a stale number authorise a probe into a channel that has since filled up."""
    try:
        me = iface.getMyNodeInfo() or {}
        v = (me.get("deviceMetrics") or {}).get("channelUtilization")
        # bool is a subclass of int, so isinstance(v, (int, float)) accepts True and calls it
        # 1.0% -- a quiet channel. And NaN passes every isinstance test while comparing False
        # against everything, so a gate written as "hold if util >= limit" lets NaN straight
        # through: the one float value that MEANS unknown was reading as permission.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        f = float(v)
        if f != f or f < 0 or f > 100:      # NaN fails both comparisons, which is the point
            return None
        return f
    except Exception:
        return None


PROBE_RING = 8           # how many outstanding probes we remember
PROBE_TTL_S = 900        # and for how long: a reply after 15 min is not this probe's reply


def remember_probe(st, pkt, dest):
    """Record the packet id of a probe we just sent, so its reply can be told from a forgery.

    Without this, `witness: addressed` keyed on nothing but the packet being addressed to us,
    and the dashboard filed anything with `requester == me` under "paths Cal measured". Any
    node on the channel could transmit an unsolicited RouteDiscovery response carrying a
    request_id Cal never issued and publish an invented path -- naming relays that carried
    nothing -- on a public page, and overwrite a genuine measurement at will, because the
    dashboard is last-one-wins. Demonstrated end to end by review.

    HONEST LIMIT, recorded rather than implied away: this is not a possession proof. Our
    request floods the channel in the clear, so an attacker who HEARS it learns the id and can
    forge a matching reply. What it removes is the unsolicited forgery -- anyone, any time,
    no timing. What remains needs an active on-air attacker who saw our probe and beat the
    real node to the answer. Same shape as the dm_unlock anchor, and worth the same candour."""
    pid = getattr(pkt, "id", None) if pkt is not None else None
    if not isinstance(pid, int):
        return
    ring = [p for p in (st.get("probes") or []) if isinstance(p, dict)]
    ring.append({"id": pid, "dest": dest, "ts": time.time()})
    st["probes"] = ring[-PROBE_RING:]
    write_json(TR_STATE, st)


def probe_match(req_id, responder):
    """True only if req_id belongs to a live probe WE sent, to THIS node.

    Checking the id alone would let a forger replay a real id from a different node, which
    reads as 'the path to X' while describing nothing."""
    if not isinstance(req_id, int):
        return False
    st = read_json_file(TR_STATE, {})
    if not isinstance(st, dict):
        return False
    now_s = time.time()
    for p in (st.get("probes") or []):
        if not isinstance(p, dict):
            continue
        if p.get("id") != req_id:
            continue
        if p.get("dest") != responder:
            continue
        if now_s - _num(p.get("ts"), 0) > PROBE_TTL_S:
            continue
        return True
    return False


_OURS = {"id": None, "warned": False}


def our_node_id(iface):
    """Cal HT's own id, cached. Used only to tell a path ADDRESSED to us from one we merely
    overheard — so failing to resolve it silently would relabel every path 'overheard' and
    nothing on the page would look wrong. It logs once instead of failing quietly."""
    if _OURS["id"]:
        return _OURS["id"]
    try:
        me = iface.getMyNodeInfo() or {}
        nid = (me.get("user") or {}).get("id") or node_id(me.get("num"), None)
        if nid:
            _OURS["id"] = nid
            return nid
    except Exception as e:
        if not _OURS["warned"]:
            _OURS["warned"] = True
            log(f"our_node_id unavailable ({e!r}) — routes will all read 'overheard'")
        return None
    if not _OURS["warned"]:
        _OURS["warned"] = True
        log("our_node_id returned nothing — routes will all read 'overheard'")
    return None


def capture_route(packet, d, ours):
    """Record a traceroute we can see. Zero airtime: this only reads packets the radio has
    already received, including ones addressed to somebody else — Cal hears far more of the
    channel than talks to it (159 nodes sampled vs 18 that ever sent it text), so other
    people's traceroutes are free topology.

    Orientation, read off the library's own consumer (mesh_interface.onResponseTraceRoute):
    a RESPONSE travels from the traced node back to whoever asked, so `to` is the requester
    and `from` is the node that was traced. The path towards the destination is therefore
    [to] + route + [from], and snr_towards carries one entry PER LINK, which is one more
    than the number of intermediate hops.

    MessageToDict omits an empty repeated field entirely, so a direct path arrives with no
    'route' key at all. Absent must read as EMPTY (a direct path) and never as unknown —
    the same omission that twice dropped the hop count on this bridge."""
    tr = d.get("traceroute") or {}
    req_id = d.get("requestId")
    is_response = req_id is not None
    frm = node_id(packet.get("from"), packet.get("fromId"))
    to  = node_id(packet.get("to"), packet.get("toId"))
    route      = _route_ids(tr.get("route"))
    route_back = _route_ids(tr.get("routeBack"))
    snr_t      = _route_snrs(tr.get("snrTowards"))
    snr_b      = _route_snrs(tr.get("snrBack"))
    # The full chain only means anything on a response; a request in flight carries a route
    # that is still being built, so it is stored but not presented as a path.
    path = ([to] + route + [frm]) if is_response else None
    rec = {"ts": now(),
           "kind": "response" if is_response else "request",
           # who asked and who was traced, in traceroute terms rather than packet terms
           "requester": to if is_response else frm,
           "traced":    frm if is_response else to,
           "path": path,
           "route": route, "snr_towards": snr_t,
           "route_back": route_back, "snr_back": snr_b,
           # a link count, not a node count: a direct path has one link and no intermediates
           "links": (len(route) + 1) if is_response else None,
           # SNR arrays are complete only when they carry one entry per link. An incomplete
           # array is not padded — a missing reading is a missing reading.
           "snr_towards_complete": len(snr_t) == len(route) + 1 if is_response else False,
           "snr_back_complete": len(snr_b) == len(route_back) + 1 if is_response else False,
           # Cal spent no airtime on an overheard one. Worth keeping: it is the whole reason
           # this tier is free, and it is also the honest label for a path nobody asked us for.
           # THREE states, not two. "addressed" now means "a reply to a probe we sent, from
           # the node we sent it to" -- the only kind that may be published as a path Cal
           # measured. A reply addressed to us that matches no outstanding probe is
           # "unsolicited": recorded, because it is worth seeing, and never presented as ours.
           # FOUR states, and each is a different relationship to the measurement:
           #   addressed   -- a reply to a probe WE sent, to the node we sent it to. The only
           #                  kind that may ever be published as a path Cal measured.
           #   unsolicited -- a reply addressed to us matching no outstanding probe. Recorded
           #                  because it is worth seeing, never presented as ours.
           #   probed      -- somebody is tracing CAL. Ordinary mesh behaviour, and not a
           #                  measurement of anything, but it is not "overheard" either.
           #   overheard   -- traffic between other nodes. Real topology, says nothing about
           #                  how Cal reaches anyone.
           "witness": ("addressed" if (ours and to == ours and is_response
                                       and probe_match(req_id, frm))
                       else "unsolicited" if (ours and to == ours and is_response)
                       else "probed" if (ours and to == ours)
                       else "overheard"),
           "packet_id": packet.get("id"), "request_id": req_id,
           "rx_snr": packet.get("rxSnr"), "rx_rssi": packet.get("rxRssi")}
    with open(ROUTES, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False, allow_nan=False) + "\n")
    COUNTS["routes"] = COUNTS.get("routes", 0) + 1
    if is_response:
        log(f"ROUTE {rec['witness']}: " + " -> ".join(h or "?" for h in path))
    return rec


def on_receive(packet=None, interface=None):
    try:
        if not packet:
            return
        # Record SNR from EVERY received packet (telemetry/position/text) — real
        # signal samples per node over time, feeding the dashboard sparklines.
        frm = node_id(packet.get("from"), packet.get("fromId"))
        snr = packet.get("rxSnr")
        if frm and snr is not None:
            append_snr(frm, snr)
        d = packet.get("decoded") or {}
        if d.get("portnum") == "TRACEROUTE_APP":
            capture_route(packet, d, our_node_id(interface))
            return
        if d.get("portnum") != "TEXT_MESSAGE_APP":
            return
        hops, hs, hl = hops_taken(packet)
        # relayNode is a ONE-BYTE truncation of the last relayer's node number, so it narrows
        # the candidates but does not identify a node. Stored raw; never resolved to a name.
        relay = packet.get("relayNode")
        # Sender authentication signal. `pkiEncrypted` is a proto3 bool with no presence, so
        # FALSE IS OMITTED ENTIRELY by MessageToDict — the same omission that discarded the
        # hop count twice. Absent must therefore read as NOT authenticated, never as unknown
        # and never as true. `is True` rather than truthiness so a stray string cannot pass.
        # This records the signal only; nothing yet decides anything with it. Note the
        # documented downgrade attack: a forged DM can present as PKC, so this is evidence,
        # not proof — which is why only forge-TOLERANT things may ever key on it.
        pki = packet.get("pkiEncrypted") is True
        pub = packet.get("publicKey")
        rec = {"ts": now(), "from": frm, "to": node_id(packet.get("to"), packet.get("toId")),
               "channel": packet.get("channel", 0), "text": d.get("text", ""),
               "snr": packet.get("rxSnr"), "rssi": packet.get("rxRssi"),
               "id": packet.get("id"),
               "hops": hops, "hop_start": hs, "hop_limit": hl,
               "relay_byte": relay if isinstance(relay, int) else None,
               "pki": pki,
               # A node's public key is public by design (it is broadcast in NodeInfo), but the
               # full value is not useful here and the dashboard is public — keep a short
               # fingerprint for display and comparison instead of the key itself.
               "pubkey_fp": pubkey_fp(pub)}
        with open(INBOX, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False, allow_nan=False) + "\n")
        COUNTS["rx"] += 1
        log(f"RX {rec['from']} -> {rec['to']} ch{rec['channel']}: {rec['text']!r}")
    except Exception as e:
        log("on_receive err: " + repr(e))


def on_lost(interface=None):
    # Ignore a late 'lost' from a superseded interface — it would needlessly cycle
    # a freshly-connected healthy link.
    if CURRENT["iface"] is not None and interface is not None and interface is not CURRENT["iface"]:
        return
    log("connection.lost")
    lost.set()


def connect(cfg):
    if cfg["TRANSPORT"].lower() == "tcp":
        host = cfg.get("HOST") or "Meshtastic.local"
        log(f"connecting TCP {host}:4403")
        return meshtastic.tcp_interface.TCPInterface(hostname=host)
    port = cfg.get("PORT") or None
    if port and not os.path.exists(port):
        log(f"configured serial port {port} absent; auto-detecting")
        port = None
    log(f"connecting serial {port or '(auto)'}")
    return meshtastic.serial_interface.SerialInterface(devPath=port)


def node_summary(iface):
    """Best-effort snapshot of my node + radio metrics."""
    out = {}
    try:
        me = iface.getMyNodeInfo() or {}
        u = me.get("user", {})
        dm = me.get("deviceMetrics", {})
        out["node"] = {"id": u.get("id"), "num": me.get("num"),
                       "longName": u.get("longName"), "shortName": u.get("shortName"),
                       "hwModel": u.get("hwModel")}
        out["metrics"] = {"battery": dm.get("batteryLevel"), "voltage": dm.get("voltage"),
                          "chUtil": dm.get("channelUtilization"), "airUtilTx": dm.get("airUtilTx"),
                          "uptime": dm.get("uptimeSeconds")}
    except Exception as e:
        out["node_err"] = repr(e)
    try:
        md = getattr(iface, "metadata", None)
        if md is not None:
            out["firmware"] = getattr(md, "firmware_version", None)
    except Exception:
        pass
    return out


def write_status(cfg, connected, iface=None):
    st = {"ts": now(), "pid": os.getpid(),
          "uptime_s": round(time.time() - START),
          "transport": cfg["TRANSPORT"],
          "port": cfg.get("PORT"), "host": cfg.get("HOST"),
          "connected": connected,
          "counts": dict(COUNTS)}
    if connected and iface is not None:
        st.update(node_summary(iface))
    write_json(STATUS, st)


def write_nodes(iface):
    try:
        rows = []
        for nid, n in (iface.nodes or {}).items():
            u = n.get("user", {})
            # The key FINGERPRINT, never the key. A node's public key is public by design, but
            # this feeds a public page and the fingerprint is all that is needed to check that
            # an authenticated DM carries the same key the node advertised in its NodeInfo —
            # heard over the air separately from the DM, so it is an independent record.
            rows.append({"id": nid, "short": u.get("shortName"), "long": u.get("longName"),
                         "hw": u.get("hwModel"), "hops": n.get("hopsAway"),
                         "snr": n.get("snr"), "lastHeard": n.get("lastHeard"),
                         "pubkey_fp": pubkey_fp(u.get("publicKey"))})
        rows.sort(key=lambda r: (r["hops"] if r["hops"] is not None else 99,
                                 -(r["snr"] or -999)))
        # Positions are deliberately NOT stored or published: nodes.json feeds a PUBLIC page,
        # and Cal HT sits at a fixed private location. Log only the COUNT of neighbours that
        # report a position — enough to know whether a private map is even feasible, and it
        # goes to the local log, never to the API. Logged on change only.
        try:
            with_pos = sum(1 for n in (iface.nodes or {}).values()
                           if (n.get("position") or {}).get("latitude") is not None)
            # Whether OUR OWN node advertises a position is the decisive one: if it does, the
            # base station's fixed location is already going out over the air to everyone in
            # range, which is a different problem from what this dashboard publishes.
            me = getattr(iface, "myInfo", None)
            my_num = getattr(me, "my_node_num", None)
            self_pos = any((n.get("position") or {}).get("latitude") is not None
                           for nid, n in (iface.nodes or {}).items()
                           if n.get("num") == my_num)
            if (with_pos, self_pos) != POS_SEEN.get("n"):
                POS_SEEN["n"] = (with_pos, self_pos)
                log(f"nodedb: {with_pos}/{len(rows)} neighbours report a position; "
                    f"OUR node advertises a position: {self_pos} "
                    f"(counts only — coordinates are never stored or published)")
        except Exception:
            pass
        write_json(NODES, {"ts": now(), "count": len(rows), "nodes": rows})
    except Exception as e:
        log("write_nodes err: " + repr(e))


def drain_outbox(iface, transport):
    for p in sorted(glob.glob(os.path.join(OUTBOX, "*"))):
        if os.path.isdir(p):
            continue
        try:
            raw = open(p).read().strip()
            if not raw:
                os.remove(p)
                continue
            text, dest, ch, ack, source = raw, "^all", 0, False, "manual"
            if raw.lstrip().startswith("{"):
                j = json.loads(raw)
                text = j.get("text", "")
                dest = j.get("dest", "^all")
                ch = int(j.get("channel", 0))
                ack = bool(j.get("wantAck", False))
                source = j.get("source", "manual")
            iface.sendText(text, destinationId=dest, channelIndex=ch, wantAck=ack)
            COUNTS["tx"] += 1
            rec = {"ts": now(), "dest": dest, "channel": ch, "text": text,
                   "wantAck": ack, "transport": transport, "bytes": len(text.encode()),
                   "source": source}
            with open(SENT_LOG, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False, allow_nan=False) + "\n")
            log(f"TX -> {dest} ch{ch}: {text!r}")
            os.replace(p, os.path.join(SENT, f"{os.path.basename(p)}.{int(time.time())}"))
        except Exception as e:
            log(f"TX err {p}: {e!r}")
            try:
                os.replace(p, os.path.join(SENT, f"{os.path.basename(p)}.err.{int(time.time())}"))
            except Exception:
                pass


_DEST_RE = __import__("re").compile(r"^!?[0-9a-fA-F]{8}$")


def valid_dest(dest):
    """A traceroute destination, normalised -- or None if it is not one we will send to.

    This is an ALLOWLIST because the denylist it replaces was four characters wide. The old
    test was `dest in ("^all", "!ffffffff")`, and `!FFFFFFFF`, `^ALL`, `'^all '` and the bare
    integer 4294967295 all walked past it; the library then resolves every one of them to the
    broadcast number. The firmware refuses a multi-hop broadcast traceroute so no airtime is
    spent, but it becomes exactly the silent no-op the guard exists to prevent -- and it would
    burn the interval and consume the queue entry for a transmission that never happened.

    It also stops the library's own our_exit(): a destination under 8 characters that is not in
    the node DB makes meshtastic call sys.exit(1). SystemExit is a BaseException, so it escapes
    every `except Exception` here, the queue entry is never quarantined, launchd restarts the
    bridge, and it dies on the same entry again. A typo becomes an unbounded crash loop."""
    if isinstance(dest, bool) or dest is None:
        return None
    if isinstance(dest, int):
        dest = f"!{dest:08x}"
    dest = str(dest).strip()
    if not _DEST_RE.match(dest):
        return None
    dest = "!" + dest.lstrip("!").lower()
    if int(dest[1:], 16) == 0xFFFFFFFF:
        return None
    return dest


def drain_traceroute(iface, cfg):
    """Send at most ONE queued traceroute per pass, and only if every guard agrees.

    Traceroute is the most expensive thing Cal can put on the air for the least payload: one
    3-hop probe is roughly 5.7 s of channel occupancy across 8 transmissions, because the
    request floods out and the reply floods back and every relay in earshot repeats both. The
    firmware will NOT hold us to that. Its only limit is 30 s per TCP client session on the
    client->radio path (PhoneAPI.cpp:828), and it is a member of the PhoneAPI instance, so it
    RESETS whenever the client reconnects -- which this bridge does on every dropped link.
    There is no responder-side and no relay-side throttle anywhere in the firmware.

    So the interval is enforced HERE, and it is kept ON DISK for exactly that reason: an
    in-memory timer would reset on the same reconnect that resets the firmware's, and the two
    would fail together at precisely the moment a flapping link was already retrying.

    A traceroute to the broadcast address with any hop limit is refused by the firmware
    (PhoneAPI.cpp:835) and is refused here too, so it never becomes a silent no-op.

    WHEN it is polite to probe is not a number invented here. The firmware already defines it,
    for exactly this category of traffic. `AirTime::isTxAllowedChannelUtil(polite=true)` tests
    channel utilization against `polite_channel_util_percent = 25` (airtime.h:71), and the
    precedent for using it is `MeshService.cpp:99`: when the node hears a stranger and wants to
    volunteer its own NodeInfo -- unsolicited, self-initiated metadata, the same category a
    traceroute falls in -- it checks that gate and logs "Skip sending NodeInfo > 25% ch. util".
    40% (`max_channel_util_percent`) is where the firmware stops sending anything at all, so 25
    is the polite floor and not the hard one. Their own word for the budget is in the comment
    on the line below it: "half of Duty Cycle allowance is ok for METADATA".

    The duty-cycle arm of that test is inert for us and is not implemented here rather than
    implemented and left always-true: `isTxAllowedAirUtil` only binds where `dutyCycle < 100`,
    and the US region is `RDEF(US, 902.0f, 928.0f, 100, ...)` (RadioInterface.cpp:53).

    The interval is no longer a politeness rule -- the channel-state gate is. It survives as a
    MEASUREMENT floor: channelUtilizationPercent() averages 6 ten-second buckets
    (CHANNEL_UTILIZATION_PERIODS, airtime.h:28), so it describes the last 60 seconds, and
    probing faster than that reads a number that does not yet contain our own last probe. The
    reply floods for several seconds after the request, so this is the difference between
    measuring the channel and measuring the channel as it was before we touched it.

    Unknown channel state FAILS CLOSED. Not being able to tell how busy the air is, is not a
    reason to transmit into it."""
    if not os.path.isdir(TR_QUEUE):
        return
    pending = sorted(p for p in glob.glob(os.path.join(TR_QUEUE, "*")) if not os.path.isdir(p))
    if not pending:
        return
    if str(cfg.get("TRACEROUTE_ENABLED", "false")).lower() != "true":
        return
    st = read_json_file(TR_STATE, {})
    if not isinstance(st, dict):
        st = {}
    min_gap = _num(cfg.get("TRACEROUTE_MIN_GAP_S", 60), 60)
    last = _num(st.get("last_ts"), 0)
    waited = time.time() - last
    if waited < min_gap:
        return
    util = channel_util(iface)
    limit = _num(cfg.get("TRACEROUTE_MAX_CH_UTIL", 25), 25)
    if util is None:
        log("traceroute held: channel utilization unknown")
        return
    if util >= limit:
        log(f"traceroute held: channel utilization {util:.1f}% >= {limit:.0f}%")
        return
    path = pending[0]
    try:
        raw = open(path).read().strip()
        want = json.loads(raw).get("dest") if raw.startswith("{") else raw
        dest = valid_dest(want)
        if dest is None:
            raise ValueError(f"refusing traceroute destination {want!r}")
        hop_limit = int(_num(cfg.get("TRACEROUTE_HOP_LIMIT", 3), 3))
        hop_limit = max(1, min(7, hop_limit))      # HOP_MAX is 7 (MeshTypes.h:38)
        # RESERVE THE SLOT BEFORE SPENDING THE AIRTIME. This used to send first and stamp
        # after, both inside one try: a state write that failed left the airtime already spent
        # and the interval never stamped, so the next pass -- one second later -- sent again.
        # Measured on a read-only state directory: ten probes in ten seconds, roughly 57 s of
        # channel occupancy, reported only as a log line that reads like a refusal. Stamping
        # first fails toward silence, which is the correct direction for a shared channel.
        st["last_ts"] = time.time()
        st["sent"] = int(_num(st.get("sent"), 0)) + 1
        st["last_dest"] = dest
        st["last_ch_util"] = util
        write_json(TR_STATE, st)
        from meshtastic.protobuf import mesh_pb2 as _m, portnums_pb2 as _pn
        pkt = iface.sendData(_m.RouteDiscovery(), destinationId=dest,
                             portNum=_pn.PortNum.TRACEROUTE_APP, wantResponse=True,
                             hopLimit=hop_limit)
        remember_probe(st, pkt, dest)
        COUNTS["tr_tx"] = COUNTS.get("tr_tx", 0) + 1
        gap = "first" if not last else f"waited {waited:.0f}s"
        log(f"TRACEROUTE -> {dest} hopLimit={hop_limit} "
            f"(chUtil {util:.1f}%, {gap}, total {st['sent']})")
        os.replace(path, os.path.join(SENT, f"tr.{os.path.basename(path)}.{int(time.time())}"))
    except BaseException as e:                       # noqa: BLE001
        # BaseException on purpose. The meshtastic library calls sys.exit(1) for an
        # unresolvable destination, and SystemExit walks past `except Exception` -- past the
        # quarantine below too, so the poison entry stayed in the queue and the bridge crash
        # -looped on it through every launchd restart. valid_dest should now prevent that;
        # this is the belt for the braces. KeyboardInterrupt is still allowed to stop us.
        if isinstance(e, KeyboardInterrupt):
            raise
        log(f"traceroute err {path}: {e!r}")
        try:
            os.replace(path, os.path.join(SENT, f"tr.{os.path.basename(path)}.err.{int(time.time())}"))
        except Exception:
            pass


def main():
    os.makedirs(OUTBOX, exist_ok=True)
    os.makedirs(TR_QUEUE, exist_ok=True)
    os.makedirs(SENT, exist_ok=True)

    lf = open(LOCK, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another bridge holds the lock; exiting")
        sys.exit(0)
    lf.write(str(os.getpid()))
    lf.flush()

    pub.subscribe(on_receive, "meshtastic.receive")
    pub.subscribe(on_lost, "meshtastic.connection.lost")
    log("cal-mesh bridge starting")

    backoff = 8
    while True:
        cfg = load_config()
        iface = None
        conn_start = None
        lost.clear()
        write_status(cfg, False)
        try:
            iface = connect(cfg)
            CURRENT["iface"] = iface
            conn_start = time.time()
            log(f"connected via {cfg['TRANSPORT']}")
            write_status(cfg, True, iface)
            write_nodes(iface)
            last_nodes = time.time()
            last_trim = time.time()
            while not lost.is_set():
                drain_outbox(iface, cfg["TRANSPORT"])
                drain_traceroute(iface, cfg)
                if time.time() - last_nodes > 30:
                    write_nodes(iface)
                    last_nodes = time.time()
                if time.time() - last_trim > 300:
                    trim_file(SENT_LOG, 5000)   # inbox is NOT trimmed (responder offset)
                    # routes.jsonl is written entirely by packets OTHER PEOPLE send, and
                    # a RouteDiscovery fills a 237-byte payload: measured 6.4x
                    # amplification, ~130 MB/day at 1 pkt/s, from a sender spending
                    # nothing. Untrimmed it fills the disk (which is also what makes the
                    # state write fail) and evicts every genuine path from the
                    # dashboard's 256 KB tail.
                    trim_file(ROUTES, 2000)
                    last_trim = time.time()
                write_status(cfg, True, iface)
                time.sleep(1)
            log("lost flag set -> cycling")
        except Exception as e:
            log("conn loop err: " + repr(e))
            log(traceback.format_exc().strip())
        finally:
            CURRENT["iface"] = None
            if iface:
                try:
                    iface.close()
                except Exception as e:
                    log("close err: " + repr(e))
        write_status(cfg, False)
        # Exponential backoff: reset after a healthy session, grow when flapping.
        if conn_start and time.time() - conn_start > 30:
            backoff = 8
        else:
            backoff = min(backoff * 2, 60)
        log(f"reconnecting in {backoff}s")
        time.sleep(backoff)


if __name__ == "__main__":
    main()
