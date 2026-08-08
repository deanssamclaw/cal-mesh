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
import os, sys, time, json, glob, fcntl, threading, traceback
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


def on_receive(packet=None, interface=None):
    try:
        if not packet:
            return
        # Record SNR from EVERY received packet (telemetry/position/text) — real
        # signal samples per node over time, feeding the dashboard sparklines.
        frm = packet.get("fromId")
        snr = packet.get("rxSnr")
        if frm and snr is not None:
            append_snr(frm, snr)
        d = packet.get("decoded") or {}
        if d.get("portnum") != "TEXT_MESSAGE_APP":
            return
        rec = {"ts": now(), "from": packet.get("fromId"), "to": packet.get("toId"),
               "channel": packet.get("channel", 0), "text": d.get("text", ""),
               "snr": packet.get("rxSnr"), "rssi": packet.get("rxRssi"),
               "id": packet.get("id")}
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
            rows.append({"id": nid, "short": u.get("shortName"), "long": u.get("longName"),
                         "hw": u.get("hwModel"), "hops": n.get("hopsAway"),
                         "snr": n.get("snr"), "lastHeard": n.get("lastHeard")})
        rows.sort(key=lambda r: (r["hops"] if r["hops"] is not None else 99,
                                 -(r["snr"] or -999)))
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
