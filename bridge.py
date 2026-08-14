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
CONFIG   = os.path.join(BASE, "config")
LOCK     = os.path.join(BASE, "bridge.lock")

from pubsub import pub
import meshtastic, meshtastic.serial_interface, meshtastic.tcp_interface

START = time.time()


def now(): return datetime.now(timezone.utc).isoformat()
def log(m): print(f"{now()} {m}", flush=True)


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False)
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
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
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
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            log(f"TX -> {dest} ch{ch}: {text!r}")
            os.replace(p, os.path.join(SENT, f"{os.path.basename(p)}.{int(time.time())}"))
        except Exception as e:
            log(f"TX err {p}: {e!r}")
            try:
                os.replace(p, os.path.join(SENT, f"{os.path.basename(p)}.err.{int(time.time())}"))
            except Exception:
                pass


def main():
    os.makedirs(OUTBOX, exist_ok=True)
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
                if time.time() - last_nodes > 30:
                    write_nodes(iface)
                    last_nodes = time.time()
                if time.time() - last_trim > 300:
                    trim_file(SENT_LOG, 5000)   # inbox is NOT trimmed (responder offset)
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
