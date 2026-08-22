#!/usr/bin/env python3
"""cal-mesh dashboard — a read-only view of every lever behind Cal on the mesh.

Serves:
    /            the CURRENT dashboard (v4 — exchanges + a per-exchange trace on a dark panel)
    /old-1       v1, retired 2026-08-09 (two-column inbound/outbound). See PAGES below:
                 an old-N slot is assigned once and never renumbered, so a link keeps
                 pointing at the same page forever.
    /old-2       v2, retired 2026-08-12 (exchanges + a flat decision trace)
    /old-3       v3, retired 2026-08-19 (the trace drawn with depth, all-light palette)
    /old-4       v4, retired 2026-08-21 (dark trace panel; the build queue as a loose card)
    /api/state   JSON aggregate of bridge status, transports, sent/recv logs, neighbors
    /api/snr     per-node SNR time series (last hour)
    /api/routes  harvested traceroute paths, split into ours vs overheard
    /api/stats   daily decision aggregates (replies, skips, gen latency)

No third-party deps (stdlib only) so it's trivially exposable via Tailscale Funnel later,
just like the rflab mesh dashboard. Binds localhost for now.
"""
import os, json, http.server, socketserver, subprocess, threading, time
from urllib.parse import urlparse

BASE     = os.path.expanduser("~/cal-mesh")
STATUS   = os.path.join(BASE, "status.json")
NODES    = os.path.join(BASE, "nodes.json")
CONFIG   = os.path.join(BASE, "config")
INBOX    = os.path.join(BASE, "inbox.jsonl")
SENT_LOG = os.path.join(BASE, "sent.jsonl")
RSTATE   = os.path.join(BASE, "responder-state.json")
DECISIONS = os.path.join(BASE, "decisions.jsonl")
SNR_HIST = os.path.join(BASE, "snr-history.jsonl")
ROUTES_LOG = os.path.join(BASE, "routes.jsonl")
PORT     = int(os.environ.get("CALMESH_PORT", "8787"))
BIND     = os.environ.get("CALMESH_BIND", "127.0.0.1")


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def count_lines(path):
    try:
        with open(path) as f:
            return sum(1 for ln in f if ln.strip())
    except Exception:
        return 0


def tail_jsonl(path, n, maxbytes=262144):
    """Read only the last `maxbytes` of the file (never the whole thing), then take
    the last n complete lines. Bounds memory/IO regardless of file size."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > maxbytes:
                f.seek(size - maxbytes)
                f.readline()   # discard the partial first line
            data = f.read()
    except Exception:
        return []
    out = []
    for ln in data.decode("utf-8", "replace").splitlines()[-n:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    out.reverse()
    return out


# --- short response cache + concurrency cap (public, unauthenticated endpoints) ---
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_SEM = threading.BoundedSemaphore(32)


def cached(key, ttl, fn):
    now = time.time()
    with _CACHE_LOCK:
        e = _CACHE.get(key)
        if e and now - e[0] < ttl:
            return e[1]
    v = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (now, v)
    return v


def read_config():
    cfg = {}
    try:
        for ln in open(CONFIG):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


_LD_CACHE = {"ts": 0.0, "val": None}


def launchd_running():
    import time
    if _LD_CACHE["val"] is not None and time.time() - _LD_CACHE["ts"] < 5:
        return _LD_CACHE["val"]
    try:
        uid = os.getuid()
        out = subprocess.run(["launchctl", "print", f"gui/{uid}/com.cal.mesh-bridge"],
                             capture_output=True, text=True, timeout=5).stdout
        state = "running" if "state = running" in out else "stopped"
        pid = None
        for ln in out.splitlines():
            if "pid =" in ln:
                pid = ln.split("=")[1].strip()
        val = {"state": state, "pid": pid}
    except Exception:
        val = {"state": "unknown", "pid": None}
    _LD_CACHE["ts"] = time.time()
    _LD_CACHE["val"] = val
    return val


def build_snr(window=3600, cap=120):
    """Per-node SNR series over the last `window` seconds, for the sparklines."""
    import time
    cutoff = time.time() - window
    series = {}
    try:
        with open(SNR_HIST) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                ts = r.get("ts")
                node = r.get("node")
                snr = r.get("snr")
                if ts is None or node is None or snr is None:
                    continue
                if ts < cutoff:
                    continue
                series.setdefault(node, []).append([ts, snr])
    except Exception:
        pass
    short = {n["id"]: n.get("short") for n in read_json(NODES, {}).get("nodes", [])}
    out = {}
    for node, pts in series.items():
        pts.sort()
        if len(pts) > cap:
            pts = pts[-cap:]
        out[node] = {"short": short.get(node), "points": pts}
    return out


def build_routes(keep=400):
    """Harvested traceroute paths, split by whether they are OURS to claim.

    The distinction is the whole point and it is easy to get wrong. A response Cal ASKED FOR
    describes the path between Cal and that node. A response Cal merely OVERHEARD describes
    the path between two other nodes — it is real topology, but it says nothing about how Cal
    reaches either of them, and presenting it next to a message as "the path" would be a
    fabrication dressed as a measurement.

    So `ours` is keyed by the far endpoint and holds only paths where Cal was the requester;
    `others` is everything else, kept as topology and labelled as such."""
    me = (read_json(STATUS, {}).get("node") or {}).get("id")
    ours, others = {}, []
    for r in tail_jsonl(ROUTES_LOG, keep):
        if r.get("kind") != "response":
            continue
        path = r.get("path") or []
        if len(path) < 2:
            continue
        rec = {"ts": r.get("ts"), "path": path,
               "snr_towards": r.get("snr_towards") or [],
               "snr_back": r.get("snr_back") or [],
               "snr_towards_complete": bool(r.get("snr_towards_complete")),
               "snr_back_complete": bool(r.get("snr_back_complete")),
               "links": r.get("links"), "witness": r.get("witness"),
               "requester": r.get("requester"), "traced": r.get("traced")}
        # `witness` is the bridge's verdict, and it is the only thing that may promote a path
        # to "ours". Keying on requester == me was forgeable by anyone on the channel: an
        # unsolicited response carrying any request_id set requester to Cal and published an
        # invented path, naming relays that carried nothing, on this public page. The bridge
        # now marks a reply "addressed" only when it answers a probe Cal actually sent, to the
        # node Cal sent it to; "unsolicited" is the new third state and is NOT ours.
        if me and r.get("witness") == "addressed" and r.get("requester") == me:
            far = r.get("traced")
            # last one wins: a path is a point-in-time measurement and the newest is the one
            # that still might be true.
            if far:
                ours[far] = rec
        else:
            others.append(rec)
    others = others[-60:]
    return {"me": me, "ours": ours, "others": others,
            "counts": {"ours": len(ours), "others": len(others)}}


def _epoch(s):
    """ISO8601 -> epoch seconds, or None. Tolerates the 'Z' suffix."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _pair_nearest(records, cands, rec_key, cand_key, window=300):
    """Greedy one-to-one pairing: for each record, take the unconsumed candidate with the
    same key whose timestamp is nearest, within `window` seconds. Keyed on exact text so a
    repeated identical message can never cross-pair with the wrong reply — the nearest-ts
    tiebreak plus consumption keeps repeats in order. Returns [(record, candidate|None)]."""
    buckets = {}
    for c in cands:
        buckets.setdefault(cand_key(c), []).append([_epoch(c.get("ts")), c, False])
    out = []
    for r in records:
        k, rt = rec_key(r), _epoch(r.get("ts"))
        best, best_dt = None, None
        for ent in buckets.get(k, []):
            if ent[2] or ent[0] is None or rt is None:
                continue
            dt = abs(ent[0] - rt)
            if dt <= window and (best_dt is None or dt < best_dt):
                best, best_dt = ent, dt
        if best is not None:
            best[2] = True
            out.append((r, best[1]))
        else:
            out.append((r, None))
    return out


def correlate(inbox, sent, decisions):
    """Attach each side of a conversation to the other, so the UI never has to guess.

      * every inbound message gets the responder's verdict for it (replied / skipped +
        reason) and, when replied, the reply text — including the messages that were
        received perfectly well but came from a node that is NOT on the allow-list
      * every responder-sent message gets the inbound message it was answering

    Both directions are computed here rather than in the browser so the pairing logic has
    one implementation and the API is useful on its own."""
    for rec, dec in _pair_nearest(inbox, decisions,
                                  lambda r: (r.get("from"), r.get("text")),
                                  lambda c: (c.get("from"), c.get("text"))):
        if dec is None:
            rec["verdict"] = None          # not yet evaluated (or predates the responder)
            continue
        rec["verdict"] = "replied" if dec.get("matched") else "skipped"
        rec["reason"] = dec.get("reason")
        rec["reply"] = dec.get("reply")
        rec["gen_ms"] = dec.get("gen_ms")
        rec["capability"] = dec.get("capability")
        # the decision trace (machinery, not introspection). Absent on records written
        # before the responder logged it — the UI degrades to "no trace recorded".
        rec["trace"] = {k: dec.get(k) for k in
                        ("gates", "sanitize", "prompt_kind", "model", "injected_fact",
                         "weather_ok", "gen_status", "injection_flagged", "dest",
                         "obs_station", "obs_age_s", "forecast_asked", "trigger_match",
                         "greeting_gates", "greeting_reason", "calc",
                         "sunmoon_match", "sunmoon",
                         # authenticated-DM path: so the trace can say the model also got the
                         # injected context + remembered thread, not just the message.
                         "dm_unlock", "dm_memory_stored")
                        if dec.get(k) is not None}

    replied = [d for d in decisions if d.get("matched") and d.get("reply")]
    auto = [s for s in sent if s.get("source") == "responder"]
    for snt, dec in _pair_nearest(auto, replied,
                                  lambda s: s.get("text"),
                                  lambda c: c.get("reply")):
        if dec is not None:
            snt["in_reply_to"] = {"from": dec.get("from"), "text": dec.get("text"),
                                  "ts": dec.get("ts")}
            snt["gen_ms"] = dec.get("gen_ms")
    return inbox, sent


def build_exchanges(inbox, sent):
    """One ordered stream of everything that happened on air, as exchanges.

    Almost every exchange starts with Cal being prompted, so the inbound message is the head
    and Cal's reply (or the reason there wasn't one) hangs off it. Two things genuinely don't
    fit that shape and must not silently vanish from a page that claims to show everything:

      * unprompted outbound — an operator send, and later a proactive transmission: Cal
        talking with no ask above it
      * overheard inbound — channel traffic never addressed to Cal at all

    The first becomes its own entry kind; the second is just an exchange whose reply is a
    non-reply, which the trace explains."""
    items = [dict(r, kind="exchange") for r in inbox]
    items += [dict(s, kind="unprompted") for s in sent if not s.get("in_reply_to")]
    items.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return items


# Only these config keys are ever exposed on the (public) API. Everything else —
# including ALLOW_FROM node IDs and any future secret — is withheld by default.
# PORT is deliberately excluded — the serial path embeds the device MAC.
PUBLIC_CONFIG_KEYS = ("TRANSPORT", "HOST", "RESPONDER_ENABLED", "RESPONDER_MODEL")


def build_decision_stats():
    """Aggregate decisions.jsonl into per-day stats: counts by verdict, avg gen_ms."""
    from collections import defaultdict
    by_day = defaultdict(lambda: {"replied": 0, "skipped": 0, "skip_reasons": {}, "gen_ms": []})
    try:
        with open(DECISIONS) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                day = (r.get("ts") or "")[:10]  # YYYY-MM-DD
                if not day:
                    continue
                e = by_day[day]
                if r.get("matched"):
                    e["replied"] += 1
                    ms = r.get("gen_ms")
                    if ms is not None:
                        e["gen_ms"].append(ms)
                else:
                    e["skipped"] += 1
                    reason = r.get("reason") or "unknown"
                    e["skip_reasons"][reason] = e["skip_reasons"].get(reason, 0) + 1
    except Exception:
        pass
    days = []
    for day in sorted(by_day.keys(), reverse=True):
        e = by_day[day]
        ms_list = e["gen_ms"]
        days.append({
            "date": day,
            "replied": e["replied"],
            "skipped": e["skipped"],
            "skip_reasons": e["skip_reasons"],
            "avg_gen_ms": round(sum(ms_list) / len(ms_list)) if ms_list else None,
            "max_gen_ms": max(ms_list) if ms_list else None,
            "min_gen_ms": min(ms_list) if ms_list else None,
        })
    return {"days": days[:30]}


def split_streams(records):
    """Split traffic into (broadcast, direct). BOTH are published — deliberately.

    The direct channel is Dean and Cal's test bench: a place to try things without spending
    everyone's airtime on the open channel. Publishing it is the point, not an oversight —
    the experiments are the interesting part and the page exists to show the machinery.

    So this is a SPLIT, not a filter, and the classification is a whitelist in both
    directions: a broadcast is one exact value, and everything else is direct. An addressing
    form this code has never seen lands in `direct`, which is the labelled, explained stream
    rather than the one a stranger reads as open-channel chatter.

    History worth keeping: this started life as `public_only`, withholding directed traffic
    entirely, after the first real DM was found being served through three separate paths at
    once. The withholding was wrong for what this channel is for — but the finding that
    matters still stands, and is why the split lives at ONE choke point instead of in each
    consumer: nothing should reach the page without a decision having been made about it.
    """
    bcast, direct = [], []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        # Inbound records carry `to`, outbound carry `dest`. NOT dict.get("to", r["dest"]) —
        # that returns None when the key exists and is null, so a record with an explicit
        # `"to": null` would never fall through to `dest` and would be misfiled.
        dest = r.get("to") if r.get("to") is not None else r.get("dest")
        (bcast if dest == "^all" else direct).append(r)
    return bcast, direct


LEDGER   = os.path.join(BASE, "gap-ledger.json")
TRIAGE   = os.path.join(BASE, "triage.json")
LHISTORY = os.path.join(BASE, "learn-history.jsonl")


def build_learning(top=6, runs=20):
    """What the distiller has found, what got built for it, and whether that shipped.

    PUBLISHED DELIBERATELY, and the reason is the same one that publishes the decision trace:
    a capability list anyone can read is a claim; a queue showing what Cal still cannot answer,
    with the commit that fixed the last one, is a demonstration. The awkward numbers are the
    load-bearing ones -- recurrences, corrections, and how many gaps this loop found on its own
    versus how many a person spotted by hand.

    Cluster keys are inbound message text, which is already published verbatim in the exchange
    streams above; node ids are NOT carried through, because nothing here needs them.
    """
    agg = read_json(LEDGER, {}) or {}
    tr = read_json(TRIAGE, {}) or {}
    clusters = agg.get("clusters", {}) or {}

    def tot(c):
        return sum((c.get("counts") or {}).values()) or c.get("count", 0)

    def vd(k):
        v = tr.get(k)
        return v if isinstance(v, dict) else None

    def rec_flag(k, c):
        v = vd(k)
        return bool(v and v.get("armed")
                    and (c.get("last_by_bucket", {}) or {}).get("GAP", "") > v["armed"])

    ranked = sorted(clusters.items(), key=lambda kv: (tot(kv[1]), kv[1].get("last_ts", "")),
                    reverse=True)
    armed, untriaged = [], []
    for k, c in ranked:
        v = vd(k)
        if v and v.get("armed"):
            armed.append({"ask": k, "oracle": v.get("oracle"), "source": v.get("source"),
                          "armed": v.get("armed"), "commit": v.get("commit"),
                          "pushed": bool(v.get("pushed")),
                          "corrections": len(v.get("corrections", [])),
                          "recurred": rec_flag(k, c), "count": tot(c)})
        elif not v:
            untriaged.append({"ask": k, "count": tot(c), "last": c.get("last_ts", "")})
    # Corrections live on triage entries that may have no cluster at all — a doer fixed after
    # arming that never appeared as a gap. Counting only clusters would hide exactly those.
    corrections = [{"ask": k, "ts": co.get("ts"), "what": co.get("what")}
                   for k, v in sorted(tr.items()) if isinstance(v, dict)
                   for co in v.get("corrections", [])]
    # tail_jsonl returns NEWEST FIRST (it reverses). So the scoreboard reads hist[0], and the
    # series is reversed back into file order for plotting. Taking hist[-1] here reads the
    # OLDEST run in the window — which looks correct for as long as the numbers stay equal and
    # goes silently stale the moment they move, which is the moment anyone would care.
    hist = tail_jsonl(LHISTORY, runs)
    last = hist[0] if hist else {}
    return {
        "scoreboard": {
            "untriaged": last.get("untriaged", len(untriaged)),
            "armed": last.get("armed", len(armed)),
            "recurred": last.get("recurred", 0),
            "corrections": last.get("corrections", len(corrections)),
            "by_loop": last.get("by_loop", 0), "by_hand": last.get("by_hand", 0),
        },
        "armed": armed[:top],
        "untriaged": untriaged[:top],
        "corrections": corrections[-top:],
        "history": [{"ts": h.get("ts"), "new_gaps": h.get("new_gaps", 0),
                     "untriaged": h.get("untriaged", 0), "armed": h.get("armed", 0)}
                    for h in reversed(hist)],
        "last_run": last.get("ts"),
    }


def build_state():
    cfg = read_config()
    safe_cfg = {k: cfg[k] for k in PUBLIC_CONFIG_KEYS if k in cfg}
    status = read_json(STATUS, {})
    status.pop("port", None)   # MAC-bearing serial path — never publish
    # Pull decisions once and use it for both the decisions feed and the in/out correlation.
    # Read deeper than the feeds so a reply near the window edge still finds its partner.
    # Traffic is split HERE, before correlation, so each stream correlates only against its
    # own kind — a broadcast reply can never be paired to a DM, or the two streams would
    # show each other's messages. One choke point rather than a split per consumer.
    all_dec = tail_jsonl(DECISIONS, 120)
    dec_b, dec_d = split_streams(all_dec)
    in_b, in_d = split_streams(tail_jsonl(INBOX, 40))
    snt_b, snt_d = split_streams(tail_jsonl(SENT_LOG, 40))
    decisions = dec_b
    inbox, sent = correlate(in_b, snt_b, dec_b)
    dm_inbox, dm_sent = correlate(in_d, snt_d, dec_d)
    return {
        "status": status,
        "config": safe_cfg,
        "bridge": launchd_running(),
        "nodes": read_json(NODES, {"nodes": [], "count": 0}),
        # sent/inbox stay in the payload verbatim — the v1 page still reads them, so this
        # endpoint serves both versions and the rollback needs no API change.
        "sent": sent,
        "inbox": inbox,
        "exchanges": build_exchanges(inbox, sent),
        # The direct channel, as its own stream. Same shape as `exchanges` so the page can
        # render it with the identical code — a second renderer would drift from the first.
        "dm_exchanges": build_exchanges(dm_inbox, dm_sent),
        "totals": {"sent": count_lines(SENT_LOG), "recv": count_lines(INBOX)},
        # 60 s cache: the distiller writes once a day, so re-reading it on every 3 s poll is
        # pure waste.
        "learning": cached("learning", 60, build_learning),
        "responder": {
            "enabled": cfg.get("RESPONDER_ENABLED", "false"),
            "model": cfg.get("RESPONDER_MODEL", ""),
            # count only — never publish which node IDs are Dean's trusted fleet
            "allow_count": len([a for a in cfg.get("ALLOW_FROM", "").split(",") if a.strip()]),
            "decisions": decisions[:30],
        },
    }


# --- v1 page: RETIRED 2026-08-09, served at /old-1. Frozen — the only thing ever added is
# the self-disabling retired banner below, which renders nothing unless the URL is an old-N
# slot, so this file stays usable as-is if it is ever restored to "/" as a rollback.
# Changes belong in the current page. Reads only fields the API still returns.
PAGE_V1 = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cal-mesh — levers</title>
<style>
:root{--bg:#0c0f14;--card:#151a22;--card2:#1b222c;--line:#232c39;--fg:#e6edf3;
--dim:#8b98a9;--accent:#4ea1ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--tx:#a371f7;--rx:#3fb950;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:linear-gradient(180deg,#0c0f14,#0c0f14ee);backdrop-filter:blur(6px);z-index:5}
header h1{font-size:17px;margin:0;letter-spacing:.3px}
header .sub{color:var(--dim);font-size:12px}
.pill{margin-left:12px;padding:5px 12px;border-radius:999px;font-weight:600;font-size:12px}
.faqlink{color:var(--accent);text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
.faqlink:hover{text-decoration:underline}
.navlinks{margin-left:auto;display:inline-flex;gap:14px;align-items:center}
.clog{max-height:620px;overflow-y:auto}
.clog .ci{padding:10px 16px;border-bottom:1px solid var(--line);font-size:13px;line-height:1.55}
.clog .ci:last-child{border-bottom:0}
.clog .cd{color:var(--dim);font-size:11px;margin-right:8px;font-variant-numeric:tabular-nums}
html{scroll-behavior:smooth}
.pill.ok{background:#12351f;color:var(--ok);border:1px solid #1c5c30}
.pill.bad{background:#3a1618;color:var(--bad);border:1px solid #6e2327}
main{padding:20px;max-width:1200px;margin:0 auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.tile .v{font-size:22px;font-weight:650;margin-top:4px}
.tile .v small{font-size:12px;color:var(--dim);font-weight:400}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.card h2{font-size:13px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);
color:var(--dim);text-transform:uppercase;letter-spacing:.6px;display:flex;gap:8px;align-items:center}
.card h2 .badge{margin-left:auto;background:var(--card2);color:var(--fg);padding:2px 8px;border-radius:6px;font-size:11px}
.msg{padding:10px 16px;border-bottom:1px solid var(--line)}
.msg:last-child{border-bottom:0}
.msg .meta{color:var(--dim);font-size:11px;display:flex;flex-wrap:wrap;gap:8px;margin-bottom:3px}
.msg .body{font-size:14px;word-break:break-word}
.tag{padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600}
.tag.tx{background:#241a3a;color:var(--tx)}
.tag.rx{background:#12351f;color:var(--rx)}
.tag.ch{background:#1a2740;color:var(--accent)}
.tag.auto{background:#3a2a12;color:var(--warn)}
.tag.offlist{background:#3a2f12;color:var(--warn);border:1px solid #6b5416}
.tag.quiet{background:#2a2f38;color:var(--dim)}
/* the reply/ask counterpart, shown inline under a message so the pairing is unambiguous */
.link{margin-top:6px;padding:6px 10px;border-left:2px solid var(--line);background:#11161d;
border-radius:0 6px 6px 0;font-size:13px}
.link .who{color:var(--dim);font-size:11px;display:block;margin-bottom:2px}
.link.out{border-left-color:var(--tx)} .link.out .txt{color:var(--tx)}
.link.in{border-left-color:var(--rx)}
.link .gen{color:var(--dim);font-size:11px}
.nolink{margin-top:5px;font-size:12px;color:var(--dim)}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 16px;font-size:13px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
td.snr-good{color:var(--ok)} td.snr-bad{color:var(--warn)}
#nodes-wrap{max-height:620px;overflow:auto}
#nodes thead th{position:sticky;top:0;background:var(--card);z-index:1}
#nodes th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
#nodes th.sortable:hover{color:var(--fg)}
.trans{display:flex;gap:10px}
.trans .t{flex:1;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.trans .t.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.trans .t .lbl{font-size:11px;color:var(--dim)} .trans .t .val{font-size:13px;margin-top:2px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.dot.on{background:var(--ok)} .dot.off{background:var(--bad)}
footer{color:var(--dim);font-size:11px;text-align:center;padding:16px}
.empty{padding:16px;color:var(--dim);font-size:13px}
.faq details{border-bottom:1px solid var(--line)}
.faq details:last-child{border-bottom:0}
.faq summary{padding:12px 16px;cursor:pointer;font-weight:600;list-style:none;display:flex;align-items:center;gap:10px}
.faq summary::-webkit-details-marker{display:none}
.faq summary::before{content:"+";color:var(--accent);font-weight:700;width:10px;display:inline-block}
.faq details[open] summary::before{content:"\2013"}
.faq .a{padding:0 16px 14px 40px;color:var(--dim);font-size:13px;line-height:1.65}
.faq .a code{background:var(--card2);padding:1px 5px;border-radius:4px;color:var(--fg);font-size:12px}
.faq .a b{color:var(--fg)}
.spark{display:inline-flex;align-items:center;gap:6px;font-size:12px;white-space:nowrap}
.spark svg{vertical-align:middle}
</style></head>
<body>
<header>
  <div><h1>📻 cal-mesh <span class="sub">— live levers</span></h1>
  <div class="sub" id="sub">connecting…</div></div>
  <span class="navlinks"><a class="faqlink" href="#faq">FAQ ↓</a><a class="faqlink" href="#changelog">Changelog ↓</a><a class="faqlink" href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">GitHub ↗</a></span>
  <span class="pill" id="conn">…</span>
</header>
<main>
  <div class="tiles" id="tiles"></div>
  <div style="margin-bottom:20px">
    <div class="card"><h2>Transports <span class="badge" id="active-t"></span></h2>
      <div class="trans" id="trans" style="padding:14px 16px"></div></div>
  </div>
  <div class="grid">
    <div class="card"><h2>↑ Outbound <span class="badge" id="tx-n">0</span></h2><div id="sent"></div></div>
    <div class="card"><h2>↓ Inbound <span class="badge" id="rx-n">0</span></h2><div id="inbox"></div></div>
  </div>
  <div style="margin-top:16px" class="card"><h2>🧠 Autonomous decisions <span class="badge" id="rstate">—</span></h2>
    <div id="decisions"></div></div>
  <div style="margin-top:16px" class="card"><h2>Neighbors heard <span class="badge" id="nn">0</span></h2>
    <div id="nodes-wrap"><table id="nodes"><thead><tr>
      <th class="sortable" data-key="short" onclick="setSort('short')">Short</th>
      <th class="sortable" data-key="long" onclick="setSort('long')">Name</th>
      <th class="sortable" data-key="hw" onclick="setSort('hw')">HW</th>
      <th class="sortable" data-key="hops" onclick="setSort('hops')">Hops</th>
      <th class="sortable" data-key="snr" onclick="setSort('snr')">SNR</th>
      <th>1h SNR trend</th></tr></thead><tbody></tbody></table></div>
  </div>
  <div style="margin-top:16px" class="card faq" id="faq"><h2>FAQ — how Cal works on the mesh</h2>
    <details><summary>What is this?</summary><div class="a">
      This is <b>Cal</b>, Dean's AI, living on a LoRa mesh radio via the node <b>Cal HT</b>.
      It's always on: it listens to the local mesh and answers when it's addressed. This page shows
      every lever live — the radio's state, each message in and out, and the reasoning behind every
      autonomous reply.</div></details>
    <details><summary>How does Cal know a message is meant for it?</summary><div class="a">
      A message qualifies if it's a <b>direct message</b> to Cal HT, <i>or</i> the text contains
      <code>cal</code> as a whole word (case-insensitive). Whole-word matching means "lo<b>cal</b>",
      "<b>cal</b>endar" and "physi<b>cal</b>" do <i>not</i> trigger it — only an actual mention of Cal.</div></details>
    <details><summary>What has to be true before Cal replies?</summary><div class="a">
      Name-mention alone isn't enough. In order, a message must pass every gate: it's <b>not Cal's own</b>
      message · it's <b>fresh</b> (under 5 min old) · the responder is <b>enabled</b> · the sender is on the
      <b>allow-list</b> · it's <b>addressed</b> (name or DM) · it's <b>within rate limits</b>. Miss any one and
      Cal stays quiet and logs why — see <b>Autonomous decisions</b> above.</div></details>
    <details><summary>How does Cal choose what to say?</summary><div class="a">
      Cal asks a headless Claude to write the reply, under a fixed persona: <b>5-7 words, plain text,
      warm and useful, and never reveal Dean's location, schedule, or personal life</b>. It runs with
      <b>no tools</b> — it answers from the incoming message alone, so it can't wander or leak. The reply is
      then cleaned (quotes stripped, length-capped) and sent. It's real generation inside a tight box, not
      canned templates.</div></details>
    <details><summary>Why are the replies so short?</summary><div class="a">
      LoRa bandwidth is tiny and airtime is <b>shared across the whole local mesh</b>. Long messages hog the
      channel, so terse replies (5-7 words) are simply good mesh etiquette.</div></details>
    <details><summary>Why do some inbound messages say "OFF-LIST"?</summary><div class="a">
      Because Cal <b>heard them perfectly well</b> and chose not to answer. Reception and reply are
      two different things: every message on the channel is received and shown here, but only senders
      on the allow-list can trigger an autonomous reply (training wheels). <b>OFF-LIST</b> means
      exactly that — good signal, message received, sender simply isn't cleared to get a reply yet.
      Other no-reply reasons (not addressed, rate-limited, kill switch off) are labelled too.</div></details>
    <details><summary>How do I tell which reply goes with which message?</summary><div class="a">
      Each side shows its counterpart inline: an inbound message displays <b>↳ Cal replied</b> with the
      exact reply underneath it, and an outbound reply displays <b>↳ answering</b> with the message it
      was responding to. Pairing is done by matching sender and text against the decision log, so the
      two columns never have to be lined up by eye. Replies also show how long generation took.</div></details>
    <details><summary>Who can Cal talk to right now?</summary><div class="a">
      Training wheels: only <b>Dean's own nodes</b> can trigger a reply. Anyone on the mesh can read the
      public channel and see Cal's messages — this dashboard is public and read-only.</div></details>
    <details><summary>Is it always on? Can it be turned off?</summary><div class="a">
      It's three always-on services — <b>radio</b> (capture + transmit), <b>cognition</b> (the autonomous
      responder), and <b>this dashboard</b> — each independent, so one can restart without dropping the others.
      A single kill switch silences autonomous replies instantly.</div></details>
    <details><summary>What about privacy?</summary><div class="a">
      The channel is public by design. Cal's hard rules forbid putting Dean's location, schedule, or work on
      the air. This page only ever shows public-channel traffic and Cal HT's own telemetry — never Dean's data.</div></details>
    <details><summary>What's the "1h SNR trend" column?</summary><div class="a">
      SNR (signal-to-noise) is how cleanly Cal HT hears another node's radio. The sparkline plots that node's
      SNR over the last hour, so you can see who's <b>stable</b> vs <b>fading</b> (↗ rising · → steady · ↘ dropping).
      It only exists for nodes heard <b>directly</b> — SNR is a single-hop measurement. A node reached only through
      a relay shows <b>"multi-hop"</b> (there's no direct signal to trend); a node heard just once shows a single
      value; a direct node not heard yet shows "— no direct signal."</div></details>
    <details><summary>Is Cal plugged in, or on WiFi?</summary><div class="a">
      Cal HT runs over <b>WiFi</b> — it sits on the local network and the bridge reaches it over TCP,
      so it can live anywhere with power and Wi-Fi instead of being tethered by USB (USB serial still
      works as a fallback). One quirk worth knowing: Meshtastic's fancy touchscreen-UI firmware leaves
      the network API compiled out, so the radio runs the simpler <b>BaseUI</b> build that actually
      serves the connection.</div></details>
    <details><summary>Is the code public? Can I run my own?</summary><div class="a">
      Yes — cal-mesh is open source; the full code (bridge, responder, dashboard) is on GitHub
      (link in the header). It ships a <code>config.example</code> — point it at your own Meshtastic
      node and you can run your own Cal-on-the-mesh.</div></details>
  </div>
  <div style="margin-top:16px" class="card" id="changelog"><h2>Changelog</h2>
    <div class="clog">
      <div class="ci"><span class="cd">2026-08-09</span><b>Inbound &amp; Outbound are now paired.</b> Every inbound message shows Cal's verdict (replied / no reply + why) with the actual reply inline, and every autonomous reply shows the message it was answering. Messages received from senders <b>not on the allow-list</b> are now called out explicitly as <b>OFF-LIST</b> — heard fine, deliberately not answered — so "received" is never confused with "ignored."</div>
      <div class="ci"><span class="cd">2026-08-09</span>Reply latency now reads in <b>seconds to two decimals</b> (e.g. 19.67s) instead of raw milliseconds, in both the decisions log and the new paired views.</div>
      <div class="ci"><span class="cd">2026-08-09</span>Battery: the fuel gauge is reporting real charge again, so the tile shows it — and a reading above 100 (Meshtastic's "gauge not ready / on external power" sentinel) now renders as <b>ext power</b> rather than a fake percentage.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Cal HT moved to <b>WiFi</b>: reflashed to the BaseUI firmware (the touchscreen-UI build excludes the webserver, so it never served the TCP API) and switched the bridge to TCP — the radio now runs untethered on the LAN, USB is just power.</div>
      <div class="ci"><span class="cd">2026-08-08</span>From Bob's PR: message latency tracking (gen_ms in decisions log + UI), /api/stats endpoint with daily aggregates (replies, skips, avg/min/max gen time).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Neighbors table: capped height + interior scroll (sticky header) and click-to-sort columns (Name, SNR, hops, …).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Changelog card now caps its height (~20 entries) and scrolls internally.</div>
      <div class="ci"><span class="cd">2026-08-08</span>From Bob's review: bridge reconnect backoff (8→60s), a <code>mesh nodes</code> CLI command, and clearer config hot-reload docs.</div>
      <div class="ci"><span class="cd">2026-08-08</span>FAQ: added an "is the code public?" entry.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Transports panel simplified to just USB / WiFi (active highlighted).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Responder status now reads "live" instead of "armed."</div>
      <div class="ci"><span class="cd">2026-08-08</span>Second security &amp; privacy audit + fixes: removed the device MAC from the public API, added DoS bounds (capped file reads, response cache, concurrency limit), and log rotation.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Published as a public GitHub repo (scrubbed of node IDs, LAN IP &amp; host); added a GitHub header link and a combined "Sent / Received" tile.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Neighbor "1h SNR" column now distinguishes trend vs single-reading vs multi-hop vs no-signal; added this changelog + header links.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Adversarial security review &amp; hardening — reply generation locked so tools cannot execute (plan mode + no MCP); public API config whitelisted; mesh content fully escaped + CSP headers; responder single-instance lock; per-record inbox safety.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Added per-neighbor 1-hour SNR sparklines — signal stability at a glance (idea from Bob).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Fixed Sent/Received tiles to show persistent totals (were resetting on restart).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Added the FAQ.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 2 — autonomous responder: Cal replies on its own when addressed (training wheels: fleet-only, kill switch, rate limits).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Dashboard published publicly via Tailscale Funnel.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 1 — always-on bridge: Cal sends &amp; receives text over LoRa (USB serial; WiFi/TCP built).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Node flashed to Meshtastic 2.7.26 and brought online as "Cal HT" (US / LONG_FAST).</div>
    </div>
  </div>
</main>
<footer>cal-mesh dashboard · auto-refresh 3s · read-only</footer>
<script>
const $=s=>document.querySelector(s);
const DIR=(function(){const p=location.pathname.replace(/\/(v2|v3|old-\d+)\/?$/,'/');return p.endsWith('/')?p:p+'/';})();
let SNR={};
let lastNodes=[], nodeSort={key:null,dir:1};
const NODE_LABELS={short:'Short',long:'Name',hw:'HW',hops:'Hops',snr:'SNR'};
function setSort(k){ nodeSort=(nodeSort.key===k)?{key:k,dir:-nodeSort.dir}:{key:k,dir:1}; renderNodes(); }
function renderNodes(){
  let ns=lastNodes.slice();
  if(nodeSort.key){ const k=nodeSort.key, dir=nodeSort.dir;
    ns.sort((a,b)=>{ let x=a[k],y=b[k];
      if(k==='hops'||k==='snr'){ if(x==null&&y==null)return 0; if(x==null)return 1; if(y==null)return -1; return (x-y)*dir; }
      x=(x||'').toString().toLowerCase(); y=(y||'').toString().toLowerCase();
      return x<y?-dir:(x>y?dir:0); }); }
  const tb=$('#nodes').querySelector('tbody');
  tb.innerHTML=ns.map(n=>{ const sg=(n.snr!=null&&n.snr>0)?'snr-good':'snr-bad';
    return `<tr><td>${esc(n.short)}</td><td>${esc(n.long)}</td><td>${esc(n.hw)}</td>`+
      `<td>${n.hops==null?'—':esc(n.hops)}</td><td class="${sg}">${n.snr==null?'—':esc(n.snr)}</td>`+
      `<td>${sparkline((SNR[n.id]||{}).points, n.hops)}</td></tr>`; }).join('');
  document.querySelectorAll('#nodes th.sortable').forEach(th=>{
    const k=th.dataset.key, on=nodeSort.key===k;
    th.textContent=NODE_LABELS[k]+(on?(nodeSort.dir>0?' ▲':' ▼'):''); });
}
async function loadSnr(){try{SNR=await (await fetch(DIR+'api/snr',{cache:'no-store'})).json();}catch(e){}}
function sparkline(pts, hops){
  if(!pts||pts.length===0){
    return (hops!=null&&hops>0)
      ? '<span style="color:var(--dim)">multi-hop</span>'
      : '<span style="color:var(--dim)">— <small>no direct signal</small></span>';
  }
  if(pts.length===1){
    const v=pts[0][1];
    return `<span class="spark" title="1 sample · ${esc(v)} dB">`+
      `<svg width="90" height="22"><circle cx="45" cy="11" r="2.5" fill="var(--accent)"/></svg>`+
      `<span style="color:var(--accent)">${esc(v)} <small>dB · 1 pt</small></span></span>`;
  }
  const W=90,H=22,pad=3;
  const ts=pts.map(p=>p[0]), vs=pts.map(p=>p[1]);
  const t0=Math.min(...ts),t1=Math.max(...ts),vmin=Math.min(...vs),vmax=Math.max(...vs);
  const sx=t=>pad+(t1===t0?(W-2*pad):((t-t0)/(t1-t0))*(W-2*pad));
  const sy=v=>pad+(1-(vmax===vmin?0.5:(v-vmin)/(vmax-vmin)))*(H-2*pad);
  const d=pts.map((p,i)=>(i?'L':'M')+sx(p[0]).toFixed(1)+' '+sy(p[1]).toFixed(1)).join(' ');
  const last=pts[pts.length-1];
  const k=Math.max(1,Math.floor(pts.length/3));
  const avg=a=>a.reduce((s,x)=>s+x,0)/a.length;
  const dv=avg(vs.slice(-k))-avg(vs.slice(0,k));
  const arrow=dv>1.5?'↗':(dv<-1.5?'↘':'→');
  const col=dv<-1.5?'var(--warn)':(dv>1.5?'var(--ok)':'var(--accent)');
  return `<span class="spark" title="${pts.length} samples · now ${esc(last[1])} dB · min ${esc(vmin)} max ${esc(vmax)}">`+
    `<svg width="${W}" height="${H}"><path d="${d}" fill="none" stroke="${col}" stroke-width="2" `+
    `stroke-linejoin="round" stroke-linecap="round"/><circle cx="${sx(last[0]).toFixed(1)}" `+
    `cy="${sy(last[1]).toFixed(1)}" r="2.5" fill="${col}"/></svg>`+
    `<span style="color:${col}">${arrow} ${esc(last[1])}</span></span>`;
}
function esc(s){return (s??"").toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hhmmss(ts){try{return new Date(ts).toLocaleTimeString();}catch(e){return ts;}}
function secs(ms){return (ms/1000).toFixed(2)+'s';}
// Meshtastic reports 101 when the fuel gauge isn't ready / the node is on external power.
// That is a sentinel, not a charge level — never render it as a percentage.
function batteryLabel(m){
  if(m.battery==null) return '—';
  if(m.battery>100) return 'ext power';
  return m.battery+'%';
}
function verdictTag(x){
  if(x.verdict==='replied') return '<span class="tag tx">REPLIED</span>';
  if(x.verdict!=='skipped') return '';
  return x.reason==='sender_not_allowed'
    ? '<span class="tag offlist">OFF-LIST · heard, not answered</span>'
    : `<span class="tag quiet">NO REPLY · ${esc(x.reason)}</span>`;
}
function skipWhy(r){
  const m={sender_not_allowed:'sender is not on the allow-list — Cal heard it perfectly well and chose not to answer',
           not_addressed:'Cal was not addressed (no "cal" mention, not a DM)',
           disabled:'the responder kill switch is off',
           too_old:'the message was older than the freshness window',
           rate:'rate limit reached for this sender',
           cooldown:'per-sender cooldown still active',
           self:'this was Cal\'s own message'};
  return m[r]||esc(r||'unknown');
}
function tile(k,v,sub){return `<div class="tile"><div class="k">${k}</div><div class="v">${v}${sub?` <small>${sub}</small>`:''}</div></div>`;}
async function tick(){
 let d; try{d=await (await fetch(DIR+'api/state',{cache:'no-store'})).json();}catch(e){$('#conn').className='pill bad';$('#conn').textContent='dashboard offline';return;}
 const st=d.status||{}, m=st.metrics||{}, node=st.node||{}, c=st.counts||{};
 const rp=d.responder||{};
 const on=st.connected;
 $('#conn').className='pill '+(on?'ok':'bad');
 $('#conn').textContent=on?'● radio connected':'● radio down';
 $('#sub').textContent=`${node.longName||'?'} (${node.shortName||'?'}) · ${node.id||''} · fw ${st.firmware||'?'}`;
 $('#tiles').innerHTML=[
   tile('Bridge', (d.bridge.state==='running'?'running':'stopped'), d.bridge.pid?('pid '+d.bridge.pid):''),
   tile('Uptime', st.uptime_s!=null?fmtDur(st.uptime_s):'—'),
   tile('Battery', batteryLabel(m), m.voltage!=null?m.voltage.toFixed(2)+'V':''),
   tile('Ch util', m.chUtil!=null?m.chUtil.toFixed(1)+'%':'—', m.airUtilTx!=null?('air '+m.airUtilTx.toFixed(2)+'%'):''),
   tile('Sent / Received', `${(d.totals&&d.totals.sent)??0} / ${(d.totals&&d.totals.recv)??0}`),
   tile('Responder', rp.enabled==='true'?'● live':'○ off',
        rp.model?rp.model.replace('claude-','').replace(/-\d+$/,''):''),
 ].join('');
 // transports
 const cfg=d.config||{}, active=(st.transport||cfg.TRANSPORT||'serial');
 $('#active-t').textContent='active: '+active;
 $('#trans').innerHTML=[
   `<div class="t ${active==='serial'?'active':''}"><div class="lbl"><span class="dot ${active==='serial'?'on':'off'}"></span>USB</div></div>`,
   `<div class="t ${active==='tcp'?'active':''}"><div class="lbl"><span class="dot ${active==='tcp'?'on':'off'}"></span>WiFi</div></div>`,
 ].join('');
 // sent
 $('#tx-n').textContent=(d.sent||[]).length;
 $('#sent').innerHTML=(d.sent&&d.sent.length)?d.sent.map(x=>`
   <div class="msg"><div class="meta"><span class="tag tx">TX</span>
     <span>${hhmmss(x.ts)}</span><span>→ ${esc(x.dest)}</span>
     <span class="tag ch">ch${esc(x.channel)}</span><span>${esc(x.bytes)}B</span><span>${esc(x.transport)}</span>
     ${x.source==='responder'?'<span class="tag auto">AUTO</span>':'<span class="tag quiet">MANUAL</span>'}</div>
   <div class="body">${esc(x.text)}</div>
   ${x.in_reply_to
     ? `<div class="link in"><span class="who">↳ answering ${esc(x.in_reply_to.from)} · ${hhmmss(x.in_reply_to.ts)}${x.gen_ms!=null?` · took ${secs(x.gen_ms)}`:''}</span>${esc(x.in_reply_to.text)}</div>`
     : (x.source==='responder'?'<div class="nolink">↳ the message this answered is older than the window shown</div>':'')}
   </div>`).join(''):'<div class="empty">nothing sent yet</div>';
 // inbox
 $('#rx-n').textContent=(d.inbox||[]).length;
 $('#inbox').innerHTML=(d.inbox&&d.inbox.length)?d.inbox.map(x=>`
   <div class="msg"><div class="meta"><span class="tag rx">RX</span>
     <span>${hhmmss(x.ts)}</span><span>${esc(x.from)} → ${esc(x.to)}</span>
     <span class="tag ch">ch${esc(x.channel)}</span>${x.snr!=null?`<span>snr ${esc(x.snr)}</span>`:''}
     ${verdictTag(x)}</div>
   <div class="body">${esc(x.text)}</div>
   ${x.verdict==='replied'&&x.reply
     ? `<div class="link out"><span class="who">↳ Cal replied${x.gen_ms!=null?` · ${secs(x.gen_ms)}`:''}${x.capability?` · ${esc(x.capability)}`:''}</span><span class="txt">${esc(x.reply)}</span></div>`
     : (x.verdict==='skipped'?`<div class="nolink">↳ received, no reply — ${skipWhy(x.reason)}</div>`:'')}
   </div>`).join(''):'<div class="empty">nothing received yet — mesh is quiet or awaiting first inbound</div>';
 // responder decisions
 const dec=(rp.decisions)||[];
 $('#rstate').textContent=(rp.enabled==='true'?'live':'disabled')+' · '+(rp.allow_count||0)+' allowed';
 $('#decisions').innerHTML=dec.length?dec.map(x=>`
   <div class="msg"><div class="meta">
     <span class="tag ${x.matched?'tx':'rx'}" style="${x.matched?'':'background:#2a2f38;color:#8b98a9'}">${x.matched?'REPLIED':'SKIP'}</span>
     <span>${hhmmss(x.ts)}</span><span>${esc(x.from)}</span>
     ${x.matched?'':`<span class="tag ch">${esc(x.reason)}</span>`}</div>
   <div class="body">${esc(x.text)}${x.reply?` <span style="color:var(--tx)">→ ${esc(x.reply)}</span>`:''}${x.gen_ms!=null?` <span style="color:var(--dim);font-size:11px">· ${secs(x.gen_ms)}</span>`:''}</div></div>`).join(''):'<div class="empty">no inbound evaluated yet</div>';
 // nodes
 lastNodes=(d.nodes&&d.nodes.nodes)||[];
 $('#nn').textContent=lastNodes.length;
 renderNodes();
}
function fmtDur(s){s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
 return h?`${h}h ${m}m`:(m?`${m}m ${ss}s`:`${ss}s`);}
// Retired-version banner. Renders ONLY when this page is being served from an /old-N slot,
// so if it is ever restored to "/" as a rollback it shows nothing and behaves as it always did.
(function(){
  const m=location.pathname.match(/\/(old-\d+)\/?$/);
  if(!m) return;
  const cur=location.pathname.replace(/\/old-\d+\/?$/,'/');
  const b=document.createElement('div');
  b.style.cssText='background:#3a2f12;color:#d29922;border-bottom:1px solid #6b5416;'+
    'padding:9px 22px;font-size:13px;text-align:center';
  b.innerHTML='This is <b>'+m[1]+'</b>, a retired version of the dashboard, kept for reference. '+
    '<a href="'+cur+'" style="color:#4ea1ff;font-weight:600">Go to the current page →</a>';
  document.body.insertBefore(b, document.body.firstChild);
})();
loadSnr(); tick(); setInterval(tick,3000); setInterval(loadSnr,30000);
</script></body></html>"""


# --- v2 page: on trial at "/v2". Two changes from v1:
#   1. Inbound/Outbound collapse into ONE "Exchanges" stream. Nearly every exchange starts
#      with Cal being prompted, so the ask is the head and the reply hangs off it. This also
#      removes v1's duplication (each reply was rendered twice) — the main source of clutter.
#   2. Each exchange opens into a DECISION TRACE: the gate ladder, what the sanitizer did,
#      the fact that was injected, model + latency. Machinery, not introspection — see the
#      note rendered at the foot of every trace panel.
PAGE_V2 = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>cal-mesh — levers (v2)</title>
<style>
:root{--bg:#f6f8fa;--card:#ffffff;--card2:#eef1f5;--line:#d6dce4;--fg:#1a1f26;
--dim:#5c6672;--accent:#0a63c9;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--tx:#6639ba;--rx:#1a7f37;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:linear-gradient(180deg,#f6f8fa,#f6f8faee);backdrop-filter:blur(6px);z-index:5}
header h1{font-size:17px;margin:0;letter-spacing:.3px}
header .sub{color:var(--dim);font-size:12px}
.pill{margin-left:12px;padding:5px 12px;border-radius:999px;font-weight:600;font-size:12px}
.pill.ok{background:#dafbe1;color:var(--ok);border:1px solid #aceebb}
.pill.bad{background:#ffebe9;color:var(--bad);border:1px solid #ffcecb}
.faqlink{color:var(--accent);text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
.faqlink:hover{text-decoration:underline}
.navlinks{margin-left:auto;display:inline-flex;gap:14px;align-items:center}
html{scroll-behavior:smooth}
main{padding:20px;max-width:1200px;margin:0 auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.tile .v{font-size:22px;font-weight:650;margin-top:4px}
.tile .v small{font-size:12px;color:var(--dim);font-weight:400}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:16px}
.card h2{font-size:13px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);
color:var(--dim);text-transform:uppercase;letter-spacing:.6px;display:flex;gap:8px;align-items:center}
.card h2 .badge{background:var(--card2);color:var(--fg);padding:2px 8px;border-radius:6px;font-size:11px;font-variant-numeric:tabular-nums}
.card h2 .badge.right{margin-left:auto}
.tag{padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600}
.tag.tx{background:#f3eefc;color:var(--tx)} .tag.rx{background:#dafbe1;color:var(--rx)}
.tag.ch{background:#ddf4ff;color:var(--accent)} .tag.auto{background:#fff8c5;color:var(--warn)}
.tag.offlist{background:#fff8c5;color:var(--warn);border:1px solid #d4a72c}
.tag.quiet{background:#eef1f5;color:var(--dim)}
/* --- exchanges --- */
.xc{padding:14px 16px;border-bottom:1px solid var(--line)}
.xc:last-child{border-bottom:0}
.xc .meta{color:var(--dim);font-size:11px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:5px}
.xc .ask{font-size:15px;word-break:break-word;max-width:78ch}
.rep .txt,.norep{max-width:78ch}
.xc.unprompted{background:#f0f3f7}
.rep{margin:9px 0 0 16px;padding:8px 12px;border-left:2px solid var(--tx);background:#f7f4fd;
border-radius:0 8px 8px 0}
.rep .who{color:var(--dim);font-size:11px;display:block;margin-bottom:2px}
.rep .txt{color:var(--tx);font-size:14px}
.norep{margin:8px 0 0 16px;padding:7px 12px;border-left:2px solid var(--line);background:#f2f4f7;
border-radius:0 8px 8px 0;color:var(--dim);font-size:12.5px}
/* --- trace disclosure --- */
details.tr{margin:10px 0 0 16px}
details.tr summary{cursor:pointer;list-style:none;color:var(--accent);font-size:13.5px;
font-weight:600;letter-spacing:.2px;display:inline-flex;gap:7px;align-items:center;
padding:4px 10px 4px 8px;border:1px solid var(--line);border-radius:7px;background:var(--card2)}
details.tr summary::-webkit-details-marker{display:none}
details.tr summary::before{content:">";font-size:13px;font-weight:700;display:inline-block;
transform-origin:50% 50%;transition:transform .15s ease}
details.tr[open] summary::before{transform:rotate(90deg)}
details.tr summary:hover{border-color:var(--accent);background:#e4e9f0}
.tp{margin-top:7px;background:#f4f6f9;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.link-d{margin:2px 0 10px;max-width:620px}
.link-d svg{width:100%;height:auto;display:block}
.trow{display:flex;gap:10px;padding:3px 0;font-size:12px;align-items:baseline}
.tk{color:var(--dim);min-width:78px;flex-shrink:0;text-transform:uppercase;font-size:10px;letter-spacing:.5px}
.tv{color:var(--fg);word-break:break-word}
.tv code{background:var(--card2);padding:1px 5px;border-radius:4px;font-size:11.5px}
.hint{color:var(--dim);font-size:11px}
.gate{display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:4px;font-size:11px}
.gp{background:#dafbe1;color:var(--ok)} .gf{background:#ffebe9;color:var(--bad)}
.tnote{margin-top:8px;padding-top:7px;border-top:1px solid var(--line);color:var(--dim);font-size:11px;line-height:1.5}
.tnone{color:var(--dim);font-size:12px}
/* --- trace: the swap. What reached the model and what did not, drawn rather than asserted.
   Two things compete to become the reply; on a capability answer one of them is cut. --- */
.swap{display:grid;grid-template-columns:minmax(0,1fr) 74px minmax(0,1fr);gap:9px 0;
align-items:center;margin:2px 0 12px}
.sw{border:1px solid var(--line);border-radius:9px;padding:8px 10px;background:var(--card);min-width:0}
.sw .swk{font-size:9.5px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);font-weight:700}
.sw .swv{font-size:12.5px;margin-top:3px;word-break:break-word;line-height:1.45}
.sw .swn{font-size:10.5px;margin-top:5px;color:var(--dim);line-height:1.4}
.sw.i-in{grid-column:1;grid-row:1} .sw.i-fact{grid-column:1;grid-row:2}
.sw.i-out{grid-column:3;grid-row:1/3;align-self:stretch;display:flex;
flex-direction:column;justify-content:center}
.sw.cut{border-style:dashed;background:#fbfcfd}
.sw.cut .swv{color:var(--dim);text-decoration:line-through;text-decoration-color:#b9c2cd}
.sw.i-fact{border-color:#aceebb;background:#f4fcf6}
.sw.i-out{border-color:#ddd0f5;background:#faf7fe}
.sw.i-out .swv{color:var(--tx);font-size:13.5px}
.conn{position:relative;height:3px;background:var(--ok);border-radius:2px}
.conn.c1{grid-column:2;grid-row:1} .conn.c2{grid-column:2;grid-row:2}
.conn::after{content:"";position:absolute;right:0;top:-4.5px;border:6px solid transparent;
border-left-color:var(--ok);border-right:0}
/* The cut is the whole point of the picture, so it is drawn as a stop and not as a hairline. */
.conn.brk{background:repeating-linear-gradient(90deg,#b9c2cd 0 4px,transparent 4px 9px)}
.conn.brk::after{content:"\2715";position:absolute;right:auto;left:50%;top:50%;border:0;
transform:translate(-50%,-50%);background:var(--card);color:var(--bad);font-size:12px;
font-weight:700;width:22px;height:22px;line-height:19px;text-align:center;border-radius:50%;
box-shadow:0 0 0 2px var(--bad) inset}
.conn.brk>b{position:absolute;left:50%;top:16px;transform:translateX(-50%);font-size:9.5px;
font-weight:700;letter-spacing:.4px;text-transform:uppercase;color:var(--bad);white-space:nowrap}
/* --- trace: the pipeline spine. The stages are a sequence in time, so they are drawn as one. --- */
.spine{list-style:none;margin:0;padding:0}
.stg{position:relative;padding:0 0 11px 25px}
.stg::before{content:"";position:absolute;left:5px;top:16px;bottom:0;width:2px;background:var(--line)}
.stg:last-child::before{display:none}
.stg>.sdot{position:absolute;left:0;top:5px;width:12px;height:12px;border-radius:50%;
background:var(--ok);border:2px solid var(--ok);box-sizing:border-box}
.stg.stop>.sdot{background:var(--bad);border-color:var(--bad)}
.stg.skip>.sdot{background:var(--card);border-color:#c3ccd7}
.stg.skip{opacity:.6}
.stg.stop::before,.stg.skip::before{background:repeating-linear-gradient(180deg,#c3ccd7 0 3px,transparent 3px 6px)}
.stg .shead{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.stg .sname{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);
font-weight:700;flex-shrink:0}
.stg .ssum{font-size:12.5px;color:var(--fg)}
.stg .sdet{margin-top:5px}
.stg .sdet:empty{display:none}
.rungn{display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:4px;font-size:11px;
background:#f2f4f7;color:var(--dim);font-style:italic}
/* --- trace: measurements drawn to the scale they were measured on --- */
.bar{position:relative;height:6px;border-radius:3px;background:#e7ebf1;margin-top:6px;max-width:280px}
.bar>i{position:absolute;top:0;bottom:0;border-radius:3px;background:#cfe6d6}
.bar>i.fill{left:0;background:var(--ok)}
.bar>i.fill.late{background:var(--warn)}
.bar .mk{position:absolute;top:-3px;width:2px;height:12px;background:var(--fg);border-radius:1px}
.barl{font-size:10.5px;color:var(--dim);margin-top:4px;line-height:1.45;max-width:60ch}
@media(max-width:640px){
.swap{grid-template-columns:minmax(0,1fr);gap:7px}
.sw.i-in,.sw.i-fact,.sw.i-out{grid-column:1;grid-row:auto}
.conn{display:none}}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 16px;font-size:13px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
td.snr-good{color:var(--ok)} td.snr-bad{color:var(--warn)}
#nodes-wrap{max-height:620px;overflow:auto}
#nodes thead th{position:sticky;top:0;background:var(--card);z-index:1}
#nodes th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
.trans{display:flex;gap:10px;padding:14px 16px}
.trans .t{flex:1;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.trans .t.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.trans .t .lbl{font-size:11px;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.tile .v .dot{width:9px;height:9px;margin-right:7px;vertical-align:middle}
.dot.on{background:var(--ok)} .dot.off{background:var(--bad)}
footer{color:var(--dim);font-size:11px;text-align:center;padding:16px}
.empty{padding:16px;color:var(--dim);font-size:13px}
.faq h3{margin:0;padding:14px 16px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent);border-bottom:1px solid var(--line);background:#f0f3f7}
.faq .a a{color:var(--accent);text-decoration:none;font-weight:600}
.faq .a a:hover{text-decoration:underline}
.faq details{border-bottom:1px solid var(--line)}
.faq details:last-child{border-bottom:0}
.faq summary{padding:12px 16px;cursor:pointer;font-weight:600;list-style:none;display:flex;align-items:center;gap:10px}
.faq summary::-webkit-details-marker{display:none}
.faq summary::before{content:"+";color:var(--accent);font-weight:700;width:10px;display:inline-block}
.faq details[open] summary::before{content:"\2013"}
.faq .a{padding:0 16px 14px 40px;color:var(--dim);font-size:13px;line-height:1.65}
.faq .a code{background:var(--card2);padding:1px 5px;border-radius:4px;color:var(--fg);font-size:12px}
.faq .a b{color:var(--fg)}
.clog{max-height:420px;overflow-y:auto}
.clog .ci{padding:10px 16px;border-bottom:1px solid var(--line);font-size:13px;line-height:1.55}
.clog .cd{color:var(--dim);font-size:11px;margin-right:8px;font-variant-numeric:tabular-nums}
.spark{display:inline-flex;align-items:center;gap:6px;font-size:12px;white-space:nowrap}
</style></head>
<body>
<header>
  <div><h1>📻 cal-mesh <span class="sub">— live levers (v2)</span></h1>
  <div class="sub" id="sub">connecting…</div></div>
  <span class="navlinks"><a class="faqlink" href="#faq">FAQ ↓</a><a class="faqlink" href="#changelog">Changelog ↓</a><a class="faqlink" href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">GitHub ↗</a></span>
  <span class="pill" id="conn">…</span>
</header>
<main>
  <div class="tiles" id="tiles"></div>
  <div class="card"><h2>Transports <span class="badge right" id="active-t"></span></h2>
    <div class="trans" id="trans"></div></div>
  <div class="card"><h2><span class="badge" id="xc-n">0</span> 💬 Exchanges</h2><div id="exchanges"></div></div>
  <div class="card"><h2><span class="badge" id="nn">0</span> Neighbors heard</h2>
    <div id="nodes-wrap"><table id="nodes"><thead><tr>
      <th class="sortable" data-key="short" onclick="setSort('short')">Short</th>
      <th class="sortable" data-key="long" onclick="setSort('long')">Name</th>
      <th class="sortable" data-key="hw" onclick="setSort('hw')">HW</th>
      <th class="sortable" data-key="hops" onclick="setSort('hops')">Hops</th>
      <th class="sortable" data-key="snr" onclick="setSort('snr')">SNR</th>
      <th>1h SNR trend</th></tr></thead><tbody></tbody></table></div>
  </div>
  <div class="card faq" id="faq"><h2>FAQ — what this is and how it works</h2>
    <h3>Start here</h3>
    <details><summary>What is this page?</summary><div class="a">
      A live, read-only window into <b>Cal</b> — an AI that lives on a <b>radio mesh network</b> and
      answers people over the air, with no internet on the far end. Everything here is real: the radio's
      state, every message in and out, and the full reasoning trace behind each automatic reply. Nothing
      is a mock-up. If Cal answered someone thirty seconds ago, it's below.</div></details>
    <details><summary>What is a mesh network?</summary><div class="a">
      A network with <b>no towers, no carrier and no internet</b>. Every radio is also a repeater: if
      two nodes are too far apart to hear each other, a third in the middle passes the message along,
      and so on. That's a <b>hop</b>. Coverage comes from the participants rather than infrastructure,
      so the network exists wherever people bring radios — and keeps working when the grid doesn't.
      That last property is the whole point: it's the tool you reach for when cell service is gone.</div></details>
    <details><summary>What is Meshtastic?</summary><div class="a">
      Free, open-source firmware that turns inexpensive <b>LoRa</b> radios (typically $30–100) into a
      mesh network for text messages and location sharing. You flash it onto a small board, pair it to
      your phone, and you're on the mesh — encrypted by channel, no account, no subscription, no
      monthly fee. It's a volunteer project with a large community, and it's what Cal's radio runs.
      <br><a href="https://meshtastic.org" target="_blank" rel="noopener noreferrer">meshtastic.org ↗</a></div></details>
    <details><summary>What is LoRa, and why does it matter here?</summary><div class="a">
      <b>Lo</b>ng <b>Ra</b>nge radio: a modulation designed to get a very small amount of data a very
      long way on very little power — miles between nodes, on a battery, with no licence required on
      the public bands. The trade is <b>bandwidth</b>. A LoRa channel carries on the order of a few
      hundred to a few thousand bits per second, and <b>every node in earshot shares it</b>. One long
      message blocks the channel for everyone. That single constraint explains most of Cal's design,
      starting with why it never says more than a few words.</div></details>
    <details><summary>Why put an AI on a mesh radio at all?</summary><div class="a">
      Because a mesh is what you use <b>when the grid isn't there</b> — off-grid, field work, dead
      coverage, emergencies — and that's exactly when knowledge is hardest to reach. The insight the
      project runs on: the mesh is off-grid, but the <b>base station usually isn't</b>. Cal's radio is
      connected to a computer with internet, so someone miles out with nothing but a handheld can ask a
      question over RF and get a real answer relayed back. Cal extends connected knowledge to the
      unconnected edge. Before this, a node could prove it was alive but couldn't actually
      <i>help</i> — presence without utility.</div></details>
    <details><summary>How did it get here?</summary><div class="a">
      Three deliberate stages, each gated before the next. <b>Level 1</b> — a bridge that owns the
      radio and can send and receive text. <b>Level 2</b> — an autonomous responder that decides on its
      own whether to answer and writes the reply, with training wheels (a small allow-list, rate limits,
      a kill switch). <b>Level 3</b> — real capabilities, where the software fetches a verified fact and
      the model only puts it into words. Each stage shipped switched <b>off</b>, went through
      adversarial review, and was turned on deliberately. The reviews have caught real problems,
      including a privacy leak in the reply path.</div></details>

    <h3>How Cal behaves</h3>
    <details><summary>How does Cal know a message is meant for it?</summary><div class="a">
      A message qualifies if it's a <b>direct message</b> to Cal's node, <i>or</i> the text contains
      <code>cal</code> as a whole word (case-insensitive). Whole-word matching means "lo<b>cal</b>",
      "<b>cal</b>endar" and "physi<b>cal</b>" do <i>not</i> trigger it.</div></details>
    <details><summary>What has to be true before Cal replies?</summary><div class="a">
      Being named isn't enough. In order, a message must pass every gate: it's <b>not Cal's own</b> ·
      it's <b>fresh</b> · the responder is <b>enabled</b> · the sender is on the <b>allow-list</b> ·
      it's <b>addressed</b> · it's <b>within rate limits</b>. Miss one and Cal stays quiet and records
      why — open <b>trace</b> on any exchange to see the whole ladder and exactly where it stopped.</div></details>
    <details><summary>Why do some messages say "OFF-LIST"?</summary><div class="a">
      Because Cal <b>heard them perfectly well</b> and chose not to answer. Reception and reply are two
      different things: every message on the channel is received and shown here, but only senders on the
      allow-list can trigger an automatic reply. <i>Whether silence is the right behaviour is under
      active review</i> — the argument against it is that on a shared channel, staying quiet to one
      person while answering another isn't neutral, it reads as a snub.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/unknown-sender-tier.md" target="_blank" rel="noopener noreferrer">The proposal to fix it ↗</a></div></details>
    <details><summary>Why are the replies so short?</summary><div class="a">
      Airtime is <b>shared by every node in range</b>, and LoRa has very little of it. A long message
      is not just slow, it takes the channel away from everyone else — including traffic that might
      matter more than a chat reply. So Cal is held to <b>5–7 words</b>. It's etiquette enforced in
      code, and it's why the answers read like radio traffic rather than chat.</div></details>
    <details><summary>Who can Cal talk to, and can it be switched off?</summary><div class="a">
      Right now only a small allow-list of nodes can trigger a reply, though <b>anyone</b> on the mesh
      can read what Cal says — the channel is public. Three independent always-on services do the work
      (radio, cognition, this dashboard), so one can restart without dropping the others, and a single
      kill switch silences all automatic replies instantly. Note that node IDs are <b>not
      authenticated</b> and can be spoofed, so the allow-list is a courtesy control, not a security
      boundary. The real controls are the kill switch and the fact that the model can't run tools.</div></details>

    <h3>How the answers are made</h3>
    <details><summary>How does Cal choose what to say?</summary><div class="a">
      A headless Claude writes the reply under a fixed persona — <b>5–7 words, plain text, warm and
      useful, never reveal the operator's location, schedule or personal life</b> — running with
      <b>no tools</b> and with no access to any private context. The important part is what it
      <i>isn't</i> allowed to do: for anything factual, Cal never looks something up. The software
      fetches a verified fact from a known source and hands it over, and the model's only job is to put
      that fact into words. We call it <b>capability injection</b>, and it's why Cal can't invent a
      temperature — if the fetch fails, it says so instead of guessing.</div></details>
    <details><summary>Where does the weather come from?</summary><div class="a">
      The US National Weather Service, and nothing else — one allow-listed source, fetched by the
      software, never by the model. Cal reads the <b>latest observation from the nearest weather
      station</b> to a fixed reference point, and refuses to answer at all if that reading is too old.
      Cal has <b>no forecast</b>: ask about tonight, tomorrow or whether it's going to rain and it
      says so outright rather than reading you a present-tense number as though it were a prediction.
      When it feels meaningfully different from the air temperature, Cal reports the <b>heat index</b>
      (or <b>wind chill</b> in the cold) alongside it — that is the number a person actually acts on,
      and it can run well above the temperature: measured here, 95&deg;F air against a 107&deg;F heat
      index. If the source publishes that value in a unit the software does not recognise, it is
      <b>dropped rather than converted on a guess</b>, because a wrong number is worse than no number.
      Known limitation, stated plainly: the station is a real place some distance away, and its
      reading can differ from the estimate for a specific spot. What Cal reports is a real
      measurement of somewhere nearby, not a forecast for where you're standing.
      <br><br>This page used to put a number on that gap — "five degrees or more". That number is
      withdrawn rather than quietly softened, and the reason is worth saying: it was measured
      against a <b>reference point that was itself nearly four miles wrong</b>, from a station
      believed to be five miles off that is actually about one. The reference has been corrected.
      The gap is real and the caution stands, but the size of it has not been honestly measured
      yet, so no figure is quoted here until it has been.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather-point-accuracy.md" target="_blank" rel="noopener noreferrer">The write-up, including the fix ↗</a></div></details>
    <details><summary>What is an "exchange"?</summary><div class="a">
      Almost everything Cal transmits is a response to being prompted, so the page is organised that
      way: the incoming message is the head, and Cal's reply is indented beneath it. Two things don't
      fit that shape and are marked separately — <b>unprompted</b> sends (an operator message, with no
      ask above it) and messages overheard but never addressed to Cal.</div></details>
    <details><summary>What's in the decision trace?</summary><div class="a">
      Open <b>trace</b> on any exchange and you get two pictures. First, <b>where the reply came
      from</b>: the message that arrived on the left, the reply that went out on the right, and — when
      Cal answered using a real capability — the <b>fact</b> the software fetched sitting between them.
      The line from the message to the reply is <b>cut</b>, because on those answers the sender's words
      are never handed to the model at all. They only decide <i>which</i> fact to go and look up; the
      fact is what the model receives, and its only job is to put that into words. On an ordinary reply
      with no capability behind it the picture inverts — nothing is cut, because the message really is
      quoted to the model.
      <br><br>Below it, the <b>stages in the order they happened</b>: received, gated, sanitized,
      grounded, narrated, sent. Each carries its own detail — which checks passed and which one stopped
      it, what the sanitizer changed, which weather station the reading came from and how old it was,
      the model and how long generation took. A message that fails a check <b>stops the spine where it
      failed</b>, and a single hollow step says outright that nothing further ran. That is read off the record rather
      than assumed: a message that was gated out carries no sanitizer result, no fact, no model and no
      destination. It is the machinery, not a narration — see below.</div></details>
    <details><summary>Why doesn't the trace show Cal's "thinking"?</summary><div class="a">
      Because there isn't any to show, and inventing some would be worse than showing nothing. Reply
      generation returns plain text — there's no hidden reasoning being discarded. We could ask the
      model to narrate why it chose a reply, but that narration <b>wouldn't be a faithful account of
      the computation</b>, and publishing it as though it were would present a plausible story as
      mechanism. It would also put unbounded, model-authored text — influenced by whatever a stranger
      transmitted — onto a public page, which is what the rest of the design works to prevent.</div></details>
    <details><summary>What's the diagram in the "received" stage?</summary><div class="a">
      The <b>link</b> the message travelled: who transmitted, who received it, how many <b>hops</b> it
      took, and the signal strength on the final leg. <b>Direct</b> means Cal heard the sender's own
      radio; anything above zero means other nodes relayed it. Where the firmware reports a relay it
      gives only <b>one byte</b> of that node's id — enough to narrow the candidates, not to name one —
      so it's shown truncated and never resolved to a name. The sender's box is coloured by what Cal
      did with the message, so the diagram and the verdict can't disagree.
      <br><br>The hop count is sometimes genuinely unknown, and the caption says which kind of unknown
      it is: a message received before this feature existed, or one where the sender reported nothing
      usable. It will not claim a reason it can't support — it did exactly that until 2026-08-11, and
      the changelog says how.</div></details>
    <details><summary>Why isn't it a real map?</summary><div class="a">
      Because this page is public and the base station sits at a fixed private address — a pin would
      publish it, and a series of pins would publish movements. So the diagram shows <b>topology</b>
      (who → who, how many hops) and never a location. No coordinates are stored by this project at
      all: the bridge deliberately reads names, hops and signal from the node database and skips the
      position field, even though about half the neighbours broadcast one. Cal's own node doesn't
      advertise a position either.</div></details>
    <details><summary>What about privacy and safety?</summary><div class="a">
      The channel is public by design, and this page only ever shows public-channel traffic and Cal's
      own telemetry — never the operator's data. Incoming text is treated as hostile: anyone in radio
      range can transmit anything, so messages are sanitized before they go anywhere near the model,
      the model runs with <b>no tools and no private context loaded</b>, and the trace reports
      <i>that</i> something was redacted and how many times, never <i>what</i>. An adversarial review
      of this exact path caught a real location leak before it shipped.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/channel-trust-and-agency.md" target="_blank" rel="noopener noreferrer">The trust model ↗</a></div></details>

    <h3>The project</h3>
    <details><summary>Is the code public? Can I run my own?</summary><div class="a">
      Yes — cal-mesh is open source, and the whole thing (bridge, responder, dashboard) is on GitHub.
      It ships a <code>config.example</code>: point it at your own Meshtastic node and you can run your
      own Cal on your own mesh. It has already had its first outside contribution via fork and pull
      request.
      <br><a href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">github.com/deanssamclaw/cal-mesh ↗</a></div></details>
    <details><summary>Where's the design reasoning written down?</summary><div class="a">
      In the repo, as proposals — including the arguments that <i>lost</i>, which are usually the more
      useful half. Each one is written to be reviewed and attacked before anything gets built.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather.md" target="_blank" rel="noopener noreferrer">Giving Cal live knowledge — the framework ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-roadmap.md" target="_blank" rel="noopener noreferrer">Capability roadmap — what Cal could learn next ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather-intent-layer.md" target="_blank" rel="noopener noreferrer">Two of my own proposals, refuted with measurements ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/unknown-sender-tier.md" target="_blank" rel="noopener noreferrer">Answering strangers — "we hear you" ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/channel-trust-and-agency.md" target="_blank" rel="noopener noreferrer">Channel trust &amp; agency — how much Cal is allowed to be ↗</a></div></details>
  </div>
  <div class="card" id="changelog"><h2>Changelog</h2>
    <div class="clog">
      <div class="ci"><span class="cd">2026-08-12</span><b>The trace shows what happened instead of listing it.</b> It was thirteen rows of grey label and value, which gave a passed check, a station reading and the words that went out over the air exactly the same weight — and buried the one thing a trace is actually about, which is the order things happened in. Two changes. <b>The reply is now drawn as what it is:</b> two things compete to become the answer, the sender's own words and a fact the software fetched, and on a capability answer the sender's words are visibly <b>cut</b> — they select which fact to look up and are never handed to the model. When there is no capability the same picture inverts honestly: the message is quoted to the model and nothing is cut. <b>Below it the stages run down a spine in the order they happen</b> — received, gated, sanitized, grounded, narrated, sent. A message that fails a check stops the spine where it failed, the rail below it goes dashed, and the stages it never reached are drawn unreached rather than left out. That is read off the record, not assumed: a gated-out decision carries no sanitizer result, no fact, no model and no destination. Two numbers now have a scale under them rather than standing alone — how old the weather reading was against the hour these stations report on, and how long generation took against the <b>7-44 s</b> the same prompt was measured spanning. A closed trace is also no longer built at all, so opening one costs the work rather than every message paying it every three seconds.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The page is light now.</b> Same information and the same layout, on a light palette instead of the dark one it launched with. Two colours had to be re-picked rather than reused: the green and amber that read clearly against a dark background land near a third of the required contrast on a white one, so they are now darker shades of the same hues. The link diagram needed a pass of its own — its colours are written into the drawing code rather than read from the page palette, so swapping the palette alone would have left dark boxes and dark labels sitting on a white card, which is exactly the kind of change that looks finished until someone opens a trace. <b>The retired version at <code>old-1</code> is deliberately still dark.</b> It is kept as a record of what the page used to be, and restyling it would make that record wrong.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Two capture bugs, and a caption that confidently explained one of them wrongly.</b> Every message received since 2026-08-09 was showing "hops unknown — this message predates routing capture". The messages did not predate anything: the hop count is <i>hop_start</i> minus <i>hop_limit</i>, and the radio library builds its packet view with a converter that omits any number equal to zero — so a message that used its <b>entire</b> hop budget arrived with <i>hop_limit</i> missing and was recorded as "no data", indistinguishable from a message that carried no routing at all. The most-relayed messages were the ones being thrown away. Worse was the caption: one asserted cause printed for a blank that has several. It now states only what the record supports, and older messages that genuinely predate the feature still say so.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Cal can tell you the heat index.</b> Asked for "current temperature and heat index", Cal answered the temperature, said nothing about the other half, and gave no sign anything had been left out — while the weather service was publishing a <b>107&deg;F</b> heat index against <b>95&deg;F</b> air in the very same reading. The software had never looked at the field. Heat index and wind chill are now included whenever they differ from the air temperature by at least 3&deg;F, and when they do they take the place of wind in the reply: at a twelve-degree gap, how hot it feels <i>is</i> the weather, and a five-to-seven word message cannot carry both. If the value ever arrives in a unit the software does not recognise it is dropped rather than converted on a guess — read as Fahrenheit instead of Celsius, that 107 becomes "42F" on a 95-degree afternoon. Checked by running it: eight replies, both numbers survived all eight times.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Reply time is no longer shown as if it were thinking time.</b> Each exchange prints how long generation took, and a reader reasonably takes that as a measure of the model. It mostly is not. Measured here: a <i>one-token</i> reply through the same locked-down command costs <b>5.4–10.5 s</b>, while a full seven-word weather reply costs <b>7–44 s</b> — the <i>same prompt</i> varying about sixfold run to run. The floor is process startup and a network round trip; the spread is noise; the part attributable to composing seven words is small. The figure now carries that context instead of standing alone. Consequence worth stating plainly: choosing a larger model would be close to invisible in these numbers, because the time is not going where it looks like it is going.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Link diagram redrawn.</b> Two things it got wrong. It drew a single "relay" box no matter how far a message had travelled, which quietly implied the whole path was known — a hop is a rebroadcast, so three hops means three relays stood in between and the firmware only ever names the last one. The relays it cannot name are now drawn dashed and counted, so the picture shows the size of what it does not know. And every sentence moved out of the drawing into the rows beneath it: a drawing has a fixed canvas and its text neither wraps nor shrinks, so the caption had been clipped at both ends and the signal reading was painting over the node it pointed at. The drawing now holds boxes and arrows only, at one fixed scale, so a message that went three hops and one that went direct are drawn the same size.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Three messages got their hop count back.</b> Once <i>hop_start</i> is known the arithmetic is forced, so records caught by the bug above were recovered rather than left blank — and they are labelled <b>recovered</b>, because reconstructed after the fact is not the same kind of fact as measured at the time. A worked example, all of it from the message the operator remembered sending from far away: he was right that it did not reach Cal directly. It spent its whole budget of <b>3 hops</b>, and the last relay's one-byte id (<code>·c6</code>) matches exactly one node — Cal's own listener across the house. The signal is the giveaway: that listener heard the sender at <b>−126 dBm</b> and barely caught it, while Cal heard the same message at <b>−50 dBm</b>, because Cal was hearing the relay, not the sender. Signal strength describes the last leg only, never the distance to whoever spoke.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Cal was dropping the sender's ID on first contact.</b> The library resolves a node's <code>!id</code> through its list of known nodes and returns nothing for a node it has not yet been introduced to — while the packet itself carries that node's number the entire time. So the ID went missing exactly when a stranger spoke to Cal for the first time. Measured here: a "Hi" on 2026-08-11 was logged from nobody; the sender's introduction arrived eleven minutes later and it was <code>!ba0cc0c0</code> all along. The bridge now falls back to the number the packet carries. Node IDs remain unauthenticated and spoofable — that has not changed and cannot.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Forecast questions are now refused, not answered.</b> Asking about tonight, tomorrow, or whether it's going to rain used to return <i>current</i> conditions — a present-tense reading dressed as a prediction. Cal now recognises a forecast-shaped question deterministically and replies "Only current conditions, no forecast yet," making no lookup at all.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>FAQ rewritten and grouped.</b> It assumed you already knew what a mesh network, Meshtastic and LoRa were, and never said why an AI on a radio is worth building. It now starts from those, explains how the project got here in stages, and links out to the source and to the design proposals — including the arguments that lost.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Link diagram in every trace.</b> Shows the path a message took — sender, any relay, Cal HT — with the hop count and the signal on the last leg. The bridge now records per-message routing (hops taken, and the one-byte relay id when the firmware supplies it); messages received before that show "hops unknown" rather than implying they were direct. It is a topology diagram, not a geographic one, on purpose — see the FAQ.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>v2 is now the main page.</b> The previous two-column layout is retired but still readable at <code>old-1</code>. Retired versions keep a permanent <code>old-N</code> address — numbered by when they were retired, never renumbered — so a link to one always shows the same page.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>v2.</b> Inbound and Outbound merged into a single <b>Exchanges</b> stream — the ask is the head, Cal's reply is indented beneath it. Removes the duplication that made v1 busy (every reply used to render twice) and reads properly on a phone.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Decision trace.</b> Every exchange opens into the machinery behind it: the gate ladder, what the sanitizer changed, the capability and the exact injected fact, the model and generation time. Deliberately no model "reasoning" — see the FAQ.</div>
      <div class="ci"><span class="cd">2026-08-09</span>Inbound/Outbound paired, reply latency in seconds, and the battery tile made sentinel-aware (a reading over 100 means external power, not a charge).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Cal HT moved to <b>WiFi</b>: reflashed to the BaseUI firmware and switched the bridge to TCP — the radio runs untethered, USB is just power.</div>
      <div class="ci"><span class="cd">2026-08-08</span>From Bob's PR: message latency tracking and an /api/stats endpoint with daily aggregates.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Second security &amp; privacy audit: device MAC removed from the public API, DoS bounds, log rotation.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Published as a public GitHub repo; per-neighbor 1-hour SNR sparklines (idea from Bob).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 2 — autonomous responder: Cal replies on its own when addressed (fleet-only, kill switch, rate limits).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 1 — always-on bridge; node flashed to Meshtastic 2.7.26 and brought online as "Cal HT".</div>
    </div>
  </div>
</main>
<footer>cal-mesh dashboard v2 · auto-refresh 3s · read-only · previous version: <a class="faqlink" id="oldlink2" href="old-1">old-1</a></footer>
<script>
const $=s=>document.querySelector(s);
const DIR=(function(){let p=location.pathname.replace(/\/(v2|old-\d+)\/?$/,'/');
 return p.endsWith('/')?p:p+'/';})();
let SNR={}, lastNodes=[], nodeSort={key:null,dir:1}, lastXsig=null;
let SELF={id:null,name:null};
const NODE_LABELS={short:'Short',long:'Name',hw:'HW',hops:'Hops',snr:'SNR'};
function esc(s){return (s??"").toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hhmmss(ts){try{return new Date(ts).toLocaleTimeString();}catch(e){return ts;}}
function daystamp(ts){try{return new Date(ts).toLocaleDateString(undefined,{month:'short',day:'numeric'});}catch(e){return '';}}
function secs(ms){return (ms/1000).toFixed(2)+'s';}
function tile(k,v,sub){return `<div class="tile"><div class="k">${k}</div><div class="v">${v}${sub?` <small>${sub}</small>`:''}</div></div>`;}
function fmtDur(s){s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
 return h?`${h}h ${m}m`:(m?`${m}m ${ss}s`:`${ss}s`);}
function batteryLabel(m){ if(m.battery==null) return '—';
  if(m.battery>100) return 'ext power';   // Meshtastic sentinel, not a charge level
  return m.battery+'%'; }
function skipWhy(r){
  const m={sender_not_allowed:'sender is not on the allow-list — Cal heard it perfectly well and chose not to answer',
           not_addressed:'Cal was not addressed (no "cal" mention, not a DM)',
           disabled:'the responder kill switch is off',
           too_old:'the message was older than the freshness window',
           rate_limited:'rate limit reached for this sender',
           cooldown:'per-sender cooldown still active',
           self:'this was Cal\'s own message'};
  return m[r]||esc(r||'unknown');
}
function verdictTag(x){
  if(x.verdict==='replied') return '<span class="tag tx">REPLIED</span>';
  if(x.verdict!=='skipped') return '<span class="tag quiet">NOT EVALUATED</span>';
  return x.reason==='sender_not_allowed'
    ? '<span class="tag offlist">OFF-LIST · heard, not answered</span>'
    : `<span class="tag quiet">NO REPLY · ${esc(x.reason)}</span>`;
}
function row(k,v){return `<div class="trow"><span class="tk">${k}</span><span class="tv">${v}</span></div>`;}
function nodeName(id){
  const n=lastNodes.find(n=>n.id===id);
  return n?(n.short||n.long||id):id;
}
// Clamp a label to what a fixed-width box can actually hold. SVG text does not wrap and does
// not shrink: an over-long node name silently paints across its own border and its neighbour.
function fitLabel(s, n){ s=(s==null?'':String(s)); return s.length>n ? s.slice(0,n-1)+'…' : s; }

// A LINK diagram, deliberately not a geographic one: who transmitted, who relayed it, who
// received it. It uses only what is already public on the air — there are no coordinates here
// and none are stored, because this page is public and the base station sits at a fixed
// private location.
//
// TWO LAYOUT RULES, both learned by shipping the violation (2026-08-11):
//
//   1. NO PROSE INSIDE THE SVG. A viewBox is a fixed canvas and <text> neither wraps nor
//      reflows, so a caption long enough to be worth reading gets clipped at BOTH ends — which
//      is exactly what happened when the caption grew to explain the relay byte. Every sentence
//      now lives in HTML underneath, in the same key/value rows the rest of the trace uses, so
//      it wraps and can never be truncated. The SVG holds boxes and arrows and nothing else.
//   2. NOTHING FLOATS BETWEEN THE BOXES. The old signal label sat at the arrow midpoint, and
//      once a third box appeared the gaps were narrower than the label — it painted over the
//      node it was pointing at. Signal is a fact about the last hop, so it is stated as such
//      in the rows below rather than squeezed into the gap.
function linkSvg(x){
  const hops=x.hops;
  const relayId = x.relay_byte!=null ? '·'+x.relay_byte.toString(16).padStart(2,'0') : null;
  // Colour the sender box by what Cal DID with it, so the diagram carries the same signal as
  // the verdict badge above. Green on an off-list sender read as "allowed" — backwards.
  const offlist = x.verdict==='skipped' && x.reason==='sender_not_allowed';
  const quiet   = x.verdict==='skipped' && !offlist;
  const senderC = offlist ? {fill:'#fff8c5', stroke:'#d4a72c'}      // matches the OFF-LIST tag
                : quiet   ? {fill:'#f2f4f7', stroke:'#8c96a3'}
                          : {fill:'#f2f4f7', stroke:'#1a7f37'};
  const stops=[x.from
    ? {lab:esc(fitLabel(nodeName(x.from),15)), sub:esc(fitLabel(x.from,15)), fill:senderC.fill, stroke:senderC.stroke}
    : {lab:'unknown', sub:'no id recorded', fill:senderC.fill, stroke:senderC.stroke}];
  // A hop is a REBROADCAST, so N hops means N relays stood between the sender and Cal — and the
  // firmware only ever tells us the last one. Drawing a single relay box for N>1 quietly implied
  // we knew the whole path. The ones we cannot name are now counted and drawn dashed, so the
  // diagram shows the size of what it does not know instead of hiding it.
  if(hops==null) stops.push({lab:'?', sub:'routing not recorded', dim:true, dash:true});
  if(hops>1) stops.push({lab:'?', sub:(hops-1)+' unknown relay'+(hops-1>1?'s':''), dim:true, dash:true});
  if(hops>0) stops.push({lab:'relay'+(relayId?' '+relayId:''), sub:relayId?'last relay':'id not reported', dim:true});
  stops.push({lab:esc(fitLabel(SELF.name||'Cal HT',15)), sub:esc(fitLabel(SELF.id||'',15)), self:true});

  // The canvas is a CONSTANT width sized for the widest case (4 boxes) and the row is centred
  // inside it. Sizing the viewBox to the content instead makes the SVG scale up to the CSS
  // width, so a two-box diagram renders with boxes nearly twice the size of a four-box one —
  // the same message looks like a different kind of object depending on how far it travelled.
  const n=stops.length, bw=140, gap=38, by=10, bh=50, MAXN=4;
  const W=20+MAXN*bw+(MAXN-1)*gap, H=by+bh+10;
  const x0=(W-(n*bw+(n-1)*gap))/2;
  let svg='';
  stops.forEach((s,i)=>{
    const bx=x0+i*(bw+gap);
    if(i>0){
      const x1=bx-gap+2, x2=bx-4;
      svg+=`<line x1="${x1}" y1="${by+bh/2}" x2="${x2-6}" y2="${by+bh/2}" stroke="#8c96a3" stroke-width="2"/>`
         +`<path d="M${x2-7} ${by+bh/2-4.5} L${x2} ${by+bh/2} L${x2-7} ${by+bh/2+4.5}z" fill="#8c96a3"/>`;
    }
    const fill=s.fill||(s.self?'#f7f4fd':'#f2f4f7');
    const stroke=s.stroke||(s.self?'#6639ba':(s.dim?'#8c96a3':'#1a7f37'));
    svg+=`<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="8" fill="${fill}" stroke="${stroke}" `
       +`stroke-width="1.5"${s.dash?' stroke-dasharray="5 4"':''}/>`
       +`<text x="${bx+bw/2}" y="${by+21}" fill="#1a1f26" font-size="13" font-weight="600" text-anchor="middle">${s.lab}</text>`
       +`<text x="${bx+bw/2}" y="${by+37}" fill="#5c6672" font-size="10.5" text-anchor="middle">${s.sub}</text>`;
  });
  const diagram=`<div class="link-d"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" `
       + `role="img" aria-label="link diagram">${svg}</svg></div>`;

  // A null hop count has more than one cause and they are not interchangeable. Records written
  // before routing capture shipped (2026-08-09) carry no hop_start KEY AT ALL; records written
  // after always carry the key, even when its value is null. Saying "predates routing capture"
  // for both put a false claim about a message's history on a public page.
  let rows='';
  if(hops==null)
    rows+=row('path', x.hop_start===undefined
      ? 'unknown — this message predates routing capture'
      : 'unknown — no usable hop count was recorded for this message');
  else if(hops===0)
    rows+=row('path','direct — Cal heard the sending radio itself, with no relay in between');
  else{
    rows+=row('path', hops+' hop'+(hops>1?'s':'')+' — relayed'
      +(hops>1?', and only the last relay is identified':''));
    if(relayId)
      rows+=row('last relay','id ends <code>'+esc(relayId)+'</code> — one byte of the node number that '
        +'relayed it, which narrows the candidates but does not identify a node');
  }
  // The signal belongs to the LAST hop and nothing else. Stating that plainly matters: a message
  // relayed from close by arrives strong no matter how far the sender is, and reading it as
  // "nearby" is the natural mistake.
  if(x.snr!=null)
    rows+=row('final leg','snr '+esc(x.snr)+(x.rssi!=null?' · rssi '+esc(x.rssi)+' dBm':'')
      +' — measured on the last hop only'+(hops>0?', which came from the relay, not the sender':''));
  // A recovered count was reconstructed after the fact, not measured at capture. It is sound —
  // the arithmetic is forced once hop_start is known — but it is not the same kind of fact, and
  // the page should not blur the two.
  if(x.hops_recovered)
    rows+=row('note','hop count recovered from a record predating the capture fix — reconstructed, not measured at the time');
  return {diagram:diagram, rows:rows, summary:(
    hops==null?'routing not recorded'
    :hops===0?'heard direct, no relay in between'
    :hops+' hop'+(hops>1?'s':'')+' — arrived by relay')};
}
// The reply is composed from a fact the harness fetched, and on a capability answer the
// sender's own words are never handed to the model at all. That is the single least obvious
// thing about this system and it was previously one clause inside a grey row. Drawn instead:
// two inputs compete to become the reply, and one of them is visibly cut.
function swapHtml(x,t){
  const inTxt=esc(x.text||''), outTxt=esc(x.reply||'');
  if(!outTxt) return '';
  const capability=!!t.injected_fact;
  const inBox=`<div class="sw i-in${capability?' cut':''}">`
    +`<div class="swk">message in</div><div class="swv">${inTxt}</div>`
    +`<div class="swn">${capability
      ? 'never handed to the model — it only selected which fact to look up'
      : 'sanitized, then quoted to the model'}</div></div>`;
  const outBox=`<div class="sw i-out"><div class="swk">reply out</div>`
    +`<div class="swv">${outTxt}</div>`
    +`<div class="swn">${esc(t.dest||'')} · 5-7 words, because every node in range shares the airtime</div></div>`;
  if(!capability)
    return `<div class="swap">${inBox}<span class="conn c1"></span>${outBox}</div>`;
  const factBox=`<div class="sw i-fact"><div class="swk">fact in</div>`
    +`<div class="swv"><code>${esc(t.injected_fact)}</code></div>`
    +`<div class="swn">measured${t.obs_station?' at station '+esc(t.obs_station):''}`
    +`${t.obs_age_s!=null?', '+Math.round(t.obs_age_s/60)+' min before the reply':''}`
    +` — injected verbatim, and the model may only put it into words</div></div>`;
  return `<div class="swap">${inBox}<span class="conn c1 brk"><b>not sent</b></span>`
    +`${factBox}<span class="conn c2"></span>${outBox}</div>`;
}
function bar(fillPct,markPct,late){
  const f=Math.max(0,Math.min(100,fillPct));
  return `<div class="bar"><i class="fill${late?' late':''}" style="width:${f.toFixed(1)}%"></i>`
    +(markPct!=null?`<span class="mk" style="left:${Math.max(0,Math.min(100,markPct)).toFixed(1)}%"></span>`:'')
    +`</div>`;
}
function stage(cls,name,summary,detail){
  return `<li class="stg ${cls}"><span class="sdot"></span>`
    +`<div class="shead"><span class="sname">${name}</span><span class="ssum">${summary}</span></div>`
    +`<div class="sdet">${detail||''}</div></li>`;
}
// The stages are a sequence in time and a gated-out message genuinely never reaches the later
// ones — verified against the records: a skipped decision carries no sanitize, no fact, no
// model and no destination. So "never reached" is read off the record, not assumed.
function spineHtml(x,t){
  const link=(x.kind==='exchange')?linkSvg(x):null;
  let s='';
  if(link) s+=stage('pass','received',link.summary,link.diagram+link.rows);
  const gated=t.gates&&t.gates.length;
  const stopped=x.verdict==='skipped';
  if(gated){
    const passed=t.gates.filter(g=>g.pass).length;
    s+=stage(stopped?'stop':'pass','gated',
      stopped?`stopped at <b>${esc((t.gates.find(g=>!g.pass)||{}).gate||'a check')}</b>`
             :`all ${passed} checks passed`,
      t.gates.map(g=>`<span class="gate ${g.pass?'gp':'gf'}">${g.pass?'✓':'✗'} ${esc(g.gate)}</span>`).join('')
      +(stopped?'<span class="rungn">later checks never evaluated</span>':''));
  }
  if(!t.model&&stopped){
    s+=stage('skip','not answered','the message was received and recorded, and nothing further ran',
      '<span class="rungn">no text was sent to a model, and nothing went on air</span>');
    return `<ol class="spine">${s}</ol>`;
  }
  if(t.sanitize){const q=t.sanitize,b=[];
    // An older record carries only the boolean and genuinely cannot say WHICH was trimmed. Say
    // that, rather than guessing — and never guess toward "your words were dropped".
    const tk=q.sentence_trim!=null?q.sentence_trim:(q.sentence_trimmed?'unknown':'none');
    if(tk==='content') b.push(`first sentence kept (${q.dropped_chars!=null?q.dropped_chars+' chars':'the rest'} dropped)`);
    else if(tk==='unknown') b.push('something was trimmed from the end — this record predates the '
      +'detail that says whether it was punctuation or content');
    else if(tk==='punctuation') b.push('trailing punctuation trimmed, no content dropped');
    if(q.length_capped) b.push('length capped');
    if(q.redactions) b.push(`${q.redactions} redaction${q.redactions>1?'s':''}`);
    if(q.flagged) b.push('injection-shaped tokens flagged');
    s+=stage('pass','sanitized',`${q.in_chars}&rarr;${q.out_chars} characters`,
      b.length?`<span class="hint">${esc(b.join(' · '))}</span>`:'<span class="hint">nothing removed</span>');}
  if(t.forecast_asked)
    s+=stage('stop','refused','asked about a future condition',
      '<span class="hint">the capability holds current observations only, so a fixed reply was sent '
      +'and no lookup was made at all</span>');
  if(x.capability){
    const ok=t.weather_ok, age=t.obs_age_s;
    let d='';
    if(age!=null){const pct=Math.min(100,(age/3600)*100);
      d=bar(pct,null,age>2700)+`<div class="barl">reading was ${Math.round(age/60)} minutes old when Cal `
       +`answered, against the hour these stations report on. A real observation from the nearest `
       +`station, never an estimate for one spot.</div>`;}
    const fstate = ok===true?'ok' : (ok===false?'FAILED':'not attempted');
    s+=stage(ok===true?'pass':(ok===false?'stop':'skip'),'grounded',
      `${esc(x.capability)} · fetch ${fstate}`
      +(t.obs_station?` · station <code>${esc(t.obs_station)}</code>`:''), d);}
  if(t.model){
    const ms=x.gen_ms;
    let d='<span class="hint">generation returns plain text — no chain of thought exists to show</span>';
    if(ms!=null){const MAXS=45,sec=ms/1000;
      const band=t.prompt_kind==='weather';
      d=`<div class="bar">${band?`<i style="left:${(7/MAXS*100).toFixed(1)}%;width:${((44-7)/MAXS*100).toFixed(1)}%"></i>`:''}`
        +`<span class="mk" style="left:${Math.max(0,Math.min(100,sec/MAXS*100)).toFixed(1)}%"></span></div>`
        +`<div class="barl">${secs(ms)} on a 0-45 s scale.`
        +(band?' The shaded band is the <b>7-44 s</b> this same prompt was measured spanning run to run.':'')
        +` Most of it is process startup and a network round trip, so read it as an order of `
        +`magnitude and not as thinking time.</div>`;}
    s+=stage('pass','narrated',`<code>${esc(t.model)}</code>`,d);}
  if(t.gen_status&&t.gen_status!=='ok')
    s+=stage('stop','generation',`<code>${esc(t.gen_status)}</code>`,'');
  if(t.dest) s+=stage('pass','sent',`on air to <code>${esc(t.dest)}</code>`,'');
  return `<ol class="spine">${s}</ol>`;
}
// The trace reads top to bottom as what happened: first the outcome and how it was arrived at
// (the swap), then the machinery stage by stage (the spine). The old flat key/value list gave a
// gate check, a station reading and the transmitted reply the same weight and the same grey
// label, which left the sequence — the only thing the trace is actually about — invisible.
function traceHtml(x){
  const t=x.trace||{};
  if(!t.gates&&!t.sanitize&&!t.model){
    const l=(x.kind==='exchange')?linkSvg(x):null;
    return `<div class="tp">${l?l.diagram+l.rows:''}`+
      '<div class="tnone">No decision trace recorded — this message predates it.</div></div>';}
  let h=swapHtml(x,t)+spineHtml(x,t);
  h+='<div class="tnote">This is the machinery, not the model\'s reasoning. Generation returns plain '
   +'text with no chain of thought, and asking for a narration would produce a plausible story rather '
   +'than an account of what actually happened — so it is not shown.</div>';
  return `<div class="tp">${h}</div>`;
}
// The page re-renders every 3s, which would wipe any <details> the reader had opened. Track
// open traces by a stable key and restore the attribute on every render, so an expanded trace
// stays expanded until it is clicked shut. (Toggle doesn't bubble — the listener captures.)
const OPEN=new Set();
// Lets a trace be built on demand when its disclosure is opened, rather than for every
// exchange on every pass. Rebuilt from the current data each render, so an open trace never
// shows a stale copy of a record that has since changed.
const XBYKEY=new Map();
function xkey(x){return (x.ts||'')+'|'+(x.from||x.dest||'');}
function exchangeHtml(x){
  if(x.kind==='unprompted') return `
    <div class="xc unprompted"><div class="meta"><span class="tag tx">TX</span>
      <span>${daystamp(x.ts)} ${hhmmss(x.ts)}</span><span>→ ${esc(x.dest)}</span>
      <span class="tag ch">ch${esc(x.channel)}</span><span>${esc(x.transport)}</span>
      <span class="tag quiet">${x.source==='responder'?'UNPAIRED':'MANUAL'}</span></div>
    <div class="ask">${esc(x.text)}</div>
    <div class="norep">↳ not a reply — Cal transmitted this with no inbound ask${x.source==='responder'?', or the ask is older than the window shown':''}</div></div>`;
  return `
    <div class="xc"><div class="meta"><span class="tag rx">RX</span>
      <span>${daystamp(x.ts)} ${hhmmss(x.ts)}</span><span>${x.from?esc(x.from):'unknown sender'} → ${esc(x.to)}</span>
      <span class="tag ch">ch${esc(x.channel)}</span>${x.snr!=null?`<span>snr ${esc(x.snr)}</span>`:''}
      ${verdictTag(x)}</div>
    <div class="ask">${esc(x.text)}</div>
    ${x.verdict==='replied'&&x.reply
      ? `<div class="rep"><span class="who">↳ Cal replied${x.gen_ms!=null?` · ${secs(x.gen_ms)}`:''}${x.capability?` · ${esc(x.capability)}`:''}</span><span class="txt">${esc(x.reply)}</span></div>`
      : (x.verdict==='skipped'?`<div class="norep">↳ received, no reply — ${skipWhy(x.reason)}</div>`:'')}
    <details class="tr" data-k="${esc(xkey(x))}"${OPEN.has(xkey(x))?' open':''}><summary>trace</summary>
    <div class="tpwrap">${OPEN.has(xkey(x))?traceHtml(x):''}</div></details></div>`;
}
function setSort(k){ nodeSort=(nodeSort.key===k)?{key:k,dir:-nodeSort.dir}:{key:k,dir:1}; renderNodes(); }
function renderNodes(){
  let ns=lastNodes.slice();
  if(nodeSort.key){ const k=nodeSort.key, dir=nodeSort.dir;
    ns.sort((a,b)=>{ let x=a[k],y=b[k];
      if(k==='hops'||k==='snr'){ if(x==null&&y==null)return 0; if(x==null)return 1; if(y==null)return -1; return (x-y)*dir; }
      x=(x||'').toString().toLowerCase(); y=(y||'').toString().toLowerCase();
      return x<y?-dir:(x>y?dir:0); }); }
  const tb=$('#nodes').querySelector('tbody');
  tb.innerHTML=ns.map(n=>{ const sg=(n.snr!=null&&n.snr>0)?'snr-good':'snr-bad';
    return `<tr><td>${esc(n.short)}</td><td>${esc(n.long)}</td><td>${esc(n.hw)}</td>`+
      `<td>${n.hops==null?'—':esc(n.hops)}</td><td class="${sg}">${n.snr==null?'—':esc(n.snr)}</td>`+
      `<td>${sparkline((SNR[n.id]||{}).points, n.hops)}</td></tr>`; }).join('');
  document.querySelectorAll('#nodes th.sortable').forEach(th=>{
    const k=th.dataset.key, on=nodeSort.key===k;
    th.textContent=NODE_LABELS[k]+(on?(nodeSort.dir>0?' ▲':' ▼'):''); });
}
async function loadSnr(){try{SNR=await (await fetch(DIR+'api/snr',{cache:'no-store'})).json();}catch(e){}}
function sparkline(pts, hops){
  if(!pts||pts.length===0){
    return (hops!=null&&hops>0)?'<span style="color:var(--dim)">multi-hop</span>'
      :'<span style="color:var(--dim)">— <small>no direct signal</small></span>';}
  if(pts.length===1){const v=pts[0][1];
    return `<span class="spark"><svg width="90" height="22"><circle cx="45" cy="11" r="2.5" fill="var(--accent)"/></svg>`+
      `<span style="color:var(--accent)">${esc(v)} <small>dB · 1 pt</small></span></span>`;}
  const W=90,H=22,pad=3;
  const ts=pts.map(p=>p[0]), vs=pts.map(p=>p[1]);
  const t0=Math.min(...ts),t1=Math.max(...ts),vmin=Math.min(...vs),vmax=Math.max(...vs);
  const sx=t=>pad+(t1===t0?(W-2*pad):((t-t0)/(t1-t0))*(W-2*pad));
  const sy=v=>pad+(1-(vmax===vmin?0.5:(v-vmin)/(vmax-vmin)))*(H-2*pad);
  const d=pts.map((p,i)=>(i?'L':'M')+sx(p[0]).toFixed(1)+' '+sy(p[1]).toFixed(1)).join(' ');
  const last=pts[pts.length-1];
  const k=Math.max(1,Math.floor(pts.length/3));
  const avg=a=>a.reduce((s,x)=>s+x,0)/a.length;
  const dv=avg(vs.slice(-k))-avg(vs.slice(0,k));
  const arrow=dv>1.5?'↗':(dv<-1.5?'↘':'→');
  const col=dv<-1.5?'var(--warn)':(dv>1.5?'var(--ok)':'var(--accent)');
  return `<span class="spark" title="${pts.length} samples · now ${esc(last[1])} dB">`+
    `<svg width="${W}" height="${H}"><path d="${d}" fill="none" stroke="${col}" stroke-width="2" `+
    `stroke-linejoin="round" stroke-linecap="round"/><circle cx="${sx(last[0]).toFixed(1)}" `+
    `cy="${sy(last[1]).toFixed(1)}" r="2.5" fill="${col}"/></svg>`+
    `<span style="color:${col}">${arrow} ${esc(last[1])}</span></span>`;
}
async function tick(){
 let d; try{d=await (await fetch(DIR+'api/state',{cache:'no-store'})).json();}
 catch(e){$('#conn').className='pill bad';$('#conn').textContent='dashboard offline';return;}
 const st=d.status||{}, m=st.metrics||{}, node=st.node||{}, rp=d.responder||{};
 const on=st.connected;
 $('#conn').className='pill '+(on?'ok':'bad');
 $('#conn').textContent=on?'● radio connected':'● radio down';
 $('#sub').textContent=`${node.longName||'?'} (${node.shortName||'?'}) · ${node.id||''} · fw ${st.firmware||'?'}`;
 const live=rp.enabled==='true';
 $('#tiles').innerHTML=[
   tile('Battery', batteryLabel(m), m.voltage!=null?m.voltage.toFixed(2)+'V':''),
   tile('Bridge', (d.bridge.state==='running'?'running':'stopped'), d.bridge.pid?('pid '+d.bridge.pid):''),
   tile('Uptime', st.uptime_s!=null?fmtDur(st.uptime_s):'—'),
   tile('Responder', `<span class="dot ${live?'on':'off'}"></span>${live?'live':'off'}`,
        (rp.model?rp.model.replace('claude-','').replace(/-\d+$/,''):'')+' · '+(rp.allow_count||0)+' allowed'),
   tile('Sent / Received', `${(d.totals&&d.totals.sent)??0} / ${(d.totals&&d.totals.recv)??0}`),
   tile('Ch util', m.chUtil!=null?m.chUtil.toFixed(1)+'%':'—', m.airUtilTx!=null?('air '+m.airUtilTx.toFixed(2)+'%'):''),
 ].join('');
 const cfg=d.config||{}, active=(st.transport||cfg.TRANSPORT||'serial');
 $('#active-t').textContent='active: '+active;
 $('#trans').innerHTML=[
   `<div class="t ${active==='serial'?'active':''}"><div class="lbl"><span class="dot ${active==='serial'?'on':'off'}"></span>USB</div></div>`,
   `<div class="t ${active==='tcp'?'active':''}"><div class="lbl"><span class="dot ${active==='tcp'?'on':'off'}"></span>WiFi</div></div>`,
 ].join('');
 SELF={id:node.id||null, name:node.shortName||node.longName||null};
 lastNodes=(d.nodes&&d.nodes.nodes)||[];
 const xs=d.exchanges||[];
 $('#xc-n').textContent=xs.length;
 // Only touch the DOM when the content actually changed. Cheap, and it stops the 3s refresh
 // from fighting the reader (lost text selection, scroll jump) when nothing has happened.
 const sig=JSON.stringify([xs,SELF,lastNodes.map(n=>[n.id,n.short])]);
 if(sig!==lastXsig){
   lastXsig=sig;
   XBYKEY.clear(); xs.forEach(x=>XBYKEY.set(xkey(x),x));
   $('#exchanges').innerHTML=xs.length?xs.map(exchangeHtml).join('')
     :'<div class="empty">nothing on air yet — mesh is quiet or awaiting first inbound</div>';
 }
 $('#nn').textContent=lastNodes.length;
 renderNodes();
}
// 'toggle' does not bubble, so listen in the capture phase on the container. Survives every
// re-render because the listener is on #exchanges, not on the details elements themselves.
// resolve the retired-version links against the app root, so they work at "/" and under a
// funnel path prefix alike
document.querySelectorAll('#oldlink,#oldlink2').forEach(a=>{a.href=DIR+'old-1';});
// A closed trace is not built. Every exchange used to render its full trace on every 3s pass
// whether or not anyone had opened it, which put a hard ceiling on how rich a trace could get.
// Bodies are now filled on first open and rebuilt by the normal render while they stay open.
$('#exchanges').addEventListener('toggle', e=>{
  const el=e.target;
  if(!el.matches||!el.matches('details.tr')) return;
  const k=el.dataset.k;
  if(!k) return;
  if(!el.open){ OPEN.delete(k); return; }
  OPEN.add(k);
  const body=el.querySelector('.tpwrap');
  const x=XBYKEY.get(k);
  if(body&&!body.firstChild&&x) body.innerHTML=traceHtml(x);
}, true);
(function(){
  const m=location.pathname.match(/\/(old-\d+)\/?$/);
  if(!m) return;
  const cur=location.pathname.replace(/\/old-\d+\/?$/,'/');
  const b=document.createElement('div');
  b.style.cssText='background:#fff8c5;color:#9a6700;border-bottom:1px solid #d4a72c;'+
    'padding:9px 22px;font-size:13px;text-align:center';
  b.innerHTML='This is <b>'+m[1]+'</b>, a retired version of the dashboard, kept for reference. '+
    '<a href="'+cur+'" style="color:#0a63c9;font-weight:600">Go to the current page &rarr;</a>';
  document.body.insertBefore(b, document.body.firstChild);
})();
loadSnr(); tick(); setInterval(tick,3000); setInterval(loadSnr,30000);
</script></body></html>"""


PAGE_V3 = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>cal-mesh — levers (v3)</title>
<style>
:root{--bg:#f6f8fa;--card:#ffffff;--card2:#eef1f5;--line:#d6dce4;--fg:#1a1f26;
--dim:#5c6672;--accent:#0a63c9;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--tx:#6639ba;--rx:#1a7f37;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:linear-gradient(180deg,#f6f8fa,#f6f8faee);backdrop-filter:blur(6px);z-index:5}
header h1{font-size:17px;margin:0;letter-spacing:.3px}
header .sub{color:var(--dim);font-size:12px}
.pill{margin-left:12px;padding:5px 12px;border-radius:999px;font-weight:600;font-size:12px}
.pill.ok{background:#dafbe1;color:var(--ok);border:1px solid #aceebb}
.pill.bad{background:#ffebe9;color:var(--bad);border:1px solid #ffcecb}
.faqlink{color:var(--accent);text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
.faqlink:hover{text-decoration:underline}
.navlinks{margin-left:auto;display:inline-flex;gap:14px;align-items:center}
html{scroll-behavior:smooth}
main{padding:20px;max-width:1200px;margin:0 auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.tile .v{font-size:22px;font-weight:650;margin-top:4px}
.tile .v small{font-size:12px;color:var(--dim);font-weight:400}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:16px}
.card h2{font-size:13px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);
color:var(--dim);text-transform:uppercase;letter-spacing:.6px;display:flex;gap:8px;align-items:center}
.card h2 .badge{background:var(--card2);color:var(--fg);padding:2px 8px;border-radius:6px;font-size:11px;font-variant-numeric:tabular-nums}
.card h2 .badge.right{margin-left:auto}
.tag{padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600}
.tag.tx{background:#f3eefc;color:var(--tx)} .tag.rx{background:#dafbe1;color:var(--rx)}
.tag.ch{background:#ddf4ff;color:var(--accent)} .tag.auto{background:#fff8c5;color:var(--warn)}
.tag.offlist{background:#fff8c5;color:var(--warn);border:1px solid #d4a72c}
.tag.quiet{background:#eef1f5;color:var(--dim)}
/* --- tabbed streams: one card, two streams, so the page does not grow by one full
   card every time a stream is added. The pane is toggled with [hidden] rather than
   re-rendered, so the 3s refresh cannot knock the reader back to the first tab. --- */
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);background:var(--card2);padding:0 6px}
.tab{appearance:none;background:none;border:0;border-bottom:2px solid transparent;color:var(--dim);
font:inherit;font-size:12.5px;padding:11px 12px;cursor:pointer;display:flex;align-items:center;gap:7px;
white-space:nowrap}
.tab:hover{color:var(--fg)}
.tab[aria-selected="true"]{color:var(--fg);border-bottom-color:var(--accent)}
.tab .badge{background:var(--card)}
.tab:focus-visible{outline:2px solid var(--accent);outline-offset:-3px;border-radius:4px}
.pane[hidden]{display:none}
.tabnote{font-size:11.5px;color:var(--dim);line-height:1.55;margin:0;padding:13px 16px 2px;max-width:80ch}
@media(max-width:520px){.tab{padding:10px 8px;font-size:11.5px}}
/* --- exchanges --- */
.xc{padding:14px 16px;border-bottom:1px solid var(--line)}
.xc:last-child{border-bottom:0}
.xc .meta{color:var(--dim);font-size:11px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:5px}
.xc .ask{font-size:15px;word-break:break-word;max-width:78ch}
.rep .txt,.norep{max-width:78ch}
.xc.unprompted{background:#f0f3f7}
.rep{margin:9px 0 0 16px;padding:8px 12px;border-left:2px solid var(--tx);background:#f7f4fd;
border-radius:0 8px 8px 0}
.rep .who{color:var(--dim);font-size:11px;display:block;margin-bottom:2px}
.rep .txt{color:var(--tx);font-size:14px}
.norep{margin:8px 0 0 16px;padding:7px 12px;border-left:2px solid var(--line);background:#f2f4f7;
border-radius:0 8px 8px 0;color:var(--dim);font-size:12.5px}
/* --- trace disclosure --- */
details.tr{margin:10px 0 0 16px}
details.tr summary{cursor:pointer;list-style:none;color:var(--accent);font-size:13.5px;
font-weight:600;letter-spacing:.2px;display:inline-flex;gap:7px;align-items:center;
padding:4px 10px 4px 8px;border:1px solid var(--line);border-radius:7px;background:var(--card2)}
details.tr summary::-webkit-details-marker{display:none}
details.tr summary::before{content:">";font-size:13px;font-weight:700;display:inline-block;
transform-origin:50% 50%;transition:transform .15s ease}
details.tr[open] summary::before{transform:rotate(90deg)}
details.tr summary:hover{border-color:var(--accent);background:#e4e9f0}
.tp{margin-top:7px;background:#f4f6f9;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.link-d{margin:2px 0 10px;max-width:620px}
.link-d svg{width:100%;height:auto;display:block}
.trow{display:flex;gap:10px;padding:3px 0;font-size:12px;align-items:baseline}
.tk{color:var(--dim);min-width:78px;flex-shrink:0;text-transform:uppercase;font-size:10px;letter-spacing:.5px}
.tv{color:var(--fg);word-break:break-word}
.tv code{background:var(--card2);padding:1px 5px;border-radius:4px;font-size:11.5px}
.hint{color:var(--dim);font-size:11px}
.gate{display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:4px;font-size:11px}
.gp{background:#dafbe1;color:var(--ok)} .gf{background:#ffebe9;color:var(--bad)}
.tnote{margin-top:8px;padding-top:7px;border-top:1px solid var(--line);color:var(--dim);font-size:11px;line-height:1.5}
.tnone{color:var(--dim);font-size:12px}
/* --- trace: the swap. What reached the model and what did not, drawn rather than asserted.
   Two things compete to become the reply; on a capability answer one of them is cut. --- */
.swap{display:grid;grid-template-columns:minmax(0,1fr) 74px minmax(0,1fr);gap:9px 0;
align-items:center;margin:2px 0 12px}
.sw{border:1px solid var(--line);border-radius:9px;padding:8px 10px;background:var(--card);min-width:0}
.sw .swk{font-size:9.5px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);font-weight:700}
.sw .swv{font-size:12.5px;margin-top:3px;word-break:break-word;line-height:1.45}
.sw .swn{font-size:10.5px;margin-top:5px;color:var(--dim);line-height:1.4}
.sw.i-in{grid-column:1;grid-row:1} .sw.i-fact{grid-column:1;grid-row:2}
.sw.i-out{grid-column:3;grid-row:1/3;align-self:stretch;display:flex;
flex-direction:column;justify-content:center}
.sw.cut{border-style:dashed;background:#fbfcfd}
.sw.cut .swv{color:var(--dim);text-decoration:line-through;text-decoration-color:#b9c2cd}
.sw.i-fact{border-color:#aceebb;background:#f4fcf6}
.sw.i-out{border-color:#ddd0f5;background:#faf7fe}
.sw.i-out .swv{color:var(--tx);font-size:13.5px}
/* --- trace: the pipeline spine. The stages are a sequence in time, so they are drawn as one. --- */
.spine{list-style:none;margin:0;padding:0}
.stg{position:relative;padding:0 0 11px 25px}
.stg::before{content:"";position:absolute;left:5px;top:16px;bottom:0;width:2px;background:var(--line)}
.stg:last-child::before{display:none}
.stg>.sdot{position:absolute;left:0;top:5px;width:12px;height:12px;border-radius:50%;
background:var(--ok);border:2px solid var(--ok);box-sizing:border-box}
.stg.stop>.sdot{background:var(--bad);border-color:var(--bad)}
.stg.skip>.sdot{background:var(--card);border-color:#c3ccd7}
.stg.skip{opacity:.6}
.stg.stop::before,.stg.skip::before{background:repeating-linear-gradient(180deg,#c3ccd7 0 3px,transparent 3px 6px)}
.stg .shead{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.stg .sname{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);
font-weight:700;flex-shrink:0}
.stg .ssum{font-size:12.5px;color:var(--fg)}
.stg .sdet{margin-top:5px}
.stg .sdet:empty{display:none}
.rungn{display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:4px;font-size:11px;
background:#f2f4f7;color:var(--dim);font-style:italic}
/* --- trace: measurements drawn to the scale they were measured on --- */
.bar{position:relative;height:6px;border-radius:3px;background:#e7ebf1;margin-top:6px;max-width:280px}
.bar>i{position:absolute;top:0;bottom:0;border-radius:3px;background:#cfe6d6}
.bar>i.fill{left:0;background:var(--ok)}
.bar>i.fill.late{background:var(--warn)}
.bar .mk{position:absolute;top:-3px;width:2px;height:12px;background:var(--fg);border-radius:1px}
.barl{font-size:10.5px;color:var(--dim);margin-top:4px;line-height:1.45;max-width:60ch}
@media(max-width:640px){
.swap{grid-template-columns:minmax(0,1fr);gap:7px}
.sw.i-in,.sw.i-fact,.sw.i-out{grid-column:1;grid-row:auto}
.conn{display:none}}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 16px;font-size:13px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
td.snr-good{color:var(--ok)} td.snr-bad{color:var(--warn)}
#nodes-wrap{max-height:620px;overflow:auto}
#nodes thead th{position:sticky;top:0;background:var(--card);z-index:1}
#nodes th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
.trans{display:flex;gap:10px;padding:14px 16px}
.trans .t{flex:1;background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.trans .t.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.trans .t .lbl{font-size:11px;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.tile .v .dot{width:9px;height:9px;margin-right:7px;vertical-align:middle}
.dot.on{background:var(--ok)} .dot.off{background:var(--bad)}
footer{color:var(--dim);font-size:11px;text-align:center;padding:16px}
.empty{padding:16px;color:var(--dim);font-size:13px}
.faq h3{margin:0;padding:14px 16px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent);border-bottom:1px solid var(--line);background:#f0f3f7}
.faq .a a{color:var(--accent);text-decoration:none;font-weight:600}
.faq .a a:hover{text-decoration:underline}
.faq details{border-bottom:1px solid var(--line)}
.faq details:last-child{border-bottom:0}
.faq summary{padding:12px 16px;cursor:pointer;font-weight:600;list-style:none;display:flex;align-items:center;gap:10px}
.faq summary::-webkit-details-marker{display:none}
.faq summary::before{content:"+";color:var(--accent);font-weight:700;width:10px;display:inline-block}
.faq details[open] summary::before{content:"\2013"}
.faq .a{padding:0 16px 14px 40px;color:var(--dim);font-size:13px;line-height:1.65}
.faq .a code{background:var(--card2);padding:1px 5px;border-radius:4px;color:var(--fg);font-size:12px}
.faq .a b{color:var(--fg)}
.clog{max-height:420px;overflow-y:auto}
.clog .ci{padding:10px 16px;border-bottom:1px solid var(--line);font-size:13px;line-height:1.55}
.clog .cd{color:var(--dim);font-size:11px;margin-right:8px;font-variant-numeric:tabular-nums}
.spark{display:inline-flex;align-items:center;gap:6px;font-size:12px;white-space:nowrap}
/* ============================ v3: depth ============================
   v2 drew a sequence that happened in time as flat boxes on one plane. Three things carry
   the third dimension here: elevation (a recessed well under raised planes), wires that are
   MEASURED from real box geometry rather than approximated with horizontal rules, and an
   assembly order that runs once when a trace is opened. */
.tp{background:linear-gradient(180deg,#f7f9fc,#eef2f7);border-radius:12px;
box-shadow:inset 0 2px 5px rgba(22,27,34,.05),0 1px 0 #fff}
/* The chain, left to right. Boxes are numbered because the whole point is the ORDER:
   a reader who does not know how this works needs to see that the question caused the lookup. */
.flow{display:grid;align-items:center;margin:2px 0 4px;
grid-template-columns:minmax(0,1fr) 62px minmax(0,.92fr) 74px minmax(0,.86fr) 104px minmax(0,1fr)}
.flow.gen{grid-template-columns:minmax(0,1fr) 150px minmax(0,1fr)}
.fb{position:relative;z-index:1;border:1px solid var(--line);border-radius:11px;padding:10px 13px;
background:linear-gradient(180deg,#fff,#fbfcfe);
box-shadow:0 1px 2px rgba(22,27,34,.05),0 6px 16px -8px rgba(22,27,34,.22)}
.fk{font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--dim)}
.fv{font-size:13px;margin-top:5px;line-height:1.45;word-break:break-word}
.fn{font-size:10.5px;margin-top:6px;color:var(--dim);line-height:1.42;overflow-wrap:anywhere}
/* the recognition step: plain word-matching, no model. Drawn as a decision, not as data. */
.fb.bx{border-color:#c3d9f2;background:linear-gradient(180deg,#f7fbff,#eef5fd);
box-shadow:0 1px 2px rgba(10,99,201,.08),0 8px 20px -10px rgba(10,99,201,.30)}
.fb.bx .fv{font-size:12.5px}
.chip{display:inline-block;margin:3px 4px 0 0;padding:1px 7px;border-radius:5px;
max-width:100%;overflow-wrap:anywhere;word-break:break-word;vertical-align:top;
background:#dceafb;color:#0a4da3;font-size:11px;font-weight:600;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.fb.b2{border-color:#a7e3b6;background:linear-gradient(180deg,#f4fdf7,#eaf9ef);
box-shadow:0 1px 2px rgba(26,127,55,.10),0 8px 20px -10px rgba(26,127,55,.35)}
.fb.b2 .fv{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.fb.b3{border-color:#d5c6f3;background:linear-gradient(180deg,#fbf8ff,#f5effd);
box-shadow:0 1px 2px rgba(102,57,186,.10),0 8px 20px -10px rgba(102,57,186,.35)}
.fb.b3 .fv{color:var(--tx);font-weight:600;font-size:14px}
.onair{color:var(--ok);font-weight:600}
/* arrows: straight, because every box shares a centre line — no measurement needed */
.arw{position:relative;height:3px;border-radius:2px;
background:linear-gradient(90deg,#cbd4de,var(--ok));transform-origin:left center}
.arw::after{content:"";position:absolute;right:-1px;top:-5.5px;border:6px solid transparent;
border-left-color:var(--ok);border-right:0}
.arw>span{position:absolute;left:0;right:0;bottom:9px;font-size:9.5px;color:var(--dim);
text-align:center;line-height:1.3}
/* the boundary: the one surprising fact, stated once, on the line it describes */
.cross{position:relative;align-self:stretch;display:flex;align-items:center}
.cross::before{content:"";position:absolute;left:50%;top:0;bottom:0;margin-left:-1px;
border-left:2px dashed #9fb0c4}
.cross .arw{flex:1;margin:0 6px}
.cross .bl{position:absolute;left:50%;top:2px;transform:translateX(-50%);background:var(--card);
border:1px solid #c3d2e2;border-radius:6px;padding:3px 6px;font-size:8.5px;font-weight:700;
text-transform:uppercase;letter-spacing:.4px;color:#3d566e;text-align:center;line-height:1.25;
width:92px;box-sizing:border-box}
.flowcap{font-size:11.5px;color:var(--dim);line-height:1.55;margin:10px 0 14px;max-width:88ch;
padding-left:2px}
.flowcap b{color:var(--fg);font-weight:650}
.stg{padding:0 0 13px 30px}
.stg::before{left:6.5px;top:17px;width:3px;border-radius:2px;
background:linear-gradient(180deg,#cfd7e1,#dde3ea)}
.stg>.sdot{left:0;top:5px;width:16px;height:16px;border:0;
background:radial-gradient(circle at 35% 32%,#5fd07f,var(--ok));
box-shadow:0 0 0 3px rgba(26,127,55,.14),0 1px 2px rgba(22,27,34,.3)}
.stg.stop>.sdot{background:radial-gradient(circle at 35% 32%,#f08b93,var(--bad));
box-shadow:0 0 0 3px rgba(207,34,46,.14),0 1px 2px rgba(22,27,34,.3)}
.stg.skip>.sdot{background:#fff;box-shadow:inset 0 0 0 2px #c3ccd7}
.sname{min-width:74px;letter-spacing:.85px}
/* instruments: a measurement drawn against the range it lives on, not a bare number */
.inst{display:flex;gap:26px;flex-wrap:wrap;margin-top:8px}
.gauge{min-width:206px;max-width:320px}
.glab{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px;gap:12px}
.gname{font-size:9px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;color:var(--dim)}
.gval{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;font-weight:600}
.track{position:relative;height:9px;border-radius:5px;background:#e4e9f0;
box-shadow:inset 0 1px 2px rgba(22,27,34,.14)}
/* sequential magnitude = ONE hue, light to dark. Not a red-amber-green rainbow: that is a
   rainbow ramp for an ordered quantity, and red/amber are ~1.5 dE apart under deuteranopia. */
.track.ramp{background:linear-gradient(90deg,#eaf6ee,#9fd8b3,#1a7f37)}
.track .band{position:absolute;top:0;bottom:0;background:rgba(26,127,55,.28);border-radius:4px}
.track .mk{position:absolute;top:-4px;width:3px;height:17px;border-radius:2px;background:var(--fg);
box-shadow:0 0 0 2px #fff,0 1px 3px rgba(22,27,34,.4);transition:left .7s cubic-bezier(.22,1,.36,1)}
.gends{display:flex;justify-content:space-between;font-size:9.5px;color:var(--dim);margin-top:5px;gap:10px}
.gnote{font-size:10.5px;color:var(--dim);margin-top:6px;line-height:1.45;max-width:52ch}
/* assembly: runs once per open, never on the 3s refresh */
.tp.anim .stg>.sdot{transform:scale(.4);opacity:0;animation:sdotpop .45s cubic-bezier(.34,1.56,.64,1) forwards}
@keyframes sdotpop{to{transform:scale(1);opacity:1}}
.tp.anim .arw{transform:scaleX(0);animation:arwgrow .5s cubic-bezier(.22,1,.36,1) forwards}
@keyframes arwgrow{to{transform:scaleX(1)}}
.tp.anim .cross .bl{opacity:0;animation:blfade .35s ease forwards;animation-delay:.45s}
@keyframes blfade{to{opacity:1}}
@media(prefers-reduced-motion:reduce){
.tp.anim .stg>.sdot,.tp.anim .arw,.tp.anim .cross .bl{animation:none;opacity:1;transform:none}
.track .mk{transition:none}}
@media(max-width:700px){
.flow,.flow.gen{grid-template-columns:minmax(0,1fr)}
.arw,.cross{display:none}}
</style></head>
<body>
<header>
  <div><h1>📻 cal-mesh <span class="sub">— live levers (v3)</span></h1>
  <div class="sub" id="sub">connecting…</div></div>
  <span class="navlinks"><a class="faqlink" href="#faq">FAQ ↓</a><a class="faqlink" href="#changelog">Changelog ↓</a><a class="faqlink" href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">GitHub ↗</a></span>
  <span class="pill" id="conn">…</span>
</header>
<main>
  <div class="tiles" id="tiles"></div>
  <div class="card"><h2>Transports <span class="badge right" id="active-t"></span></h2>
    <div class="trans" id="trans"></div></div>
  <div class="card">
   <div class="tabs" role="tablist" id="xtabs">
    <button class="tab" role="tab" id="tab-open" aria-controls="pane-open" aria-selected="true">💬 Open Exchanges <span class="badge" id="xc-n">0</span></button>
    <button class="tab" role="tab" id="tab-dm" aria-controls="pane-dm" aria-selected="false">🔒 Direct Messages <span class="badge" id="dm-n">0</span></button>
   </div>
   <div class="pane" id="pane-open" role="tabpanel" aria-labelledby="tab-open"><div id="exchanges"></div></div>
   <div class="pane" id="pane-dm" role="tabpanel" aria-labelledby="tab-dm" hidden>
    <p class="tabnote">Cal and Dean&rsquo;s test bench. Trying things on the open channel costs every
    node in range airtime, so experiments happen here instead &mdash; one link, two nodes. <b>It is
    published for the same reason everything else here is:</b> the interesting part is what is being
    tried and how it works, and a private tier you cannot see is a claim rather than a demonstration.
    These are authenticated direct messages, so unlike the open channel the sender is
    cryptographically established rather than merely asserted.</p>
    <div id="dm-exchanges"></div>
   </div>
  </div>
  <div class="card"><h2><span class="badge" id="nn">0</span> Neighbors heard</h2>
    <div id="nodes-wrap"><table id="nodes"><thead><tr>
      <th class="sortable" data-key="short" onclick="setSort('short')">Short</th>
      <th class="sortable" data-key="long" onclick="setSort('long')">Name</th>
      <th class="sortable" data-key="hw" onclick="setSort('hw')">HW</th>
      <th class="sortable" data-key="hops" onclick="setSort('hops')">Hops</th>
      <th class="sortable" data-key="snr" onclick="setSort('snr')">SNR</th>
      <th>1h SNR trend</th></tr></thead><tbody></tbody></table></div>
  </div>
  <div class="card faq" id="faq"><h2>FAQ — what this is and how it works</h2>
    <h3>Start here</h3>
    <details><summary>What is this page?</summary><div class="a">
      A live, read-only window into <b>Cal</b> — an AI that lives on a <b>radio mesh network</b> and
      answers people over the air, with no internet on the far end. Everything here is real: the radio's
      state, every message in and out, and the full reasoning trace behind each automatic reply. Nothing
      is a mock-up. If Cal answered someone thirty seconds ago, it's below.</div></details>
    <details><summary>What is a mesh network?</summary><div class="a">
      A network with <b>no towers, no carrier and no internet</b>. Every radio is also a repeater: if
      two nodes are too far apart to hear each other, a third in the middle passes the message along,
      and so on. That's a <b>hop</b>. Coverage comes from the participants rather than infrastructure,
      so the network exists wherever people bring radios — and keeps working when the grid doesn't.
      That last property is the whole point: it's the tool you reach for when cell service is gone.</div></details>
    <details><summary>What is Meshtastic?</summary><div class="a">
      Free, open-source firmware that turns inexpensive <b>LoRa</b> radios (typically $30–100) into a
      mesh network for text messages and location sharing. You flash it onto a small board, pair it to
      your phone, and you're on the mesh — encrypted by channel, no account, no subscription, no
      monthly fee. It's a volunteer project with a large community, and it's what Cal's radio runs.
      <br><a href="https://meshtastic.org" target="_blank" rel="noopener noreferrer">meshtastic.org ↗</a></div></details>
    <details><summary>What is LoRa, and why does it matter here?</summary><div class="a">
      <b>Lo</b>ng <b>Ra</b>nge radio: a modulation designed to get a very small amount of data a very
      long way on very little power — miles between nodes, on a battery, with no licence required on
      the public bands. The trade is <b>bandwidth</b>. A LoRa channel carries on the order of a few
      hundred to a few thousand bits per second, and <b>every node in earshot shares it</b>. One long
      message blocks the channel for everyone. That single constraint explains most of Cal's design,
      starting with why it never says more than a few words.</div></details>
    <details><summary>Why put an AI on a mesh radio at all?</summary><div class="a">
      Because a mesh is what you use <b>when the grid isn't there</b> — off-grid, field work, dead
      coverage, emergencies — and that's exactly when knowledge is hardest to reach. The insight the
      project runs on: the mesh is off-grid, but the <b>base station usually isn't</b>. Cal's radio is
      connected to a computer with internet, so someone miles out with nothing but a handheld can ask a
      question over RF and get a real answer relayed back. Cal extends connected knowledge to the
      unconnected edge. Before this, a node could prove it was alive but couldn't actually
      <i>help</i> — presence without utility.</div></details>
    <details><summary>How did it get here?</summary><div class="a">
      Three deliberate stages, each gated before the next. <b>Level 1</b> — a bridge that owns the
      radio and can send and receive text. <b>Level 2</b> — an autonomous responder that decides on its
      own whether to answer and writes the reply, with training wheels (a small allow-list, rate limits,
      a kill switch). <b>Level 3</b> — real capabilities, where the software fetches a verified fact and
      the model only puts it into words. Each stage shipped switched <b>off</b>, went through
      adversarial review, and was turned on deliberately. The reviews have caught real problems,
      including a privacy leak in the reply path.</div></details>

    <h3>How Cal behaves</h3>
    <details><summary>How does Cal know a message is meant for it?</summary><div class="a">
      A message qualifies if it's a <b>direct message</b> to Cal's node, <i>or</i> the text contains
      <code>cal</code> as a whole word (case-insensitive). Whole-word matching means "lo<b>cal</b>",
      "<b>cal</b>endar" and "physi<b>cal</b>" do <i>not</i> trigger it.</div></details>
    <details><summary>What has to be true before Cal replies?</summary><div class="a">
      Being named isn't enough. In order, a message must pass every gate: it's <b>not Cal's own</b> ·
      it's <b>fresh</b> · the responder is <b>enabled</b> · the sender is on the <b>allow-list</b> ·
      it's <b>addressed</b> · it's <b>within rate limits</b>. Miss one and Cal stays quiet and records
      why — open <b>trace</b> on any exchange to see the whole ladder and exactly where it stopped.</div></details>
    <details><summary>Why do some messages say "OFF-LIST"?</summary><div class="a">
      Because Cal <b>heard them perfectly well</b> and chose not to answer. Reception and reply are two
      different things: every message on the channel is received and shown here, but only senders on the
      allow-list can trigger an automatic reply. <i>Whether silence is the right behaviour is under
      active review</i> — the argument against it is that on a shared channel, staying quiet to one
      person while answering another isn't neutral, it reads as a snub.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/unknown-sender-tier.md" target="_blank" rel="noopener noreferrer">The proposal to fix it ↗</a></div></details>
    <details><summary>Why are the replies so short?</summary><div class="a">
      Airtime is <b>shared by every node in range</b>, and LoRa has very little of it. A long message
      is not just slow, it takes the channel away from everyone else — including traffic that might
      matter more than a chat reply. So Cal is held to <b>5–7 words</b>. It's etiquette enforced in
      code, and it's why the answers read like radio traffic rather than chat.</div></details>
    <details><summary>Who can Cal talk to, and can it be switched off?</summary><div class="a">
      Right now only a small allow-list of nodes can trigger a reply, though <b>anyone</b> on the mesh
      can read what Cal says — the channel is public. Three independent always-on services do the work
      (radio, cognition, this dashboard), so one can restart without dropping the others, and a single
      kill switch silences all automatic replies instantly. Note that node IDs are <b>not
      authenticated</b> and can be spoofed, so the allow-list is a courtesy control, not a security
      boundary. The real controls are the kill switch and the fact that the model can't run tools.</div></details>

    <h3>How the answers are made</h3>
    <details><summary>How does Cal choose what to say?</summary><div class="a">
      A headless Claude writes the reply under a fixed persona — <b>5–7 words, plain text, warm and
      useful, never reveal the operator's location, schedule or personal life</b> — running with
      <b>no tools</b> and with no access to any private context. The important part is what it
      <i>isn't</i> allowed to do: for anything factual, Cal never looks something up. The software
      fetches a verified fact from a known source and hands it over, and the model's only job is to put
      that fact into words. We call it <b>capability injection</b>, and it's why Cal can't invent a
      temperature — if the fetch fails, it says so instead of guessing.</div></details>
    <details><summary>Where does the weather come from?</summary><div class="a">
      The US National Weather Service, and nothing else — one allow-listed source, fetched by the
      software, never by the model. Cal reads the <b>latest observation from the nearest weather
      station</b> to a fixed reference point, and refuses to answer at all if that reading is too old.
      Cal has <b>no forecast</b>: ask about tonight, tomorrow or whether it's going to rain and it
      says so outright rather than reading you a present-tense number as though it were a prediction.
      When it feels meaningfully different from the air temperature, Cal reports the <b>heat index</b>
      (or <b>wind chill</b> in the cold) alongside it — that is the number a person actually acts on,
      and it can run well above the temperature: measured here, 95&deg;F air against a 107&deg;F heat
      index. If the source publishes that value in a unit the software does not recognise, it is
      <b>dropped rather than converted on a guess</b>, because a wrong number is worse than no number.
      Known limitation, stated plainly: the station is a real place some distance away, and its
      reading can differ from the estimate for a specific spot. What Cal reports is a real
      measurement of somewhere nearby, not a forecast for where you're standing.
      <br><br>This page used to put a number on that gap — "five degrees or more". That number is
      withdrawn rather than quietly softened, and the reason is worth saying: it was measured
      against a <b>reference point that was itself nearly four miles wrong</b>, from a station
      believed to be five miles off that is actually about one. The reference has been corrected.
      The gap is real and the caution stands, but the size of it has not been honestly measured
      yet, so no figure is quoted here until it has been.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather-point-accuracy.md" target="_blank" rel="noopener noreferrer">The write-up, including the fix ↗</a></div></details>
    <details><summary>What is an "exchange"?</summary><div class="a">
      Almost everything Cal transmits is a response to being prompted, so the page is organised that
      way: the incoming message is the head, and Cal's reply is indented beneath it. Two things don't
      fit that shape and are marked separately — <b>unprompted</b> sends (an operator message, with no
      ask above it) and messages overheard but never addressed to Cal.</div></details>
    <details><summary>What's in the decision trace?</summary><div class="a">
      Open <b>trace</b> on an exchange Cal <i>answered</i> and you get two pictures; on one it did not
      answer, the chain is skipped and only the second appears. First, <b>the chain that produced
      the reply</b>, read left to right and numbered: the question that arrived, <b>what the software
      recognised in it</b>, what it went and fetched as a result, and finally what the model wrote.
      The recognition step is worth a look, because it is the least magical part of the whole system:
      Cal decides a message is a weather question by <b>matching plain words</b> — one strong word like
      <code>temperature</code> is enough on its own, otherwise it takes two weather words, or one plus
      a question mark. No model takes part in that decision, and the trace shows you which words
      actually matched. The question is
      <b>received normally</b> — it does real work, because it is what decided which fact to look up —
      but it stops at the dashed line. <b>Only the fetched fact crosses</b> into the model, whose
      entire job is to put that fact into words. That is why Cal cannot invent a temperature. On an
      ordinary reply with no lookup behind it there is no dashed line, because the model really was
      given the message itself.
      <br><br>Below it, the <b>stages in the order they happened</b>: received, gated, sanitized,
      grounded, narrated, sent. Each carries its own detail — which checks passed and which one stopped
      it, what the sanitizer changed, which weather station the reading came from and how old it was,
      the model and how long generation took. A message that fails a check <b>stops the spine where it
      failed</b>, and a single hollow step says outright that nothing further ran. That is read off the record rather
      than assumed: a message that was gated out carries no sanitizer result, no fact, no model and no
      destination. It is the machinery, not a narration — see below.</div></details>
    <details><summary>Why doesn't the trace show Cal's "thinking"?</summary><div class="a">
      Because there isn't any to show, and inventing some would be worse than showing nothing. Reply
      generation returns plain text — there's no hidden reasoning being discarded. We could ask the
      model to narrate why it chose a reply, but that narration <b>wouldn't be a faithful account of
      the computation</b>, and publishing it as though it were would present a plausible story as
      mechanism. It would also put unbounded, model-authored text — influenced by whatever a stranger
      transmitted — onto a public page, which is what the rest of the design works to prevent.</div></details>
    <details><summary>What's the diagram in the "received" stage?</summary><div class="a">
      The <b>link</b> the message travelled: who transmitted, who received it, how many <b>hops</b> it
      took, and the signal strength on the final leg. <b>Direct</b> means Cal heard the sender's own
      radio; anything above zero means other nodes relayed it. Where the firmware reports a relay it
      gives only <b>one byte</b> of that node's id — enough to narrow the candidates, not to name one —
      so it's shown truncated and never resolved to a name. The sender's box is coloured by what Cal
      did with the message, so the diagram and the verdict can't disagree.
      <br><br>The hop count is sometimes genuinely unknown, and the caption says which kind of unknown
      it is: a message received before this feature existed, or one where the sender reported nothing
      usable. It will not claim a reason it can't support — it did exactly that until 2026-08-11, and
      the changelog says how.</div></details>
    <details><summary>Why isn't it a real map?</summary><div class="a">
      Because this page is public and the base station sits at a fixed private address — a pin would
      publish it, and a series of pins would publish movements. So the diagram shows <b>topology</b>
      (who → who, how many hops) and never a location. No coordinates are stored by this project at
      all: the bridge deliberately reads names, hops and signal from the node database and skips the
      position field, even though about half the neighbours broadcast one. Cal's own node doesn't
      advertise a position either.</div></details>
    <details><summary>What about privacy and safety?</summary><div class="a">
      The channel is public by design, and this page only ever shows public-channel traffic and Cal's
      own telemetry — never the operator's data. Incoming text is treated as hostile: anyone in radio
      range can transmit anything, so messages are sanitized before they go anywhere near the model,
      the model runs with <b>no tools and no private context loaded</b>, and the trace reports
      <i>that</i> something was redacted and how many times, never <i>what</i>. An adversarial review
      of this exact path caught a real location leak before it shipped.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/channel-trust-and-agency.md" target="_blank" rel="noopener noreferrer">The trust model ↗</a></div></details>

    <h3>The project</h3>
    <details><summary>Is the code public? Can I run my own?</summary><div class="a">
      Yes — cal-mesh is open source, and the whole thing (bridge, responder, dashboard) is on GitHub.
      It ships a <code>config.example</code>: point it at your own Meshtastic node and you can run your
      own Cal on your own mesh. It has already had its first outside contribution via fork and pull
      request.
      <br><a href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">github.com/deanssamclaw/cal-mesh ↗</a></div></details>
    <details><summary>Where's the design reasoning written down?</summary><div class="a">
      In the repo, as proposals — including the arguments that <i>lost</i>, which are usually the more
      useful half. Each one is written to be reviewed and attacked before anything gets built.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather.md" target="_blank" rel="noopener noreferrer">Giving Cal live knowledge — the framework ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-roadmap.md" target="_blank" rel="noopener noreferrer">Capability roadmap — what Cal could learn next ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather-intent-layer.md" target="_blank" rel="noopener noreferrer">Two of my own proposals, refuted with measurements ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/unknown-sender-tier.md" target="_blank" rel="noopener noreferrer">Answering strangers — "we hear you" ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/channel-trust-and-agency.md" target="_blank" rel="noopener noreferrer">Channel trust &amp; agency — how much Cal is allowed to be ↗</a></div></details>
  </div>
  <div class="card" id="changelog"><h2>Changelog</h2>
    <div class="clog">
      <div class="ci"><span class="cd">2026-08-12</span><b>The step where Cal decides what a message is about is now shown.</b> The chain jumped straight from the question to the observation, which left the most important join unexplained: <i>how</i> did a sentence become a decision to go and read a particular weather station? It is not a model, and it is not clever — Cal matches <b>plain words</b>. One strong word such as <code>temperature</code> or <code>heat index</code> is enough on its own; failing that it takes two weather words together, or one plus a question mark. The trace now shows that as its own step, including <b>which words actually matched</b> and which of those three rules fired. This is the exact place a defect hid on 2026-08-11: "whats the heat index?" matched nothing, so the weather capability never ran at all, and there was nothing on the page that could have shown why. The reason is recorded by the same call that makes the decision, so what is displayed and what happened cannot drift apart. Older exchanges predate the field and say so rather than guessing.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>A fixed reply no longer claims a model wrote it.</b> When Cal refuses a forecast question, or cannot reach the weather service, it sends a sentence written into the software and no model runs at all — but the record named one anyway, so the trace would have credited it. The model is now recorded only when it actually ran, the box is labelled <b>what Cal sent</b> rather than what the model wrote, and the two fixed cases stopped sharing one status: a deliberate refusal and a failed fetch are different events, and calling both "weather unavailable" made the design working look like something breaking.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The top of the trace was telling people their message had failed.</b> It drew the sender's own words struck through, with <b>NOT SENT</b> beside them. That was meant to say "not forwarded to the model" — but to anyone who did not already know how this works it reads as <i>your message did not send</i>, which is the opposite of the truth: it arrived perfectly, and it is the very thing that caused the lookup to happen. The picture also never showed where the fetched fact came from, so the reply appeared out of nowhere with no visible connection to the question. It is now a numbered chain read left to right — the question arrives and <b>chooses what to look up</b>, the software fetches a real observation, and only that observation crosses a marked line into the model. Nothing is struck through, because nothing was discarded. The clever part of the old drawing was the curved wires; they were sophistication in the service of a layout that misled, and a straight line that reads correctly beats a bezier that does not.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>v3 — the trace is drawn with depth.</b> Same record and the same claims as <code>old-2</code>, drawn as a mechanism that runs rather than a list that sits. The two connectors are now <b>measured</b> from real box geometry and curve so they actually land on the reply, with the cut drawn as a genuine break in one of them. Surfaces carry elevation: the panel is a recessed well, the fetched fact and the reply sit on raised planes, and the message that was never forwarded is flat and unlit — so the hierarchy is visible rather than announced. The signal stopped being two bare numbers: <b>rssi</b> and <b>snr</b> are drawn against the range a LoRa link actually lives on, which is how you can see at a glance that this message arrived strong, and why it was heard direct. And the stages now <b>assemble in the order they ran</b> when a trace is opened, once per open — a trace records something that happened in time, and drawing it as furniture was the flattest thing about it. The colour ramp under the instruments is a single hue by rule: an ordered quantity gets one hue light to dark, never a red-amber-green rainbow, which is both a rainbow ramp and a pair that sits about 1.5 units apart under the commonest form of colour blindness.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The trace shows what happened instead of listing it.</b> It was thirteen rows of grey label and value, which gave a passed check, a station reading and the words that went out over the air exactly the same weight — and buried the one thing a trace is actually about, which is the order things happened in. Two changes. <b>The reply is now drawn as what it is:</b> two things compete to become the answer, the sender's own words and a fact the software fetched, and on a capability answer the sender's words are visibly <b>cut</b> — they select which fact to look up and are never handed to the model. When there is no capability the same picture inverts honestly: the message is quoted to the model and nothing is cut. <b>Below it the stages run down a spine in the order they happen</b> — received, gated, sanitized, grounded, narrated, sent. A message that fails a check stops the spine where it failed, the rail below it goes dashed, and the stages it never reached are drawn unreached rather than left out. That is read off the record, not assumed: a gated-out decision carries no sanitizer result, no fact, no model and no destination. Two numbers now have a scale under them rather than standing alone — how old the weather reading was against the hour these stations report on, and how long generation took against the <b>7-44 s</b> the same prompt was measured spanning. A closed trace is also no longer built at all, so opening one costs the work rather than every message paying it every three seconds.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The page is light now.</b> Same information and the same layout, on a light palette instead of the dark one it launched with. Two colours had to be re-picked rather than reused: the green and amber that read clearly against a dark background land near a third of the required contrast on a white one, so they are now darker shades of the same hues. The link diagram needed a pass of its own — its colours are written into the drawing code rather than read from the page palette, so swapping the palette alone would have left dark boxes and dark labels sitting on a white card, which is exactly the kind of change that looks finished until someone opens a trace. <b>The retired version at <code>old-1</code> is deliberately still dark.</b> It is kept as a record of what the page used to be, and restyling it would make that record wrong.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Two capture bugs, and a caption that confidently explained one of them wrongly.</b> Every message received since 2026-08-09 was showing "hops unknown — this message predates routing capture". The messages did not predate anything: the hop count is <i>hop_start</i> minus <i>hop_limit</i>, and the radio library builds its packet view with a converter that omits any number equal to zero — so a message that used its <b>entire</b> hop budget arrived with <i>hop_limit</i> missing and was recorded as "no data", indistinguishable from a message that carried no routing at all. The most-relayed messages were the ones being thrown away. Worse was the caption: one asserted cause printed for a blank that has several. It now states only what the record supports, and older messages that genuinely predate the feature still say so.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Cal can tell you the heat index.</b> Asked for "current temperature and heat index", Cal answered the temperature, said nothing about the other half, and gave no sign anything had been left out — while the weather service was publishing a <b>107&deg;F</b> heat index against <b>95&deg;F</b> air in the very same reading. The software had never looked at the field. Heat index and wind chill are now included whenever they differ from the air temperature by at least 3&deg;F, and when they do they take the place of wind in the reply: at a twelve-degree gap, how hot it feels <i>is</i> the weather, and a five-to-seven word message cannot carry both. If the value ever arrives in a unit the software does not recognise it is dropped rather than converted on a guess — read as Fahrenheit instead of Celsius, that 107 becomes "42F" on a 95-degree afternoon. Checked by running it: eight replies, both numbers survived all eight times.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Reply time is no longer shown as if it were thinking time.</b> Each exchange prints how long generation took, and a reader reasonably takes that as a measure of the model. It mostly is not. Measured here: a <i>one-token</i> reply through the same locked-down command costs <b>5.4–10.5 s</b>, while a full seven-word weather reply costs <b>7–44 s</b> — the <i>same prompt</i> varying about sixfold run to run. The floor is process startup and a network round trip; the spread is noise; the part attributable to composing seven words is small. The figure now carries that context instead of standing alone. Consequence worth stating plainly: choosing a larger model would be close to invisible in these numbers, because the time is not going where it looks like it is going.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Link diagram redrawn.</b> Two things it got wrong. It drew a single "relay" box no matter how far a message had travelled, which quietly implied the whole path was known — a hop is a rebroadcast, so three hops means three relays stood in between and the firmware only ever names the last one. The relays it cannot name are now drawn dashed and counted, so the picture shows the size of what it does not know. And every sentence moved out of the drawing into the rows beneath it: a drawing has a fixed canvas and its text neither wraps nor shrinks, so the caption had been clipped at both ends and the signal reading was painting over the node it pointed at. The drawing now holds boxes and arrows only, at one fixed scale, so a message that went three hops and one that went direct are drawn the same size.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Three messages got their hop count back.</b> Once <i>hop_start</i> is known the arithmetic is forced, so records caught by the bug above were recovered rather than left blank — and they are labelled <b>recovered</b>, because reconstructed after the fact is not the same kind of fact as measured at the time. A worked example, all of it from the message the operator remembered sending from far away: he was right that it did not reach Cal directly. It spent its whole budget of <b>3 hops</b>, and the last relay's one-byte id (<code>·c6</code>) matches exactly one node — Cal's own listener across the house. The signal is the giveaway: that listener heard the sender at <b>−126 dBm</b> and barely caught it, while Cal heard the same message at <b>−50 dBm</b>, because Cal was hearing the relay, not the sender. Signal strength describes the last leg only, never the distance to whoever spoke.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Cal was dropping the sender's ID on first contact.</b> The library resolves a node's <code>!id</code> through its list of known nodes and returns nothing for a node it has not yet been introduced to — while the packet itself carries that node's number the entire time. So the ID went missing exactly when a stranger spoke to Cal for the first time. Measured here: a "Hi" on 2026-08-11 was logged from nobody; the sender's introduction arrived eleven minutes later and it was <code>!ba0cc0c0</code> all along. The bridge now falls back to the number the packet carries. Node IDs remain unauthenticated and spoofable — that has not changed and cannot.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Forecast questions are now refused, not answered.</b> Asking about tonight, tomorrow, or whether it's going to rain used to return <i>current</i> conditions — a present-tense reading dressed as a prediction. Cal now recognises a forecast-shaped question deterministically and replies "Only current conditions, no forecast yet," making no lookup at all.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>FAQ rewritten and grouped.</b> It assumed you already knew what a mesh network, Meshtastic and LoRa were, and never said why an AI on a radio is worth building. It now starts from those, explains how the project got here in stages, and links out to the source and to the design proposals — including the arguments that lost.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Link diagram in every trace.</b> Shows the path a message took — sender, any relay, Cal HT — with the hop count and the signal on the last leg. The bridge now records per-message routing (hops taken, and the one-byte relay id when the firmware supplies it); messages received before that show "hops unknown" rather than implying they were direct. It is a topology diagram, not a geographic one, on purpose — see the FAQ.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>v2 is now the main page.</b> The previous two-column layout is retired but still readable at <code>old-1</code>. Retired versions keep a permanent <code>old-N</code> address — numbered by when they were retired, never renumbered — so a link to one always shows the same page.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>v2.</b> Inbound and Outbound merged into a single <b>Exchanges</b> stream — the ask is the head, Cal's reply is indented beneath it. Removes the duplication that made v1 busy (every reply used to render twice) and reads properly on a phone.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Decision trace.</b> Every exchange opens into the machinery behind it: the gate ladder, what the sanitizer changed, the capability and the exact injected fact, the model and generation time. Deliberately no model "reasoning" — see the FAQ.</div>
      <div class="ci"><span class="cd">2026-08-09</span>Inbound/Outbound paired, reply latency in seconds, and the battery tile made sentinel-aware (a reading over 100 means external power, not a charge).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Cal HT moved to <b>WiFi</b>: reflashed to the BaseUI firmware and switched the bridge to TCP — the radio runs untethered, USB is just power.</div>
      <div class="ci"><span class="cd">2026-08-08</span>From Bob's PR: message latency tracking and an /api/stats endpoint with daily aggregates.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Second security &amp; privacy audit: device MAC removed from the public API, DoS bounds, log rotation.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Published as a public GitHub repo; per-neighbor 1-hour SNR sparklines (idea from Bob).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 2 — autonomous responder: Cal replies on its own when addressed (fleet-only, kill switch, rate limits).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 1 — always-on bridge; node flashed to Meshtastic 2.7.26 and brought online as "Cal HT".</div>
    </div>
  </div>
</main>
<footer>cal-mesh dashboard v3 · auto-refresh 3s · read-only · previous version: <a class="faqlink" id="oldlink2" href="old-2">old-2</a></footer>
<script>
const $=s=>document.querySelector(s);
const DIR=(function(){let p=location.pathname.replace(/\/(v2|v3|old-\d+)\/?$/,'/');
 return p.endsWith('/')?p:p+'/';})();
let SNR={}, lastNodes=[], nodeSort={key:null,dir:1}, lastXsig=null;
let SELF={id:null,name:null};
const NODE_LABELS={short:'Short',long:'Name',hw:'HW',hops:'Hops',snr:'SNR'};
function esc(s){return (s??"").toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hhmmss(ts){try{return new Date(ts).toLocaleTimeString();}catch(e){return ts;}}
function daystamp(ts){try{return new Date(ts).toLocaleDateString(undefined,{month:'short',day:'numeric'});}catch(e){return '';}}
function secs(ms){return (ms/1000).toFixed(2)+'s';}
function tile(k,v,sub){return `<div class="tile"><div class="k">${k}</div><div class="v">${v}${sub?` <small>${sub}</small>`:''}</div></div>`;}
function fmtDur(s){s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
 return h?`${h}h ${m}m`:(m?`${m}m ${ss}s`:`${ss}s`);}
function batteryLabel(m){ if(m.battery==null) return '—';
  if(m.battery>100) return 'ext power';   // Meshtastic sentinel, not a charge level
  return m.battery+'%'; }
function skipWhy(r){
  const m={sender_not_allowed:'sender is not on the allow-list — Cal heard it perfectly well and chose not to answer',
           not_addressed:'Cal was not addressed (no "cal" mention, not a DM)',
           disabled:'the responder kill switch is off',
           too_old:'the message was older than the freshness window',
           rate_limited:'rate limit reached for this sender',
           cooldown:'per-sender cooldown still active',
           self:'this was Cal\'s own message'};
  return m[r]||esc(r||'unknown');
}
function verdictTag(x){
  if(x.verdict==='replied') return '<span class="tag tx">REPLIED</span>';
  if(x.verdict!=='skipped') return '<span class="tag quiet">NOT EVALUATED</span>';
  return x.reason==='sender_not_allowed'
    ? '<span class="tag offlist">OFF-LIST · heard, not answered</span>'
    : `<span class="tag quiet">NO REPLY · ${esc(x.reason)}</span>`;
}
function row(k,v){return `<div class="trow"><span class="tk">${k}</span><span class="tv">${v}</span></div>`;}
function nodeName(id){
  const n=lastNodes.find(n=>n.id===id);
  return n?(n.short||n.long||id):id;
}
// Clamp a label to what a fixed-width box can actually hold. SVG text does not wrap and does
// not shrink: an over-long node name silently paints across its own border and its neighbour.
function fitLabel(s, n){ s=(s==null?'':String(s)); return s.length>n ? s.slice(0,n-1)+'…' : s; }

// A LINK diagram, deliberately not a geographic one: who transmitted, who relayed it, who
// received it. It uses only what is already public on the air — there are no coordinates here
// and none are stored, because this page is public and the base station sits at a fixed
// private location.
//
// TWO LAYOUT RULES, both learned by shipping the violation (2026-08-11):
//
//   1. NO PROSE INSIDE THE SVG. A viewBox is a fixed canvas and <text> neither wraps nor
//      reflows, so a caption long enough to be worth reading gets clipped at BOTH ends — which
//      is exactly what happened when the caption grew to explain the relay byte. Every sentence
//      now lives in HTML underneath, in the same key/value rows the rest of the trace uses, so
//      it wraps and can never be truncated. The SVG holds boxes and arrows and nothing else.
//   2. NOTHING FLOATS BETWEEN THE BOXES. The old signal label sat at the arrow midpoint, and
//      once a third box appeared the gaps were narrower than the label — it painted over the
//      node it was pointing at. Signal is a fact about the last hop, so it is stated as such
//      in the rows below rather than squeezed into the gap.
function linkSvg(x){
  const hops=x.hops;
  const relayId = x.relay_byte!=null ? '·'+x.relay_byte.toString(16).padStart(2,'0') : null;
  // Colour the sender box by what Cal DID with it, so the diagram carries the same signal as
  // the verdict badge above. Green on an off-list sender read as "allowed" — backwards.
  const offlist = x.verdict==='skipped' && x.reason==='sender_not_allowed';
  const quiet   = x.verdict==='skipped' && !offlist;
  const senderC = offlist ? {fill:'#fff8c5', stroke:'#d4a72c'}      // matches the OFF-LIST tag
                : quiet   ? {fill:'#f2f4f7', stroke:'#8c96a3'}
                          : {fill:'#f2f4f7', stroke:'#1a7f37'};
  const stops=[x.from
    ? {lab:esc(fitLabel(nodeName(x.from),15)), sub:esc(fitLabel(x.from,15)), fill:senderC.fill, stroke:senderC.stroke}
    : {lab:'unknown', sub:'no id recorded', fill:senderC.fill, stroke:senderC.stroke}];
  // A hop is a REBROADCAST, so N hops means N relays stood between the sender and Cal — and the
  // firmware only ever tells us the last one. Drawing a single relay box for N>1 quietly implied
  // we knew the whole path. The ones we cannot name are now counted and drawn dashed, so the
  // diagram shows the size of what it does not know instead of hiding it.
  if(hops==null) stops.push({lab:'?', sub:'routing not recorded', dim:true, dash:true});
  if(hops>1) stops.push({lab:'?', sub:(hops-1)+' unknown relay'+(hops-1>1?'s':''), dim:true, dash:true});
  if(hops>0) stops.push({lab:'relay'+(relayId?' '+relayId:''), sub:relayId?'last relay':'id not reported', dim:true});
  stops.push({lab:esc(fitLabel(SELF.name||'Cal HT',15)), sub:esc(fitLabel(SELF.id||'',15)), self:true});

  // The canvas is a CONSTANT width sized for the widest case (4 boxes) and the row is centred
  // inside it. Sizing the viewBox to the content instead makes the SVG scale up to the CSS
  // width, so a two-box diagram renders with boxes nearly twice the size of a four-box one —
  // the same message looks like a different kind of object depending on how far it travelled.
  const n=stops.length, bw=140, gap=38, by=10, bh=50, MAXN=4;
  const W=20+MAXN*bw+(MAXN-1)*gap, H=by+bh+10;
  const x0=(W-(n*bw+(n-1)*gap))/2;
  let svg='';
  stops.forEach((s,i)=>{
    const bx=x0+i*(bw+gap);
    if(i>0){
      const x1=bx-gap+2, x2=bx-4;
      svg+=`<line x1="${x1}" y1="${by+bh/2}" x2="${x2-6}" y2="${by+bh/2}" stroke="#8c96a3" stroke-width="2"/>`
         +`<path d="M${x2-7} ${by+bh/2-4.5} L${x2} ${by+bh/2} L${x2-7} ${by+bh/2+4.5}z" fill="#8c96a3"/>`;
    }
    const fill=s.fill||(s.self?'#f7f4fd':'#f2f4f7');
    const stroke=s.stroke||(s.self?'#6639ba':(s.dim?'#8c96a3':'#1a7f37'));
    svg+=`<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="8" fill="${fill}" stroke="${stroke}" `
       +`stroke-width="1.5"${s.dash?' stroke-dasharray="5 4"':''}/>`
       +`<text x="${bx+bw/2}" y="${by+21}" fill="#1a1f26" font-size="13" font-weight="600" text-anchor="middle">${s.lab}</text>`
       +`<text x="${bx+bw/2}" y="${by+37}" fill="#5c6672" font-size="10.5" text-anchor="middle">${s.sub}</text>`;
  });
  const diagram=`<div class="link-d"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" `
       + `role="img" aria-label="link diagram">${svg}</svg></div>`;

  // A null hop count has more than one cause and they are not interchangeable. Records written
  // before routing capture shipped (2026-08-09) carry no hop_start KEY AT ALL; records written
  // after always carry the key, even when its value is null. Saying "predates routing capture"
  // for both put a false claim about a message's history on a public page.
  let rows='';
  if(hops==null)
    rows+=row('path', x.hop_start===undefined
      ? 'unknown — this message predates routing capture'
      : 'unknown — no usable hop count was recorded for this message');
  else if(hops===0)
    rows+=row('path','direct — Cal heard the sending radio itself, with no relay in between');
  else{
    rows+=row('path', hops+' hop'+(hops>1?'s':'')+' — relayed'
      +(hops>1?', and only the last relay is identified':''));
    if(relayId)
      rows+=row('last relay','id ends <code>'+esc(relayId)+'</code> — one byte of the node number that '
        +'relayed it, which narrows the candidates but does not identify a node');
  }
  // The signal belongs to the LAST hop and nothing else. Stating that plainly matters: a message
  // relayed from close by arrives strong no matter how far the sender is, and reading it as
  // "nearby" is the natural mistake.
  // Two numbers told a stranger nothing. Drawn against the range a LoRa link actually lives on,
  // -41 dBm is visibly near the strong end — which is the finding, and why this arrived direct.
  if(x.snr!=null||x.rssi!=null){
    let g='';
    if(x.rssi!=null) g+=gauge('signal · rssi',esc(x.rssi)+' dBm',(x.rssi+120)/90*100,
      ['-120 weak','-30 strong']);
    if(x.snr!=null) g+=gauge('signal · snr',(x.snr>0?'+':'')+esc(x.snr)+' dB',(x.snr+20)/30*100,
      ['-20 dB','+10 dB']);
    rows+=`<div class="inst">${g}</div>`
      +`<span class="hint">Measured on the <b>last hop only</b>`
      +(hops>0?' — which came from the relay, not the sender, however far away the sender was.'
              :', and this one was direct, so it does describe the sender.')+`</span>`;}
  // A recovered count was reconstructed after the fact, not measured at capture. It is sound —
  // the arithmetic is forced once hop_start is known — but it is not the same kind of fact, and
  // the page should not blur the two.
  if(x.hops_recovered)
    rows+=row('note','hop count recovered from a record predating the capture fix — reconstructed, not measured at the time');
  return {diagram:diagram, rows:rows, summary:(
    hops==null?'routing not recorded'
    :hops===0?'heard direct, no relay in between'
    :hops+' hop'+(hops>1?'s':'')+' — arrived by relay')};
}
// The reply is composed from a fact the harness fetched, and on a capability answer the
// sender's own words are never handed to the model at all. That is the single least obvious
// thing about this system and it was previously one clause inside a grey row. Drawn instead:
// two inputs compete to become the reply, and one of them is visibly cut.
function flowHtml(x,t){
  const inTxt=esc(x.text||''), outTxt=esc(x.reply||'');
  if(!outTxt) return '';
  // What the software MATCHED, what it actually GOT, and whether a model ran are three
  // different things. Collapsing them is what made a refused forecast claim the model was
  // handed the message.
  const capability=!!(x.capability||(t.trigger_match&&t.trigger_match.via));
  const fetched=!!t.injected_fact;
  const modelRan=!!t.model;
  const crosses=fetched&&modelRan;
  // A DM lands on one screen, not every node in range, so the 5-7 word rule does not apply to it.
  const isDM=!!(t.dest&&t.dest.charAt(0)==='!');
  const arrow=(label)=>`<span class="arw"><span>${label}</span></span>`;
  const b1=`<div class="fb b1"><div class="fk">1 · the question</div>`
    +`<div class="fv">${inTxt}</div>`
    +`<div class="fn"><span class="onair">✓ received on air</span> — and it is what decided `
    +`${capability?'which fact to look up':'how to reply'}</div></div>`;
  const lastN=capability?4:2;
  const b3=`<div class="fb b3"><div class="fk">${lastN} · ${modelRan?'what the model wrote':'what Cal sent'}</div>`
    +`<div class="fv">${outTxt}</div>`
    +`<div class="fn"><span class="onair">✓ sent on air to ${esc(t.dest||'')}</span> — `
    +(modelRan?(isDM?'a sentence or two — a direct message lands on one screen, not every node in range'
                    :'5-7 words, because every node in range shares the airtime')
             :'a fixed sentence written into the software — no model ran for this one')+`</div></div>`;
  // A greeting ack is a THIRD shape, and both of the branches below would misdescribe it.
  // The capability branch is weather-shaped ("what Cal looked up"); the general branch says
  // the model was handed the message. Here nothing was fetched AND no model ran: plain word
  // matching selected a sentence written in advance. Drawn as exactly that.
  if(x.capability==='greeting'){
    const g1=`<div class="fb b1"><div class="fk">1 · what they said</div><div class="fv">${inTxt}</div>`
      +`<div class="fn"><span class="onair">✓ received on air</span> — from a node that is `
      +`<b>not on Cal's reply list</b>, so no answer was generated for it</div></div>`;
    const g2=`<div class="fb bx"><div class="fk">2 · what the software recognised</div>`
      +`<div class="fv">a greeting, and nothing else</div>`
      +`<div class="fn">the whole message had to be a greeting — a question mark or a real `
      +`request and this does not fire — plain word matching, <b>no model involved</b></div></div>`;
    const g3=`<div class="fb b3"><div class="fk">3 · what Cal sent</div><div class="fv">${outTxt}</div>`
      +`<div class="fn"><span class="onair">✓ sent on air to ${esc(t.dest||'^all')}</span> — the `
      +`greeting mirrored back, and only once per node per day</div></div>`;
    return `<div class="flow gen">${g1}${arrow('')}${g2}${arrow('')}${g3}</div>`
      +`<div class="flowcap">Read left to right. <b>Nothing was looked up and no model ran.</b> `
      +`Cal answers questions only from known nodes, but staying silent when a stranger says `
      +`hello reads as a snub — so a greeting gets one back, to say it was heard. Which line `
      +`goes out is <b>chosen</b> by the greeting they used, from five written in advance `
      +`(morning, afternoon, evening, day, or plain hello). Nothing they wrote is ever copied `
      +`into it, so there is nothing in the reply for a stranger to steer.</div>`;
  }
  // A computed answer is a FOURTH shape. The capability branch below is weather-shaped and
  // would say "what Cal looked up" and, with no injected_fact, "the lookup failed — the weather
  // service could not be reached" — for a reply that never touched the network. Nothing is
  // fetched here and no model runs: Python parsed the question and computed every digit.
  if(x.capability==='sunmoon'){
    const sm=t.sunmoon||{}, smm=t.sunmoon_match||{};
    const intent=sm.intent?String(sm.intent):'', ev=sm.event?String(sm.event):'';
    const refused=sm.refused?String(sm.refused):'';
    const words=[].concat(smm.sun||[],smm.moon||[]).join(', ');
    const s1=`<div class="fb b1"><div class="fk">1 &middot; the question</div><div class="fv">${inTxt}</div>`
      +`<div class="fn"><span class="onair">&#10003; received on air</span> — the wording `
      +`${words?('matched <b>'+esc(words)+'</b>, which'):'matched sun/moon wording, which'} `
      +`selected this path</div></div>`;
    const s2=`<div class="fb bx"><div class="fk">2 &middot; what the software recognised</div>`
      +`<div class="fv">${esc(intent||'a sun/moon question')}</div>`
      +`<div class="fn">the wording is classified only to <b>choose which fact to compute</b>, `
      +`never to shape the sentence</div></div>`;
    const s3=`<div class="fb bx"><div class="fk">3 &middot; what Cal computed</div>`
      +`<div class="fv">${refused?esc('refused: '+refused):esc(ev||'closed-form astronomy')}</div>`
      +`<div class="fn"><b>nothing was fetched and no model ran.</b> Sun and moon positions are `
      +`computed from closed-form astronomy — no network, so this answer works when the base is `
      +`offline. Measured against 43 U.S. Naval Observatory times: worst error 43 seconds. `
      +`No coordinate appears in the reply; the observing point is an input, never an output.`
      +`</div></div>`;
    const s4=`<div class="fb b3"><div class="fk">4 &middot; what Cal sent</div><div class="fv">${outTxt}</div>`
      +`<div class="fn"><span class="onair">&#10003; sent on air to ${esc(t.dest||'')}</span></div></div>`;
    return `<div class="flow gen">${s1}${arrow('')}${s2}${arrow('')}${s3}${arrow('')}${s4}</div>`
      +`<div class="flowcap">Read left to right. <b>Nothing was looked up and no model ran.</b> `
      +`Python computes the time and formats the sentence. Where the event does not occur at all `
      +`— a polar day, or a twilight the sun never reaches — Cal says which one is missing rather `
      +`than reporting the nearest thing it could calculate. Moonrise and moonset are not built `
      +`yet, and are refused rather than estimated.</div>`;
  }
  if(x.capability==='calc'){
    const handler=(t.calc&&t.calc.handler)?String(t.calc.handler):'';
    const c1=`<div class="fb b1"><div class="fk">1 · the question</div><div class="fv">${inTxt}</div>`
      +`<div class="fn"><span class="onair">✓ received on air</span> — it parsed as a `
      +`calculation, which is what selected this path</div></div>`;
    const c2=`<div class="fb bx"><div class="fk">2 · what the software recognised</div>`
      +`<div class="fv">${esc(handler||'a calculation')}</div>`
      +`<div class="fn">a <b>successful bounded parse</b>, not merely a number in the text — `
      +`anything that does not parse gets no answer at all</div></div>`;
    const c3=`<div class="fb bx"><div class="fk">3 · what Cal computed</div>`
      +`<div class="fv">Python, from exact constants</div>`
      +`<div class="fn"><b>nothing was fetched and no model ran</b> — the digits are computed `
      +`and formatted by the software itself</div></div>`;
    const c4=`<div class="fb b3"><div class="fk">4 · what Cal sent</div><div class="fv">${outTxt}</div>`
      +`<div class="fn"><span class="onair">✓ sent on air to ${esc(t.dest||'')}</span></div></div>`;
    return `<div class="flow gen">${c1}${arrow('')}${c2}${arrow('')}${c3}${arrow('')}${c4}</div>`
      +`<div class="flowcap">Read left to right. <b>Nothing was looked up and no model ran.</b> `
      +`The model is not in the number path at all — Python parses the question, computes the `
      +`answer from exact defined constants, and formats the sentence. Where a value is `
      +`ambiguous (a gallon is not the same on both sides of the Atlantic) or falls outside `
      +`what can be answered exactly, Cal says nothing rather than guess.</div>`;
  }
  if(!capability){
    // An authenticated DM from Dean is the same "no lookup, model ran" shape, but the model did
    // NOT see only the message — the harness also injected Cal's saved context and, when present,
    // the remembered thread. Saying "the message itself" here would be the same collapse that
    // once made a refused forecast claim the model was handed the message.
    if(t.dm_unlock){
      const mem=t.dm_memory_stored?' and the recent messages it remembers':'';
      return `<div class="flow gen">${b1}${arrow('given to the model with<br>Cal\'s saved context')}${b3}</div>`
        +`<div class="flowcap">This is an <b>authenticated direct message from Dean</b>, so the model was `
        +`given the message <b>plus Cal&rsquo;s saved context${mem}</b> — which is why the reply can be `
        +`longer and carry a thread. The context is the operator&rsquo;s public file; no secret crosses.</div>`;
    }
    return `<div class="flow gen">${b1}${arrow('sanitized, then given<br>to the model')}${b3}</div>`
      +`<div class="flowcap">Nothing was looked up for this one, so the model was given `
      +`<b>the message itself</b> and wrote a reply from it.</div>`;
  }
  // The step between the question and the lookup: plain word-matching that decides WHICH
  // capability runs. No model is involved, and it is where a 2026-08-11 defect hid — a question
  // that matched nothing never reached the capability at all, with nothing on the page to say so.
  const tm=t.trigger_match||null;
  let why='this record predates Cal keeping the matched words, so they cannot be shown', chips='';
  if(tm){
    const words=(tm.strong&&tm.strong.length?tm.strong:tm.weak)||[];
    chips=words.map(w=>`<span class="chip">${esc(w)}</span>`).join('');
    why = tm.via==='strong'
        ? (words.length>1?'any one of these is enough on its own'
                         :'this word is enough on its own')
        : tm.via==='two_weak'
          ? 'two weather words together'
          : 'one weather word plus a question mark';
  }
  const bx=`<div class="fb bx"><div class="fk">2 · what the software recognised</div>`
    +`<div class="fv">a weather question${t.forecast_asked?' about the <b>future</b>':''}</div>`
    +`<div class="fn">${chips}${chips?'<br>':''}${why} — plain word matching, `
    +`<b>no model involved</b></div></div>`;
  const st=t.obs_station?esc(t.obs_station):null;
  const age=t.obs_age_s!=null?Math.round(t.obs_age_s/60)+' min old':null;
  const warn='border-color:#e6c98a;background:linear-gradient(180deg,#fffdf5,#fdf6e3)';
  const b2=t.forecast_asked
    ? `<div class="fb b2" style="${warn}">`
      +`<div class="fk">3 · what Cal looked up</div><div class="fv">nothing</div>`
      +`<div class="fn">Cal holds current observations only, so a question about later is refused `
      +`outright — no lookup was attempted at all</div></div>`
    : !fetched
      ? `<div class="fb b2" style="${warn}">`
        +`<div class="fk">3 · what Cal looked up</div><div class="fv">the lookup failed</div>`
        +`<div class="fn">the weather service could not be reached, so Cal sent a fixed sentence `
        +`rather than guess a number — the fail-safe working, not a model deciding</div></div>`
      : `<div class="fb b2"><div class="fk">3 · what Cal's software fetched</div>`
        +`<div class="fv">${esc(t.injected_fact)}</div>`
        +`<div class="fn">a real observation${st?' from station '+st:''}${age?', '+age:''} — fetched `
        +`from the US National Weather Service by the software, never by the model</div></div>`;
  const join=crosses
    ? `<span class="cross"><span class="bl">only this crosses</span>${arrow('')}</span>`
    : arrow('');
  const cap=crosses
    ? `Read left to right. The question arrived fine and did real work — <b>its wording is what chose `
      +`the lookup</b> — but it never reached the model. Cal&rsquo;s software matched the words, went and `
      +`got the observation, and <b>only that observation</b> crossed the dashed line. The model&rsquo;s `
      +`entire job was to put it into words, which is why it cannot invent a temperature.`
    : `Read left to right. The question arrived fine and did real work — <b>its wording is what Cal `
      +`matched on</b> — but nothing was looked up, so <b>no model ran at all</b>. What went out is a `
      +`fixed sentence written into the software. There is no boundary drawn here because nothing `
      +`crossed one.`;
  return `<div class="flow">${b1}${arrow('reads')}${bx}${arrow(crosses?'so it<br>fetches':'so it<br>stops')}`
    +`${b2}${join}${b3}</div><div class="flowcap">${cap}</div>`;
}
function gauge(name,val,pct,ends,band,note){
  return `<div class="gauge"><div class="glab"><span class="gname">${name}</span>`
    +`<span class="gval">${val}</span></div>`
    +`<div class="track${band?'':' ramp'}">`
    +(band?`<span class="band" style="left:${band[0].toFixed(1)}%;width:${band[1].toFixed(1)}%"></span>`:'')
    +(Number.isFinite(pct)
       ? `<span class="mk" style="left:${Math.max(0,Math.min(100,pct)).toFixed(1)}%"></span>`
       : '')+`</div>`
    +`<div class="gends"><span>${ends[0]}</span><span>${ends[1]}</span></div>`
    +(note?`<div class="gnote">${note}</div>`:'')+`</div>`;
}
function stage(cls,name,summary,detail){
  return `<li class="stg ${cls}"><span class="sdot"></span>`
    +`<div class="shead"><span class="sname">${name}</span><span class="ssum">${summary}</span></div>`
    +`<div class="sdet">${detail||''}</div></li>`;
}
// The stages are a sequence in time and a gated-out message genuinely never reaches the later
// ones — verified against the records: a skipped decision carries no sanitize, no fact, no
// model and no destination. So "never reached" is read off the record, not assumed.
function spineHtml(x,t){
  const link=(x.kind==='exchange')?linkSvg(x):null;
  let s='';
  if(link) s+=stage('pass','received',link.summary,link.diagram+link.rows);
  const gated=t.gates&&t.gates.length;
  const stopped=x.verdict==='skipped';
  if(gated){
    const passed=t.gates.filter(g=>g.pass).length;
    s+=stage(stopped?'stop':'pass','gated',
      stopped?`stopped at <b>${esc((t.gates.find(g=>!g.pass)||{}).gate||'a check')}</b>`
             :`all ${passed} checks passed`,
      t.gates.map(g=>`<span class="gate ${g.pass?'gp':'gf'}">${g.pass?'✓':'✗'} ${esc(g.gate)}</span>`).join('')
      +(stopped?'<span class="rungn">later checks never evaluated</span>':''));
  }
  if(!t.model&&stopped){
    s+=stage('skip','not answered','the message was received and recorded, and nothing further ran',
      '<span class="rungn">no text was sent to a model, and nothing went on air</span>');
    return `<ol class="spine">${s}</ol>`;
  }
  if(t.sanitize){const q=t.sanitize,b=[];
    // An older record carries only the boolean and genuinely cannot say WHICH was trimmed. Say
    // that, rather than guessing — and never guess toward "your words were dropped".
    const tk=q.sentence_trim!=null?q.sentence_trim:(q.sentence_trimmed?'unknown':'none');
    if(tk==='content') b.push(`first sentence kept (${q.dropped_chars!=null?q.dropped_chars+' chars':'the rest'} dropped)`);
    else if(tk==='unknown') b.push('something was trimmed from the end — this record predates the '
      +'detail that says whether it was punctuation or content');
    else if(tk==='punctuation') b.push('trailing punctuation trimmed, no content dropped');
    if(q.length_capped) b.push('length capped');
    if(q.redactions) b.push(`${q.redactions} redaction${q.redactions>1?'s':''}`);
    if(q.flagged) b.push('injection-shaped tokens flagged');
    s+=stage('pass','sanitized',`${q.in_chars}&rarr;${q.out_chars} characters`,
      b.length?`<span class="hint">${esc(b.join(' · '))}</span>`:'<span class="hint">nothing removed</span>');}
  if(t.forecast_asked)
    s+=stage('stop','refused','asked about a future condition',
      '<span class="hint">the capability holds current observations only, so a fixed reply was sent '
      +'and no lookup was made at all</span>');
  if(x.capability){
    const ok=t.weather_ok, age=t.obs_age_s;
    let d='';
    if(age!=null){
      d=gauge('reading age',Math.round(age/60)+' min',(age/3600)*100,
        ['just measured','1 h — how often these stations report'],[0,0.001],
        'A real observation from the nearest station, never an estimate for one spot.');}
    const fstate = ok===true?'ok' : (ok===false?'FAILED':'not attempted');
    s+=stage(ok===true?'pass':(ok===false?'stop':'skip'),'grounded',
      `${esc(x.capability)} · fetch ${fstate}`
      +(t.obs_station?` · station <code>${esc(t.obs_station)}</code>`:''), d);}
  if(t.model){
    const ms=x.gen_ms;
    let d='<span class="hint">generation returns plain text — no chain of thought exists to show</span>';
    if(ms!=null){const MAXS=45,sec=ms/1000;
      const weather=t.prompt_kind==='weather';
      d=gauge('generation',secs(ms),sec/MAXS*100,['0 s',MAXS+' s'],
        weather?[7/MAXS*100,(44-7)/MAXS*100]:[0,0.001],
        (weather?'The shaded band is the <b>7-44 s</b> this same prompt was measured spanning, run '
                +'to run. ':'')
        +'Most of it is process startup and a network round trip — an order of magnitude, not '
        +'thinking time.');}
    s+=stage('pass','narrated',`<code>${esc(t.model)}</code>`,d);}
  if(t.gen_status&&t.gen_status!=='ok')
    s+=stage('stop','generation',`<code>${esc(t.gen_status)}</code>`,'');
  if(t.dest) s+=stage('pass','sent',`on air to <code>${esc(t.dest)}</code>`,'');
  return `<ol class="spine">${s}</ol>`;
}
// The trace reads top to bottom as what happened: first the outcome and how it was arrived at
// (the swap), then the machinery stage by stage (the spine). The old flat key/value list gave a
// gate check, a station reading and the transmitted reply the same weight and the same grey
// label, which left the sequence — the only thing the trace is actually about — invisible.
function traceHtml(x){
  const t=x.trace||{};
  if(!t.gates&&!t.sanitize&&!t.model){
    const l=(x.kind==='exchange')?linkSvg(x):null;
    return `<div class="tp">${l?l.diagram+l.rows:''}`+
      '<div class="tnone">No decision trace recorded — this message predates it.</div></div>';}
  let h=flowHtml(x,t)+spineHtml(x,t);
  h+='<div class="tnote">This is the machinery, not the model\'s reasoning. Generation returns plain '
   +'text with no chain of thought, and asking for a narration would produce a plausible story rather '
   +'than an account of what actually happened — so it is not shown.</div>';
  return `<div class="tp">${h}</div>`;
}
// The page re-renders every 3s, which would wipe any <details> the reader had opened. Track
// open traces by a stable key and restore the attribute on every render, so an expanded trace
// stays expanded until it is clicked shut. (Toggle doesn't bubble — the listener captures.)
const OPEN=new Set();
// Lets a trace be built on demand when its disclosure is opened, rather than for every
// exchange on every pass. Rebuilt from the current data each render, so an open trace never
// shows a stale copy of a record that has since changed.
const XBYKEY=new Map();
function xkey(x){return (x.ts||'')+'|'+(x.from||x.dest||'');}
function exchangeHtml(x){
  if(x.kind==='unprompted') return `
    <div class="xc unprompted"><div class="meta"><span class="tag tx">TX</span>
      <span>${daystamp(x.ts)} ${hhmmss(x.ts)}</span><span>→ ${esc(x.dest)}</span>
      <span class="tag ch">ch${esc(x.channel)}</span><span>${esc(x.transport)}</span>
      <span class="tag quiet">${x.source==='responder'?'UNPAIRED':'MANUAL'}</span></div>
    <div class="ask">${esc(x.text)}</div>
    <div class="norep">↳ not a reply — Cal transmitted this with no inbound ask${x.source==='responder'?', or the ask is older than the window shown':''}</div></div>`;
  return `
    <div class="xc"><div class="meta"><span class="tag rx">RX</span>
      <span>${daystamp(x.ts)} ${hhmmss(x.ts)}</span><span>${x.from?esc(x.from):'unknown sender'} → ${esc(x.to)}</span>
      <span class="tag ch">ch${esc(x.channel)}</span>${x.snr!=null?`<span>snr ${esc(x.snr)}</span>`:''}
      ${verdictTag(x)}</div>
    <div class="ask">${esc(x.text)}</div>
    ${x.verdict==='replied'&&x.reply
      ? `<div class="rep"><span class="who">↳ Cal replied${x.gen_ms!=null?` · ${secs(x.gen_ms)}`:''}${x.capability?` · ${esc(x.capability)}`:''}</span><span class="txt">${esc(x.reply)}</span></div>`
      : (x.verdict==='skipped'?`<div class="norep">↳ received, no reply — ${skipWhy(x.reason)}</div>`:'')}
    <details class="tr" data-k="${esc(xkey(x))}"${OPEN.has(xkey(x))?' open':''}><summary>trace</summary>
    <div class="tpwrap">${OPEN.has(xkey(x))?traceHtml(x):''}</div></details></div>`;
}
function setSort(k){ nodeSort=(nodeSort.key===k)?{key:k,dir:-nodeSort.dir}:{key:k,dir:1}; renderNodes(); }
function renderNodes(){
  let ns=lastNodes.slice();
  if(nodeSort.key){ const k=nodeSort.key, dir=nodeSort.dir;
    ns.sort((a,b)=>{ let x=a[k],y=b[k];
      if(k==='hops'||k==='snr'){ if(x==null&&y==null)return 0; if(x==null)return 1; if(y==null)return -1; return (x-y)*dir; }
      x=(x||'').toString().toLowerCase(); y=(y||'').toString().toLowerCase();
      return x<y?-dir:(x>y?dir:0); }); }
  const tb=$('#nodes').querySelector('tbody');
  tb.innerHTML=ns.map(n=>{ const sg=(n.snr!=null&&n.snr>0)?'snr-good':'snr-bad';
    return `<tr><td>${esc(n.short)}</td><td>${esc(n.long)}</td><td>${esc(n.hw)}</td>`+
      `<td>${n.hops==null?'—':esc(n.hops)}</td><td class="${sg}">${n.snr==null?'—':esc(n.snr)}</td>`+
      `<td>${sparkline((SNR[n.id]||{}).points, n.hops)}</td></tr>`; }).join('');
  document.querySelectorAll('#nodes th.sortable').forEach(th=>{
    const k=th.dataset.key, on=nodeSort.key===k;
    th.textContent=NODE_LABELS[k]+(on?(nodeSort.dir>0?' ▲':' ▼'):''); });
}
async function loadSnr(){try{SNR=await (await fetch(DIR+'api/snr',{cache:'no-store'})).json();}catch(e){}}
function sparkline(pts, hops){
  if(!pts||pts.length===0){
    return (hops!=null&&hops>0)?'<span style="color:var(--dim)">multi-hop</span>'
      :'<span style="color:var(--dim)">— <small>no direct signal</small></span>';}
  if(pts.length===1){const v=pts[0][1];
    return `<span class="spark"><svg width="90" height="22"><circle cx="45" cy="11" r="2.5" fill="var(--accent)"/></svg>`+
      `<span style="color:var(--accent)">${esc(v)} <small>dB · 1 pt</small></span></span>`;}
  const W=90,H=22,pad=3;
  const ts=pts.map(p=>p[0]), vs=pts.map(p=>p[1]);
  const t0=Math.min(...ts),t1=Math.max(...ts),vmin=Math.min(...vs),vmax=Math.max(...vs);
  const sx=t=>pad+(t1===t0?(W-2*pad):((t-t0)/(t1-t0))*(W-2*pad));
  const sy=v=>pad+(1-(vmax===vmin?0.5:(v-vmin)/(vmax-vmin)))*(H-2*pad);
  const d=pts.map((p,i)=>(i?'L':'M')+sx(p[0]).toFixed(1)+' '+sy(p[1]).toFixed(1)).join(' ');
  const last=pts[pts.length-1];
  const k=Math.max(1,Math.floor(pts.length/3));
  const avg=a=>a.reduce((s,x)=>s+x,0)/a.length;
  const dv=avg(vs.slice(-k))-avg(vs.slice(0,k));
  const arrow=dv>1.5?'↗':(dv<-1.5?'↘':'→');
  const col=dv<-1.5?'var(--warn)':(dv>1.5?'var(--ok)':'var(--accent)');
  return `<span class="spark" title="${pts.length} samples · now ${esc(last[1])} dB">`+
    `<svg width="${W}" height="${H}"><path d="${d}" fill="none" stroke="${col}" stroke-width="2" `+
    `stroke-linejoin="round" stroke-linecap="round"/><circle cx="${sx(last[0]).toFixed(1)}" `+
    `cy="${sy(last[1]).toFixed(1)}" r="2.5" fill="${col}"/></svg>`+
    `<span style="color:${col}">${arrow} ${esc(last[1])}</span></span>`;
}
async function tick(){
 let d; try{d=await (await fetch(DIR+'api/state',{cache:'no-store'})).json();}
 catch(e){$('#conn').className='pill bad';$('#conn').textContent='dashboard offline';return;}
 const st=d.status||{}, m=st.metrics||{}, node=st.node||{}, rp=d.responder||{};
 const on=st.connected;
 $('#conn').className='pill '+(on?'ok':'bad');
 $('#conn').textContent=on?'● radio connected':'● radio down';
 $('#sub').textContent=`${node.longName||'?'} (${node.shortName||'?'}) · ${node.id||''} · fw ${st.firmware||'?'}`;
 const live=rp.enabled==='true';
 $('#tiles').innerHTML=[
   tile('Battery', batteryLabel(m), m.voltage!=null?m.voltage.toFixed(2)+'V':''),
   tile('Bridge', (d.bridge.state==='running'?'running':'stopped'), d.bridge.pid?('pid '+d.bridge.pid):''),
   tile('Uptime', st.uptime_s!=null?fmtDur(st.uptime_s):'—'),
   tile('Responder', `<span class="dot ${live?'on':'off'}"></span>${live?'live':'off'}`,
        (rp.model?rp.model.replace('claude-','').replace(/-\d+$/,''):'')+' · '+(rp.allow_count||0)+' allowed'),
   tile('Sent / Received', `${(d.totals&&d.totals.sent)??0} / ${(d.totals&&d.totals.recv)??0}`),
   tile('Ch util', m.chUtil!=null?m.chUtil.toFixed(1)+'%':'—', m.airUtilTx!=null?('air '+m.airUtilTx.toFixed(2)+'%'):''),
 ].join('');
 const cfg=d.config||{}, active=(st.transport||cfg.TRANSPORT||'serial');
 $('#active-t').textContent='active: '+active;
 $('#trans').innerHTML=[
   `<div class="t ${active==='serial'?'active':''}"><div class="lbl"><span class="dot ${active==='serial'?'on':'off'}"></span>USB</div></div>`,
   `<div class="t ${active==='tcp'?'active':''}"><div class="lbl"><span class="dot ${active==='tcp'?'on':'off'}"></span>WiFi</div></div>`,
 ].join('');
 SELF={id:node.id||null, name:node.shortName||node.longName||null};
 lastNodes=(d.nodes&&d.nodes.nodes)||[];
 const xs=d.exchanges||[];
 const dms=d.dm_exchanges||[];
 $('#xc-n').textContent=xs.length;
 $('#dm-n').textContent=dms.length;
 // Only touch the DOM when the content actually changed. Cheap, and it stops the 3s refresh
 // from fighting the reader (lost text selection, scroll jump) when nothing has happened.
 const sig=JSON.stringify([xs,dms,SELF,lastNodes.map(n=>[n.id,n.short])]);
 if(sig!==lastXsig){
   lastXsig=sig;
   XBYKEY.clear(); xs.forEach(x=>XBYKEY.set(xkey(x),x));
   dms.forEach(x=>XBYKEY.set(xkey(x),x));
   $('#exchanges').innerHTML=xs.length?xs.map(exchangeHtml).join('')
     :'<div class="empty">nothing on air yet — mesh is quiet or awaiting first inbound</div>';
   // Same renderer, deliberately. A second one would drift from the first, and the whole
   // point of the trace is that what it shows and what happened cannot diverge.
   $('#dm-exchanges').innerHTML=dms.length?dms.map(exchangeHtml).join('')
     :'<div class="empty">no direct messages yet</div>';
   hydrateOpen();
 }
 $('#nn').textContent=lastNodes.length;
 renderNodes();
}
// 'toggle' does not bubble, so listen in the capture phase on the container. Survives every
// re-render because the listener is on #exchanges, not on the details elements themselves.
// resolve the retired-version links against the app root, so they work at "/" and under a
// funnel path prefix alike
document.querySelectorAll('#oldlink,#oldlink2').forEach(a=>{a.href=DIR+'old-2';});
// A closed trace is not built. Every exchange used to render its full trace on every 3s pass
// whether or not anyone had opened it, which put a hard ceiling on how rich a trace could get.
// Bodies are now filled on first open and rebuilt by the normal render while they stay open.
// The assembly runs once per OPEN and is keyed separately from OPEN itself, because the 3s
// refresh rebuilds an open trace's markup — without this it would restart every three seconds.
// Nothing is measured from layout any more: the boxes share a centre line, so the arrows are
// straight and the geometry is the grid's problem, not ours.
const ANIMATED=new Set();
function hydrate(el,k,animate){
  const tp=el.querySelector('.tp'); if(!tp) return;
  if(!animate||ANIMATED.has(k)) return;
  ANIMATED.add(k);
  tp.classList.add('anim');
  tp.querySelectorAll('.arw').forEach((a,i)=>{a.style.animationDelay=(60+i*160)+'ms';});
  tp.querySelectorAll('.stg>.sdot').forEach((d,i)=>{d.style.animationDelay=(420+i*120)+'ms';});
}
function hydrateOpen(){
  document.querySelectorAll('#exchanges details.tr[open]').forEach(el=>hydrate(el,el.dataset.k,false));
}
// Tabs. Bound once at load, never from tick(), and the panes are hidden rather than
// rebuilt — so a refresh mid-read cannot switch the tab out from under you.
$('#xtabs').addEventListener('click', e=>{
  const b=e.target.closest('.tab'); if(!b) return;
  document.querySelectorAll('#xtabs .tab').forEach(t=>{
    const on = t===b;
    t.setAttribute('aria-selected', on?'true':'false');
    const pane=document.getElementById(t.getAttribute('aria-controls'));
    if(pane) pane.hidden = !on;
  });
});
$('#xtabs').addEventListener('keydown', e=>{
  if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight') return;
  const tabs=[...document.querySelectorAll('#xtabs .tab')];
  const cur=tabs.findIndex(t=>t.getAttribute('aria-selected')==='true');
  const nxt=tabs[(cur+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length];
  nxt.click(); nxt.focus(); e.preventDefault();
});
[$('#exchanges'),$('#dm-exchanges')].forEach(c=>c.addEventListener('toggle', e=>{
  const el=e.target;
  if(!el.matches||!el.matches('details.tr')) return;
  const k=el.dataset.k;
  if(!k) return;
  if(!el.open){ OPEN.delete(k); ANIMATED.delete(k);
    // the class must come off, or re-adding it on reopen is a no-op and nothing replays
    const tpc=el.querySelector('.tp'); if(tpc) tpc.classList.remove('anim');
    return; }
  OPEN.add(k);
  const body=el.querySelector('.tpwrap');
  const x=XBYKEY.get(k);
  if(body&&!body.firstChild&&x) body.innerHTML=traceHtml(x);
  hydrate(el,k,true);
}, true));
(function(){
  const m=location.pathname.match(/\/(old-\d+)\/?$/);
  if(!m) return;
  const cur=location.pathname.replace(/\/old-\d+\/?$/,'/');
  const b=document.createElement('div');
  b.style.cssText='background:#fff8c5;color:#9a6700;border-bottom:1px solid #d4a72c;'+
    'padding:9px 22px;font-size:13px;text-align:center';
  b.innerHTML='This is <b>'+m[1]+'</b>, a retired version of the dashboard, kept for reference. '+
    '<a href="'+cur+'" style="color:#0a63c9;font-weight:600">Go to the current page &rarr;</a>';
  document.body.insertBefore(b, document.body.firstChild);
})();
loadSnr(); tick(); setInterval(tick,3000); setInterval(loadSnr,30000);
</script></body></html>"""


PAGE_V4 = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>cal-mesh — levers (v4)</title>
<style>
:root{--bg:#f6f8fa;--card:#ffffff;--card2:#eef1f5;--line:#d6dce4;--fg:#1a1f26;
--dim:#5c6672;--accent:#0a63c9;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--tx:#6639ba;--rx:#1a7f37;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:linear-gradient(180deg,#f6f8fa,#f6f8faee);backdrop-filter:blur(6px);z-index:5}
header h1{font-size:17px;margin:0;letter-spacing:.3px}
header .sub{color:var(--dim);font-size:12px}
.pill{margin-left:12px;padding:5px 12px;border-radius:999px;font-weight:600;font-size:12px}
.pill.ok{background:#dafbe1;color:var(--ok);border:1px solid #aceebb}
.pill.bad{background:#ffebe9;color:var(--bad);border:1px solid #ffcecb}
.faqlink{color:var(--accent);text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
.faqlink:hover{text-decoration:underline}
.navlinks{margin-left:auto;display:inline-flex;gap:14px;align-items:center}
html{scroll-behavior:smooth}
main{padding:20px;max-width:1200px;margin:0 auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.tile .v{font-size:22px;font-weight:650;margin-top:4px}
.tile .v small{font-size:12px;color:var(--dim);font-weight:400}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:16px}
.card h2{font-size:13px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);
color:var(--dim);text-transform:uppercase;letter-spacing:.6px;display:flex;gap:8px;align-items:center}
.card h2 .badge{background:var(--card2);color:var(--fg);padding:2px 8px;border-radius:6px;font-size:11px;font-variant-numeric:tabular-nums}
.card h2 .badge.right{margin-left:auto}
/* A state badge that has only one appearance is not a state badge. `live` and `down`
   were rendering identically until this existed. */
.card h2 .badge.ok{background:#dafbe1;color:var(--ok)}
.card h2 .badge.warn{background:#ffebe9;color:var(--bad)}
.tag{padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600}
.tag.tx{background:#f3eefc;color:var(--tx)} .tag.rx{background:#dafbe1;color:var(--rx)}
.tag.ch{background:#ddf4ff;color:var(--accent)} .tag.auto{background:#fff8c5;color:var(--warn)}
.tag.offlist{background:#fff8c5;color:var(--warn);border:1px solid #d4a72c}
.tag.quiet{background:#eef1f5;color:var(--dim)}
/* --- tabbed streams: one card, two streams, so the page does not grow by one full
   card every time a stream is added. The pane is toggled with [hidden] rather than
   re-rendered, so the 3s refresh cannot knock the reader back to the first tab. --- */
/* Tabs, drawn as tabs. They were a row of grey text with a 2px underline on the active one:
   the inactive tab read as DISABLED LABEL rather than "another view you can click", and the
   active one was distinguished by a hairline most people never consciously see. Two views of
   the traffic is a fact about this page, and it was being whispered.
   The shape now carries it -- each tab is a filled, bordered folder tab, and the selected one
   is the one that rises out of the strip and MERGES INTO THE PANEL below by covering the
   strip's own bottom border. That join is the thing that says "this tab owns what is under
   it", and it is why the selected tab is white while the others are not.
   The border is deliberately not held to the 3:1 non-text bar: it is a refinement, and what
   actually identifies these controls is the fill, the label colour and the accent bar, which
   measure 7.07:1, 5.77:1 and 5.77:1. A boundary that is not doing the work does not need to
   carry the weight of one. */
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);background:var(--card2);
padding:9px 10px 0}
.tab{appearance:none;font:inherit;font-size:14px;font-weight:600;color:#404c5c;
background:#e2e8ef;border:1px solid #a3aebd;border-bottom:0;border-radius:10px 10px 0 0;
padding:11px 18px;margin-bottom:-1px;cursor:pointer;display:flex;align-items:center;gap:8px;
white-space:nowrap;transition:background .12s ease,color .12s ease}
.tab:hover{background:#eef2f6;color:var(--fg)}
.tab[aria-selected="true"]{background:var(--card);color:var(--accent);
border-color:var(--line);border-bottom:1px solid var(--card);
box-shadow:inset 0 3px 0 var(--accent)}
.tab .badge{background:#d8dee6;color:#404c5c;font-weight:700}
.tab[aria-selected="true"] .badge{background:#ddf4ff;color:var(--accent)}
.tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(max-width:520px){.tab{padding:10px 12px;font-size:13px}}
.pane[hidden]{display:none}
.tabnote{font-size:11.5px;color:var(--dim);line-height:1.55;margin:0;padding:13px 16px 2px;max-width:80ch}
/* --- exchanges --- */
.xc{padding:14px 16px;border-bottom:1px solid var(--line)}
.xc:last-child{border-bottom:0}
.xc .meta{color:var(--dim);font-size:11px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:5px}
.xc .ask{font-size:15px;word-break:break-word;max-width:78ch}
.rep .txt,.norep{max-width:78ch}
.xc.unprompted{background:#f0f3f7}
.rep{margin:9px 0 0 16px;padding:8px 12px;border-left:2px solid var(--tx);background:#f7f4fd;
border-radius:0 8px 8px 0}
.rep .who{color:var(--dim);font-size:11px;display:block;margin-bottom:2px}
.rep .txt{color:var(--tx);font-size:14px}
.norep{margin:8px 0 0 16px;padding:7px 12px;border-left:2px solid var(--line);background:#f2f4f7;
border-radius:0 8px 8px 0;color:var(--dim);font-size:12.5px}
/* --- trace disclosure --- */
details.tr{margin:10px 0 0 16px}
details.tr summary{cursor:pointer;list-style:none;color:var(--accent);font-size:13.5px;
font-weight:600;letter-spacing:.2px;display:inline-flex;gap:7px;align-items:center;
padding:4px 10px 4px 8px;border:1px solid var(--line);border-radius:7px;background:var(--card2)}
details.tr summary::-webkit-details-marker{display:none}
details.tr summary::before{content:">";font-size:13px;font-weight:700;display:inline-block;
transform-origin:50% 50%;transition:transform .15s ease}
details.tr[open] summary::before{transform:rotate(90deg)}
details.tr summary:hover{border-color:var(--accent);background:#e4e9f0}
.tp{margin-top:7px;background:#f4f6f9;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.link-d{margin:2px 0 10px;max-width:620px}
.link-d svg{width:100%;height:auto;display:block}
.trow{display:flex;gap:10px;padding:3px 0;font-size:12px;align-items:baseline}
.tk{color:var(--dim);min-width:78px;flex-shrink:0;text-transform:uppercase;font-size:10px;letter-spacing:.5px}
.tv{color:var(--fg);word-break:break-word}
.tv code{background:var(--card2);padding:1px 5px;border-radius:4px;font-size:11.5px}
.hint{color:var(--dim);font-size:11px}
.gate{display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:4px;font-size:11px}
.gp{background:#dafbe1;color:var(--ok)} .gf{background:#ffebe9;color:var(--bad)}
.tnote{margin-top:8px;padding-top:7px;border-top:1px solid var(--line);color:var(--dim);font-size:11px;line-height:1.5}
.tnone{color:var(--dim);font-size:12px}
/* --- trace: the swap. What reached the model and what did not, drawn rather than asserted.
   Two things compete to become the reply; on a capability answer one of them is cut. --- */
.swap{display:grid;grid-template-columns:minmax(0,1fr) 74px minmax(0,1fr);gap:9px 0;
align-items:center;margin:2px 0 12px}
.sw{border:1px solid var(--line);border-radius:9px;padding:8px 10px;background:var(--card);min-width:0}
.sw .swk{font-size:9.5px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);font-weight:700}
.sw .swv{font-size:12.5px;margin-top:3px;word-break:break-word;line-height:1.45}
.sw .swn{font-size:10.5px;margin-top:5px;color:var(--dim);line-height:1.4}
.sw.i-in{grid-column:1;grid-row:1} .sw.i-fact{grid-column:1;grid-row:2}
.sw.i-out{grid-column:3;grid-row:1/3;align-self:stretch;display:flex;
flex-direction:column;justify-content:center}
.sw.cut{border-style:dashed;background:#fbfcfd}
.sw.cut .swv{color:var(--dim);text-decoration:line-through;text-decoration-color:#b9c2cd}
.sw.i-fact{border-color:#aceebb;background:#f4fcf6}
.sw.i-out{border-color:#ddd0f5;background:#faf7fe}
.sw.i-out .swv{color:var(--tx);font-size:13.5px}
/* --- trace: the pipeline spine. The stages are a sequence in time, so they are drawn as one. --- */
.spine{list-style:none;margin:0;padding:0}
.stg{position:relative;padding:0 0 11px 25px}
.stg::before{content:"";position:absolute;left:5px;top:16px;bottom:0;width:2px;background:var(--line)}
.stg:last-child::before{display:none}
.stg>.sdot{position:absolute;left:0;top:5px;width:12px;height:12px;border-radius:50%;
background:var(--ok);border:2px solid var(--ok);box-sizing:border-box}
.stg.stop>.sdot{background:var(--bad);border-color:var(--bad)}
.stg.skip>.sdot{background:var(--card);border-color:#c3ccd7}
.stg.skip{opacity:.6}
.stg.stop::before,.stg.skip::before{background:repeating-linear-gradient(180deg,#c3ccd7 0 3px,transparent 3px 6px)}
.stg .shead{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.stg .sname{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);
font-weight:700;flex-shrink:0}
.stg .ssum{font-size:12.5px;color:var(--fg)}
.stg .sdet{margin-top:5px}
.stg .sdet:empty{display:none}
.rungn{display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:4px;font-size:11px;
background:#f2f4f7;color:var(--dim);font-style:italic}
/* --- trace: measurements drawn to the scale they were measured on --- */
.bar{position:relative;height:6px;border-radius:3px;background:#e7ebf1;margin-top:6px;max-width:280px}
.bar>i{position:absolute;top:0;bottom:0;border-radius:3px;background:#cfe6d6}
.bar>i.fill{left:0;background:var(--ok)}
.bar>i.fill.late{background:var(--warn)}
.bar .mk{position:absolute;top:-3px;width:2px;height:12px;background:var(--fg);border-radius:1px}
.barl{font-size:10.5px;color:var(--dim);margin-top:4px;line-height:1.45;max-width:60ch}
@media(max-width:640px){
.swap{grid-template-columns:minmax(0,1fr);gap:7px}
.sw.i-in,.sw.i-fact,.sw.i-out{grid-column:1;grid-row:auto}
.conn{display:none}}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 16px;font-size:13px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
td.snr-good{color:var(--ok)} td.snr-bad{color:var(--warn)}
#nodes-wrap{max-height:620px;overflow:auto}
#nodes thead th{position:sticky;top:0;background:var(--card);z-index:1}
#nodes th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
/* The radio speaks over exactly ONE transport at a time, and that is a switch position, not
   two things. It used to be drawn as two cards stretched to the full width of the page, each
   the size of a real panel, with the live one marked by a 1px border tint -- so the loudest
   thing about it was the pair, and the quietest was which one was actually carrying traffic.
   A segmented control says "one of these, and it is this one" in its shape, and it stops
   claiming a whole row of the page for one binary fact. The idle segment is still shown, on
   purpose: "USB exists and is not in use" is a different statement from "USB is broken", and
   dropping it would lose that. */
.trans{padding:14px 16px}
.seg{display:inline-flex;border:1px solid #a3aebd;border-radius:9px;overflow:hidden;
background:#e2e8ef}
.sg{padding:8px 18px;font-size:13.5px;font-weight:600;color:#404c5c;
border-right:1px solid #a3aebd;letter-spacing:.2px}
.sg:last-child{border-right:0}
/* Filled vs unfilled, which is a LIGHTNESS difference and survives any colour vision. The
   old version encoded it as a border hue, which does not. */
.sg.on{background:var(--accent);color:#fff}
.segd{margin-top:10px;font-size:11.5px;color:var(--dim);line-height:1.5}
.segd code{background:var(--card2);padding:1px 5px;border-radius:4px;font-size:11px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.tile .v .dot{width:9px;height:9px;margin-right:7px;vertical-align:middle}
.dot.on{background:var(--ok)} .dot.off{background:var(--bad)}
footer{color:var(--dim);font-size:11px;text-align:center;padding:16px}
.empty{padding:16px;color:var(--dim);font-size:13px}
#learning .lstats{display:flex;flex-wrap:wrap;gap:1px;background:var(--line);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
#learning .lstat{flex:1 1 150px;background:var(--card);padding:11px 16px}
#learning .lstat.warn{background:var(--card2)}
#learning .lk{font-size:10.5px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim)}
#learning .lv{font-size:19px;font-weight:650;margin-top:2px}
#learning .lstat.warn .lv{color:var(--warn)}
#learning .lsec h3{margin:0;padding:14px 16px 6px;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent)}
#learning .lnote{margin:0;padding:0 16px 8px;font-size:11.5px;color:var(--dim);line-height:1.5;max-width:80ch}
#learning .lrow{padding:9px 16px;border-top:1px solid var(--line)}
#learning .lrow.bad{border-left:3px solid var(--bad)}
#learning .lask{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;word-break:break-word}
#learning .lmeta{font-size:11.5px;color:var(--dim);margin-top:2px}
#learning .lsha{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent);text-decoration:none}
#learning .lsha:hover{text-decoration:underline}
#learning .lwarn{color:var(--warn);font-weight:600}
.faq h3{margin:0;padding:14px 16px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent);border-bottom:1px solid var(--line);background:#f0f3f7}
.faq .a a{color:var(--accent);text-decoration:none;font-weight:600}
.faq .a a:hover{text-decoration:underline}
.faq details{border-bottom:1px solid var(--line)}
.faq details:last-child{border-bottom:0}
.faq summary{padding:12px 16px;cursor:pointer;font-weight:600;list-style:none;display:flex;align-items:center;gap:10px}
.faq summary::-webkit-details-marker{display:none}
.faq summary::before{content:"+";color:var(--accent);font-weight:700;width:10px;display:inline-block}
.faq details[open] summary::before{content:"\2013"}
.faq .a{padding:0 16px 14px 40px;color:var(--dim);font-size:13px;line-height:1.65}
.faq .a code{background:var(--card2);padding:1px 5px;border-radius:4px;color:var(--fg);font-size:12px}
.faq .a b{color:var(--fg)}
.clog{max-height:420px;overflow-y:auto}
.clog .ci{padding:10px 16px;border-bottom:1px solid var(--line);font-size:13px;line-height:1.55}
.clog .cd{color:var(--dim);font-size:11px;margin-right:8px;font-variant-numeric:tabular-nums}
.spark{display:inline-flex;align-items:center;gap:6px;font-size:12px;white-space:nowrap}
/* ============================ v3: depth ============================
   v2 drew a sequence that happened in time as flat boxes on one plane. Three things carry
   the third dimension here: elevation (a recessed well under raised planes), wires that are
   MEASURED from real box geometry rather than approximated with horizontal rules, and an
   assembly order that runs once when a trace is opened. */
.tp{background:linear-gradient(180deg,#f7f9fc,#eef2f7);border-radius:12px;
box-shadow:inset 0 2px 5px rgba(22,27,34,.05),0 1px 0 #fff}
/* The chain, left to right. Boxes are numbered because the whole point is the ORDER:
   a reader who does not know how this works needs to see that the question caused the lookup. */
.flow{display:grid;align-items:center;margin:2px 0 4px;
grid-template-columns:minmax(0,1fr) 62px minmax(0,.92fr) 74px minmax(0,.86fr) 104px minmax(0,1fr)}
.flow.gen{grid-template-columns:minmax(0,1fr) 150px minmax(0,1fr)}
.fb{position:relative;z-index:1;border:1px solid var(--line);border-radius:11px;padding:10px 13px;
background:linear-gradient(180deg,#fff,#fbfcfe);
box-shadow:0 1px 2px rgba(22,27,34,.05),0 6px 16px -8px rgba(22,27,34,.22)}
.fk{font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--dim)}
.fv{font-size:13px;margin-top:5px;line-height:1.45;word-break:break-word}
.fn{font-size:10.5px;margin-top:6px;color:var(--dim);line-height:1.42;overflow-wrap:anywhere}
/* the recognition step: plain word-matching, no model. Drawn as a decision, not as data. */
.fb.bx{border-color:#c3d9f2;background:linear-gradient(180deg,#f7fbff,#eef5fd);
box-shadow:0 1px 2px rgba(10,99,201,.08),0 8px 20px -10px rgba(10,99,201,.30)}
.fb.bx .fv{font-size:12.5px}
.chip{display:inline-block;margin:3px 4px 0 0;padding:1px 7px;border-radius:5px;
max-width:100%;overflow-wrap:anywhere;word-break:break-word;vertical-align:top;
background:#dceafb;color:#0a4da3;font-size:11px;font-weight:600;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.fb.b2{border-color:#a7e3b6;background:linear-gradient(180deg,#f4fdf7,#eaf9ef);
box-shadow:0 1px 2px rgba(26,127,55,.10),0 8px 20px -10px rgba(26,127,55,.35)}
.fb.b2 .fv{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.fb.b3{border-color:#d5c6f3;background:linear-gradient(180deg,#fbf8ff,#f5effd);
box-shadow:0 1px 2px rgba(102,57,186,.10),0 8px 20px -10px rgba(102,57,186,.35)}
.fb.b3 .fv{color:var(--tx);font-weight:600;font-size:14px}
.onair{color:var(--ok);font-weight:600}
/* arrows: straight, because every box shares a centre line — no measurement needed */
.arw{position:relative;height:3px;border-radius:2px;
background:linear-gradient(90deg,#cbd4de,var(--ok));transform-origin:left center}
.arw::after{content:"";position:absolute;right:-1px;top:-5.5px;border:6px solid transparent;
border-left-color:var(--ok);border-right:0}
.arw>span{position:absolute;left:0;right:0;bottom:9px;font-size:9.5px;color:var(--dim);
text-align:center;line-height:1.3}
/* the boundary: the one surprising fact, stated once, on the line it describes */
.cross{position:relative;align-self:stretch;display:flex;align-items:center}
.cross::before{content:"";position:absolute;left:50%;top:0;bottom:0;margin-left:-1px;
border-left:2px dashed #9fb0c4}
.cross .arw{flex:1;margin:0 6px}
.cross .bl{position:absolute;left:50%;top:2px;transform:translateX(-50%);background:var(--card);
border:1px solid #c3d2e2;border-radius:6px;padding:3px 6px;font-size:8.5px;font-weight:700;
text-transform:uppercase;letter-spacing:.4px;color:#3d566e;text-align:center;line-height:1.25;
width:92px;box-sizing:border-box}
.flowcap{font-size:11.5px;color:var(--dim);line-height:1.55;margin:10px 0 14px;max-width:88ch;
padding-left:2px}
.flowcap b{color:var(--fg);font-weight:650}
.stg{padding:0 0 13px 30px}
.stg::before{left:6.5px;top:17px;width:3px;border-radius:2px;
background:linear-gradient(180deg,#cfd7e1,#dde3ea)}
.stg>.sdot{left:0;top:5px;width:16px;height:16px;border:0;
background:radial-gradient(circle at 35% 32%,#5fd07f,var(--ok));
box-shadow:0 0 0 3px rgba(26,127,55,.14),0 1px 2px rgba(22,27,34,.3)}
.stg.stop>.sdot{background:radial-gradient(circle at 35% 32%,#f08b93,var(--bad));
box-shadow:0 0 0 3px rgba(207,34,46,.14),0 1px 2px rgba(22,27,34,.3)}
.stg.skip>.sdot{background:#fff;box-shadow:inset 0 0 0 2px #c3ccd7}
.sname{min-width:74px;letter-spacing:.85px}
/* instruments: a measurement drawn against the range it lives on, not a bare number */
.inst{display:flex;gap:26px;flex-wrap:wrap;margin-top:8px}
.gauge{min-width:206px;max-width:320px}
.glab{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px;gap:12px}
.gname{font-size:9px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;color:var(--dim)}
.gval{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;font-weight:600}
.track{position:relative;height:9px;border-radius:5px;background:#e4e9f0;
box-shadow:inset 0 1px 2px rgba(22,27,34,.14)}
/* sequential magnitude = ONE hue, light to dark. Not a red-amber-green rainbow: that is a
   rainbow ramp for an ordered quantity, and red/amber are ~1.5 dE apart under deuteranopia. */
.track.ramp{background:linear-gradient(90deg,#eaf6ee,#9fd8b3,#1a7f37)}
.track .band{position:absolute;top:0;bottom:0;background:rgba(26,127,55,.28);border-radius:4px}
.track .mk{position:absolute;top:-4px;width:3px;height:17px;border-radius:2px;background:var(--fg);
box-shadow:0 0 0 2px #fff,0 1px 3px rgba(22,27,34,.4);transition:left .7s cubic-bezier(.22,1,.36,1)}
.gends{display:flex;justify-content:space-between;font-size:9.5px;color:var(--dim);margin-top:5px;gap:10px}
.gnote{font-size:10.5px;color:var(--dim);margin-top:6px;line-height:1.45;max-width:52ch}
/* assembly: runs once per open, never on the 3s refresh */
.tp.anim .stg>.sdot{transform:scale(.4);opacity:0;animation:sdotpop .45s cubic-bezier(.34,1.56,.64,1) forwards}
@keyframes sdotpop{to{transform:scale(1);opacity:1}}
.tp.anim .arw{transform:scaleX(0);animation:arwgrow .5s cubic-bezier(.22,1,.36,1) forwards}
@keyframes arwgrow{to{transform:scaleX(1)}}
.tp.anim .cross .bl{opacity:0;animation:blfade .35s ease forwards;animation-delay:.45s}
@keyframes blfade{to{opacity:1}}
@media(prefers-reduced-motion:reduce){
.tp.anim .stg>.sdot,.tp.anim .arw,.tp.anim .cross .bl{animation:none;opacity:1;transform:none}
.track .mk{transition:none}}
@media(max-width:700px){
.flow,.flow.gen{grid-template-columns:minmax(0,1fr)}
.arw,.cross{display:none}}
/* ==================== v4: the trace is a dark instrument well ====================
   The page stays light. The trace panel — and ONLY the trace panel — is dark.
   The reasoning is that these are two different kinds of surface. The page is a
   status board you scan; the trace is an instrument you read one record on, and
   dropping it out of the page's light gives the boxes, wires and dots somewhere to
   sit that is not the same plane as the list they came from. It also makes the
   elevation model honest in both directions: on a light page a raised plane is
   whiter than its ground, and on a dark one it is lighter than its ground, so the
   well/plane hierarchy v3 built survives the inversion instead of reading backwards.

   MECHANISM: every colour under .tp is read from a token, so the palette is
   re-declared ONCE on .tp and every descendant follows. Two things do NOT follow,
   and they are the trap the 2026-08-12 light switch already paid for: the link
   diagram's colours are written into linkSvg, and the warn box's are an inline
   style in flowHtml. Both are changed at their source in THIS page's script.

   Contrast was measured, not eyeballed. Every text pair in this panel is >= 6.0:1
   against the surface it sits on (AA needs 4.5), and every boundary that carries
   meaning — box borders, the spine rail, the gauge track — is >= 3.0:1 against the
   panel (AA non-text needs 3.0). The light palette's own separators do not clear
   that bar; going dark is where a 1.2:1 hairline stops being a hairline and starts
   being invisible, so it was worth paying for here. */
.tp{
  --fg:#e6edf3; --dim:#9aa7b4; --line:#5c6673; --card:#161b22; --card2:#21262d;
  --accent:#6cb6ff; --ok:#3fb950; --warn:#e3b341; --bad:#ff7b72; --tx:#d2a8ff;
  color:var(--fg);
  margin-top:0;
  border:1px solid #2a3038;
  border-top-left-radius:0;
  background:linear-gradient(180deg,#0b0f14,#11161d);
  box-shadow:inset 0 2px 10px rgba(0,0,0,.55),0 1px 0 rgba(255,255,255,.04);
  padding:12px 14px;
}
/* the disclosure becomes a tab ON the well when open, so the dark panel reads as
   something that was opened rather than a stray dark box in a light page */
details.tr[open]>summary{background:linear-gradient(180deg,#1c232c,#11161d);
  border-color:#2a3038;color:#9ecbff;border-bottom:0;padding-bottom:6px;
  border-radius:7px 7px 0 0}
details.tr[open]>summary:hover{border-color:#4478ad;
  background:linear-gradient(180deg,#232b36,#161c24)}
/* raised planes: lighter than their ground, which is the dark-mode form of "raised" */
.tp .fb{border-color:#5c6673;background:linear-gradient(180deg,#1c232c,#161c24);
  box-shadow:0 1px 2px rgba(0,0,0,.5),0 8px 20px -10px rgba(0,0,0,.85)}
.tp .fb.bx{border-color:#4478ad;background:linear-gradient(180deg,#16243a,#111c2e);
  box-shadow:0 1px 2px rgba(0,0,0,.5),0 8px 22px -10px rgba(68,120,173,.45)}
.tp .chip{background:#17324f;color:#9ecbff}
.tp .fb.b2{border-color:#3a8752;background:linear-gradient(180deg,#122a1a,#0f2216);
  box-shadow:0 1px 2px rgba(0,0,0,.5),0 8px 22px -10px rgba(58,135,82,.5)}
.tp .fb.b3{border-color:#7d5fbd;background:linear-gradient(180deg,#221a35,#1b1529);
  box-shadow:0 1px 2px rgba(0,0,0,.5),0 8px 22px -10px rgba(125,95,189,.5)}
.tp .arw{background:linear-gradient(90deg,#39414b,var(--ok))}
.tp .cross::before{border-left-color:#7d8590}
.tp .cross .bl{background:#161b22;border-color:#5c6673;color:#adbac7}
/* the spine: rail and dots. A connector that carries sequence is a meaningful
   boundary, so it is held to 3:1 rather than left as a hairline. */
/* the rail runs top-to-bottom, so BOTH ends of it are the boundary — the first pass shaded
   it away to #3d4753 at the bottom, which is 1.9:1 and simply not a line any more. */
.tp .stg::before{background:linear-gradient(180deg,#6b7684,#5c6673)}
.tp .stg.stop::before,.tp .stg.skip::before{
  background:repeating-linear-gradient(180deg,#5c6673 0 3px,transparent 3px 6px)}
.tp .stg>.sdot{background:radial-gradient(circle at 35% 32%,#56d364,#2ea043);
  box-shadow:0 0 0 3px rgba(63,185,80,.20),0 1px 3px rgba(0,0,0,.7)}
.tp .stg.stop>.sdot{background:radial-gradient(circle at 35% 32%,#ff7b72,#da3633);
  box-shadow:0 0 0 3px rgba(248,81,73,.20),0 1px 3px rgba(0,0,0,.7)}
.tp .stg.skip>.sdot{background:#11161d;box-shadow:inset 0 0 0 2px #5c6673}
.tp .gate.gp{background:rgba(63,185,80,.16);color:#56d364}
.tp .gate.gf{background:rgba(248,81,73,.16);color:#ff7b72}
.tp .rungn{background:#1c222b;color:var(--dim)}
/* instruments. The ramp keeps the single-hue rule and simply runs the other way:
   an ordered quantity goes dark-to-bright on a dark ground, which is the same
   "light to dark" instruction read against its own background. The extent of the
   track is carried by an inset ring, so the low end of the ramp can go as dark as
   it likes without the scale itself disappearing. */
.tp .track{background:#21262d;
  box-shadow:inset 0 0 0 1px #5c6673,inset 0 1px 3px rgba(0,0,0,.5)}
.tp .track.ramp{background:linear-gradient(90deg,#0f2a19,#2a8f4c,#56d364)}
.tp .track .band{background:rgba(63,185,80,.30)}
.tp .track .mk{background:var(--fg);box-shadow:0 0 0 2px #11161d,0 1px 3px rgba(0,0,0,.8)}
.tp .bar{background:#21262d}
.tp .bar>i{background:#1f4d2e}
.tp .bar>i.fill{background:var(--ok)}
.tp .bar>i.fill.late{background:var(--warn)}
.tp .bar .mk{background:var(--fg)}
/* a harvested path: a separate measurement, so it gets its own plane and its own rule above
   it rather than blending into the rows of the diagram it sits under */
.tp .pathm{margin-top:11px;padding-top:10px;border-top:1px solid #3d4753}
.tp .pathh{font-size:9.5px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;
  color:var(--dim);margin-bottom:8px}
.tp .pathh .pathage{font-weight:600;letter-spacing:.3px;text-transform:none;color:#9ecbff}
.tp .pdir{font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;
  color:var(--dim);margin:7px 0 3px}
.tp .pchain{display:flex;flex-wrap:wrap;align-items:center;gap:2px 0;margin-bottom:2px}
.tp .phop{display:inline-block;padding:3px 9px;border-radius:7px;font-size:12px;font-weight:600;
  background:linear-gradient(180deg,#1c232c,#161c24);border:1px solid #5c6673;color:var(--fg)}
.tp .phop.unk{border-style:dashed;color:var(--dim);font-weight:500;font-style:italic}
.tp .plink{display:inline-flex;flex-direction:column;align-items:center;margin:0 2px;min-width:56px}
.tp .plink .parr{display:block;width:100%;height:2px;border-radius:1px;background:#6b7684;
  position:relative}
.tp .plink .parr::after{content:"";position:absolute;right:0;top:-3.5px;border:4.5px solid transparent;
  border-left-color:#6b7684;border-right:0}
.tp .plink .psnr{font-size:9.5px;color:var(--dim);margin-top:2px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}
/* the v2-era swap boxes. Not rendered by this page, but the rules are still in the
   sheet above and an unstyled light box would be the thing nobody notices until a
   record shaped the old way turns up. */
.tp .sw{background:#1c232c;border-color:#5c6673}
.tp .sw.cut{background:#141a21}
.tp .sw.cut .swv{text-decoration-color:#5c6673}
.tp .sw.i-fact{border-color:#3a8752;background:#122a1a}
.tp .sw.i-out{border-color:#7d5fbd;background:#221a35}
</style></head>
<body>
<header>
  <div><h1>📻 cal-mesh <span class="sub">— live levers (v4)</span></h1>
  <div class="sub" id="sub">connecting…</div></div>
  <span class="navlinks"><a class="faqlink" href="#faq">FAQ ↓</a><a class="faqlink" href="#changelog">Changelog ↓</a><a class="faqlink" href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">GitHub ↗</a></span>
  <span class="pill" id="conn">…</span>
</header>
<main>
  <div class="tiles" id="tiles"></div>
  <div class="card"><h2>Transports <span class="badge right" id="active-t"></span></h2>
    <div class="trans" id="trans"></div></div>
  <div class="card">
   <div class="tabs" role="tablist" id="xtabs">
    <button class="tab" role="tab" id="tab-open" aria-controls="pane-open" aria-selected="true">💬 Open Exchanges <span class="badge" id="xc-n">0</span></button>
    <button class="tab" role="tab" id="tab-dm" aria-controls="pane-dm" aria-selected="false">🔒 Direct Messages <span class="badge" id="dm-n">0</span></button>
   </div>
   <div class="pane" id="pane-open" role="tabpanel" aria-labelledby="tab-open"><div id="exchanges"></div></div>
   <div class="pane" id="pane-dm" role="tabpanel" aria-labelledby="tab-dm" hidden>
    <p class="tabnote">Cal and Dean&rsquo;s test bench. Trying things on the open channel costs every
    node in range airtime, so experiments happen here instead &mdash; one link, two nodes. <b>It is
    published for the same reason everything else here is:</b> the interesting part is what is being
    tried and how it works, and a private tier you cannot see is a claim rather than a demonstration.
    These are authenticated direct messages, so unlike the open channel the sender is
    cryptographically established rather than merely asserted.</p>
    <div id="dm-exchanges"></div>
   </div>
  </div>
  <div class="card"><h2><span class="badge" id="nn">0</span> Neighbors heard</h2>
    <div id="nodes-wrap"><table id="nodes"><thead><tr>
      <th class="sortable" data-key="short" onclick="setSort('short')">Short</th>
      <th class="sortable" data-key="long" onclick="setSort('long')">Name</th>
      <th class="sortable" data-key="hw" onclick="setSort('hw')">HW</th>
      <th class="sortable" data-key="hops" onclick="setSort('hops')">Hops</th>
      <th class="sortable" data-key="snr" onclick="setSort('snr')">SNR</th>
      <th>1h SNR trend</th></tr></thead><tbody></tbody></table></div>
  </div>
  <div class="card" id="learning"><h2>What Cal could not answer <span class="badge right" id="lrn-untriaged">0</span></h2>
    <p class="tabnote">A distiller reads every exchange once a day and files what reached the
    model instead of a capability. That queue is the build list, and it is published for the
    same reason the trace is: a list of what this node <i>can</i> do is a claim, while a list of
    what it still <b>cannot</b> — next to the commit that fixed the last one — is checkable.
    The uncomfortable numbers are the load-bearing ones.</p>
    <div class="lstats" id="lrn-stats"></div>
    <div class="lsec"><h3>Armed</h3><div id="lrn-armed"></div></div>
    <div class="lsec"><h3>Waiting on an oracle</h3>
      <p class="lnote">Nothing is built from these until someone decides what the right answer
      is measured against. A doer graded only by its own test suite is a guess with a green
      check next to it.</p>
      <div id="lrn-queue"></div></div>
    <div class="lsec"><h3>Corrections after arming</h3>
      <p class="lnote">The counter-metric. Kept in view on purpose — a loop scored only on what
      it builds will build.</p>
      <div id="lrn-corr"></div></div>
  </div>
  <div class="card faq" id="faq"><h2>FAQ — what this is and how it works</h2>
    <h3>Start here</h3>
    <details><summary>What is this page?</summary><div class="a">
      A live, read-only window into <b>Cal</b> — an AI that lives on a <b>radio mesh network</b> and
      answers people over the air, with no internet on the far end. Everything here is real: the radio's
      state, every message in and out, and the full reasoning trace behind each automatic reply. Nothing
      is a mock-up. If Cal answered someone thirty seconds ago, it's below.</div></details>
    <details><summary>What is a mesh network?</summary><div class="a">
      A network with <b>no towers, no carrier and no internet</b>. Every radio is also a repeater: if
      two nodes are too far apart to hear each other, a third in the middle passes the message along,
      and so on. That's a <b>hop</b>. Coverage comes from the participants rather than infrastructure,
      so the network exists wherever people bring radios — and keeps working when the grid doesn't.
      That last property is the whole point: it's the tool you reach for when cell service is gone.</div></details>
    <details><summary>What is Meshtastic?</summary><div class="a">
      Free, open-source firmware that turns inexpensive <b>LoRa</b> radios (typically $30–100) into a
      mesh network for text messages and location sharing. You flash it onto a small board, pair it to
      your phone, and you're on the mesh — encrypted by channel, no account, no subscription, no
      monthly fee. It's a volunteer project with a large community, and it's what Cal's radio runs.
      <br><a href="https://meshtastic.org" target="_blank" rel="noopener noreferrer">meshtastic.org ↗</a></div></details>
    <details><summary>What is LoRa, and why does it matter here?</summary><div class="a">
      <b>Lo</b>ng <b>Ra</b>nge radio: a modulation designed to get a very small amount of data a very
      long way on very little power — miles between nodes, on a battery, with no licence required on
      the public bands. The trade is <b>bandwidth</b>. A LoRa channel carries on the order of a few
      hundred to a few thousand bits per second, and <b>every node in earshot shares it</b>. One long
      message blocks the channel for everyone. That single constraint explains most of Cal's design,
      starting with why it never says more than a few words.</div></details>
    <details><summary>Why put an AI on a mesh radio at all?</summary><div class="a">
      Because a mesh is what you use <b>when the grid isn't there</b> — off-grid, field work, dead
      coverage, emergencies — and that's exactly when knowledge is hardest to reach. The insight the
      project runs on: the mesh is off-grid, but the <b>base station usually isn't</b>. Cal's radio is
      connected to a computer with internet, so someone miles out with nothing but a handheld can ask a
      question over RF and get a real answer relayed back. Cal extends connected knowledge to the
      unconnected edge. Before this, a node could prove it was alive but couldn't actually
      <i>help</i> — presence without utility.</div></details>
    <details><summary>How did it get here?</summary><div class="a">
      Three deliberate stages, each gated before the next. <b>Level 1</b> — a bridge that owns the
      radio and can send and receive text. <b>Level 2</b> — an autonomous responder that decides on its
      own whether to answer and writes the reply, with training wheels (a small allow-list, rate limits,
      a kill switch). <b>Level 3</b> — real capabilities, where the software fetches a verified fact and
      the model only puts it into words. Each stage shipped switched <b>off</b>, went through
      adversarial review, and was turned on deliberately. The reviews have caught real problems,
      including a privacy leak in the reply path.</div></details>

    <h3>How Cal behaves</h3>
    <details><summary>How does Cal know a message is meant for it?</summary><div class="a">
      A message qualifies if it's a <b>direct message</b> to Cal's node, <i>or</i> the text contains
      <code>cal</code> as a whole word (case-insensitive). Whole-word matching means "lo<b>cal</b>",
      "<b>cal</b>endar" and "physi<b>cal</b>" do <i>not</i> trigger it.</div></details>
    <details><summary>What has to be true before Cal replies?</summary><div class="a">
      Being named isn't enough. In order, a message must pass every gate: it's <b>not Cal's own</b> ·
      it's <b>fresh</b> · the responder is <b>enabled</b> · the sender is on the <b>allow-list</b> ·
      it's <b>addressed</b> · it's <b>within rate limits</b>. Miss one and Cal stays quiet and records
      why — open <b>trace</b> on any exchange to see the whole ladder and exactly where it stopped.</div></details>
    <details><summary>Why do some messages say "OFF-LIST"?</summary><div class="a">
      Because Cal <b>heard them perfectly well</b> and chose not to answer. Reception and reply are two
      different things: every message on the channel is received and shown here, but only senders on the
      allow-list can trigger an automatic reply. <i>Whether silence is the right behaviour is under
      active review</i> — the argument against it is that on a shared channel, staying quiet to one
      person while answering another isn't neutral, it reads as a snub.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/unknown-sender-tier.md" target="_blank" rel="noopener noreferrer">The proposal to fix it ↗</a></div></details>
    <details><summary>Why are the replies so short?</summary><div class="a">
      Airtime is <b>shared by every node in range</b>, and LoRa has very little of it. A long message
      is not just slow, it takes the channel away from everyone else — including traffic that might
      matter more than a chat reply. So Cal is held to <b>5–7 words</b>. It's etiquette enforced in
      code, and it's why the answers read like radio traffic rather than chat.</div></details>
    <details><summary>Who can Cal talk to, and can it be switched off?</summary><div class="a">
      Right now only a small allow-list of nodes can trigger a reply, though <b>anyone</b> on the mesh
      can read what Cal says — the channel is public. Three independent always-on services do the work
      (radio, cognition, this dashboard), so one can restart without dropping the others, and a single
      kill switch silences all automatic replies instantly. Note that node IDs are <b>not
      authenticated</b> and can be spoofed, so the allow-list is a courtesy control, not a security
      boundary. The real controls are the kill switch and the fact that the model can't run tools.</div></details>

    <h3>How the answers are made</h3>
    <details><summary>How does Cal choose what to say?</summary><div class="a">
      A headless Claude writes the reply under a fixed persona — <b>5–7 words, plain text, warm and
      useful, never reveal the operator's location, schedule or personal life</b> — running with
      <b>no tools</b> and with no access to any private context. The important part is what it
      <i>isn't</i> allowed to do: for anything factual, Cal never looks something up. The software
      fetches a verified fact from a known source and hands it over, and the model's only job is to put
      that fact into words. We call it <b>capability injection</b>, and it's why Cal can't invent a
      temperature — if the fetch fails, it says so instead of guessing.</div></details>
    <details><summary>Where does the weather come from?</summary><div class="a">
      The US National Weather Service, and nothing else — one allow-listed source, fetched by the
      software, never by the model. Cal reads the <b>latest observation from the nearest weather
      station</b> to a fixed reference point, and refuses to answer at all if that reading is too old.
      Cal has <b>no forecast</b>: ask about tonight, tomorrow or whether it's going to rain and it
      says so outright rather than reading you a present-tense number as though it were a prediction.
      When it feels meaningfully different from the air temperature, Cal reports the <b>heat index</b>
      (or <b>wind chill</b> in the cold) alongside it — that is the number a person actually acts on,
      and it can run well above the temperature: measured here, 95&deg;F air against a 107&deg;F heat
      index. If the source publishes that value in a unit the software does not recognise, it is
      <b>dropped rather than converted on a guess</b>, because a wrong number is worse than no number.
      Known limitation, stated plainly: the station is a real place some distance away, and its
      reading can differ from the estimate for a specific spot. What Cal reports is a real
      measurement of somewhere nearby, not a forecast for where you're standing.
      <br><br>This page used to put a number on that gap — "five degrees or more". That number is
      withdrawn rather than quietly softened, and the reason is worth saying: it was measured
      against a <b>reference point that was itself nearly four miles wrong</b>, from a station
      believed to be five miles off that is actually about one. The reference has been corrected.
      The gap is real and the caution stands, but the size of it has not been honestly measured
      yet, so no figure is quoted here until it has been.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather-point-accuracy.md" target="_blank" rel="noopener noreferrer">The write-up, including the fix ↗</a></div></details>
    <details><summary>What is an "exchange"?</summary><div class="a">
      Almost everything Cal transmits is a response to being prompted, so the page is organised that
      way: the incoming message is the head, and Cal's reply is indented beneath it. Two things don't
      fit that shape and are marked separately — <b>unprompted</b> sends (an operator message, with no
      ask above it) and messages overheard but never addressed to Cal.</div></details>
    <details><summary>What's in the decision trace?</summary><div class="a">
      Open <b>trace</b> on an exchange Cal <i>answered</i> and you get two pictures; on one it did not
      answer, the chain is skipped and only the second appears. First, <b>the chain that produced
      the reply</b>, read left to right and numbered: the question that arrived, <b>what the software
      recognised in it</b>, what it went and fetched as a result, and finally what the model wrote.
      The recognition step is worth a look, because it is the least magical part of the whole system:
      Cal decides a message is a weather question by <b>matching plain words</b> — one strong word like
      <code>temperature</code> is enough on its own, otherwise it takes two weather words, or one plus
      a question mark. No model takes part in that decision, and the trace shows you which words
      actually matched. The question is
      <b>received normally</b> — it does real work, because it is what decided which fact to look up —
      but it stops at the dashed line. <b>Only the fetched fact crosses</b> into the model, whose
      entire job is to put that fact into words. That is why Cal cannot invent a temperature. On an
      ordinary reply with no lookup behind it there is no dashed line, because the model really was
      given the message itself.
      <br><br>Below it, the <b>stages in the order they happened</b>: received, gated, sanitized,
      grounded, narrated, sent. Each carries its own detail — which checks passed and which one stopped
      it, what the sanitizer changed, which weather station the reading came from and how old it was,
      the model and how long generation took. A message that fails a check <b>stops the spine where it
      failed</b>, and a single hollow step says outright that nothing further ran. That is read off the record rather
      than assumed: a message that was gated out carries no sanitizer result, no fact, no model and no
      destination. It is the machinery, not a narration — see below.</div></details>
    <details><summary>Why doesn't the trace show Cal's "thinking"?</summary><div class="a">
      Because there isn't any to show, and inventing some would be worse than showing nothing. Reply
      generation returns plain text — there's no hidden reasoning being discarded. We could ask the
      model to narrate why it chose a reply, but that narration <b>wouldn't be a faithful account of
      the computation</b>, and publishing it as though it were would present a plausible story as
      mechanism. It would also put unbounded, model-authored text — influenced by whatever a stranger
      transmitted — onto a public page, which is what the rest of the design works to prevent.</div></details>
    <details><summary>What's the diagram in the "received" stage?</summary><div class="a">
      The <b>link</b> the message travelled: who transmitted, who received it, how many <b>hops</b> it
      took, and the signal strength on the final leg. <b>Direct</b> means Cal heard the sender's own
      radio; anything above zero means other nodes relayed it. Where the firmware reports a relay it
      gives only <b>one byte</b> of that node's id — enough to narrow the candidates, not to name one —
      so it's shown truncated and never resolved to a name. The sender's box is coloured by what Cal
      did with the message, so the diagram and the verdict can't disagree.
      <br><br>The hop count is sometimes genuinely unknown, and the caption says which kind of unknown
      it is: a message received before this feature existed, or one where the sender reported nothing
      usable. It will not claim a reason it can't support — it did exactly that until 2026-08-11, and
      the changelog says how.</div></details>
    <details><summary>Why isn't it a real map?</summary><div class="a">
      Because this page is public and the base station sits at a fixed private address — a pin would
      publish it, and a series of pins would publish movements. So the diagram shows <b>topology</b>
      (who → who, how many hops) and never a location. No coordinates are stored by this project at
      all: the bridge deliberately reads names, hops and signal from the node database and skips the
      position field, even though about half the neighbours broadcast one. Cal's own node doesn't
      advertise a position either.</div></details>
    <details><summary>What about privacy and safety?</summary><div class="a">
      The channel is public by design, and this page only ever shows public-channel traffic and Cal's
      own telemetry — never the operator's data. Incoming text is treated as hostile: anyone in radio
      range can transmit anything, so messages are sanitized before they go anywhere near the model,
      the model runs with <b>no tools and no private context loaded</b>, and the trace reports
      <i>that</i> something was redacted and how many times, never <i>what</i>. An adversarial review
      of this exact path caught a real location leak before it shipped.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/channel-trust-and-agency.md" target="_blank" rel="noopener noreferrer">The trust model ↗</a></div></details>

    <h3>The project</h3>
    <details><summary>Is the code public? Can I run my own?</summary><div class="a">
      Yes — cal-mesh is open source, and the whole thing (bridge, responder, dashboard) is on GitHub.
      It ships a <code>config.example</code>: point it at your own Meshtastic node and you can run your
      own Cal on your own mesh. It has already had its first outside contribution via fork and pull
      request.
      <br><a href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">github.com/deanssamclaw/cal-mesh ↗</a></div></details>
    <details><summary>Where's the design reasoning written down?</summary><div class="a">
      In the repo, as proposals — including the arguments that <i>lost</i>, which are usually the more
      useful half. Each one is written to be reviewed and attacked before anything gets built.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather.md" target="_blank" rel="noopener noreferrer">Giving Cal live knowledge — the framework ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-roadmap.md" target="_blank" rel="noopener noreferrer">Capability roadmap — what Cal could learn next ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather-intent-layer.md" target="_blank" rel="noopener noreferrer">Two of my own proposals, refuted with measurements ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/unknown-sender-tier.md" target="_blank" rel="noopener noreferrer">Answering strangers — "we hear you" ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/channel-trust-and-agency.md" target="_blank" rel="noopener noreferrer">Channel trust &amp; agency — how much Cal is allowed to be ↗</a></div></details>
  </div>
  <div class="card" id="changelog"><h2>Changelog</h2>
    <div class="clog">
      <div class="ci"><span class="cd">2026-08-19</span><b>v4 &mdash; the trace is dark, and only the trace.</b> The page around it is unchanged: same tiles, same streams, same light palette it moved to on 12 August. What changed is that opening a <b>trace</b> now drops you onto a dark instrument panel instead of a lighter shade of the same page. The reason is that these are two different things to look at. The page is a board you <i>scan</i> &mdash; is the radio up, what came in, which nodes are near. A trace is one record you <i>read</i>, and it is drawn with boxes, wires and dots that have nowhere to sit when their ground is the same white as the list they came out of. It also keeps v3&rsquo;s elevation honest. On a light page a raised surface is whiter than what it sits on; on a dark one it is lighter than what it sits on. Inverting the ground without inverting that rule would have left the well and the raised planes reading backwards, so the recessed panel is now the darkest thing on screen and every box lifted off it is lighter, which is the same hierarchy stated the other way round. The colour ramp under the gauges follows the same logic: still one hue, still never a rainbow, but running dark-to-bright, because "light to dark" is an instruction about the background as much as the ink. Two colours had to be re-picked by hand rather than swapped, and they are the <i>same two places</i> that had to be re-picked when this page went light &mdash; the link diagram&rsquo;s colours are written into the drawing code, and the "nothing was looked up" box carries its colour inline, so neither of them can ever be reached by changing a palette. Contrast was measured rather than judged: every piece of text in the panel clears the AA threshold with room to spare, and the borders, the spine rail and the gauge track were each brightened until they clear the separate, stricter bar that applies to a line carrying meaning. That last part is a real change and not a formality &mdash; a hairline that reads fine at 1.2:1 on white is simply not there on black.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The step where Cal decides what a message is about is now shown.</b> The chain jumped straight from the question to the observation, which left the most important join unexplained: <i>how</i> did a sentence become a decision to go and read a particular weather station? It is not a model, and it is not clever — Cal matches <b>plain words</b>. One strong word such as <code>temperature</code> or <code>heat index</code> is enough on its own; failing that it takes two weather words together, or one plus a question mark. The trace now shows that as its own step, including <b>which words actually matched</b> and which of those three rules fired. This is the exact place a defect hid on 2026-08-11: "whats the heat index?" matched nothing, so the weather capability never ran at all, and there was nothing on the page that could have shown why. The reason is recorded by the same call that makes the decision, so what is displayed and what happened cannot drift apart. Older exchanges predate the field and say so rather than guessing.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>A fixed reply no longer claims a model wrote it.</b> When Cal refuses a forecast question, or cannot reach the weather service, it sends a sentence written into the software and no model runs at all — but the record named one anyway, so the trace would have credited it. The model is now recorded only when it actually ran, the box is labelled <b>what Cal sent</b> rather than what the model wrote, and the two fixed cases stopped sharing one status: a deliberate refusal and a failed fetch are different events, and calling both "weather unavailable" made the design working look like something breaking.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The top of the trace was telling people their message had failed.</b> It drew the sender's own words struck through, with <b>NOT SENT</b> beside them. That was meant to say "not forwarded to the model" — but to anyone who did not already know how this works it reads as <i>your message did not send</i>, which is the opposite of the truth: it arrived perfectly, and it is the very thing that caused the lookup to happen. The picture also never showed where the fetched fact came from, so the reply appeared out of nowhere with no visible connection to the question. It is now a numbered chain read left to right — the question arrives and <b>chooses what to look up</b>, the software fetches a real observation, and only that observation crosses a marked line into the model. Nothing is struck through, because nothing was discarded. The clever part of the old drawing was the curved wires; they were sophistication in the service of a layout that misled, and a straight line that reads correctly beats a bezier that does not.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>v3 — the trace is drawn with depth.</b> Same record and the same claims as <code>old-2</code>, drawn as a mechanism that runs rather than a list that sits. The two connectors are now <b>measured</b> from real box geometry and curve so they actually land on the reply, with the cut drawn as a genuine break in one of them. Surfaces carry elevation: the panel is a recessed well, the fetched fact and the reply sit on raised planes, and the message that was never forwarded is flat and unlit — so the hierarchy is visible rather than announced. The signal stopped being two bare numbers: <b>rssi</b> and <b>snr</b> are drawn against the range a LoRa link actually lives on, which is how you can see at a glance that this message arrived strong, and why it was heard direct. And the stages now <b>assemble in the order they ran</b> when a trace is opened, once per open — a trace records something that happened in time, and drawing it as furniture was the flattest thing about it. The colour ramp under the instruments is a single hue by rule: an ordered quantity gets one hue light to dark, never a red-amber-green rainbow, which is both a rainbow ramp and a pair that sits about 1.5 units apart under the commonest form of colour blindness.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The trace shows what happened instead of listing it.</b> It was thirteen rows of grey label and value, which gave a passed check, a station reading and the words that went out over the air exactly the same weight — and buried the one thing a trace is actually about, which is the order things happened in. Two changes. <b>The reply is now drawn as what it is:</b> two things compete to become the answer, the sender's own words and a fact the software fetched, and on a capability answer the sender's words are visibly <b>cut</b> — they select which fact to look up and are never handed to the model. When there is no capability the same picture inverts honestly: the message is quoted to the model and nothing is cut. <b>Below it the stages run down a spine in the order they happen</b> — received, gated, sanitized, grounded, narrated, sent. A message that fails a check stops the spine where it failed, the rail below it goes dashed, and the stages it never reached are drawn unreached rather than left out. That is read off the record, not assumed: a gated-out decision carries no sanitizer result, no fact, no model and no destination. Two numbers now have a scale under them rather than standing alone — how old the weather reading was against the hour these stations report on, and how long generation took against the <b>7-44 s</b> the same prompt was measured spanning. A closed trace is also no longer built at all, so opening one costs the work rather than every message paying it every three seconds.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The page is light now.</b> Same information and the same layout, on a light palette instead of the dark one it launched with. Two colours had to be re-picked rather than reused: the green and amber that read clearly against a dark background land near a third of the required contrast on a white one, so they are now darker shades of the same hues. The link diagram needed a pass of its own — its colours are written into the drawing code rather than read from the page palette, so swapping the palette alone would have left dark boxes and dark labels sitting on a white card, which is exactly the kind of change that looks finished until someone opens a trace. <b>The retired version at <code>old-1</code> is deliberately still dark.</b> It is kept as a record of what the page used to be, and restyling it would make that record wrong.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Two capture bugs, and a caption that confidently explained one of them wrongly.</b> Every message received since 2026-08-09 was showing "hops unknown — this message predates routing capture". The messages did not predate anything: the hop count is <i>hop_start</i> minus <i>hop_limit</i>, and the radio library builds its packet view with a converter that omits any number equal to zero — so a message that used its <b>entire</b> hop budget arrived with <i>hop_limit</i> missing and was recorded as "no data", indistinguishable from a message that carried no routing at all. The most-relayed messages were the ones being thrown away. Worse was the caption: one asserted cause printed for a blank that has several. It now states only what the record supports, and older messages that genuinely predate the feature still say so.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Cal can tell you the heat index.</b> Asked for "current temperature and heat index", Cal answered the temperature, said nothing about the other half, and gave no sign anything had been left out — while the weather service was publishing a <b>107&deg;F</b> heat index against <b>95&deg;F</b> air in the very same reading. The software had never looked at the field. Heat index and wind chill are now included whenever they differ from the air temperature by at least 3&deg;F, and when they do they take the place of wind in the reply: at a twelve-degree gap, how hot it feels <i>is</i> the weather, and a five-to-seven word message cannot carry both. If the value ever arrives in a unit the software does not recognise it is dropped rather than converted on a guess — read as Fahrenheit instead of Celsius, that 107 becomes "42F" on a 95-degree afternoon. Checked by running it: eight replies, both numbers survived all eight times.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Reply time is no longer shown as if it were thinking time.</b> Each exchange prints how long generation took, and a reader reasonably takes that as a measure of the model. It mostly is not. Measured here: a <i>one-token</i> reply through the same locked-down command costs <b>5.4–10.5 s</b>, while a full seven-word weather reply costs <b>7–44 s</b> — the <i>same prompt</i> varying about sixfold run to run. The floor is process startup and a network round trip; the spread is noise; the part attributable to composing seven words is small. The figure now carries that context instead of standing alone. Consequence worth stating plainly: choosing a larger model would be close to invisible in these numbers, because the time is not going where it looks like it is going.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Link diagram redrawn.</b> Two things it got wrong. It drew a single "relay" box no matter how far a message had travelled, which quietly implied the whole path was known — a hop is a rebroadcast, so three hops means three relays stood in between and the firmware only ever names the last one. The relays it cannot name are now drawn dashed and counted, so the picture shows the size of what it does not know. And every sentence moved out of the drawing into the rows beneath it: a drawing has a fixed canvas and its text neither wraps nor shrinks, so the caption had been clipped at both ends and the signal reading was painting over the node it pointed at. The drawing now holds boxes and arrows only, at one fixed scale, so a message that went three hops and one that went direct are drawn the same size.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Three messages got their hop count back.</b> Once <i>hop_start</i> is known the arithmetic is forced, so records caught by the bug above were recovered rather than left blank — and they are labelled <b>recovered</b>, because reconstructed after the fact is not the same kind of fact as measured at the time. A worked example, all of it from the message the operator remembered sending from far away: he was right that it did not reach Cal directly. It spent its whole budget of <b>3 hops</b>, and the last relay's one-byte id (<code>·c6</code>) matches exactly one node — Cal's own listener across the house. The signal is the giveaway: that listener heard the sender at <b>−126 dBm</b> and barely caught it, while Cal heard the same message at <b>−50 dBm</b>, because Cal was hearing the relay, not the sender. Signal strength describes the last leg only, never the distance to whoever spoke.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Cal was dropping the sender's ID on first contact.</b> The library resolves a node's <code>!id</code> through its list of known nodes and returns nothing for a node it has not yet been introduced to — while the packet itself carries that node's number the entire time. So the ID went missing exactly when a stranger spoke to Cal for the first time. Measured here: a "Hi" on 2026-08-11 was logged from nobody; the sender's introduction arrived eleven minutes later and it was <code>!ba0cc0c0</code> all along. The bridge now falls back to the number the packet carries. Node IDs remain unauthenticated and spoofable — that has not changed and cannot.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Forecast questions are now refused, not answered.</b> Asking about tonight, tomorrow, or whether it's going to rain used to return <i>current</i> conditions — a present-tense reading dressed as a prediction. Cal now recognises a forecast-shaped question deterministically and replies "Only current conditions, no forecast yet," making no lookup at all.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>FAQ rewritten and grouped.</b> It assumed you already knew what a mesh network, Meshtastic and LoRa were, and never said why an AI on a radio is worth building. It now starts from those, explains how the project got here in stages, and links out to the source and to the design proposals — including the arguments that lost.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Link diagram in every trace.</b> Shows the path a message took — sender, any relay, Cal HT — with the hop count and the signal on the last leg. The bridge now records per-message routing (hops taken, and the one-byte relay id when the firmware supplies it); messages received before that show "hops unknown" rather than implying they were direct. It is a topology diagram, not a geographic one, on purpose — see the FAQ.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>v2 is now the main page.</b> The previous two-column layout is retired but still readable at <code>old-1</code>. Retired versions keep a permanent <code>old-N</code> address — numbered by when they were retired, never renumbered — so a link to one always shows the same page.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>v2.</b> Inbound and Outbound merged into a single <b>Exchanges</b> stream — the ask is the head, Cal's reply is indented beneath it. Removes the duplication that made v1 busy (every reply used to render twice) and reads properly on a phone.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Decision trace.</b> Every exchange opens into the machinery behind it: the gate ladder, what the sanitizer changed, the capability and the exact injected fact, the model and generation time. Deliberately no model "reasoning" — see the FAQ.</div>
      <div class="ci"><span class="cd">2026-08-09</span>Inbound/Outbound paired, reply latency in seconds, and the battery tile made sentinel-aware (a reading over 100 means external power, not a charge).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Cal HT moved to <b>WiFi</b>: reflashed to the BaseUI firmware and switched the bridge to TCP — the radio runs untethered, USB is just power.</div>
      <div class="ci"><span class="cd">2026-08-08</span>From Bob's PR: message latency tracking and an /api/stats endpoint with daily aggregates.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Second security &amp; privacy audit: device MAC removed from the public API, DoS bounds, log rotation.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Published as a public GitHub repo; per-neighbor 1-hour SNR sparklines (idea from Bob).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 2 — autonomous responder: Cal replies on its own when addressed (fleet-only, kill switch, rate limits).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 1 — always-on bridge; node flashed to Meshtastic 2.7.26 and brought online as "Cal HT".</div>
    </div>
  </div>
</main>
<footer>cal-mesh dashboard v4 · auto-refresh 3s · read-only · previous version: <a class="faqlink" id="oldlink2" href="old-3">old-3</a></footer>
<script>
const $=s=>document.querySelector(s);
const DIR=(function(){let p=location.pathname.replace(/\/(v2|v3|v4|old-\d+)\/?$/,'/');
 return p.endsWith('/')?p:p+'/';})();
let SNR={}, lastNodes=[], nodeSort={key:null,dir:1}, lastXsig=null;
let ROUTES={me:null,ours:{},others:[]};
let SELF={id:null,name:null};
const NODE_LABELS={short:'Short',long:'Name',hw:'HW',hops:'Hops',snr:'SNR'};
function esc(s){return (s??"").toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hhmmss(ts){try{return new Date(ts).toLocaleTimeString();}catch(e){return ts;}}
function daystamp(ts){try{return new Date(ts).toLocaleDateString(undefined,{month:'short',day:'numeric'});}catch(e){return '';}}
function secs(ms){return (ms/1000).toFixed(2)+'s';}
function tile(k,v,sub){return `<div class="tile"><div class="k">${k}</div><div class="v">${v}${sub?` <small>${sub}</small>`:''}</div></div>`;}
function fmtDur(s){s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
 return h?`${h}h ${m}m`:(m?`${m}m ${ss}s`:`${ss}s`);}
function batteryLabel(m){ if(m.battery==null) return '—';
  if(m.battery>100) return 'ext power';   // Meshtastic sentinel, not a charge level
  return m.battery+'%'; }
function skipWhy(r){
  const m={sender_not_allowed:'sender is not on the allow-list — Cal heard it perfectly well and chose not to answer',
           not_addressed:'Cal was not addressed (no "cal" mention, not a DM)',
           disabled:'the responder kill switch is off',
           too_old:'the message was older than the freshness window',
           rate_limited:'rate limit reached for this sender',
           cooldown:'per-sender cooldown still active',
           self:'this was Cal\'s own message'};
  return m[r]||esc(r||'unknown');
}
function verdictTag(x){
  if(x.verdict==='replied') return '<span class="tag tx">REPLIED</span>';
  if(x.verdict!=='skipped') return '<span class="tag quiet">NOT EVALUATED</span>';
  return x.reason==='sender_not_allowed'
    ? '<span class="tag offlist">OFF-LIST · heard, not answered</span>'
    : `<span class="tag quiet">NO REPLY · ${esc(x.reason)}</span>`;
}
function row(k,v){return `<div class="trow"><span class="tk">${k}</span><span class="tv">${v}</span></div>`;}
function nodeName(id){
  const n=lastNodes.find(n=>n.id===id);
  return n?(n.short||n.long||id):id;
}
// Clamp a label to what a fixed-width box can actually hold. SVG text does not wrap and does
// not shrink: an over-long node name silently paints across its own border and its neighbour.
function fitLabel(s, n){ s=(s==null?'':String(s)); return s.length>n ? s.slice(0,n-1)+'…' : s; }

// A LINK diagram, deliberately not a geographic one: who transmitted, who relayed it, who
// received it. It uses only what is already public on the air — there are no coordinates here
// and none are stored, because this page is public and the base station sits at a fixed
// private location.
//
// TWO LAYOUT RULES, both learned by shipping the violation (2026-08-11):
//
//   1. NO PROSE INSIDE THE SVG. A viewBox is a fixed canvas and <text> neither wraps nor
//      reflows, so a caption long enough to be worth reading gets clipped at BOTH ends — which
//      is exactly what happened when the caption grew to explain the relay byte. Every sentence
//      now lives in HTML underneath, in the same key/value rows the rest of the trace uses, so
//      it wraps and can never be truncated. The SVG holds boxes and arrows and nothing else.
//   2. NOTHING FLOATS BETWEEN THE BOXES. The old signal label sat at the arrow midpoint, and
//      once a third box appeared the gaps were narrower than the label — it painted over the
//      node it was pointing at. Signal is a fact about the last hop, so it is stated as such
//      in the rows below rather than squeezed into the gap.
// A harvested traceroute path, drawn as what it is: a SEPARATE measurement taken at its own
// moment, not a better version of this message's own diagram. Backfilling it into that diagram
// was the tempting move and it would be a fabrication -- a path is true only when it is
// measured, and a message that arrived two hops ago did not necessarily take the route a
// traceroute found four minutes later. So it sits below, with its own timestamp and age, and
// it says outright that it is not this message's path.
//
// Only paths where CAL was the requester are drawn here. An overheard traceroute between two
// other nodes is real topology but says nothing about how Cal reaches anybody; the server
// separates the two and this reads only `ours`.
function pathAge(ts){
  const t=Date.parse(ts); if(!isFinite(t)) return null;
  const s=Math.max(0,(Date.now()-t)/1000);
  if(s<90) return Math.round(s)+' s ago';
  if(s<5400) return Math.round(s/60)+' min ago';
  if(s<172800) return Math.round(s/3600)+' h ago';
  return Math.round(s/86400)+' days ago';
}
function chain(nodes, snrs, complete){
  // One SNR per LINK, in order, exactly as the firmware fills it. When the array is not one
  // entry per link it is NOT stretched to fit -- a missing reading is drawn missing.
  let h='<div class="pchain">';
  nodes.forEach((n,i)=>{
    h += (n==null) ? '<span class="phop unk">unnamed</span>'
                   : '<span class="phop">'+esc(nodeName(n))+'</span>';
    if(i<nodes.length-1){
      const v = (complete && snrs && snrs.length>i) ? snrs[i] : null;
      h+='<span class="plink"><span class="parr"></span>'
       + '<span class="psnr">'+(v==null?'? dB':(v>0?'+':'')+esc(v)+' dB')+'</span></span>';
    }
  });
  return h+'</div>';
}
function pathHtml(nodeId){
  const r = (ROUTES.ours||{})[nodeId];
  if(!r || !r.path || r.path.length<2) return '';
  const age = pathAge(r.ts);
  const back = [r.traced].concat(r.route_back||[], [r.requester]);
  const hasBack = r.snr_back_complete && r.snr_back && r.snr_back.length===back.length-1;
  return '<div class="pathm"><div class="pathh">measured path to this node'
    + (age?' <span class="pathage">&middot; traceroute '+esc(age)+'</span>':'')
    + '</div>'
    + '<div class="pdir">out</div>' + chain(r.path, r.snr_towards, r.snr_towards_complete)
    + (hasBack ? '<div class="pdir">back</div>' + chain(back, r.snr_back, true) : '')
    + '<span class="hint">A traceroute Cal sent and got an answer to, so every hop is named '
    + 'rather than counted. <b>This is not this message&rsquo;s path</b> &mdash; it was measured '
    + 'at its own moment, and a route is only true when it is measured. The two directions are '
    + 'listed separately because they are measured separately and often differ.</span></div>';
}
function linkSvg(x){
  const hops=x.hops;
  const relayId = x.relay_byte!=null ? '·'+x.relay_byte.toString(16).padStart(2,'0') : null;
  // Colour the sender box by what Cal DID with it, so the diagram carries the same signal as
  // the verdict badge above. Green on an off-list sender read as "allowed" — backwards.
  const offlist = x.verdict==='skipped' && x.reason==='sender_not_allowed';
  const quiet   = x.verdict==='skipped' && !offlist;
  // v4: these are the colours the 2026-08-12 light switch had to re-pick by hand, for the
  // same reason — they live here, not in the sheet, so a palette change alone never reaches them.
  const senderC = offlist ? {fill:'#3a2d0a', stroke:'#e3b341'}      // matches the OFF-LIST tag
                : quiet   ? {fill:'#1c222b', stroke:'#7d8590'}
                          : {fill:'#1c222b', stroke:'#3fb950'};
  const stops=[x.from
    ? {lab:esc(fitLabel(nodeName(x.from),15)), sub:esc(fitLabel(x.from,15)), fill:senderC.fill, stroke:senderC.stroke}
    : {lab:'unknown', sub:'no id recorded', fill:senderC.fill, stroke:senderC.stroke}];
  // A hop is a REBROADCAST, so N hops means N relays stood between the sender and Cal — and the
  // firmware only ever tells us the last one. Drawing a single relay box for N>1 quietly implied
  // we knew the whole path. The ones we cannot name are now counted and drawn dashed, so the
  // diagram shows the size of what it does not know instead of hiding it.
  if(hops==null) stops.push({lab:'?', sub:'routing not recorded', dim:true, dash:true});
  if(hops>1) stops.push({lab:'?', sub:(hops-1)+' unknown relay'+(hops-1>1?'s':''), dim:true, dash:true});
  if(hops>0) stops.push({lab:'relay'+(relayId?' '+relayId:''), sub:relayId?'last relay':'id not reported', dim:true});
  stops.push({lab:esc(fitLabel(SELF.name||'Cal HT',15)), sub:esc(fitLabel(SELF.id||'',15)), self:true});

  // The canvas is a CONSTANT width sized for the widest case (4 boxes) and the row is centred
  // inside it. Sizing the viewBox to the content instead makes the SVG scale up to the CSS
  // width, so a two-box diagram renders with boxes nearly twice the size of a four-box one —
  // the same message looks like a different kind of object depending on how far it travelled.
  const n=stops.length, bw=140, gap=38, by=10, bh=50, MAXN=4;
  const W=20+MAXN*bw+(MAXN-1)*gap, H=by+bh+10;
  const x0=(W-(n*bw+(n-1)*gap))/2;
  let svg='';
  stops.forEach((s,i)=>{
    const bx=x0+i*(bw+gap);
    if(i>0){
      const x1=bx-gap+2, x2=bx-4;
      svg+=`<line x1="${x1}" y1="${by+bh/2}" x2="${x2-6}" y2="${by+bh/2}" stroke="#7d8590" stroke-width="2"/>`
         +`<path d="M${x2-7} ${by+bh/2-4.5} L${x2} ${by+bh/2} L${x2-7} ${by+bh/2+4.5}z" fill="#7d8590"/>`;
    }
    const fill=s.fill||(s.self?'#221a35':'#1c222b');
    const stroke=s.stroke||(s.self?'#bc8cff':(s.dim?'#7d8590':'#3fb950'));
    svg+=`<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="8" fill="${fill}" stroke="${stroke}" `
       +`stroke-width="1.5"${s.dash?' stroke-dasharray="5 4"':''}/>`
       +`<text x="${bx+bw/2}" y="${by+21}" fill="#e6edf3" font-size="13" font-weight="600" text-anchor="middle">${s.lab}</text>`
       +`<text x="${bx+bw/2}" y="${by+37}" fill="#9aa7b4" font-size="10.5" text-anchor="middle">${s.sub}</text>`;
  });
  const diagram=`<div class="link-d"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" `
       + `role="img" aria-label="link diagram">${svg}</svg></div>`;

  // A null hop count has more than one cause and they are not interchangeable. Records written
  // before routing capture shipped (2026-08-09) carry no hop_start KEY AT ALL; records written
  // after always carry the key, even when its value is null. Saying "predates routing capture"
  // for both put a false claim about a message's history on a public page.
  let rows='';
  if(hops==null)
    rows+=row('path', x.hop_start===undefined
      ? 'unknown — this message predates routing capture'
      : 'unknown — no usable hop count was recorded for this message');
  else if(hops===0)
    rows+=row('path','direct — Cal heard the sending radio itself, with no relay in between');
  else{
    rows+=row('path', hops+' hop'+(hops>1?'s':'')+' — relayed'
      +(hops>1?', and only the last relay is identified':''));
    if(relayId)
      rows+=row('last relay','id ends <code>'+esc(relayId)+'</code> — one byte of the node number that '
        +'relayed it, which narrows the candidates but does not identify a node');
  }
  // The signal belongs to the LAST hop and nothing else. Stating that plainly matters: a message
  // relayed from close by arrives strong no matter how far the sender is, and reading it as
  // "nearby" is the natural mistake.
  // Two numbers told a stranger nothing. Drawn against the range a LoRa link actually lives on,
  // -41 dBm is visibly near the strong end — which is the finding, and why this arrived direct.
  if(x.snr!=null||x.rssi!=null){
    let g='';
    if(x.rssi!=null) g+=gauge('signal · rssi',esc(x.rssi)+' dBm',(x.rssi+120)/90*100,
      ['-120 weak','-30 strong']);
    if(x.snr!=null) g+=gauge('signal · snr',(x.snr>0?'+':'')+esc(x.snr)+' dB',(x.snr+20)/30*100,
      ['-20 dB','+10 dB']);
    rows+=`<div class="inst">${g}</div>`
      +`<span class="hint">Measured on the <b>last hop only</b>`
      +(hops>0?' — which came from the relay, not the sender, however far away the sender was.'
              :', and this one was direct, so it does describe the sender.')+`</span>`;}
  // A recovered count was reconstructed after the fact, not measured at capture. It is sound —
  // the arithmetic is forced once hop_start is known — but it is not the same kind of fact, and
  // the page should not blur the two.
  if(x.hops_recovered)
    rows+=row('note','hop count recovered from a record predating the capture fix — reconstructed, not measured at the time');
  rows += pathHtml(x.from);
  return {diagram:diagram, rows:rows, summary:(
    hops==null?'routing not recorded'
    :hops===0?'heard direct, no relay in between'
    :hops+' hop'+(hops>1?'s':'')+' — arrived by relay')};
}
// The reply is composed from a fact the harness fetched, and on a capability answer the
// sender's own words are never handed to the model at all. That is the single least obvious
// thing about this system and it was previously one clause inside a grey row. Drawn instead:
// two inputs compete to become the reply, and one of them is visibly cut.
function flowHtml(x,t){
  const inTxt=esc(x.text||''), outTxt=esc(x.reply||'');
  if(!outTxt) return '';
  // What the software MATCHED, what it actually GOT, and whether a model ran are three
  // different things. Collapsing them is what made a refused forecast claim the model was
  // handed the message.
  const capability=!!(x.capability||(t.trigger_match&&t.trigger_match.via));
  const fetched=!!t.injected_fact;
  const modelRan=!!t.model;
  const crosses=fetched&&modelRan;
  // A DM lands on one screen, not every node in range, so the 5-7 word rule does not apply to it.
  const isDM=!!(t.dest&&t.dest.charAt(0)==='!');
  const arrow=(label)=>`<span class="arw"><span>${label}</span></span>`;
  const b1=`<div class="fb b1"><div class="fk">1 · the question</div>`
    +`<div class="fv">${inTxt}</div>`
    +`<div class="fn"><span class="onair">✓ received on air</span> — and it is what decided `
    +`${capability?'which fact to look up':'how to reply'}</div></div>`;
  const lastN=capability?4:2;
  const b3=`<div class="fb b3"><div class="fk">${lastN} · ${modelRan?'what the model wrote':'what Cal sent'}</div>`
    +`<div class="fv">${outTxt}</div>`
    +`<div class="fn"><span class="onair">✓ sent on air to ${esc(t.dest||'')}</span> — `
    +(modelRan?(isDM?'a sentence or two — a direct message lands on one screen, not every node in range'
                    :'5-7 words, because every node in range shares the airtime')
             :'a fixed sentence written into the software — no model ran for this one')+`</div></div>`;
  // A greeting ack is a THIRD shape, and both of the branches below would misdescribe it.
  // The capability branch is weather-shaped ("what Cal looked up"); the general branch says
  // the model was handed the message. Here nothing was fetched AND no model ran: plain word
  // matching selected a sentence written in advance. Drawn as exactly that.
  if(x.capability==='greeting'){
    const g1=`<div class="fb b1"><div class="fk">1 · what they said</div><div class="fv">${inTxt}</div>`
      +`<div class="fn"><span class="onair">✓ received on air</span> — from a node that is `
      +`<b>not on Cal's reply list</b>, so no answer was generated for it</div></div>`;
    const g2=`<div class="fb bx"><div class="fk">2 · what the software recognised</div>`
      +`<div class="fv">a greeting, and nothing else</div>`
      +`<div class="fn">the whole message had to be a greeting — a question mark or a real `
      +`request and this does not fire — plain word matching, <b>no model involved</b></div></div>`;
    const g3=`<div class="fb b3"><div class="fk">3 · what Cal sent</div><div class="fv">${outTxt}</div>`
      +`<div class="fn"><span class="onair">✓ sent on air to ${esc(t.dest||'^all')}</span> — the `
      +`greeting mirrored back, and only once per node per day</div></div>`;
    return `<div class="flow gen">${g1}${arrow('')}${g2}${arrow('')}${g3}</div>`
      +`<div class="flowcap">Read left to right. <b>Nothing was looked up and no model ran.</b> `
      +`Cal answers questions only from known nodes, but staying silent when a stranger says `
      +`hello reads as a snub — so a greeting gets one back, to say it was heard. Which line `
      +`goes out is <b>chosen</b> by the greeting they used, from five written in advance `
      +`(morning, afternoon, evening, day, or plain hello). Nothing they wrote is ever copied `
      +`into it, so there is nothing in the reply for a stranger to steer.</div>`;
  }
  // A computed answer is a FOURTH shape. The capability branch below is weather-shaped and
  // would say "what Cal looked up" and, with no injected_fact, "the lookup failed — the weather
  // service could not be reached" — for a reply that never touched the network. Nothing is
  // fetched here and no model runs: Python parsed the question and computed every digit.
  if(x.capability==='sunmoon'){
    const sm=t.sunmoon||{}, smm=t.sunmoon_match||{};
    const intent=sm.intent?String(sm.intent):'', ev=sm.event?String(sm.event):'';
    const refused=sm.refused?String(sm.refused):'';
    const words=[].concat(smm.sun||[],smm.moon||[]).join(', ');
    const s1=`<div class="fb b1"><div class="fk">1 &middot; the question</div><div class="fv">${inTxt}</div>`
      +`<div class="fn"><span class="onair">&#10003; received on air</span> — the wording `
      +`${words?('matched <b>'+esc(words)+'</b>, which'):'matched sun/moon wording, which'} `
      +`selected this path</div></div>`;
    const s2=`<div class="fb bx"><div class="fk">2 &middot; what the software recognised</div>`
      +`<div class="fv">${esc(intent||'a sun/moon question')}</div>`
      +`<div class="fn">the wording is classified only to <b>choose which fact to compute</b>, `
      +`never to shape the sentence</div></div>`;
    const s3=`<div class="fb bx"><div class="fk">3 &middot; what Cal computed</div>`
      +`<div class="fv">${refused?esc('refused: '+refused):esc(ev||'closed-form astronomy')}</div>`
      +`<div class="fn"><b>nothing was fetched and no model ran.</b> Sun and moon positions are `
      +`computed from closed-form astronomy — no network, so this answer works when the base is `
      +`offline. Measured against 43 U.S. Naval Observatory times: worst error 43 seconds. `
      +`No coordinate appears in the reply; the observing point is an input, never an output.`
      +`</div></div>`;
    const s4=`<div class="fb b3"><div class="fk">4 &middot; what Cal sent</div><div class="fv">${outTxt}</div>`
      +`<div class="fn"><span class="onair">&#10003; sent on air to ${esc(t.dest||'')}</span></div></div>`;
    return `<div class="flow gen">${s1}${arrow('')}${s2}${arrow('')}${s3}${arrow('')}${s4}</div>`
      +`<div class="flowcap">Read left to right. <b>Nothing was looked up and no model ran.</b> `
      +`Python computes the time and formats the sentence. Where the event does not occur at all `
      +`— a polar day, or a twilight the sun never reaches — Cal says which one is missing rather `
      +`than reporting the nearest thing it could calculate. Moonrise and moonset are not built `
      +`yet, and are refused rather than estimated.</div>`;
  }
  if(x.capability==='calc'){
    const handler=(t.calc&&t.calc.handler)?String(t.calc.handler):'';
    const c1=`<div class="fb b1"><div class="fk">1 · the question</div><div class="fv">${inTxt}</div>`
      +`<div class="fn"><span class="onair">✓ received on air</span> — it parsed as a `
      +`calculation, which is what selected this path</div></div>`;
    const c2=`<div class="fb bx"><div class="fk">2 · what the software recognised</div>`
      +`<div class="fv">${esc(handler||'a calculation')}</div>`
      +`<div class="fn">a <b>successful bounded parse</b>, not merely a number in the text — `
      +`anything that does not parse gets no answer at all</div></div>`;
    const c3=`<div class="fb bx"><div class="fk">3 · what Cal computed</div>`
      +`<div class="fv">Python, from exact constants</div>`
      +`<div class="fn"><b>nothing was fetched and no model ran</b> — the digits are computed `
      +`and formatted by the software itself</div></div>`;
    const c4=`<div class="fb b3"><div class="fk">4 · what Cal sent</div><div class="fv">${outTxt}</div>`
      +`<div class="fn"><span class="onair">✓ sent on air to ${esc(t.dest||'')}</span></div></div>`;
    return `<div class="flow gen">${c1}${arrow('')}${c2}${arrow('')}${c3}${arrow('')}${c4}</div>`
      +`<div class="flowcap">Read left to right. <b>Nothing was looked up and no model ran.</b> `
      +`The model is not in the number path at all — Python parses the question, computes the `
      +`answer from exact defined constants, and formats the sentence. Where a value is `
      +`ambiguous (a gallon is not the same on both sides of the Atlantic) or falls outside `
      +`what can be answered exactly, Cal says nothing rather than guess.</div>`;
  }
  if(!capability){
    // An authenticated DM from Dean is the same "no lookup, model ran" shape, but the model did
    // NOT see only the message — the harness also injected Cal's saved context and, when present,
    // the remembered thread. Saying "the message itself" here would be the same collapse that
    // once made a refused forecast claim the model was handed the message.
    if(t.dm_unlock){
      const mem=t.dm_memory_stored?' and the recent messages it remembers':'';
      return `<div class="flow gen">${b1}${arrow('given to the model with<br>Cal\'s saved context')}${b3}</div>`
        +`<div class="flowcap">This is an <b>authenticated direct message from Dean</b>, so the model was `
        +`given the message <b>plus Cal&rsquo;s saved context${mem}</b> — which is why the reply can be `
        +`longer and carry a thread. The context is the operator&rsquo;s public file; no secret crosses.</div>`;
    }
    return `<div class="flow gen">${b1}${arrow('sanitized, then given<br>to the model')}${b3}</div>`
      +`<div class="flowcap">Nothing was looked up for this one, so the model was given `
      +`<b>the message itself</b> and wrote a reply from it.</div>`;
  }
  // The step between the question and the lookup: plain word-matching that decides WHICH
  // capability runs. No model is involved, and it is where a 2026-08-11 defect hid — a question
  // that matched nothing never reached the capability at all, with nothing on the page to say so.
  const tm=t.trigger_match||null;
  let why='this record predates Cal keeping the matched words, so they cannot be shown', chips='';
  if(tm){
    const words=(tm.strong&&tm.strong.length?tm.strong:tm.weak)||[];
    chips=words.map(w=>`<span class="chip">${esc(w)}</span>`).join('');
    why = tm.via==='strong'
        ? (words.length>1?'any one of these is enough on its own'
                         :'this word is enough on its own')
        : tm.via==='two_weak'
          ? 'two weather words together'
          : 'one weather word plus a question mark';
  }
  const bx=`<div class="fb bx"><div class="fk">2 · what the software recognised</div>`
    +`<div class="fv">a weather question${t.forecast_asked?' about the <b>future</b>':''}</div>`
    +`<div class="fn">${chips}${chips?'<br>':''}${why} — plain word matching, `
    +`<b>no model involved</b></div></div>`;
  const st=t.obs_station?esc(t.obs_station):null;
  const age=t.obs_age_s!=null?Math.round(t.obs_age_s/60)+' min old':null;
  const warn='border-color:#8a6d1f;background:linear-gradient(180deg,#2a2213,#1f190e)';
  const b2=t.forecast_asked
    ? `<div class="fb b2" style="${warn}">`
      +`<div class="fk">3 · what Cal looked up</div><div class="fv">nothing</div>`
      +`<div class="fn">Cal holds current observations only, so a question about later is refused `
      +`outright — no lookup was attempted at all</div></div>`
    : !fetched
      ? `<div class="fb b2" style="${warn}">`
        +`<div class="fk">3 · what Cal looked up</div><div class="fv">the lookup failed</div>`
        +`<div class="fn">the weather service could not be reached, so Cal sent a fixed sentence `
        +`rather than guess a number — the fail-safe working, not a model deciding</div></div>`
      : `<div class="fb b2"><div class="fk">3 · what Cal's software fetched</div>`
        +`<div class="fv">${esc(t.injected_fact)}</div>`
        +`<div class="fn">a real observation${st?' from station '+st:''}${age?', '+age:''} — fetched `
        +`from the US National Weather Service by the software, never by the model</div></div>`;
  const join=crosses
    ? `<span class="cross"><span class="bl">only this crosses</span>${arrow('')}</span>`
    : arrow('');
  const cap=crosses
    ? `Read left to right. The question arrived fine and did real work — <b>its wording is what chose `
      +`the lookup</b> — but it never reached the model. Cal&rsquo;s software matched the words, went and `
      +`got the observation, and <b>only that observation</b> crossed the dashed line. The model&rsquo;s `
      +`entire job was to put it into words, which is why it cannot invent a temperature.`
    : `Read left to right. The question arrived fine and did real work — <b>its wording is what Cal `
      +`matched on</b> — but nothing was looked up, so <b>no model ran at all</b>. What went out is a `
      +`fixed sentence written into the software. There is no boundary drawn here because nothing `
      +`crossed one.`;
  return `<div class="flow">${b1}${arrow('reads')}${bx}${arrow(crosses?'so it<br>fetches':'so it<br>stops')}`
    +`${b2}${join}${b3}</div><div class="flowcap">${cap}</div>`;
}
function gauge(name,val,pct,ends,band,note){
  return `<div class="gauge"><div class="glab"><span class="gname">${name}</span>`
    +`<span class="gval">${val}</span></div>`
    +`<div class="track${band?'':' ramp'}">`
    +(band?`<span class="band" style="left:${band[0].toFixed(1)}%;width:${band[1].toFixed(1)}%"></span>`:'')
    +(Number.isFinite(pct)
       ? `<span class="mk" style="left:${Math.max(0,Math.min(100,pct)).toFixed(1)}%"></span>`
       : '')+`</div>`
    +`<div class="gends"><span>${ends[0]}</span><span>${ends[1]}</span></div>`
    +(note?`<div class="gnote">${note}</div>`:'')+`</div>`;
}
function stage(cls,name,summary,detail){
  return `<li class="stg ${cls}"><span class="sdot"></span>`
    +`<div class="shead"><span class="sname">${name}</span><span class="ssum">${summary}</span></div>`
    +`<div class="sdet">${detail||''}</div></li>`;
}
// The stages are a sequence in time and a gated-out message genuinely never reaches the later
// ones — verified against the records: a skipped decision carries no sanitize, no fact, no
// model and no destination. So "never reached" is read off the record, not assumed.
function spineHtml(x,t){
  const link=(x.kind==='exchange')?linkSvg(x):null;
  let s='';
  if(link) s+=stage('pass','received',link.summary,link.diagram+link.rows);
  const gated=t.gates&&t.gates.length;
  const stopped=x.verdict==='skipped';
  if(gated){
    const passed=t.gates.filter(g=>g.pass).length;
    s+=stage(stopped?'stop':'pass','gated',
      stopped?`stopped at <b>${esc((t.gates.find(g=>!g.pass)||{}).gate||'a check')}</b>`
             :`all ${passed} checks passed`,
      t.gates.map(g=>`<span class="gate ${g.pass?'gp':'gf'}">${g.pass?'✓':'✗'} ${esc(g.gate)}</span>`).join('')
      +(stopped?'<span class="rungn">later checks never evaluated</span>':''));
  }
  if(!t.model&&stopped){
    s+=stage('skip','not answered','the message was received and recorded, and nothing further ran',
      '<span class="rungn">no text was sent to a model, and nothing went on air</span>');
    return `<ol class="spine">${s}</ol>`;
  }
  if(t.sanitize){const q=t.sanitize,b=[];
    // An older record carries only the boolean and genuinely cannot say WHICH was trimmed. Say
    // that, rather than guessing — and never guess toward "your words were dropped".
    const tk=q.sentence_trim!=null?q.sentence_trim:(q.sentence_trimmed?'unknown':'none');
    if(tk==='content') b.push(`first sentence kept (${q.dropped_chars!=null?q.dropped_chars+' chars':'the rest'} dropped)`);
    else if(tk==='unknown') b.push('something was trimmed from the end — this record predates the '
      +'detail that says whether it was punctuation or content');
    else if(tk==='punctuation') b.push('trailing punctuation trimmed, no content dropped');
    if(q.length_capped) b.push('length capped');
    if(q.redactions) b.push(`${q.redactions} redaction${q.redactions>1?'s':''}`);
    if(q.flagged) b.push('injection-shaped tokens flagged');
    s+=stage('pass','sanitized',`${q.in_chars}&rarr;${q.out_chars} characters`,
      b.length?`<span class="hint">${esc(b.join(' · '))}</span>`:'<span class="hint">nothing removed</span>');}
  if(t.forecast_asked)
    s+=stage('stop','refused','asked about a future condition',
      '<span class="hint">the capability holds current observations only, so a fixed reply was sent '
      +'and no lookup was made at all</span>');
  if(x.capability){
    const ok=t.weather_ok, age=t.obs_age_s;
    let d='';
    if(age!=null){
      d=gauge('reading age',Math.round(age/60)+' min',(age/3600)*100,
        ['just measured','1 h — how often these stations report'],[0,0.001],
        'A real observation from the nearest station, never an estimate for one spot.');}
    const fstate = ok===true?'ok' : (ok===false?'FAILED':'not attempted');
    s+=stage(ok===true?'pass':(ok===false?'stop':'skip'),'grounded',
      `${esc(x.capability)} · fetch ${fstate}`
      +(t.obs_station?` · station <code>${esc(t.obs_station)}</code>`:''), d);}
  if(t.model){
    const ms=x.gen_ms;
    let d='<span class="hint">generation returns plain text — no chain of thought exists to show</span>';
    if(ms!=null){const MAXS=45,sec=ms/1000;
      const weather=t.prompt_kind==='weather';
      d=gauge('generation',secs(ms),sec/MAXS*100,['0 s',MAXS+' s'],
        weather?[7/MAXS*100,(44-7)/MAXS*100]:[0,0.001],
        (weather?'The shaded band is the <b>7-44 s</b> this same prompt was measured spanning, run '
                +'to run. ':'')
        +'Most of it is process startup and a network round trip — an order of magnitude, not '
        +'thinking time.');}
    s+=stage('pass','narrated',`<code>${esc(t.model)}</code>`,d);}
  if(t.gen_status&&t.gen_status!=='ok')
    s+=stage('stop','generation',`<code>${esc(t.gen_status)}</code>`,'');
  if(t.dest) s+=stage('pass','sent',`on air to <code>${esc(t.dest)}</code>`,'');
  return `<ol class="spine">${s}</ol>`;
}
// The trace reads top to bottom as what happened: first the outcome and how it was arrived at
// (the swap), then the machinery stage by stage (the spine). The old flat key/value list gave a
// gate check, a station reading and the transmitted reply the same weight and the same grey
// label, which left the sequence — the only thing the trace is actually about — invisible.
function traceHtml(x){
  const t=x.trace||{};
  if(!t.gates&&!t.sanitize&&!t.model){
    const l=(x.kind==='exchange')?linkSvg(x):null;
    return `<div class="tp">${l?l.diagram+l.rows:''}`+
      '<div class="tnone">No decision trace recorded — this message predates it.</div></div>';}
  let h=flowHtml(x,t)+spineHtml(x,t);
  h+='<div class="tnote">This is the machinery, not the model\'s reasoning. Generation returns plain '
   +'text with no chain of thought, and asking for a narration would produce a plausible story rather '
   +'than an account of what actually happened — so it is not shown.</div>';
  return `<div class="tp">${h}</div>`;
}
// The page re-renders every 3s, which would wipe any <details> the reader had opened. Track
// open traces by a stable key and restore the attribute on every render, so an expanded trace
// stays expanded until it is clicked shut. (Toggle doesn't bubble — the listener captures.)
const OPEN=new Set();
// Lets a trace be built on demand when its disclosure is opened, rather than for every
// exchange on every pass. Rebuilt from the current data each render, so an open trace never
// shows a stale copy of a record that has since changed.
const XBYKEY=new Map();
function xkey(x){return (x.ts||'')+'|'+(x.from||x.dest||'');}
function exchangeHtml(x){
  if(x.kind==='unprompted') return `
    <div class="xc unprompted"><div class="meta"><span class="tag tx">TX</span>
      <span>${daystamp(x.ts)} ${hhmmss(x.ts)}</span><span>→ ${esc(x.dest)}</span>
      <span class="tag ch">ch${esc(x.channel)}</span><span>${esc(x.transport)}</span>
      <span class="tag quiet">${x.source==='responder'?'UNPAIRED':'MANUAL'}</span></div>
    <div class="ask">${esc(x.text)}</div>
    <div class="norep">↳ not a reply — Cal transmitted this with no inbound ask${x.source==='responder'?', or the ask is older than the window shown':''}</div></div>`;
  return `
    <div class="xc"><div class="meta"><span class="tag rx">RX</span>
      <span>${daystamp(x.ts)} ${hhmmss(x.ts)}</span><span>${x.from?esc(x.from):'unknown sender'} → ${esc(x.to)}</span>
      <span class="tag ch">ch${esc(x.channel)}</span>${x.snr!=null?`<span>snr ${esc(x.snr)}</span>`:''}
      ${verdictTag(x)}</div>
    <div class="ask">${esc(x.text)}</div>
    ${x.verdict==='replied'&&x.reply
      ? `<div class="rep"><span class="who">↳ Cal replied${x.gen_ms!=null?` · ${secs(x.gen_ms)}`:''}${x.capability?` · ${esc(x.capability)}`:''}</span><span class="txt">${esc(x.reply)}</span></div>`
      : (x.verdict==='skipped'?`<div class="norep">↳ received, no reply — ${skipWhy(x.reason)}</div>`:'')}
    <details class="tr" data-k="${esc(xkey(x))}"${OPEN.has(xkey(x))?' open':''}><summary>trace</summary>
    <div class="tpwrap">${OPEN.has(xkey(x))?traceHtml(x):''}</div></details></div>`;
}
function setSort(k){ nodeSort=(nodeSort.key===k)?{key:k,dir:-nodeSort.dir}:{key:k,dir:1}; renderNodes(); }
function renderNodes(){
  let ns=lastNodes.slice();
  if(nodeSort.key){ const k=nodeSort.key, dir=nodeSort.dir;
    ns.sort((a,b)=>{ let x=a[k],y=b[k];
      if(k==='hops'||k==='snr'){ if(x==null&&y==null)return 0; if(x==null)return 1; if(y==null)return -1; return (x-y)*dir; }
      x=(x||'').toString().toLowerCase(); y=(y||'').toString().toLowerCase();
      return x<y?-dir:(x>y?dir:0); }); }
  const tb=$('#nodes').querySelector('tbody');
  tb.innerHTML=ns.map(n=>{ const sg=(n.snr!=null&&n.snr>0)?'snr-good':'snr-bad';
    return `<tr><td>${esc(n.short)}</td><td>${esc(n.long)}</td><td>${esc(n.hw)}</td>`+
      `<td>${n.hops==null?'—':esc(n.hops)}</td><td class="${sg}">${n.snr==null?'—':esc(n.snr)}</td>`+
      `<td>${sparkline((SNR[n.id]||{}).points, n.hops)}</td></tr>`; }).join('');
  document.querySelectorAll('#nodes th.sortable').forEach(th=>{
    const k=th.dataset.key, on=nodeSort.key===k;
    th.textContent=NODE_LABELS[k]+(on?(nodeSort.dir>0?' ▲':' ▼'):''); });
}
async function loadSnr(){try{SNR=await (await fetch(DIR+'api/snr',{cache:'no-store'})).json();}catch(e){}}
async function loadRoutes(){try{ROUTES=await (await fetch(DIR+'api/routes',{cache:'no-store'})).json();}catch(e){}}
function sparkline(pts, hops){
  if(!pts||pts.length===0){
    return (hops!=null&&hops>0)?'<span style="color:var(--dim)">multi-hop</span>'
      :'<span style="color:var(--dim)">— <small>no direct signal</small></span>';}
  if(pts.length===1){const v=pts[0][1];
    return `<span class="spark"><svg width="90" height="22"><circle cx="45" cy="11" r="2.5" fill="var(--accent)"/></svg>`+
      `<span style="color:var(--accent)">${esc(v)} <small>dB · 1 pt</small></span></span>`;}
  const W=90,H=22,pad=3;
  const ts=pts.map(p=>p[0]), vs=pts.map(p=>p[1]);
  const t0=Math.min(...ts),t1=Math.max(...ts),vmin=Math.min(...vs),vmax=Math.max(...vs);
  const sx=t=>pad+(t1===t0?(W-2*pad):((t-t0)/(t1-t0))*(W-2*pad));
  const sy=v=>pad+(1-(vmax===vmin?0.5:(v-vmin)/(vmax-vmin)))*(H-2*pad);
  const d=pts.map((p,i)=>(i?'L':'M')+sx(p[0]).toFixed(1)+' '+sy(p[1]).toFixed(1)).join(' ');
  const last=pts[pts.length-1];
  const k=Math.max(1,Math.floor(pts.length/3));
  const avg=a=>a.reduce((s,x)=>s+x,0)/a.length;
  const dv=avg(vs.slice(-k))-avg(vs.slice(0,k));
  const arrow=dv>1.5?'↗':(dv<-1.5?'↘':'→');
  const col=dv<-1.5?'var(--warn)':(dv>1.5?'var(--ok)':'var(--accent)');
  return `<span class="spark" title="${pts.length} samples · now ${esc(last[1])} dB">`+
    `<svg width="${W}" height="${H}"><path d="${d}" fill="none" stroke="${col}" stroke-width="2" `+
    `stroke-linejoin="round" stroke-linecap="round"/><circle cx="${sx(last[0]).toFixed(1)}" `+
    `cy="${sy(last[1]).toFixed(1)}" r="2.5" fill="${col}"/></svg>`+
    `<span style="color:${col}">${arrow} ${esc(last[1])}</span></span>`;
}
async function tick(){
 let d; try{d=await (await fetch(DIR+'api/state',{cache:'no-store'})).json();}
 catch(e){$('#conn').className='pill bad';$('#conn').textContent='dashboard offline';return;}
 const st=d.status||{}, m=st.metrics||{}, node=st.node||{}, rp=d.responder||{};
 const on=st.connected;
 $('#conn').className='pill '+(on?'ok':'bad');
 $('#conn').textContent=on?'● radio connected':'● radio down';
 $('#sub').textContent=`${node.longName||'?'} (${node.shortName||'?'}) · ${node.id||''} · fw ${st.firmware||'?'}`;
 const live=rp.enabled==='true';
 $('#tiles').innerHTML=[
   tile('Battery', batteryLabel(m), m.voltage!=null?m.voltage.toFixed(2)+'V':''),
   tile('Bridge', (d.bridge.state==='running'?'running':'stopped'), d.bridge.pid?('pid '+d.bridge.pid):''),
   tile('Uptime', st.uptime_s!=null?fmtDur(st.uptime_s):'—'),
   tile('Responder', `<span class="dot ${live?'on':'off'}"></span>${live?'live':'off'}`,
        (rp.model?rp.model.replace('claude-','').replace(/-\d+$/,''):'')+' · '+(rp.allow_count||0)+' allowed'),
   tile('Sent / Received', `${(d.totals&&d.totals.sent)??0} / ${(d.totals&&d.totals.recv)??0}`),
   tile('Ch util', m.chUtil!=null?m.chUtil.toFixed(1)+'%':'—', m.airUtilTx!=null?('air '+m.airUtilTx.toFixed(2)+'%'):''),
 ].join('');
 const cfg=d.config||{}, active=(st.transport||cfg.TRANSPORT||'serial');
 $('#active-t').textContent = on ? 'live' : 'down';
 $('#active-t').className = 'badge right ' + (on ? 'ok' : 'warn');
 const SEGS=[['serial','USB'],['tcp','WiFi']];
 $('#trans').innerHTML =
   `<div class="seg" role="group" aria-label="radio transport">`
   + SEGS.map(([k,lab])=>`<span class="sg${active===k?' on':''}"`
       + (active===k?' aria-current="true"':'') + `>${lab}</span>`).join('')
   + `</div><div class="segd">`
   + (on ? `Carrying traffic over <b>${active==='tcp'?'WiFi':'USB'}</b>`
         : `Not connected &mdash; last configured for <b>${active==='tcp'?'WiFi':'USB'}</b>`)
   + (active==='tcp'&&cfg.HOST?` to <code>${esc(cfg.HOST)}</code>`:'')
   + `. The other is idle, which is not the same as broken.</div>`;
 SELF={id:node.id||null, name:node.shortName||node.longName||null};
 lastNodes=(d.nodes&&d.nodes.nodes)||[];
 const xs=d.exchanges||[];
 const dms=d.dm_exchanges||[];
 $('#xc-n').textContent=xs.length;
 $('#dm-n').textContent=dms.length;
 // Only touch the DOM when the content actually changed. Cheap, and it stops the 3s refresh
 // from fighting the reader (lost text selection, scroll jump) when nothing has happened.
 const sig=JSON.stringify([xs,dms,SELF,lastNodes.map(n=>[n.id,n.short])]);
 if(sig!==lastXsig){
   lastXsig=sig;
   XBYKEY.clear(); xs.forEach(x=>XBYKEY.set(xkey(x),x));
   dms.forEach(x=>XBYKEY.set(xkey(x),x));
   $('#exchanges').innerHTML=xs.length?xs.map(exchangeHtml).join('')
     :'<div class="empty">nothing on air yet — mesh is quiet or awaiting first inbound</div>';
   // Same renderer, deliberately. A second one would drift from the first, and the whole
   // point of the trace is that what it shows and what happened cannot diverge.
   $('#dm-exchanges').innerHTML=dms.length?dms.map(exchangeHtml).join('')
     :'<div class="empty">no direct messages yet</div>';
   hydrateOpen();
 }
 $('#nn').textContent=lastNodes.length;
 renderNodes();
 renderLearning(d.learning||{});
}
function shaLink(c,pushed){
  if(!c) return '<span class="lwarn">not committed</span>';
  const url='https://github.com/deanssamclaw/cal-mesh/commit/'+encodeURIComponent(c);
  // "pushed" is not decoration: a commit only on the Mac is work nobody else can see, and
  // saying "armed" without saying that would overstate it.
  return `<a class="lsha" href="${url}" target="_blank" rel="noopener noreferrer">${esc(c)}</a>`
       + (pushed?'':' <span class="lwarn">local only</span>');
}
function renderLearning(L){
  const sb=L.scoreboard||{};
  $('#lrn-untriaged').textContent=(sb.untriaged??0)+' open';
  const stat=(k,v,warn)=>`<div class="lstat${warn?' warn':''}"><div class="lk">${k}</div><div class="lv">${v}</div></div>`;
  $('#lrn-stats').innerHTML=[
    stat('needs an oracle', sb.untriaged??0),
    stat('armed', sb.armed??0),
    stat('recurred after arming', sb.recurred??0, (sb.recurred||0)>0),
    stat('corrections', sb.corrections??0, (sb.corrections||0)>0),
    stat('found by loop / by hand', (sb.by_loop??0)+' / '+(sb.by_hand??0), (sb.by_loop||0)===0),
  ].join('');
  const A=L.armed||[];
  $('#lrn-armed').innerHTML=A.length?A.map(a=>
    `<div class="lrow${a.recurred?' bad':''}"><div class="lask">${esc(a.ask)}</div>`
    +`<div class="lmeta">${a.source?esc(a.source):'no source recorded'}</div>`
    +`<div class="lmeta">${shaLink(a.commit,a.pushed)}`
    +(a.armed?` · armed ${daystamp(a.armed)}`:'')
    +(a.corrections?` · <span class="lwarn">${a.corrections} correction${a.corrections>1?'s':''}</span>`:'')
    +(a.recurred?' · <span class="lwarn">still reaching the model</span>':'')
    +`</div></div>`).join(''):'<div class="empty">nothing armed from the queue yet</div>';
  const Q=L.untriaged||[];
  $('#lrn-queue').innerHTML=Q.length?Q.map(q=>
    `<div class="lrow"><div class="lask">${esc(q.ask)}</div>`
    +`<div class="lmeta">seen ${q.count}×${q.last?' · last '+daystamp(q.last):''}</div></div>`
   ).join(''):'<div class="empty">queue is empty — every ask has a verdict</div>';
  const C=L.corrections||[];
  $('#lrn-corr').innerHTML=C.length?C.map(c=>
    `<div class="lrow"><div class="lask">${esc(c.ask)}</div>`
    +`<div class="lmeta">${daystamp(c.ts)} — ${esc(c.what)}</div></div>`
   ).join(''):'<div class="empty">none yet</div>';
}
// 'toggle' does not bubble, so listen in the capture phase on the container. Survives every
// re-render because the listener is on #exchanges, not on the details elements themselves.
// resolve the retired-version links against the app root, so they work at "/" and under a
// funnel path prefix alike
document.querySelectorAll('#oldlink,#oldlink2').forEach(a=>{a.href=DIR+'old-3';});
// A closed trace is not built. Every exchange used to render its full trace on every 3s pass
// whether or not anyone had opened it, which put a hard ceiling on how rich a trace could get.
// Bodies are now filled on first open and rebuilt by the normal render while they stay open.
// The assembly runs once per OPEN and is keyed separately from OPEN itself, because the 3s
// refresh rebuilds an open trace's markup — without this it would restart every three seconds.
// Nothing is measured from layout any more: the boxes share a centre line, so the arrows are
// straight and the geometry is the grid's problem, not ours.
const ANIMATED=new Set();
function hydrate(el,k,animate){
  const tp=el.querySelector('.tp'); if(!tp) return;
  if(!animate||ANIMATED.has(k)) return;
  ANIMATED.add(k);
  tp.classList.add('anim');
  tp.querySelectorAll('.arw').forEach((a,i)=>{a.style.animationDelay=(60+i*160)+'ms';});
  tp.querySelectorAll('.stg>.sdot').forEach((d,i)=>{d.style.animationDelay=(420+i*120)+'ms';});
}
function hydrateOpen(){
  document.querySelectorAll('#exchanges details.tr[open]').forEach(el=>hydrate(el,el.dataset.k,false));
}
// Tabs. Bound once at load, never from tick(), and the panes are hidden rather than
// rebuilt — so a refresh mid-read cannot switch the tab out from under you.
$('#xtabs').addEventListener('click', e=>{
  const b=e.target.closest('.tab'); if(!b) return;
  document.querySelectorAll('#xtabs .tab').forEach(t=>{
    const on = t===b;
    t.setAttribute('aria-selected', on?'true':'false');
    const pane=document.getElementById(t.getAttribute('aria-controls'));
    if(pane) pane.hidden = !on;
  });
});
$('#xtabs').addEventListener('keydown', e=>{
  if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight') return;
  const tabs=[...document.querySelectorAll('#xtabs .tab')];
  const cur=tabs.findIndex(t=>t.getAttribute('aria-selected')==='true');
  const nxt=tabs[(cur+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length];
  nxt.click(); nxt.focus(); e.preventDefault();
});
[$('#exchanges'),$('#dm-exchanges')].forEach(c=>c.addEventListener('toggle', e=>{
  const el=e.target;
  if(!el.matches||!el.matches('details.tr')) return;
  const k=el.dataset.k;
  if(!k) return;
  if(!el.open){ OPEN.delete(k); ANIMATED.delete(k);
    // the class must come off, or re-adding it on reopen is a no-op and nothing replays
    const tpc=el.querySelector('.tp'); if(tpc) tpc.classList.remove('anim');
    return; }
  OPEN.add(k);
  const body=el.querySelector('.tpwrap');
  const x=XBYKEY.get(k);
  if(body&&!body.firstChild&&x) body.innerHTML=traceHtml(x);
  hydrate(el,k,true);
}, true));
(function(){
  const m=location.pathname.match(/\/(old-\d+)\/?$/);
  if(!m) return;
  const cur=location.pathname.replace(/\/old-\d+\/?$/,'/');
  const b=document.createElement('div');
  b.style.cssText='background:#fff8c5;color:#9a6700;border-bottom:1px solid #d4a72c;'+
    'padding:9px 22px;font-size:13px;text-align:center';
  b.innerHTML='This is <b>'+m[1]+'</b>, a retired version of the dashboard, kept for reference. '+
    '<a href="'+cur+'" style="color:#0a63c9;font-weight:600">Go to the current page &rarr;</a>';
  document.body.insertBefore(b, document.body.firstChild);
})();
loadSnr(); loadRoutes(); tick(); setInterval(tick,3000);
setInterval(loadSnr,30000); setInterval(loadRoutes,30000);
</script></body></html>"""


PAGE_V5 = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>cal-mesh — levers (v4)</title>
<style>
:root{--bg:#f6f8fa;--card:#ffffff;--card2:#eef1f5;--line:#d6dce4;--fg:#1a1f26;
--dim:#5c6672;--accent:#0a63c9;--ok:#1a7f37;--warn:#9a6700;--bad:#cf222e;--tx:#6639ba;--rx:#1a7f37;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:linear-gradient(180deg,#f6f8fa,#f6f8faee);backdrop-filter:blur(6px);z-index:5}
header h1{font-size:17px;margin:0;letter-spacing:.3px}
header .sub{color:var(--dim);font-size:12px}
.pill{margin-left:12px;padding:5px 12px;border-radius:999px;font-weight:600;font-size:12px}
.pill.ok{background:#dafbe1;color:var(--ok);border:1px solid #aceebb}
.pill.bad{background:#ffebe9;color:var(--bad);border:1px solid #ffcecb}
.faqlink{color:var(--accent);text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
.faqlink:hover{text-decoration:underline}
.navlinks{margin-left:auto;display:inline-flex;gap:14px;align-items:center}
html{scroll-behavior:smooth}
main{padding:20px;max-width:1200px;margin:0 auto}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.tile .v{font-size:22px;font-weight:650;margin-top:4px}
.tile .v small{font-size:12px;color:var(--dim);font-weight:400}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin-top:16px}
.card h2{font-size:13px;margin:0;padding:12px 16px;border-bottom:1px solid var(--line);
color:var(--dim);text-transform:uppercase;letter-spacing:.6px;display:flex;gap:8px;align-items:center}
.card h2 .badge{background:var(--card2);color:var(--fg);padding:2px 8px;border-radius:6px;font-size:11px;font-variant-numeric:tabular-nums}
.card h2 .badge.right{margin-left:auto}
/* A state badge that has only one appearance is not a state badge. `live` and `down`
   were rendering identically until this existed. */
.card h2 .badge.ok{background:#dafbe1;color:var(--ok)}
.card h2 .badge.warn{background:#ffebe9;color:var(--bad)}
.tag{padding:1px 7px;border-radius:5px;font-size:11px;font-weight:600}
.tag.tx{background:#f3eefc;color:var(--tx)} .tag.rx{background:#dafbe1;color:var(--rx)}
.tag.ch{background:#ddf4ff;color:var(--accent)} .tag.auto{background:#fff8c5;color:var(--warn)}
.tag.offlist{background:#fff8c5;color:var(--warn);border:1px solid #d4a72c}
.tag.quiet{background:#eef1f5;color:var(--dim)}
/* --- tabbed streams: one card, two streams, so the page does not grow by one full
   card every time a stream is added. The pane is toggled with [hidden] rather than
   re-rendered, so the 3s refresh cannot knock the reader back to the first tab. --- */
/* Tabs, drawn as tabs. They were a row of grey text with a 2px underline on the active one:
   the inactive tab read as DISABLED LABEL rather than "another view you can click", and the
   active one was distinguished by a hairline most people never consciously see. Two views of
   the traffic is a fact about this page, and it was being whispered.
   The shape now carries it -- each tab is a filled, bordered folder tab, and the selected one
   is the one that rises out of the strip and MERGES INTO THE PANEL below by covering the
   strip's own bottom border. That join is the thing that says "this tab owns what is under
   it", and it is why the selected tab is white while the others are not.
   The border is deliberately not held to the 3:1 non-text bar: it is a refinement, and what
   actually identifies these controls is the fill, the label colour and the accent bar, which
   measure 7.07:1, 5.77:1 and 5.77:1. A boundary that is not doing the work does not need to
   carry the weight of one. */
.tabs{display:flex;gap:6px;border-bottom:1px solid var(--line);background:var(--card2);
padding:9px 10px 0}
.tab{appearance:none;font:inherit;font-size:14px;font-weight:600;color:#404c5c;
background:#e2e8ef;border:1px solid #a3aebd;border-bottom:0;border-radius:10px 10px 0 0;
padding:11px 18px;margin-bottom:-1px;cursor:pointer;display:flex;align-items:center;gap:8px;
white-space:nowrap;transition:background .12s ease,color .12s ease}
.tab:hover{background:#eef2f6;color:var(--fg)}
.tab[aria-selected="true"]{background:var(--card);color:var(--accent);
border-color:var(--line);border-bottom:1px solid var(--card);
box-shadow:inset 0 3px 0 var(--accent)}
.tab .badge{background:#d8dee6;color:#404c5c;font-weight:700}
.tab[aria-selected="true"] .badge{background:#ddf4ff;color:var(--accent)}
.tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(max-width:520px){.tab{padding:10px 12px;font-size:13px}}
.pane[hidden]{display:none}
.tabnote{font-size:11.5px;color:var(--dim);line-height:1.55;margin:0;padding:13px 16px 2px;max-width:80ch}
/* --- exchanges --- */
.xc{padding:14px 16px;border-bottom:1px solid var(--line)}
.xc:last-child{border-bottom:0}
.xc .meta{color:var(--dim);font-size:11px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:5px}
.xc .ask{font-size:15px;word-break:break-word;max-width:78ch}
.rep .txt,.norep{max-width:78ch}
.xc.unprompted{background:#f0f3f7}
.rep{margin:9px 0 0 16px;padding:8px 12px;border-left:2px solid var(--tx);background:#f7f4fd;
border-radius:0 8px 8px 0}
.rep .who{color:var(--dim);font-size:11px;display:block;margin-bottom:2px}
.rep .txt{color:var(--tx);font-size:14px}
.norep{margin:8px 0 0 16px;padding:7px 12px;border-left:2px solid var(--line);background:#f2f4f7;
border-radius:0 8px 8px 0;color:var(--dim);font-size:12.5px}
/* --- trace disclosure --- */
details.tr{margin:10px 0 0 16px}
details.tr summary{cursor:pointer;list-style:none;color:var(--accent);font-size:13.5px;
font-weight:600;letter-spacing:.2px;display:inline-flex;gap:7px;align-items:center;
padding:4px 10px 4px 8px;border:1px solid var(--line);border-radius:7px;background:var(--card2)}
details.tr summary::-webkit-details-marker{display:none}
details.tr summary::before{content:">";font-size:13px;font-weight:700;display:inline-block;
transform-origin:50% 50%;transition:transform .15s ease}
details.tr[open] summary::before{transform:rotate(90deg)}
details.tr summary:hover{border-color:var(--accent);background:#e4e9f0}
.tp{margin-top:7px;background:#f4f6f9;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.link-d{margin:2px 0 10px;max-width:620px}
.link-d svg{width:100%;height:auto;display:block}
.trow{display:flex;gap:10px;padding:3px 0;font-size:12px;align-items:baseline}
.tk{color:var(--dim);min-width:78px;flex-shrink:0;text-transform:uppercase;font-size:10px;letter-spacing:.5px}
.tv{color:var(--fg);word-break:break-word}
.tv code{background:var(--card2);padding:1px 5px;border-radius:4px;font-size:11.5px}
.hint{color:var(--dim);font-size:11px}
.gate{display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:4px;font-size:11px}
.gp{background:#dafbe1;color:var(--ok)} .gf{background:#ffebe9;color:var(--bad)}
.tnote{margin-top:8px;padding-top:7px;border-top:1px solid var(--line);color:var(--dim);font-size:11px;line-height:1.5}
.tnone{color:var(--dim);font-size:12px}
/* --- trace: the swap. What reached the model and what did not, drawn rather than asserted.
   Two things compete to become the reply; on a capability answer one of them is cut. --- */
.swap{display:grid;grid-template-columns:minmax(0,1fr) 74px minmax(0,1fr);gap:9px 0;
align-items:center;margin:2px 0 12px}
.sw{border:1px solid var(--line);border-radius:9px;padding:8px 10px;background:var(--card);min-width:0}
.sw .swk{font-size:9.5px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);font-weight:700}
.sw .swv{font-size:12.5px;margin-top:3px;word-break:break-word;line-height:1.45}
.sw .swn{font-size:10.5px;margin-top:5px;color:var(--dim);line-height:1.4}
.sw.i-in{grid-column:1;grid-row:1} .sw.i-fact{grid-column:1;grid-row:2}
.sw.i-out{grid-column:3;grid-row:1/3;align-self:stretch;display:flex;
flex-direction:column;justify-content:center}
.sw.cut{border-style:dashed;background:#fbfcfd}
.sw.cut .swv{color:var(--dim);text-decoration:line-through;text-decoration-color:#b9c2cd}
.sw.i-fact{border-color:#aceebb;background:#f4fcf6}
.sw.i-out{border-color:#ddd0f5;background:#faf7fe}
.sw.i-out .swv{color:var(--tx);font-size:13.5px}
/* --- trace: the pipeline spine. The stages are a sequence in time, so they are drawn as one. --- */
.spine{list-style:none;margin:0;padding:0}
.stg{position:relative;padding:0 0 11px 25px}
.stg::before{content:"";position:absolute;left:5px;top:16px;bottom:0;width:2px;background:var(--line)}
.stg:last-child::before{display:none}
.stg>.sdot{position:absolute;left:0;top:5px;width:12px;height:12px;border-radius:50%;
background:var(--ok);border:2px solid var(--ok);box-sizing:border-box}
.stg.stop>.sdot{background:var(--bad);border-color:var(--bad)}
.stg.skip>.sdot{background:var(--card);border-color:#c3ccd7}
.stg.skip{opacity:.6}
.stg.stop::before,.stg.skip::before{background:repeating-linear-gradient(180deg,#c3ccd7 0 3px,transparent 3px 6px)}
.stg .shead{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}
.stg .sname{font-size:10px;text-transform:uppercase;letter-spacing:.7px;color:var(--dim);
font-weight:700;flex-shrink:0}
.stg .ssum{font-size:12.5px;color:var(--fg)}
.stg .sdet{margin-top:5px}
.stg .sdet:empty{display:none}
.rungn{display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:4px;font-size:11px;
background:#f2f4f7;color:var(--dim);font-style:italic}
/* --- trace: measurements drawn to the scale they were measured on --- */
.bar{position:relative;height:6px;border-radius:3px;background:#e7ebf1;margin-top:6px;max-width:280px}
.bar>i{position:absolute;top:0;bottom:0;border-radius:3px;background:#cfe6d6}
.bar>i.fill{left:0;background:var(--ok)}
.bar>i.fill.late{background:var(--warn)}
.bar .mk{position:absolute;top:-3px;width:2px;height:12px;background:var(--fg);border-radius:1px}
.barl{font-size:10.5px;color:var(--dim);margin-top:4px;line-height:1.45;max-width:60ch}
@media(max-width:640px){
.swap{grid-template-columns:minmax(0,1fr);gap:7px}
.sw.i-in,.sw.i-fact,.sw.i-out{grid-column:1;grid-row:auto}
.conn{display:none}}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 16px;font-size:13px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
td.snr-good{color:var(--ok)} td.snr-bad{color:var(--warn)}
#nodes-wrap{max-height:620px;overflow:auto}
#nodes thead th{position:sticky;top:0;background:var(--card);z-index:1}
#nodes th.sortable{cursor:pointer;user-select:none;white-space:nowrap}
/* The radio speaks over exactly ONE transport at a time, and that is a switch position, not
   two things. It used to be drawn as two cards stretched to the full width of the page, each
   the size of a real panel, with the live one marked by a 1px border tint -- so the loudest
   thing about it was the pair, and the quietest was which one was actually carrying traffic.
   A segmented control says "one of these, and it is this one" in its shape, and it stops
   claiming a whole row of the page for one binary fact. The idle segment is still shown, on
   purpose: "USB exists and is not in use" is a different statement from "USB is broken", and
   dropping it would lose that. */
.trans{padding:14px 16px}
.seg{display:inline-flex;border:1px solid #a3aebd;border-radius:9px;overflow:hidden;
background:#e2e8ef}
.sg{padding:8px 18px;font-size:13.5px;font-weight:600;color:#404c5c;
border-right:1px solid #a3aebd;letter-spacing:.2px}
.sg:last-child{border-right:0}
/* Filled vs unfilled, which is a LIGHTNESS difference and survives any colour vision. The
   old version encoded it as a border hue, which does not. */
.sg.on{background:var(--accent);color:#fff}
.segd{margin-top:10px;font-size:11.5px;color:var(--dim);line-height:1.5}
.segd code{background:var(--card2);padding:1px 5px;border-radius:4px;font-size:11px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:4px}
.tile .v .dot{width:9px;height:9px;margin-right:7px;vertical-align:middle}
.dot.on{background:var(--ok)} .dot.off{background:var(--bad)}
footer{color:var(--dim);font-size:11px;text-align:center;padding:16px}
.empty{padding:16px;color:var(--dim);font-size:13px}
/* Scoped to the PANE. These rules were written for a standalone card and kept its id after
   the content moved into the tab strip, so every one of them matched nothing and the tab
   rendered unstyled — invisible to the evals here, which read the JSON and the script and
   never resolve a selector against the markup. The stat row is the page's own .tiles/.tile,
   not a second component that looks like it. */
#pane-learn .lsec h3{margin:0;padding:16px 16px 6px;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent)}
#pane-learn .lnote{margin:0;padding:0 16px 8px;font-size:11.5px;color:var(--dim);line-height:1.55;max-width:80ch}
#pane-learn .tiles{padding:4px 16px 0;margin-bottom:8px}
/* 3px reserved gutter + 13px = the 16px left edge every other element in this pane sits on.
   Written as 19 first, which kicked every row 6px right of its own heading while the empty
   state stayed at 16 — so a section changed alignment depending on whether it had content. */
#pane-learn .lrow{padding:9px 16px 9px 13px;border-top:1px solid var(--line);border-left:3px solid transparent}
#pane-learn .lrow.bad{border-left-color:var(--bad)}
/* Monospace because this is NOT the sentence somebody sent. It is the cluster key: lowercased,
   trigger word dropped, punctuation stripped, so two spellings of one question collapse into a
   single row. Setting it in the body sans at the size the exchange list uses would present a
   derived string as a quotation. The row caption says so, since the key looks enough like prose
   to be mistaken for it. Measure capped to match the ask/meta pair in the exchange stream. */
#pane-learn .lask{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;word-break:break-word;max-width:78ch}
#pane-learn .lmeta{font-size:11px;color:var(--dim);margin-top:2px;max-width:78ch}
/* A queue row is one short key and one short count. Stacked, it used the left third of the card
   and left the rest blank; the count sits at the far end instead and the row reads across.
   Only the queue: an armed row's metadata is two full lines and belongs under its key. */
#pane-learn .lrow.split{display:flex;gap:24px;align-items:baseline;justify-content:space-between}
#pane-learn .lrow.split .lmeta{margin-top:0;white-space:nowrap;flex:none}
/* A .tile at the top of the page is white on the body's grey and reads as raised. In here the
   card behind it is also white, so the same component was white on white with a hairline doing
   all the work. Recessed against the card instead — the surface changed, not the component. */
#pane-learn .tile{background:var(--card2)}
#pane-learn .lsha{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent);text-decoration:none}
#pane-learn .lsha:hover{text-decoration:underline}
#pane-learn .lwarn{color:var(--warn);font-weight:600}
.tile.alarm{border-color:var(--warn)} .tile.alarm .v{color:var(--warn)}
.faq h3{margin:0;padding:14px 16px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent);border-bottom:1px solid var(--line);background:#f0f3f7}
.faq .a a{color:var(--accent);text-decoration:none;font-weight:600}
.faq .a a:hover{text-decoration:underline}
.faq details{border-bottom:1px solid var(--line)}
.faq details:last-child{border-bottom:0}
.faq summary{padding:12px 16px;cursor:pointer;font-weight:600;list-style:none;display:flex;align-items:center;gap:10px}
.faq summary::-webkit-details-marker{display:none}
.faq summary::before{content:"+";color:var(--accent);font-weight:700;width:10px;display:inline-block}
.faq details[open] summary::before{content:"\2013"}
.faq .a{padding:0 16px 14px 40px;color:var(--dim);font-size:13px;line-height:1.65}
.faq .a code{background:var(--card2);padding:1px 5px;border-radius:4px;color:var(--fg);font-size:12px}
.faq .a b{color:var(--fg)}
.clog{max-height:420px;overflow-y:auto}
.clog .ci{padding:10px 16px;border-bottom:1px solid var(--line);font-size:13px;line-height:1.55}
.clog .cd{color:var(--dim);font-size:11px;margin-right:8px;font-variant-numeric:tabular-nums}
.spark{display:inline-flex;align-items:center;gap:6px;font-size:12px;white-space:nowrap}
/* ============================ v3: depth ============================
   v2 drew a sequence that happened in time as flat boxes on one plane. Three things carry
   the third dimension here: elevation (a recessed well under raised planes), wires that are
   MEASURED from real box geometry rather than approximated with horizontal rules, and an
   assembly order that runs once when a trace is opened. */
.tp{background:linear-gradient(180deg,#f7f9fc,#eef2f7);border-radius:12px;
box-shadow:inset 0 2px 5px rgba(22,27,34,.05),0 1px 0 #fff}
/* The chain, left to right. Boxes are numbered because the whole point is the ORDER:
   a reader who does not know how this works needs to see that the question caused the lookup. */
.flow{display:grid;align-items:center;margin:2px 0 4px;
grid-template-columns:minmax(0,1fr) 62px minmax(0,.92fr) 74px minmax(0,.86fr) 104px minmax(0,1fr)}
.flow.gen{grid-template-columns:minmax(0,1fr) 150px minmax(0,1fr)}
.fb{position:relative;z-index:1;border:1px solid var(--line);border-radius:11px;padding:10px 13px;
background:linear-gradient(180deg,#fff,#fbfcfe);
box-shadow:0 1px 2px rgba(22,27,34,.05),0 6px 16px -8px rgba(22,27,34,.22)}
.fk{font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:var(--dim)}
.fv{font-size:13px;margin-top:5px;line-height:1.45;word-break:break-word}
.fn{font-size:10.5px;margin-top:6px;color:var(--dim);line-height:1.42;overflow-wrap:anywhere}
/* the recognition step: plain word-matching, no model. Drawn as a decision, not as data. */
.fb.bx{border-color:#c3d9f2;background:linear-gradient(180deg,#f7fbff,#eef5fd);
box-shadow:0 1px 2px rgba(10,99,201,.08),0 8px 20px -10px rgba(10,99,201,.30)}
.fb.bx .fv{font-size:12.5px}
.chip{display:inline-block;margin:3px 4px 0 0;padding:1px 7px;border-radius:5px;
max-width:100%;overflow-wrap:anywhere;word-break:break-word;vertical-align:top;
background:#dceafb;color:#0a4da3;font-size:11px;font-weight:600;
font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.fb.b2{border-color:#a7e3b6;background:linear-gradient(180deg,#f4fdf7,#eaf9ef);
box-shadow:0 1px 2px rgba(26,127,55,.10),0 8px 20px -10px rgba(26,127,55,.35)}
.fb.b2 .fv{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.fb.b3{border-color:#d5c6f3;background:linear-gradient(180deg,#fbf8ff,#f5effd);
box-shadow:0 1px 2px rgba(102,57,186,.10),0 8px 20px -10px rgba(102,57,186,.35)}
.fb.b3 .fv{color:var(--tx);font-weight:600;font-size:14px}
.onair{color:var(--ok);font-weight:600}
/* arrows: straight, because every box shares a centre line — no measurement needed */
.arw{position:relative;height:3px;border-radius:2px;
background:linear-gradient(90deg,#cbd4de,var(--ok));transform-origin:left center}
.arw::after{content:"";position:absolute;right:-1px;top:-5.5px;border:6px solid transparent;
border-left-color:var(--ok);border-right:0}
.arw>span{position:absolute;left:0;right:0;bottom:9px;font-size:9.5px;color:var(--dim);
text-align:center;line-height:1.3}
/* the boundary: the one surprising fact, stated once, on the line it describes */
.cross{position:relative;align-self:stretch;display:flex;align-items:center}
.cross::before{content:"";position:absolute;left:50%;top:0;bottom:0;margin-left:-1px;
border-left:2px dashed #9fb0c4}
.cross .arw{flex:1;margin:0 6px}
.cross .bl{position:absolute;left:50%;top:2px;transform:translateX(-50%);background:var(--card);
border:1px solid #c3d2e2;border-radius:6px;padding:3px 6px;font-size:8.5px;font-weight:700;
text-transform:uppercase;letter-spacing:.4px;color:#3d566e;text-align:center;line-height:1.25;
width:92px;box-sizing:border-box}
.flowcap{font-size:11.5px;color:var(--dim);line-height:1.55;margin:10px 0 14px;max-width:88ch;
padding-left:2px}
.flowcap b{color:var(--fg);font-weight:650}
.stg{padding:0 0 13px 30px}
.stg::before{left:6.5px;top:17px;width:3px;border-radius:2px;
background:linear-gradient(180deg,#cfd7e1,#dde3ea)}
.stg>.sdot{left:0;top:5px;width:16px;height:16px;border:0;
background:radial-gradient(circle at 35% 32%,#5fd07f,var(--ok));
box-shadow:0 0 0 3px rgba(26,127,55,.14),0 1px 2px rgba(22,27,34,.3)}
.stg.stop>.sdot{background:radial-gradient(circle at 35% 32%,#f08b93,var(--bad));
box-shadow:0 0 0 3px rgba(207,34,46,.14),0 1px 2px rgba(22,27,34,.3)}
.stg.skip>.sdot{background:#fff;box-shadow:inset 0 0 0 2px #c3ccd7}
.sname{min-width:74px;letter-spacing:.85px}
/* instruments: a measurement drawn against the range it lives on, not a bare number */
.inst{display:flex;gap:26px;flex-wrap:wrap;margin-top:8px}
.gauge{min-width:206px;max-width:320px}
.glab{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:5px;gap:12px}
.gname{font-size:9px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;color:var(--dim)}
.gval{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;font-weight:600}
.track{position:relative;height:9px;border-radius:5px;background:#e4e9f0;
box-shadow:inset 0 1px 2px rgba(22,27,34,.14)}
/* sequential magnitude = ONE hue, light to dark. Not a red-amber-green rainbow: that is a
   rainbow ramp for an ordered quantity, and red/amber are ~1.5 dE apart under deuteranopia. */
.track.ramp{background:linear-gradient(90deg,#eaf6ee,#9fd8b3,#1a7f37)}
.track .band{position:absolute;top:0;bottom:0;background:rgba(26,127,55,.28);border-radius:4px}
.track .mk{position:absolute;top:-4px;width:3px;height:17px;border-radius:2px;background:var(--fg);
box-shadow:0 0 0 2px #fff,0 1px 3px rgba(22,27,34,.4);transition:left .7s cubic-bezier(.22,1,.36,1)}
.gends{display:flex;justify-content:space-between;font-size:9.5px;color:var(--dim);margin-top:5px;gap:10px}
.gnote{font-size:10.5px;color:var(--dim);margin-top:6px;line-height:1.45;max-width:52ch}
/* assembly: runs once per open, never on the 3s refresh */
.tp.anim .stg>.sdot{transform:scale(.4);opacity:0;animation:sdotpop .45s cubic-bezier(.34,1.56,.64,1) forwards}
@keyframes sdotpop{to{transform:scale(1);opacity:1}}
.tp.anim .arw{transform:scaleX(0);animation:arwgrow .5s cubic-bezier(.22,1,.36,1) forwards}
@keyframes arwgrow{to{transform:scaleX(1)}}
.tp.anim .cross .bl{opacity:0;animation:blfade .35s ease forwards;animation-delay:.45s}
@keyframes blfade{to{opacity:1}}
@media(prefers-reduced-motion:reduce){
.tp.anim .stg>.sdot,.tp.anim .arw,.tp.anim .cross .bl{animation:none;opacity:1;transform:none}
.track .mk{transition:none}}
@media(max-width:700px){
.flow,.flow.gen{grid-template-columns:minmax(0,1fr)}
.arw,.cross{display:none}}
/* ==================== v4: the trace is a dark instrument well ====================
   The page stays light. The trace panel — and ONLY the trace panel — is dark.
   The reasoning is that these are two different kinds of surface. The page is a
   status board you scan; the trace is an instrument you read one record on, and
   dropping it out of the page's light gives the boxes, wires and dots somewhere to
   sit that is not the same plane as the list they came from. It also makes the
   elevation model honest in both directions: on a light page a raised plane is
   whiter than its ground, and on a dark one it is lighter than its ground, so the
   well/plane hierarchy v3 built survives the inversion instead of reading backwards.

   MECHANISM: every colour under .tp is read from a token, so the palette is
   re-declared ONCE on .tp and every descendant follows. Two things do NOT follow,
   and they are the trap the 2026-08-12 light switch already paid for: the link
   diagram's colours are written into linkSvg, and the warn box's are an inline
   style in flowHtml. Both are changed at their source in THIS page's script.

   Contrast was measured, not eyeballed. Every text pair in this panel is >= 6.0:1
   against the surface it sits on (AA needs 4.5), and every boundary that carries
   meaning — box borders, the spine rail, the gauge track — is >= 3.0:1 against the
   panel (AA non-text needs 3.0). The light palette's own separators do not clear
   that bar; going dark is where a 1.2:1 hairline stops being a hairline and starts
   being invisible, so it was worth paying for here. */
.tp{
  --fg:#e6edf3; --dim:#9aa7b4; --line:#5c6673; --card:#161b22; --card2:#21262d;
  --accent:#6cb6ff; --ok:#3fb950; --warn:#e3b341; --bad:#ff7b72; --tx:#d2a8ff;
  color:var(--fg);
  margin-top:0;
  border:1px solid #2a3038;
  border-top-left-radius:0;
  background:linear-gradient(180deg,#0b0f14,#11161d);
  box-shadow:inset 0 2px 10px rgba(0,0,0,.55),0 1px 0 rgba(255,255,255,.04);
  padding:12px 14px;
}
/* the disclosure becomes a tab ON the well when open, so the dark panel reads as
   something that was opened rather than a stray dark box in a light page */
details.tr[open]>summary{background:linear-gradient(180deg,#1c232c,#11161d);
  border-color:#2a3038;color:#9ecbff;border-bottom:0;padding-bottom:6px;
  border-radius:7px 7px 0 0}
details.tr[open]>summary:hover{border-color:#4478ad;
  background:linear-gradient(180deg,#232b36,#161c24)}
/* raised planes: lighter than their ground, which is the dark-mode form of "raised" */
.tp .fb{border-color:#5c6673;background:linear-gradient(180deg,#1c232c,#161c24);
  box-shadow:0 1px 2px rgba(0,0,0,.5),0 8px 20px -10px rgba(0,0,0,.85)}
.tp .fb.bx{border-color:#4478ad;background:linear-gradient(180deg,#16243a,#111c2e);
  box-shadow:0 1px 2px rgba(0,0,0,.5),0 8px 22px -10px rgba(68,120,173,.45)}
.tp .chip{background:#17324f;color:#9ecbff}
.tp .fb.b2{border-color:#3a8752;background:linear-gradient(180deg,#122a1a,#0f2216);
  box-shadow:0 1px 2px rgba(0,0,0,.5),0 8px 22px -10px rgba(58,135,82,.5)}
.tp .fb.b3{border-color:#7d5fbd;background:linear-gradient(180deg,#221a35,#1b1529);
  box-shadow:0 1px 2px rgba(0,0,0,.5),0 8px 22px -10px rgba(125,95,189,.5)}
.tp .arw{background:linear-gradient(90deg,#39414b,var(--ok))}
.tp .cross::before{border-left-color:#7d8590}
.tp .cross .bl{background:#161b22;border-color:#5c6673;color:#adbac7}
/* the spine: rail and dots. A connector that carries sequence is a meaningful
   boundary, so it is held to 3:1 rather than left as a hairline. */
/* the rail runs top-to-bottom, so BOTH ends of it are the boundary — the first pass shaded
   it away to #3d4753 at the bottom, which is 1.9:1 and simply not a line any more. */
.tp .stg::before{background:linear-gradient(180deg,#6b7684,#5c6673)}
.tp .stg.stop::before,.tp .stg.skip::before{
  background:repeating-linear-gradient(180deg,#5c6673 0 3px,transparent 3px 6px)}
.tp .stg>.sdot{background:radial-gradient(circle at 35% 32%,#56d364,#2ea043);
  box-shadow:0 0 0 3px rgba(63,185,80,.20),0 1px 3px rgba(0,0,0,.7)}
.tp .stg.stop>.sdot{background:radial-gradient(circle at 35% 32%,#ff7b72,#da3633);
  box-shadow:0 0 0 3px rgba(248,81,73,.20),0 1px 3px rgba(0,0,0,.7)}
.tp .stg.skip>.sdot{background:#11161d;box-shadow:inset 0 0 0 2px #5c6673}
.tp .gate.gp{background:rgba(63,185,80,.16);color:#56d364}
.tp .gate.gf{background:rgba(248,81,73,.16);color:#ff7b72}
.tp .rungn{background:#1c222b;color:var(--dim)}
/* instruments. The ramp keeps the single-hue rule and simply runs the other way:
   an ordered quantity goes dark-to-bright on a dark ground, which is the same
   "light to dark" instruction read against its own background. The extent of the
   track is carried by an inset ring, so the low end of the ramp can go as dark as
   it likes without the scale itself disappearing. */
.tp .track{background:#21262d;
  box-shadow:inset 0 0 0 1px #5c6673,inset 0 1px 3px rgba(0,0,0,.5)}
.tp .track.ramp{background:linear-gradient(90deg,#0f2a19,#2a8f4c,#56d364)}
.tp .track .band{background:rgba(63,185,80,.30)}
.tp .track .mk{background:var(--fg);box-shadow:0 0 0 2px #11161d,0 1px 3px rgba(0,0,0,.8)}
.tp .bar{background:#21262d}
.tp .bar>i{background:#1f4d2e}
.tp .bar>i.fill{background:var(--ok)}
.tp .bar>i.fill.late{background:var(--warn)}
.tp .bar .mk{background:var(--fg)}
/* a harvested path: a separate measurement, so it gets its own plane and its own rule above
   it rather than blending into the rows of the diagram it sits under */
.tp .pathm{margin-top:11px;padding-top:10px;border-top:1px solid #3d4753}
.tp .pathh{font-size:9.5px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;
  color:var(--dim);margin-bottom:8px}
.tp .pathh .pathage{font-weight:600;letter-spacing:.3px;text-transform:none;color:#9ecbff}
.tp .pdir{font-size:9px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;
  color:var(--dim);margin:7px 0 3px}
.tp .pchain{display:flex;flex-wrap:wrap;align-items:center;gap:2px 0;margin-bottom:2px}
.tp .phop{display:inline-block;padding:3px 9px;border-radius:7px;font-size:12px;font-weight:600;
  background:linear-gradient(180deg,#1c232c,#161c24);border:1px solid #5c6673;color:var(--fg)}
.tp .phop.unk{border-style:dashed;color:var(--dim);font-weight:500;font-style:italic}
.tp .plink{display:inline-flex;flex-direction:column;align-items:center;margin:0 2px;min-width:56px}
.tp .plink .parr{display:block;width:100%;height:2px;border-radius:1px;background:#6b7684;
  position:relative}
.tp .plink .parr::after{content:"";position:absolute;right:0;top:-3.5px;border:4.5px solid transparent;
  border-left-color:#6b7684;border-right:0}
.tp .plink .psnr{font-size:9.5px;color:var(--dim);margin-top:2px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}
/* the v2-era swap boxes. Not rendered by this page, but the rules are still in the
   sheet above and an unstyled light box would be the thing nobody notices until a
   record shaped the old way turns up. */
.tp .sw{background:#1c232c;border-color:#5c6673}
.tp .sw.cut{background:#141a21}
.tp .sw.cut .swv{text-decoration-color:#5c6673}
.tp .sw.i-fact{border-color:#3a8752;background:#122a1a}
.tp .sw.i-out{border-color:#7d5fbd;background:#221a35}
</style></head>
<body>
<header>
  <div><h1>📻 cal-mesh <span class="sub">— live levers (v4)</span></h1>
  <div class="sub" id="sub">connecting…</div></div>
  <span class="navlinks"><a class="faqlink" href="#faq">FAQ ↓</a><a class="faqlink" href="#changelog">Changelog ↓</a><a class="faqlink" href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">GitHub ↗</a></span>
  <span class="pill" id="conn">…</span>
</header>
<main>
  <div class="tiles" id="tiles"></div>
  <div class="card"><h2>Transports <span class="badge right" id="active-t"></span></h2>
    <div class="trans" id="trans"></div></div>
  <div class="card">
   <div class="tabs" role="tablist" id="xtabs">
    <button class="tab" role="tab" id="tab-open" aria-controls="pane-open" aria-selected="true">💬 Open Exchanges <span class="badge" id="xc-n">0</span></button>
    <button class="tab" role="tab" id="tab-dm" aria-controls="pane-dm" aria-selected="false">🔒 Direct Messages <span class="badge" id="dm-n">0</span></button>
    <button class="tab" role="tab" id="tab-learn" aria-controls="pane-learn" aria-selected="false">🔧 Build Queue <span class="badge" id="lrn-untriaged">0</span></button>
   </div>
   <div class="pane" id="pane-open" role="tabpanel" aria-labelledby="tab-open"><div id="exchanges"></div></div>
   <div class="pane" id="pane-dm" role="tabpanel" aria-labelledby="tab-dm" hidden>
    <p class="tabnote">Cal and Dean&rsquo;s test bench. Trying things on the open channel costs every
    node in range airtime, so experiments happen here instead &mdash; one link, two nodes. <b>It is
    published for the same reason everything else here is:</b> the interesting part is what is being
    tried and how it works, and a private tier you cannot see is a claim rather than a demonstration.
    These are authenticated direct messages, so unlike the open channel the sender is
    cryptographically established rather than merely asserted.</p>
    <div id="dm-exchanges"></div>
   </div>
   <div class="pane" id="pane-learn" role="tabpanel" aria-labelledby="tab-learn" hidden>
    <p class="tabnote">A distiller reads every exchange once a day and files what reached the
    model instead of a capability. That queue is the build list, and it belongs in this strip
    beside the two message streams because it is made <i>of</i> them &mdash; the same traffic,
    read for what it could not answer. Published for the reason the trace is: a list of what
    this node <b>can</b> do is a claim, while a list of what it still cannot, next to the commit
    that fixed the last one, is checkable. The uncomfortable numbers are the load-bearing ones.</p>
    <div class="tiles" id="lrn-stats"></div>
    <div class="lsec"><h3>Armed</h3><div id="lrn-armed"></div></div>
    <div class="lsec"><h3>Waiting on an oracle</h3>
     <p class="lnote">Nothing is built from these until someone decides what the right answer is
     measured against &mdash; a doer graded only by its own test suite is a guess with a green
     check next to it. Each row is a <i>cluster key</i> rather than a quotation, which is why it
     is set in the machine face: lowercased, trigger word dropped, punctuation stripped, so two
     spellings of one question count once.</p>
     <div id="lrn-queue"></div></div>
    <div class="lsec"><h3>Corrections after arming</h3>
     <p class="lnote">The counter-metric, itemised. Kept in view on purpose &mdash; a loop scored
     only on what it builds will build.</p>
     <div id="lrn-corr"></div></div>
   </div>
  </div>
  <div class="card"><h2><span class="badge" id="nn">0</span> Neighbors heard</h2>
    <div id="nodes-wrap"><table id="nodes"><thead><tr>
      <th class="sortable" data-key="short" onclick="setSort('short')">Short</th>
      <th class="sortable" data-key="long" onclick="setSort('long')">Name</th>
      <th class="sortable" data-key="hw" onclick="setSort('hw')">HW</th>
      <th class="sortable" data-key="hops" onclick="setSort('hops')">Hops</th>
      <th class="sortable" data-key="snr" onclick="setSort('snr')">SNR</th>
      <th>1h SNR trend</th></tr></thead><tbody></tbody></table></div>
  </div>
  <div class="card faq" id="faq"><h2>FAQ — what this is and how it works</h2>
    <h3>Start here</h3>
    <details><summary>What is this page?</summary><div class="a">
      A live, read-only window into <b>Cal</b> — an AI that lives on a <b>radio mesh network</b> and
      answers people over the air, with no internet on the far end. Everything here is real: the radio's
      state, every message in and out, and the full reasoning trace behind each automatic reply. Nothing
      is a mock-up. If Cal answered someone thirty seconds ago, it's below.</div></details>
    <details><summary>What is a mesh network?</summary><div class="a">
      A network with <b>no towers, no carrier and no internet</b>. Every radio is also a repeater: if
      two nodes are too far apart to hear each other, a third in the middle passes the message along,
      and so on. That's a <b>hop</b>. Coverage comes from the participants rather than infrastructure,
      so the network exists wherever people bring radios — and keeps working when the grid doesn't.
      That last property is the whole point: it's the tool you reach for when cell service is gone.</div></details>
    <details><summary>What is Meshtastic?</summary><div class="a">
      Free, open-source firmware that turns inexpensive <b>LoRa</b> radios (typically $30–100) into a
      mesh network for text messages and location sharing. You flash it onto a small board, pair it to
      your phone, and you're on the mesh — encrypted by channel, no account, no subscription, no
      monthly fee. It's a volunteer project with a large community, and it's what Cal's radio runs.
      <br><a href="https://meshtastic.org" target="_blank" rel="noopener noreferrer">meshtastic.org ↗</a></div></details>
    <details><summary>What is LoRa, and why does it matter here?</summary><div class="a">
      <b>Lo</b>ng <b>Ra</b>nge radio: a modulation designed to get a very small amount of data a very
      long way on very little power — miles between nodes, on a battery, with no licence required on
      the public bands. The trade is <b>bandwidth</b>. A LoRa channel carries on the order of a few
      hundred to a few thousand bits per second, and <b>every node in earshot shares it</b>. One long
      message blocks the channel for everyone. That single constraint explains most of Cal's design,
      starting with why it never says more than a few words.</div></details>
    <details><summary>Why put an AI on a mesh radio at all?</summary><div class="a">
      Because a mesh is what you use <b>when the grid isn't there</b> — off-grid, field work, dead
      coverage, emergencies — and that's exactly when knowledge is hardest to reach. The insight the
      project runs on: the mesh is off-grid, but the <b>base station usually isn't</b>. Cal's radio is
      connected to a computer with internet, so someone miles out with nothing but a handheld can ask a
      question over RF and get a real answer relayed back. Cal extends connected knowledge to the
      unconnected edge. Before this, a node could prove it was alive but couldn't actually
      <i>help</i> — presence without utility.</div></details>
    <details><summary>How did it get here?</summary><div class="a">
      Three deliberate stages, each gated before the next. <b>Level 1</b> — a bridge that owns the
      radio and can send and receive text. <b>Level 2</b> — an autonomous responder that decides on its
      own whether to answer and writes the reply, with training wheels (a small allow-list, rate limits,
      a kill switch). <b>Level 3</b> — real capabilities, where the software fetches a verified fact and
      the model only puts it into words. Each stage shipped switched <b>off</b>, went through
      adversarial review, and was turned on deliberately. The reviews have caught real problems,
      including a privacy leak in the reply path.</div></details>

    <h3>How Cal behaves</h3>
    <details><summary>How does Cal know a message is meant for it?</summary><div class="a">
      A message qualifies if it's a <b>direct message</b> to Cal's node, <i>or</i> the text contains
      <code>cal</code> as a whole word (case-insensitive). Whole-word matching means "lo<b>cal</b>",
      "<b>cal</b>endar" and "physi<b>cal</b>" do <i>not</i> trigger it.</div></details>
    <details><summary>What has to be true before Cal replies?</summary><div class="a">
      Being named isn't enough. In order, a message must pass every gate: it's <b>not Cal's own</b> ·
      it's <b>fresh</b> · the responder is <b>enabled</b> · the sender is on the <b>allow-list</b> ·
      it's <b>addressed</b> · it's <b>within rate limits</b>. Miss one and Cal stays quiet and records
      why — open <b>trace</b> on any exchange to see the whole ladder and exactly where it stopped.</div></details>
    <details><summary>Why do some messages say "OFF-LIST"?</summary><div class="a">
      Because Cal <b>heard them perfectly well</b> and chose not to answer. Reception and reply are two
      different things: every message on the channel is received and shown here, but only senders on the
      allow-list can trigger an automatic reply. <i>Whether silence is the right behaviour is under
      active review</i> — the argument against it is that on a shared channel, staying quiet to one
      person while answering another isn't neutral, it reads as a snub.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/unknown-sender-tier.md" target="_blank" rel="noopener noreferrer">The proposal to fix it ↗</a></div></details>
    <details><summary>Why are the replies so short?</summary><div class="a">
      Airtime is <b>shared by every node in range</b>, and LoRa has very little of it. A long message
      is not just slow, it takes the channel away from everyone else — including traffic that might
      matter more than a chat reply. So Cal is held to <b>5–7 words</b>. It's etiquette enforced in
      code, and it's why the answers read like radio traffic rather than chat.</div></details>
    <details><summary>Who can Cal talk to, and can it be switched off?</summary><div class="a">
      Right now only a small allow-list of nodes can trigger a reply, though <b>anyone</b> on the mesh
      can read what Cal says — the channel is public. Three independent always-on services do the work
      (radio, cognition, this dashboard), so one can restart without dropping the others, and a single
      kill switch silences all automatic replies instantly. Note that node IDs are <b>not
      authenticated</b> and can be spoofed, so the allow-list is a courtesy control, not a security
      boundary. The real controls are the kill switch and the fact that the model can't run tools.</div></details>

    <h3>How the answers are made</h3>
    <details><summary>How does Cal choose what to say?</summary><div class="a">
      A headless Claude writes the reply under a fixed persona — <b>5–7 words, plain text, warm and
      useful, never reveal the operator's location, schedule or personal life</b> — running with
      <b>no tools</b> and with no access to any private context. The important part is what it
      <i>isn't</i> allowed to do: for anything factual, Cal never looks something up. The software
      fetches a verified fact from a known source and hands it over, and the model's only job is to put
      that fact into words. We call it <b>capability injection</b>, and it's why Cal can't invent a
      temperature — if the fetch fails, it says so instead of guessing.</div></details>
    <details><summary>Where does the weather come from?</summary><div class="a">
      The US National Weather Service, and nothing else — one allow-listed source, fetched by the
      software, never by the model. Cal reads the <b>latest observation from the nearest weather
      station</b> to a fixed reference point, and refuses to answer at all if that reading is too old.
      Cal has <b>no forecast</b>: ask about tonight, tomorrow or whether it's going to rain and it
      says so outright rather than reading you a present-tense number as though it were a prediction.
      When it feels meaningfully different from the air temperature, Cal reports the <b>heat index</b>
      (or <b>wind chill</b> in the cold) alongside it — that is the number a person actually acts on,
      and it can run well above the temperature: measured here, 95&deg;F air against a 107&deg;F heat
      index. If the source publishes that value in a unit the software does not recognise, it is
      <b>dropped rather than converted on a guess</b>, because a wrong number is worse than no number.
      Known limitation, stated plainly: the station is a real place some distance away, and its
      reading can differ from the estimate for a specific spot. What Cal reports is a real
      measurement of somewhere nearby, not a forecast for where you're standing.
      <br><br>This page used to put a number on that gap — "five degrees or more". That number is
      withdrawn rather than quietly softened, and the reason is worth saying: it was measured
      against a <b>reference point that was itself nearly four miles wrong</b>, from a station
      believed to be five miles off that is actually about one. The reference has been corrected.
      The gap is real and the caution stands, but the size of it has not been honestly measured
      yet, so no figure is quoted here until it has been.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather-point-accuracy.md" target="_blank" rel="noopener noreferrer">The write-up, including the fix ↗</a></div></details>
    <details><summary>What is an "exchange"?</summary><div class="a">
      Almost everything Cal transmits is a response to being prompted, so the page is organised that
      way: the incoming message is the head, and Cal's reply is indented beneath it. Two things don't
      fit that shape and are marked separately — <b>unprompted</b> sends (an operator message, with no
      ask above it) and messages overheard but never addressed to Cal.</div></details>
    <details><summary>What's in the decision trace?</summary><div class="a">
      Open <b>trace</b> on an exchange Cal <i>answered</i> and you get two pictures; on one it did not
      answer, the chain is skipped and only the second appears. First, <b>the chain that produced
      the reply</b>, read left to right and numbered: the question that arrived, <b>what the software
      recognised in it</b>, what it went and fetched as a result, and finally what the model wrote.
      The recognition step is worth a look, because it is the least magical part of the whole system:
      Cal decides a message is a weather question by <b>matching plain words</b> — one strong word like
      <code>temperature</code> is enough on its own, otherwise it takes two weather words, or one plus
      a question mark. No model takes part in that decision, and the trace shows you which words
      actually matched. The question is
      <b>received normally</b> — it does real work, because it is what decided which fact to look up —
      but it stops at the dashed line. <b>Only the fetched fact crosses</b> into the model, whose
      entire job is to put that fact into words. That is why Cal cannot invent a temperature. On an
      ordinary reply with no lookup behind it there is no dashed line, because the model really was
      given the message itself.
      <br><br>Below it, the <b>stages in the order they happened</b>: received, gated, sanitized,
      grounded, narrated, sent. Each carries its own detail — which checks passed and which one stopped
      it, what the sanitizer changed, which weather station the reading came from and how old it was,
      the model and how long generation took. A message that fails a check <b>stops the spine where it
      failed</b>, and a single hollow step says outright that nothing further ran. That is read off the record rather
      than assumed: a message that was gated out carries no sanitizer result, no fact, no model and no
      destination. It is the machinery, not a narration — see below.</div></details>
    <details><summary>Why doesn't the trace show Cal's "thinking"?</summary><div class="a">
      Because there isn't any to show, and inventing some would be worse than showing nothing. Reply
      generation returns plain text — there's no hidden reasoning being discarded. We could ask the
      model to narrate why it chose a reply, but that narration <b>wouldn't be a faithful account of
      the computation</b>, and publishing it as though it were would present a plausible story as
      mechanism. It would also put unbounded, model-authored text — influenced by whatever a stranger
      transmitted — onto a public page, which is what the rest of the design works to prevent.</div></details>
    <details><summary>What's the diagram in the "received" stage?</summary><div class="a">
      The <b>link</b> the message travelled: who transmitted, who received it, how many <b>hops</b> it
      took, and the signal strength on the final leg. <b>Direct</b> means Cal heard the sender's own
      radio; anything above zero means other nodes relayed it. Where the firmware reports a relay it
      gives only <b>one byte</b> of that node's id — enough to narrow the candidates, not to name one —
      so it's shown truncated and never resolved to a name. The sender's box is coloured by what Cal
      did with the message, so the diagram and the verdict can't disagree.
      <br><br>The hop count is sometimes genuinely unknown, and the caption says which kind of unknown
      it is: a message received before this feature existed, or one where the sender reported nothing
      usable. It will not claim a reason it can't support — it did exactly that until 2026-08-11, and
      the changelog says how.</div></details>
    <details><summary>Why isn't it a real map?</summary><div class="a">
      Because this page is public and the base station sits at a fixed private address — a pin would
      publish it, and a series of pins would publish movements. So the diagram shows <b>topology</b>
      (who → who, how many hops) and never a location. No coordinates are stored by this project at
      all: the bridge deliberately reads names, hops and signal from the node database and skips the
      position field, even though about half the neighbours broadcast one. Cal's own node doesn't
      advertise a position either.</div></details>
    <details><summary>What about privacy and safety?</summary><div class="a">
      The channel is public by design, and this page only ever shows public-channel traffic and Cal's
      own telemetry — never the operator's data. Incoming text is treated as hostile: anyone in radio
      range can transmit anything, so messages are sanitized before they go anywhere near the model,
      the model runs with <b>no tools and no private context loaded</b>, and the trace reports
      <i>that</i> something was redacted and how many times, never <i>what</i>. An adversarial review
      of this exact path caught a real location leak before it shipped.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/channel-trust-and-agency.md" target="_blank" rel="noopener noreferrer">The trust model ↗</a></div></details>

    <h3>The project</h3>
    <details><summary>Is the code public? Can I run my own?</summary><div class="a">
      Yes — cal-mesh is open source, and the whole thing (bridge, responder, dashboard) is on GitHub.
      It ships a <code>config.example</code>: point it at your own Meshtastic node and you can run your
      own Cal on your own mesh. It has already had its first outside contribution via fork and pull
      request.
      <br><a href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">github.com/deanssamclaw/cal-mesh ↗</a></div></details>
    <details><summary>Where's the design reasoning written down?</summary><div class="a">
      In the repo, as proposals — including the arguments that <i>lost</i>, which are usually the more
      useful half. Each one is written to be reviewed and attacked before anything gets built.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather.md" target="_blank" rel="noopener noreferrer">Giving Cal live knowledge — the framework ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-roadmap.md" target="_blank" rel="noopener noreferrer">Capability roadmap — what Cal could learn next ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather-intent-layer.md" target="_blank" rel="noopener noreferrer">Two of my own proposals, refuted with measurements ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/unknown-sender-tier.md" target="_blank" rel="noopener noreferrer">Answering strangers — "we hear you" ↗</a>
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/channel-trust-and-agency.md" target="_blank" rel="noopener noreferrer">Channel trust &amp; agency — how much Cal is allowed to be ↗</a></div></details>
  </div>
  <div class="card" id="changelog"><h2>Changelog</h2>
    <div class="clog">
      <div class="ci"><span class="cd">2026-08-21</span><b>v5 &mdash; what Cal <i>cannot</i> answer is now a tab, next to what it did.</b> A distiller reads every exchange once a day and files the ones that reached the model instead of a capability. That list used to live in a file on the machine that nobody opened, which is indistinguishable from the thing not running. It sits in the tab strip now, beside Open Exchanges and Direct Messages, because it is made <i>of</i> them &mdash; the same traffic, read for what it could not answer. Three things are published that a capability list would not tell you. <b>The queue itself</b>, which is what this node still gets wrong. <b>The commit</b> that armed each answer, linked, and whether that commit has actually been pushed &mdash; a sha sitting only on the machine that made it is work nobody can see, and "armed" would overstate it. And <b>corrections</b>: doers that had to be fixed <i>after</i> they were armed, itemised rather than counted, because a loop scored only on what it builds will build. The number worth watching is the one at the end of that row: how many of these the distiller found versus how many a person spotted. It currently reads zero to three. Nothing is built from the queue until someone decides what the right answer would be measured against &mdash; every answer that has held up here was pinned to something outside this repo, and a doer graded only by its own test suite is a guess with a green check next to it.</div>
      <div class="ci"><span class="cd">2026-08-19</span><b>v4 &mdash; the trace is dark, and only the trace.</b> The page around it is unchanged: same tiles, same streams, same light palette it moved to on 12 August. What changed is that opening a <b>trace</b> now drops you onto a dark instrument panel instead of a lighter shade of the same page. The reason is that these are two different things to look at. The page is a board you <i>scan</i> &mdash; is the radio up, what came in, which nodes are near. A trace is one record you <i>read</i>, and it is drawn with boxes, wires and dots that have nowhere to sit when their ground is the same white as the list they came out of. It also keeps v3&rsquo;s elevation honest. On a light page a raised surface is whiter than what it sits on; on a dark one it is lighter than what it sits on. Inverting the ground without inverting that rule would have left the well and the raised planes reading backwards, so the recessed panel is now the darkest thing on screen and every box lifted off it is lighter, which is the same hierarchy stated the other way round. The colour ramp under the gauges follows the same logic: still one hue, still never a rainbow, but running dark-to-bright, because "light to dark" is an instruction about the background as much as the ink. Two colours had to be re-picked by hand rather than swapped, and they are the <i>same two places</i> that had to be re-picked when this page went light &mdash; the link diagram&rsquo;s colours are written into the drawing code, and the "nothing was looked up" box carries its colour inline, so neither of them can ever be reached by changing a palette. Contrast was measured rather than judged: every piece of text in the panel clears the AA threshold with room to spare, and the borders, the spine rail and the gauge track were each brightened until they clear the separate, stricter bar that applies to a line carrying meaning. That last part is a real change and not a formality &mdash; a hairline that reads fine at 1.2:1 on white is simply not there on black.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The step where Cal decides what a message is about is now shown.</b> The chain jumped straight from the question to the observation, which left the most important join unexplained: <i>how</i> did a sentence become a decision to go and read a particular weather station? It is not a model, and it is not clever — Cal matches <b>plain words</b>. One strong word such as <code>temperature</code> or <code>heat index</code> is enough on its own; failing that it takes two weather words together, or one plus a question mark. The trace now shows that as its own step, including <b>which words actually matched</b> and which of those three rules fired. This is the exact place a defect hid on 2026-08-11: "whats the heat index?" matched nothing, so the weather capability never ran at all, and there was nothing on the page that could have shown why. The reason is recorded by the same call that makes the decision, so what is displayed and what happened cannot drift apart. Older exchanges predate the field and say so rather than guessing.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>A fixed reply no longer claims a model wrote it.</b> When Cal refuses a forecast question, or cannot reach the weather service, it sends a sentence written into the software and no model runs at all — but the record named one anyway, so the trace would have credited it. The model is now recorded only when it actually ran, the box is labelled <b>what Cal sent</b> rather than what the model wrote, and the two fixed cases stopped sharing one status: a deliberate refusal and a failed fetch are different events, and calling both "weather unavailable" made the design working look like something breaking.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The top of the trace was telling people their message had failed.</b> It drew the sender's own words struck through, with <b>NOT SENT</b> beside them. That was meant to say "not forwarded to the model" — but to anyone who did not already know how this works it reads as <i>your message did not send</i>, which is the opposite of the truth: it arrived perfectly, and it is the very thing that caused the lookup to happen. The picture also never showed where the fetched fact came from, so the reply appeared out of nowhere with no visible connection to the question. It is now a numbered chain read left to right — the question arrives and <b>chooses what to look up</b>, the software fetches a real observation, and only that observation crosses a marked line into the model. Nothing is struck through, because nothing was discarded. The clever part of the old drawing was the curved wires; they were sophistication in the service of a layout that misled, and a straight line that reads correctly beats a bezier that does not.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>v3 — the trace is drawn with depth.</b> Same record and the same claims as <code>old-2</code>, drawn as a mechanism that runs rather than a list that sits. The two connectors are now <b>measured</b> from real box geometry and curve so they actually land on the reply, with the cut drawn as a genuine break in one of them. Surfaces carry elevation: the panel is a recessed well, the fetched fact and the reply sit on raised planes, and the message that was never forwarded is flat and unlit — so the hierarchy is visible rather than announced. The signal stopped being two bare numbers: <b>rssi</b> and <b>snr</b> are drawn against the range a LoRa link actually lives on, which is how you can see at a glance that this message arrived strong, and why it was heard direct. And the stages now <b>assemble in the order they ran</b> when a trace is opened, once per open — a trace records something that happened in time, and drawing it as furniture was the flattest thing about it. The colour ramp under the instruments is a single hue by rule: an ordered quantity gets one hue light to dark, never a red-amber-green rainbow, which is both a rainbow ramp and a pair that sits about 1.5 units apart under the commonest form of colour blindness.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The trace shows what happened instead of listing it.</b> It was thirteen rows of grey label and value, which gave a passed check, a station reading and the words that went out over the air exactly the same weight — and buried the one thing a trace is actually about, which is the order things happened in. Two changes. <b>The reply is now drawn as what it is:</b> two things compete to become the answer, the sender's own words and a fact the software fetched, and on a capability answer the sender's words are visibly <b>cut</b> — they select which fact to look up and are never handed to the model. When there is no capability the same picture inverts honestly: the message is quoted to the model and nothing is cut. <b>Below it the stages run down a spine in the order they happen</b> — received, gated, sanitized, grounded, narrated, sent. A message that fails a check stops the spine where it failed, the rail below it goes dashed, and the stages it never reached are drawn unreached rather than left out. That is read off the record, not assumed: a gated-out decision carries no sanitizer result, no fact, no model and no destination. Two numbers now have a scale under them rather than standing alone — how old the weather reading was against the hour these stations report on, and how long generation took against the <b>7-44 s</b> the same prompt was measured spanning. A closed trace is also no longer built at all, so opening one costs the work rather than every message paying it every three seconds.</div>
      <div class="ci"><span class="cd">2026-08-12</span><b>The page is light now.</b> Same information and the same layout, on a light palette instead of the dark one it launched with. Two colours had to be re-picked rather than reused: the green and amber that read clearly against a dark background land near a third of the required contrast on a white one, so they are now darker shades of the same hues. The link diagram needed a pass of its own — its colours are written into the drawing code rather than read from the page palette, so swapping the palette alone would have left dark boxes and dark labels sitting on a white card, which is exactly the kind of change that looks finished until someone opens a trace. <b>The retired version at <code>old-1</code> is deliberately still dark.</b> It is kept as a record of what the page used to be, and restyling it would make that record wrong.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Two capture bugs, and a caption that confidently explained one of them wrongly.</b> Every message received since 2026-08-09 was showing "hops unknown — this message predates routing capture". The messages did not predate anything: the hop count is <i>hop_start</i> minus <i>hop_limit</i>, and the radio library builds its packet view with a converter that omits any number equal to zero — so a message that used its <b>entire</b> hop budget arrived with <i>hop_limit</i> missing and was recorded as "no data", indistinguishable from a message that carried no routing at all. The most-relayed messages were the ones being thrown away. Worse was the caption: one asserted cause printed for a blank that has several. It now states only what the record supports, and older messages that genuinely predate the feature still say so.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Cal can tell you the heat index.</b> Asked for "current temperature and heat index", Cal answered the temperature, said nothing about the other half, and gave no sign anything had been left out — while the weather service was publishing a <b>107&deg;F</b> heat index against <b>95&deg;F</b> air in the very same reading. The software had never looked at the field. Heat index and wind chill are now included whenever they differ from the air temperature by at least 3&deg;F, and when they do they take the place of wind in the reply: at a twelve-degree gap, how hot it feels <i>is</i> the weather, and a five-to-seven word message cannot carry both. If the value ever arrives in a unit the software does not recognise it is dropped rather than converted on a guess — read as Fahrenheit instead of Celsius, that 107 becomes "42F" on a 95-degree afternoon. Checked by running it: eight replies, both numbers survived all eight times.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Reply time is no longer shown as if it were thinking time.</b> Each exchange prints how long generation took, and a reader reasonably takes that as a measure of the model. It mostly is not. Measured here: a <i>one-token</i> reply through the same locked-down command costs <b>5.4–10.5 s</b>, while a full seven-word weather reply costs <b>7–44 s</b> — the <i>same prompt</i> varying about sixfold run to run. The floor is process startup and a network round trip; the spread is noise; the part attributable to composing seven words is small. The figure now carries that context instead of standing alone. Consequence worth stating plainly: choosing a larger model would be close to invisible in these numbers, because the time is not going where it looks like it is going.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Link diagram redrawn.</b> Two things it got wrong. It drew a single "relay" box no matter how far a message had travelled, which quietly implied the whole path was known — a hop is a rebroadcast, so three hops means three relays stood in between and the firmware only ever names the last one. The relays it cannot name are now drawn dashed and counted, so the picture shows the size of what it does not know. And every sentence moved out of the drawing into the rows beneath it: a drawing has a fixed canvas and its text neither wraps nor shrinks, so the caption had been clipped at both ends and the signal reading was painting over the node it pointed at. The drawing now holds boxes and arrows only, at one fixed scale, so a message that went three hops and one that went direct are drawn the same size.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Three messages got their hop count back.</b> Once <i>hop_start</i> is known the arithmetic is forced, so records caught by the bug above were recovered rather than left blank — and they are labelled <b>recovered</b>, because reconstructed after the fact is not the same kind of fact as measured at the time. A worked example, all of it from the message the operator remembered sending from far away: he was right that it did not reach Cal directly. It spent its whole budget of <b>3 hops</b>, and the last relay's one-byte id (<code>·c6</code>) matches exactly one node — Cal's own listener across the house. The signal is the giveaway: that listener heard the sender at <b>−126 dBm</b> and barely caught it, while Cal heard the same message at <b>−50 dBm</b>, because Cal was hearing the relay, not the sender. Signal strength describes the last leg only, never the distance to whoever spoke.</div>
      <div class="ci"><span class="cd">2026-08-11</span><b>Cal was dropping the sender's ID on first contact.</b> The library resolves a node's <code>!id</code> through its list of known nodes and returns nothing for a node it has not yet been introduced to — while the packet itself carries that node's number the entire time. So the ID went missing exactly when a stranger spoke to Cal for the first time. Measured here: a "Hi" on 2026-08-11 was logged from nobody; the sender's introduction arrived eleven minutes later and it was <code>!ba0cc0c0</code> all along. The bridge now falls back to the number the packet carries. Node IDs remain unauthenticated and spoofable — that has not changed and cannot.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Forecast questions are now refused, not answered.</b> Asking about tonight, tomorrow, or whether it's going to rain used to return <i>current</i> conditions — a present-tense reading dressed as a prediction. Cal now recognises a forecast-shaped question deterministically and replies "Only current conditions, no forecast yet," making no lookup at all.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>FAQ rewritten and grouped.</b> It assumed you already knew what a mesh network, Meshtastic and LoRa were, and never said why an AI on a radio is worth building. It now starts from those, explains how the project got here in stages, and links out to the source and to the design proposals — including the arguments that lost.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Link diagram in every trace.</b> Shows the path a message took — sender, any relay, Cal HT — with the hop count and the signal on the last leg. The bridge now records per-message routing (hops taken, and the one-byte relay id when the firmware supplies it); messages received before that show "hops unknown" rather than implying they were direct. It is a topology diagram, not a geographic one, on purpose — see the FAQ.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>v2 is now the main page.</b> The previous two-column layout is retired but still readable at <code>old-1</code>. Retired versions keep a permanent <code>old-N</code> address — numbered by when they were retired, never renumbered — so a link to one always shows the same page.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>v2.</b> Inbound and Outbound merged into a single <b>Exchanges</b> stream — the ask is the head, Cal's reply is indented beneath it. Removes the duplication that made v1 busy (every reply used to render twice) and reads properly on a phone.</div>
      <div class="ci"><span class="cd">2026-08-09</span><b>Decision trace.</b> Every exchange opens into the machinery behind it: the gate ladder, what the sanitizer changed, the capability and the exact injected fact, the model and generation time. Deliberately no model "reasoning" — see the FAQ.</div>
      <div class="ci"><span class="cd">2026-08-09</span>Inbound/Outbound paired, reply latency in seconds, and the battery tile made sentinel-aware (a reading over 100 means external power, not a charge).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Cal HT moved to <b>WiFi</b>: reflashed to the BaseUI firmware and switched the bridge to TCP — the radio runs untethered, USB is just power.</div>
      <div class="ci"><span class="cd">2026-08-08</span>From Bob's PR: message latency tracking and an /api/stats endpoint with daily aggregates.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Second security &amp; privacy audit: device MAC removed from the public API, DoS bounds, log rotation.</div>
      <div class="ci"><span class="cd">2026-08-08</span>Published as a public GitHub repo; per-neighbor 1-hour SNR sparklines (idea from Bob).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 2 — autonomous responder: Cal replies on its own when addressed (fleet-only, kill switch, rate limits).</div>
      <div class="ci"><span class="cd">2026-08-08</span>Level 1 — always-on bridge; node flashed to Meshtastic 2.7.26 and brought online as "Cal HT".</div>
    </div>
  </div>
</main>
<footer>cal-mesh dashboard v5 · auto-refresh 3s · read-only · previous version: <a class="faqlink" id="oldlink2" href="old-4">old-4</a></footer>
<script>
const $=s=>document.querySelector(s);
const DIR=(function(){let p=location.pathname.replace(/\/(v2|v3|v4|old-\d+)\/?$/,'/');
 return p.endsWith('/')?p:p+'/';})();
let SNR={}, lastNodes=[], nodeSort={key:null,dir:1}, lastXsig=null, lastLsig=null;
let ROUTES={me:null,ours:{},others:[]};
let SELF={id:null,name:null};
const NODE_LABELS={short:'Short',long:'Name',hw:'HW',hops:'Hops',snr:'SNR'};
function esc(s){return (s??"").toString().replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hhmmss(ts){try{return new Date(ts).toLocaleTimeString();}catch(e){return ts;}}
function daystamp(ts){try{return new Date(ts).toLocaleDateString(undefined,{month:'short',day:'numeric'});}catch(e){return '';}}
function secs(ms){return (ms/1000).toFixed(2)+'s';}
function tile(k,v,sub,cls){return `<div class="tile${cls?' '+cls:''}"><div class="k">${k}</div><div class="v">${v}${sub?` <small>${sub}</small>`:''}</div></div>`;}
function fmtDur(s){s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
 return h?`${h}h ${m}m`:(m?`${m}m ${ss}s`:`${ss}s`);}
function batteryLabel(m){ if(m.battery==null) return '—';
  if(m.battery>100) return 'ext power';   // Meshtastic sentinel, not a charge level
  return m.battery+'%'; }
function skipWhy(r){
  const m={sender_not_allowed:'sender is not on the allow-list — Cal heard it perfectly well and chose not to answer',
           not_addressed:'Cal was not addressed (no "cal" mention, not a DM)',
           disabled:'the responder kill switch is off',
           too_old:'the message was older than the freshness window',
           rate_limited:'rate limit reached for this sender',
           cooldown:'per-sender cooldown still active',
           self:'this was Cal\'s own message'};
  return m[r]||esc(r||'unknown');
}
function verdictTag(x){
  if(x.verdict==='replied') return '<span class="tag tx">REPLIED</span>';
  if(x.verdict!=='skipped') return '<span class="tag quiet">NOT EVALUATED</span>';
  return x.reason==='sender_not_allowed'
    ? '<span class="tag offlist">OFF-LIST · heard, not answered</span>'
    : `<span class="tag quiet">NO REPLY · ${esc(x.reason)}</span>`;
}
function row(k,v){return `<div class="trow"><span class="tk">${k}</span><span class="tv">${v}</span></div>`;}
function nodeName(id){
  const n=lastNodes.find(n=>n.id===id);
  return n?(n.short||n.long||id):id;
}
// Clamp a label to what a fixed-width box can actually hold. SVG text does not wrap and does
// not shrink: an over-long node name silently paints across its own border and its neighbour.
function fitLabel(s, n){ s=(s==null?'':String(s)); return s.length>n ? s.slice(0,n-1)+'…' : s; }

// A LINK diagram, deliberately not a geographic one: who transmitted, who relayed it, who
// received it. It uses only what is already public on the air — there are no coordinates here
// and none are stored, because this page is public and the base station sits at a fixed
// private location.
//
// TWO LAYOUT RULES, both learned by shipping the violation (2026-08-11):
//
//   1. NO PROSE INSIDE THE SVG. A viewBox is a fixed canvas and <text> neither wraps nor
//      reflows, so a caption long enough to be worth reading gets clipped at BOTH ends — which
//      is exactly what happened when the caption grew to explain the relay byte. Every sentence
//      now lives in HTML underneath, in the same key/value rows the rest of the trace uses, so
//      it wraps and can never be truncated. The SVG holds boxes and arrows and nothing else.
//   2. NOTHING FLOATS BETWEEN THE BOXES. The old signal label sat at the arrow midpoint, and
//      once a third box appeared the gaps were narrower than the label — it painted over the
//      node it was pointing at. Signal is a fact about the last hop, so it is stated as such
//      in the rows below rather than squeezed into the gap.
// A harvested traceroute path, drawn as what it is: a SEPARATE measurement taken at its own
// moment, not a better version of this message's own diagram. Backfilling it into that diagram
// was the tempting move and it would be a fabrication -- a path is true only when it is
// measured, and a message that arrived two hops ago did not necessarily take the route a
// traceroute found four minutes later. So it sits below, with its own timestamp and age, and
// it says outright that it is not this message's path.
//
// Only paths where CAL was the requester are drawn here. An overheard traceroute between two
// other nodes is real topology but says nothing about how Cal reaches anybody; the server
// separates the two and this reads only `ours`.
function pathAge(ts){
  const t=Date.parse(ts); if(!isFinite(t)) return null;
  const s=Math.max(0,(Date.now()-t)/1000);
  if(s<90) return Math.round(s)+' s ago';
  if(s<5400) return Math.round(s/60)+' min ago';
  if(s<172800) return Math.round(s/3600)+' h ago';
  return Math.round(s/86400)+' days ago';
}
function chain(nodes, snrs, complete){
  // One SNR per LINK, in order, exactly as the firmware fills it. When the array is not one
  // entry per link it is NOT stretched to fit -- a missing reading is drawn missing.
  let h='<div class="pchain">';
  nodes.forEach((n,i)=>{
    h += (n==null) ? '<span class="phop unk">unnamed</span>'
                   : '<span class="phop">'+esc(nodeName(n))+'</span>';
    if(i<nodes.length-1){
      const v = (complete && snrs && snrs.length>i) ? snrs[i] : null;
      h+='<span class="plink"><span class="parr"></span>'
       + '<span class="psnr">'+(v==null?'? dB':(v>0?'+':'')+esc(v)+' dB')+'</span></span>';
    }
  });
  return h+'</div>';
}
function pathHtml(nodeId){
  const r = (ROUTES.ours||{})[nodeId];
  if(!r || !r.path || r.path.length<2) return '';
  const age = pathAge(r.ts);
  const back = [r.traced].concat(r.route_back||[], [r.requester]);
  const hasBack = r.snr_back_complete && r.snr_back && r.snr_back.length===back.length-1;
  return '<div class="pathm"><div class="pathh">measured path to this node'
    + (age?' <span class="pathage">&middot; traceroute '+esc(age)+'</span>':'')
    + '</div>'
    + '<div class="pdir">out</div>' + chain(r.path, r.snr_towards, r.snr_towards_complete)
    + (hasBack ? '<div class="pdir">back</div>' + chain(back, r.snr_back, true) : '')
    + '<span class="hint">A traceroute Cal sent and got an answer to, so every hop is named '
    + 'rather than counted. <b>This is not this message&rsquo;s path</b> &mdash; it was measured '
    + 'at its own moment, and a route is only true when it is measured. The two directions are '
    + 'listed separately because they are measured separately and often differ.</span></div>';
}
function linkSvg(x){
  const hops=x.hops;
  const relayId = x.relay_byte!=null ? '·'+x.relay_byte.toString(16).padStart(2,'0') : null;
  // Colour the sender box by what Cal DID with it, so the diagram carries the same signal as
  // the verdict badge above. Green on an off-list sender read as "allowed" — backwards.
  const offlist = x.verdict==='skipped' && x.reason==='sender_not_allowed';
  const quiet   = x.verdict==='skipped' && !offlist;
  // v4: these are the colours the 2026-08-12 light switch had to re-pick by hand, for the
  // same reason — they live here, not in the sheet, so a palette change alone never reaches them.
  const senderC = offlist ? {fill:'#3a2d0a', stroke:'#e3b341'}      // matches the OFF-LIST tag
                : quiet   ? {fill:'#1c222b', stroke:'#7d8590'}
                          : {fill:'#1c222b', stroke:'#3fb950'};
  const stops=[x.from
    ? {lab:esc(fitLabel(nodeName(x.from),15)), sub:esc(fitLabel(x.from,15)), fill:senderC.fill, stroke:senderC.stroke}
    : {lab:'unknown', sub:'no id recorded', fill:senderC.fill, stroke:senderC.stroke}];
  // A hop is a REBROADCAST, so N hops means N relays stood between the sender and Cal — and the
  // firmware only ever tells us the last one. Drawing a single relay box for N>1 quietly implied
  // we knew the whole path. The ones we cannot name are now counted and drawn dashed, so the
  // diagram shows the size of what it does not know instead of hiding it.
  if(hops==null) stops.push({lab:'?', sub:'routing not recorded', dim:true, dash:true});
  if(hops>1) stops.push({lab:'?', sub:(hops-1)+' unknown relay'+(hops-1>1?'s':''), dim:true, dash:true});
  if(hops>0) stops.push({lab:'relay'+(relayId?' '+relayId:''), sub:relayId?'last relay':'id not reported', dim:true});
  stops.push({lab:esc(fitLabel(SELF.name||'Cal HT',15)), sub:esc(fitLabel(SELF.id||'',15)), self:true});

  // The canvas is a CONSTANT width sized for the widest case (4 boxes) and the row is centred
  // inside it. Sizing the viewBox to the content instead makes the SVG scale up to the CSS
  // width, so a two-box diagram renders with boxes nearly twice the size of a four-box one —
  // the same message looks like a different kind of object depending on how far it travelled.
  const n=stops.length, bw=140, gap=38, by=10, bh=50, MAXN=4;
  const W=20+MAXN*bw+(MAXN-1)*gap, H=by+bh+10;
  const x0=(W-(n*bw+(n-1)*gap))/2;
  let svg='';
  stops.forEach((s,i)=>{
    const bx=x0+i*(bw+gap);
    if(i>0){
      const x1=bx-gap+2, x2=bx-4;
      svg+=`<line x1="${x1}" y1="${by+bh/2}" x2="${x2-6}" y2="${by+bh/2}" stroke="#7d8590" stroke-width="2"/>`
         +`<path d="M${x2-7} ${by+bh/2-4.5} L${x2} ${by+bh/2} L${x2-7} ${by+bh/2+4.5}z" fill="#7d8590"/>`;
    }
    const fill=s.fill||(s.self?'#221a35':'#1c222b');
    const stroke=s.stroke||(s.self?'#bc8cff':(s.dim?'#7d8590':'#3fb950'));
    svg+=`<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="8" fill="${fill}" stroke="${stroke}" `
       +`stroke-width="1.5"${s.dash?' stroke-dasharray="5 4"':''}/>`
       +`<text x="${bx+bw/2}" y="${by+21}" fill="#e6edf3" font-size="13" font-weight="600" text-anchor="middle">${s.lab}</text>`
       +`<text x="${bx+bw/2}" y="${by+37}" fill="#9aa7b4" font-size="10.5" text-anchor="middle">${s.sub}</text>`;
  });
  const diagram=`<div class="link-d"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" `
       + `role="img" aria-label="link diagram">${svg}</svg></div>`;

  // A null hop count has more than one cause and they are not interchangeable. Records written
  // before routing capture shipped (2026-08-09) carry no hop_start KEY AT ALL; records written
  // after always carry the key, even when its value is null. Saying "predates routing capture"
  // for both put a false claim about a message's history on a public page.
  let rows='';
  if(hops==null)
    rows+=row('path', x.hop_start===undefined
      ? 'unknown — this message predates routing capture'
      : 'unknown — no usable hop count was recorded for this message');
  else if(hops===0)
    rows+=row('path','direct — Cal heard the sending radio itself, with no relay in between');
  else{
    rows+=row('path', hops+' hop'+(hops>1?'s':'')+' — relayed'
      +(hops>1?', and only the last relay is identified':''));
    if(relayId)
      rows+=row('last relay','id ends <code>'+esc(relayId)+'</code> — one byte of the node number that '
        +'relayed it, which narrows the candidates but does not identify a node');
  }
  // The signal belongs to the LAST hop and nothing else. Stating that plainly matters: a message
  // relayed from close by arrives strong no matter how far the sender is, and reading it as
  // "nearby" is the natural mistake.
  // Two numbers told a stranger nothing. Drawn against the range a LoRa link actually lives on,
  // -41 dBm is visibly near the strong end — which is the finding, and why this arrived direct.
  if(x.snr!=null||x.rssi!=null){
    let g='';
    if(x.rssi!=null) g+=gauge('signal · rssi',esc(x.rssi)+' dBm',(x.rssi+120)/90*100,
      ['-120 weak','-30 strong']);
    if(x.snr!=null) g+=gauge('signal · snr',(x.snr>0?'+':'')+esc(x.snr)+' dB',(x.snr+20)/30*100,
      ['-20 dB','+10 dB']);
    rows+=`<div class="inst">${g}</div>`
      +`<span class="hint">Measured on the <b>last hop only</b>`
      +(hops>0?' — which came from the relay, not the sender, however far away the sender was.'
              :', and this one was direct, so it does describe the sender.')+`</span>`;}
  // A recovered count was reconstructed after the fact, not measured at capture. It is sound —
  // the arithmetic is forced once hop_start is known — but it is not the same kind of fact, and
  // the page should not blur the two.
  if(x.hops_recovered)
    rows+=row('note','hop count recovered from a record predating the capture fix — reconstructed, not measured at the time');
  rows += pathHtml(x.from);
  return {diagram:diagram, rows:rows, summary:(
    hops==null?'routing not recorded'
    :hops===0?'heard direct, no relay in between'
    :hops+' hop'+(hops>1?'s':'')+' — arrived by relay')};
}
// The reply is composed from a fact the harness fetched, and on a capability answer the
// sender's own words are never handed to the model at all. That is the single least obvious
// thing about this system and it was previously one clause inside a grey row. Drawn instead:
// two inputs compete to become the reply, and one of them is visibly cut.
function flowHtml(x,t){
  const inTxt=esc(x.text||''), outTxt=esc(x.reply||'');
  if(!outTxt) return '';
  // What the software MATCHED, what it actually GOT, and whether a model ran are three
  // different things. Collapsing them is what made a refused forecast claim the model was
  // handed the message.
  const capability=!!(x.capability||(t.trigger_match&&t.trigger_match.via));
  const fetched=!!t.injected_fact;
  const modelRan=!!t.model;
  const crosses=fetched&&modelRan;
  // A DM lands on one screen, not every node in range, so the 5-7 word rule does not apply to it.
  const isDM=!!(t.dest&&t.dest.charAt(0)==='!');
  const arrow=(label)=>`<span class="arw"><span>${label}</span></span>`;
  const b1=`<div class="fb b1"><div class="fk">1 · the question</div>`
    +`<div class="fv">${inTxt}</div>`
    +`<div class="fn"><span class="onair">✓ received on air</span> — and it is what decided `
    +`${capability?'which fact to look up':'how to reply'}</div></div>`;
  const lastN=capability?4:2;
  const b3=`<div class="fb b3"><div class="fk">${lastN} · ${modelRan?'what the model wrote':'what Cal sent'}</div>`
    +`<div class="fv">${outTxt}</div>`
    +`<div class="fn"><span class="onair">✓ sent on air to ${esc(t.dest||'')}</span> — `
    +(modelRan?(isDM?'a sentence or two — a direct message lands on one screen, not every node in range'
                    :'5-7 words, because every node in range shares the airtime')
             :'a fixed sentence written into the software — no model ran for this one')+`</div></div>`;
  // A greeting ack is a THIRD shape, and both of the branches below would misdescribe it.
  // The capability branch is weather-shaped ("what Cal looked up"); the general branch says
  // the model was handed the message. Here nothing was fetched AND no model ran: plain word
  // matching selected a sentence written in advance. Drawn as exactly that.
  if(x.capability==='greeting'){
    const g1=`<div class="fb b1"><div class="fk">1 · what they said</div><div class="fv">${inTxt}</div>`
      +`<div class="fn"><span class="onair">✓ received on air</span> — from a node that is `
      +`<b>not on Cal's reply list</b>, so no answer was generated for it</div></div>`;
    const g2=`<div class="fb bx"><div class="fk">2 · what the software recognised</div>`
      +`<div class="fv">a greeting, and nothing else</div>`
      +`<div class="fn">the whole message had to be a greeting — a question mark or a real `
      +`request and this does not fire — plain word matching, <b>no model involved</b></div></div>`;
    const g3=`<div class="fb b3"><div class="fk">3 · what Cal sent</div><div class="fv">${outTxt}</div>`
      +`<div class="fn"><span class="onair">✓ sent on air to ${esc(t.dest||'^all')}</span> — the `
      +`greeting mirrored back, and only once per node per day</div></div>`;
    return `<div class="flow gen">${g1}${arrow('')}${g2}${arrow('')}${g3}</div>`
      +`<div class="flowcap">Read left to right. <b>Nothing was looked up and no model ran.</b> `
      +`Cal answers questions only from known nodes, but staying silent when a stranger says `
      +`hello reads as a snub — so a greeting gets one back, to say it was heard. Which line `
      +`goes out is <b>chosen</b> by the greeting they used, from five written in advance `
      +`(morning, afternoon, evening, day, or plain hello). Nothing they wrote is ever copied `
      +`into it, so there is nothing in the reply for a stranger to steer.</div>`;
  }
  // A computed answer is a FOURTH shape. The capability branch below is weather-shaped and
  // would say "what Cal looked up" and, with no injected_fact, "the lookup failed — the weather
  // service could not be reached" — for a reply that never touched the network. Nothing is
  // fetched here and no model runs: Python parsed the question and computed every digit.
  if(x.capability==='sunmoon'){
    const sm=t.sunmoon||{}, smm=t.sunmoon_match||{};
    const intent=sm.intent?String(sm.intent):'', ev=sm.event?String(sm.event):'';
    const refused=sm.refused?String(sm.refused):'';
    const words=[].concat(smm.sun||[],smm.moon||[]).join(', ');
    const s1=`<div class="fb b1"><div class="fk">1 &middot; the question</div><div class="fv">${inTxt}</div>`
      +`<div class="fn"><span class="onair">&#10003; received on air</span> — the wording `
      +`${words?('matched <b>'+esc(words)+'</b>, which'):'matched sun/moon wording, which'} `
      +`selected this path</div></div>`;
    const s2=`<div class="fb bx"><div class="fk">2 &middot; what the software recognised</div>`
      +`<div class="fv">${esc(intent||'a sun/moon question')}</div>`
      +`<div class="fn">the wording is classified only to <b>choose which fact to compute</b>, `
      +`never to shape the sentence</div></div>`;
    const s3=`<div class="fb bx"><div class="fk">3 &middot; what Cal computed</div>`
      +`<div class="fv">${refused?esc('refused: '+refused):esc(ev||'closed-form astronomy')}</div>`
      +`<div class="fn"><b>nothing was fetched and no model ran.</b> Sun and moon positions are `
      +`computed from closed-form astronomy — no network, so this answer works when the base is `
      +`offline. Measured against 43 U.S. Naval Observatory times: worst error 43 seconds. `
      +`No coordinate appears in the reply; the observing point is an input, never an output.`
      +`</div></div>`;
    const s4=`<div class="fb b3"><div class="fk">4 &middot; what Cal sent</div><div class="fv">${outTxt}</div>`
      +`<div class="fn"><span class="onair">&#10003; sent on air to ${esc(t.dest||'')}</span></div></div>`;
    return `<div class="flow gen">${s1}${arrow('')}${s2}${arrow('')}${s3}${arrow('')}${s4}</div>`
      +`<div class="flowcap">Read left to right. <b>Nothing was looked up and no model ran.</b> `
      +`Python computes the time and formats the sentence. Where the event does not occur at all `
      +`— a polar day, or a twilight the sun never reaches — Cal says which one is missing rather `
      +`than reporting the nearest thing it could calculate. Moonrise and moonset are not built `
      +`yet, and are refused rather than estimated.</div>`;
  }
  if(x.capability==='calc'){
    const handler=(t.calc&&t.calc.handler)?String(t.calc.handler):'';
    const c1=`<div class="fb b1"><div class="fk">1 · the question</div><div class="fv">${inTxt}</div>`
      +`<div class="fn"><span class="onair">✓ received on air</span> — it parsed as a `
      +`calculation, which is what selected this path</div></div>`;
    const c2=`<div class="fb bx"><div class="fk">2 · what the software recognised</div>`
      +`<div class="fv">${esc(handler||'a calculation')}</div>`
      +`<div class="fn">a <b>successful bounded parse</b>, not merely a number in the text — `
      +`anything that does not parse gets no answer at all</div></div>`;
    const c3=`<div class="fb bx"><div class="fk">3 · what Cal computed</div>`
      +`<div class="fv">Python, from exact constants</div>`
      +`<div class="fn"><b>nothing was fetched and no model ran</b> — the digits are computed `
      +`and formatted by the software itself</div></div>`;
    const c4=`<div class="fb b3"><div class="fk">4 · what Cal sent</div><div class="fv">${outTxt}</div>`
      +`<div class="fn"><span class="onair">✓ sent on air to ${esc(t.dest||'')}</span></div></div>`;
    return `<div class="flow gen">${c1}${arrow('')}${c2}${arrow('')}${c3}${arrow('')}${c4}</div>`
      +`<div class="flowcap">Read left to right. <b>Nothing was looked up and no model ran.</b> `
      +`The model is not in the number path at all — Python parses the question, computes the `
      +`answer from exact defined constants, and formats the sentence. Where a value is `
      +`ambiguous (a gallon is not the same on both sides of the Atlantic) or falls outside `
      +`what can be answered exactly, Cal says nothing rather than guess.</div>`;
  }
  if(!capability){
    // An authenticated DM from Dean is the same "no lookup, model ran" shape, but the model did
    // NOT see only the message — the harness also injected Cal's saved context and, when present,
    // the remembered thread. Saying "the message itself" here would be the same collapse that
    // once made a refused forecast claim the model was handed the message.
    if(t.dm_unlock){
      const mem=t.dm_memory_stored?' and the recent messages it remembers':'';
      return `<div class="flow gen">${b1}${arrow('given to the model with<br>Cal\'s saved context')}${b3}</div>`
        +`<div class="flowcap">This is an <b>authenticated direct message from Dean</b>, so the model was `
        +`given the message <b>plus Cal&rsquo;s saved context${mem}</b> — which is why the reply can be `
        +`longer and carry a thread. The context is the operator&rsquo;s public file; no secret crosses.</div>`;
    }
    return `<div class="flow gen">${b1}${arrow('sanitized, then given<br>to the model')}${b3}</div>`
      +`<div class="flowcap">Nothing was looked up for this one, so the model was given `
      +`<b>the message itself</b> and wrote a reply from it.</div>`;
  }
  // The step between the question and the lookup: plain word-matching that decides WHICH
  // capability runs. No model is involved, and it is where a 2026-08-11 defect hid — a question
  // that matched nothing never reached the capability at all, with nothing on the page to say so.
  const tm=t.trigger_match||null;
  let why='this record predates Cal keeping the matched words, so they cannot be shown', chips='';
  if(tm){
    const words=(tm.strong&&tm.strong.length?tm.strong:tm.weak)||[];
    chips=words.map(w=>`<span class="chip">${esc(w)}</span>`).join('');
    why = tm.via==='strong'
        ? (words.length>1?'any one of these is enough on its own'
                         :'this word is enough on its own')
        : tm.via==='two_weak'
          ? 'two weather words together'
          : 'one weather word plus a question mark';
  }
  const bx=`<div class="fb bx"><div class="fk">2 · what the software recognised</div>`
    +`<div class="fv">a weather question${t.forecast_asked?' about the <b>future</b>':''}</div>`
    +`<div class="fn">${chips}${chips?'<br>':''}${why} — plain word matching, `
    +`<b>no model involved</b></div></div>`;
  const st=t.obs_station?esc(t.obs_station):null;
  const age=t.obs_age_s!=null?Math.round(t.obs_age_s/60)+' min old':null;
  const warn='border-color:#8a6d1f;background:linear-gradient(180deg,#2a2213,#1f190e)';
  const b2=t.forecast_asked
    ? `<div class="fb b2" style="${warn}">`
      +`<div class="fk">3 · what Cal looked up</div><div class="fv">nothing</div>`
      +`<div class="fn">Cal holds current observations only, so a question about later is refused `
      +`outright — no lookup was attempted at all</div></div>`
    : !fetched
      ? `<div class="fb b2" style="${warn}">`
        +`<div class="fk">3 · what Cal looked up</div><div class="fv">the lookup failed</div>`
        +`<div class="fn">the weather service could not be reached, so Cal sent a fixed sentence `
        +`rather than guess a number — the fail-safe working, not a model deciding</div></div>`
      : `<div class="fb b2"><div class="fk">3 · what Cal's software fetched</div>`
        +`<div class="fv">${esc(t.injected_fact)}</div>`
        +`<div class="fn">a real observation${st?' from station '+st:''}${age?', '+age:''} — fetched `
        +`from the US National Weather Service by the software, never by the model</div></div>`;
  const join=crosses
    ? `<span class="cross"><span class="bl">only this crosses</span>${arrow('')}</span>`
    : arrow('');
  const cap=crosses
    ? `Read left to right. The question arrived fine and did real work — <b>its wording is what chose `
      +`the lookup</b> — but it never reached the model. Cal&rsquo;s software matched the words, went and `
      +`got the observation, and <b>only that observation</b> crossed the dashed line. The model&rsquo;s `
      +`entire job was to put it into words, which is why it cannot invent a temperature.`
    : `Read left to right. The question arrived fine and did real work — <b>its wording is what Cal `
      +`matched on</b> — but nothing was looked up, so <b>no model ran at all</b>. What went out is a `
      +`fixed sentence written into the software. There is no boundary drawn here because nothing `
      +`crossed one.`;
  return `<div class="flow">${b1}${arrow('reads')}${bx}${arrow(crosses?'so it<br>fetches':'so it<br>stops')}`
    +`${b2}${join}${b3}</div><div class="flowcap">${cap}</div>`;
}
function gauge(name,val,pct,ends,band,note){
  return `<div class="gauge"><div class="glab"><span class="gname">${name}</span>`
    +`<span class="gval">${val}</span></div>`
    +`<div class="track${band?'':' ramp'}">`
    +(band?`<span class="band" style="left:${band[0].toFixed(1)}%;width:${band[1].toFixed(1)}%"></span>`:'')
    +(Number.isFinite(pct)
       ? `<span class="mk" style="left:${Math.max(0,Math.min(100,pct)).toFixed(1)}%"></span>`
       : '')+`</div>`
    +`<div class="gends"><span>${ends[0]}</span><span>${ends[1]}</span></div>`
    +(note?`<div class="gnote">${note}</div>`:'')+`</div>`;
}
function stage(cls,name,summary,detail){
  return `<li class="stg ${cls}"><span class="sdot"></span>`
    +`<div class="shead"><span class="sname">${name}</span><span class="ssum">${summary}</span></div>`
    +`<div class="sdet">${detail||''}</div></li>`;
}
// The stages are a sequence in time and a gated-out message genuinely never reaches the later
// ones — verified against the records: a skipped decision carries no sanitize, no fact, no
// model and no destination. So "never reached" is read off the record, not assumed.
function spineHtml(x,t){
  const link=(x.kind==='exchange')?linkSvg(x):null;
  let s='';
  if(link) s+=stage('pass','received',link.summary,link.diagram+link.rows);
  const gated=t.gates&&t.gates.length;
  const stopped=x.verdict==='skipped';
  if(gated){
    const passed=t.gates.filter(g=>g.pass).length;
    s+=stage(stopped?'stop':'pass','gated',
      stopped?`stopped at <b>${esc((t.gates.find(g=>!g.pass)||{}).gate||'a check')}</b>`
             :`all ${passed} checks passed`,
      t.gates.map(g=>`<span class="gate ${g.pass?'gp':'gf'}">${g.pass?'✓':'✗'} ${esc(g.gate)}</span>`).join('')
      +(stopped?'<span class="rungn">later checks never evaluated</span>':''));
  }
  if(!t.model&&stopped){
    s+=stage('skip','not answered','the message was received and recorded, and nothing further ran',
      '<span class="rungn">no text was sent to a model, and nothing went on air</span>');
    return `<ol class="spine">${s}</ol>`;
  }
  if(t.sanitize){const q=t.sanitize,b=[];
    // An older record carries only the boolean and genuinely cannot say WHICH was trimmed. Say
    // that, rather than guessing — and never guess toward "your words were dropped".
    const tk=q.sentence_trim!=null?q.sentence_trim:(q.sentence_trimmed?'unknown':'none');
    if(tk==='content') b.push(`first sentence kept (${q.dropped_chars!=null?q.dropped_chars+' chars':'the rest'} dropped)`);
    else if(tk==='unknown') b.push('something was trimmed from the end — this record predates the '
      +'detail that says whether it was punctuation or content');
    else if(tk==='punctuation') b.push('trailing punctuation trimmed, no content dropped');
    if(q.length_capped) b.push('length capped');
    if(q.redactions) b.push(`${q.redactions} redaction${q.redactions>1?'s':''}`);
    if(q.flagged) b.push('injection-shaped tokens flagged');
    s+=stage('pass','sanitized',`${q.in_chars}&rarr;${q.out_chars} characters`,
      b.length?`<span class="hint">${esc(b.join(' · '))}</span>`:'<span class="hint">nothing removed</span>');}
  if(t.forecast_asked)
    s+=stage('stop','refused','asked about a future condition',
      '<span class="hint">the capability holds current observations only, so a fixed reply was sent '
      +'and no lookup was made at all</span>');
  if(x.capability){
    const ok=t.weather_ok, age=t.obs_age_s;
    let d='';
    if(age!=null){
      d=gauge('reading age',Math.round(age/60)+' min',(age/3600)*100,
        ['just measured','1 h — how often these stations report'],[0,0.001],
        'A real observation from the nearest station, never an estimate for one spot.');}
    const fstate = ok===true?'ok' : (ok===false?'FAILED':'not attempted');
    s+=stage(ok===true?'pass':(ok===false?'stop':'skip'),'grounded',
      `${esc(x.capability)} · fetch ${fstate}`
      +(t.obs_station?` · station <code>${esc(t.obs_station)}</code>`:''), d);}
  if(t.model){
    const ms=x.gen_ms;
    let d='<span class="hint">generation returns plain text — no chain of thought exists to show</span>';
    if(ms!=null){const MAXS=45,sec=ms/1000;
      const weather=t.prompt_kind==='weather';
      d=gauge('generation',secs(ms),sec/MAXS*100,['0 s',MAXS+' s'],
        weather?[7/MAXS*100,(44-7)/MAXS*100]:[0,0.001],
        (weather?'The shaded band is the <b>7-44 s</b> this same prompt was measured spanning, run '
                +'to run. ':'')
        +'Most of it is process startup and a network round trip — an order of magnitude, not '
        +'thinking time.');}
    s+=stage('pass','narrated',`<code>${esc(t.model)}</code>`,d);}
  if(t.gen_status&&t.gen_status!=='ok')
    s+=stage('stop','generation',`<code>${esc(t.gen_status)}</code>`,'');
  if(t.dest) s+=stage('pass','sent',`on air to <code>${esc(t.dest)}</code>`,'');
  return `<ol class="spine">${s}</ol>`;
}
// The trace reads top to bottom as what happened: first the outcome and how it was arrived at
// (the swap), then the machinery stage by stage (the spine). The old flat key/value list gave a
// gate check, a station reading and the transmitted reply the same weight and the same grey
// label, which left the sequence — the only thing the trace is actually about — invisible.
function traceHtml(x){
  const t=x.trace||{};
  if(!t.gates&&!t.sanitize&&!t.model){
    const l=(x.kind==='exchange')?linkSvg(x):null;
    return `<div class="tp">${l?l.diagram+l.rows:''}`+
      '<div class="tnone">No decision trace recorded — this message predates it.</div></div>';}
  let h=flowHtml(x,t)+spineHtml(x,t);
  h+='<div class="tnote">This is the machinery, not the model\'s reasoning. Generation returns plain '
   +'text with no chain of thought, and asking for a narration would produce a plausible story rather '
   +'than an account of what actually happened — so it is not shown.</div>';
  return `<div class="tp">${h}</div>`;
}
// The page re-renders every 3s, which would wipe any <details> the reader had opened. Track
// open traces by a stable key and restore the attribute on every render, so an expanded trace
// stays expanded until it is clicked shut. (Toggle doesn't bubble — the listener captures.)
const OPEN=new Set();
// Lets a trace be built on demand when its disclosure is opened, rather than for every
// exchange on every pass. Rebuilt from the current data each render, so an open trace never
// shows a stale copy of a record that has since changed.
const XBYKEY=new Map();
function xkey(x){return (x.ts||'')+'|'+(x.from||x.dest||'');}
function exchangeHtml(x){
  if(x.kind==='unprompted') return `
    <div class="xc unprompted"><div class="meta"><span class="tag tx">TX</span>
      <span>${daystamp(x.ts)} ${hhmmss(x.ts)}</span><span>→ ${esc(x.dest)}</span>
      <span class="tag ch">ch${esc(x.channel)}</span><span>${esc(x.transport)}</span>
      <span class="tag quiet">${x.source==='responder'?'UNPAIRED':'MANUAL'}</span></div>
    <div class="ask">${esc(x.text)}</div>
    <div class="norep">↳ not a reply — Cal transmitted this with no inbound ask${x.source==='responder'?', or the ask is older than the window shown':''}</div></div>`;
  return `
    <div class="xc"><div class="meta"><span class="tag rx">RX</span>
      <span>${daystamp(x.ts)} ${hhmmss(x.ts)}</span><span>${x.from?esc(x.from):'unknown sender'} → ${esc(x.to)}</span>
      <span class="tag ch">ch${esc(x.channel)}</span>${x.snr!=null?`<span>snr ${esc(x.snr)}</span>`:''}
      ${verdictTag(x)}</div>
    <div class="ask">${esc(x.text)}</div>
    ${x.verdict==='replied'&&x.reply
      ? `<div class="rep"><span class="who">↳ Cal replied${x.gen_ms!=null?` · ${secs(x.gen_ms)}`:''}${x.capability?` · ${esc(x.capability)}`:''}</span><span class="txt">${esc(x.reply)}</span></div>`
      : (x.verdict==='skipped'?`<div class="norep">↳ received, no reply — ${skipWhy(x.reason)}</div>`:'')}
    <details class="tr" data-k="${esc(xkey(x))}"${OPEN.has(xkey(x))?' open':''}><summary>trace</summary>
    <div class="tpwrap">${OPEN.has(xkey(x))?traceHtml(x):''}</div></details></div>`;
}
function setSort(k){ nodeSort=(nodeSort.key===k)?{key:k,dir:-nodeSort.dir}:{key:k,dir:1}; renderNodes(); }
function renderNodes(){
  let ns=lastNodes.slice();
  if(nodeSort.key){ const k=nodeSort.key, dir=nodeSort.dir;
    ns.sort((a,b)=>{ let x=a[k],y=b[k];
      if(k==='hops'||k==='snr'){ if(x==null&&y==null)return 0; if(x==null)return 1; if(y==null)return -1; return (x-y)*dir; }
      x=(x||'').toString().toLowerCase(); y=(y||'').toString().toLowerCase();
      return x<y?-dir:(x>y?dir:0); }); }
  const tb=$('#nodes').querySelector('tbody');
  tb.innerHTML=ns.map(n=>{ const sg=(n.snr!=null&&n.snr>0)?'snr-good':'snr-bad';
    return `<tr><td>${esc(n.short)}</td><td>${esc(n.long)}</td><td>${esc(n.hw)}</td>`+
      `<td>${n.hops==null?'—':esc(n.hops)}</td><td class="${sg}">${n.snr==null?'—':esc(n.snr)}</td>`+
      `<td>${sparkline((SNR[n.id]||{}).points, n.hops)}</td></tr>`; }).join('');
  document.querySelectorAll('#nodes th.sortable').forEach(th=>{
    const k=th.dataset.key, on=nodeSort.key===k;
    th.textContent=NODE_LABELS[k]+(on?(nodeSort.dir>0?' ▲':' ▼'):''); });
}
async function loadSnr(){try{SNR=await (await fetch(DIR+'api/snr',{cache:'no-store'})).json();}catch(e){}}
async function loadRoutes(){try{ROUTES=await (await fetch(DIR+'api/routes',{cache:'no-store'})).json();}catch(e){}}
function sparkline(pts, hops){
  if(!pts||pts.length===0){
    return (hops!=null&&hops>0)?'<span style="color:var(--dim)">multi-hop</span>'
      :'<span style="color:var(--dim)">— <small>no direct signal</small></span>';}
  if(pts.length===1){const v=pts[0][1];
    return `<span class="spark"><svg width="90" height="22"><circle cx="45" cy="11" r="2.5" fill="var(--accent)"/></svg>`+
      `<span style="color:var(--accent)">${esc(v)} <small>dB · 1 pt</small></span></span>`;}
  const W=90,H=22,pad=3;
  const ts=pts.map(p=>p[0]), vs=pts.map(p=>p[1]);
  const t0=Math.min(...ts),t1=Math.max(...ts),vmin=Math.min(...vs),vmax=Math.max(...vs);
  const sx=t=>pad+(t1===t0?(W-2*pad):((t-t0)/(t1-t0))*(W-2*pad));
  const sy=v=>pad+(1-(vmax===vmin?0.5:(v-vmin)/(vmax-vmin)))*(H-2*pad);
  const d=pts.map((p,i)=>(i?'L':'M')+sx(p[0]).toFixed(1)+' '+sy(p[1]).toFixed(1)).join(' ');
  const last=pts[pts.length-1];
  const k=Math.max(1,Math.floor(pts.length/3));
  const avg=a=>a.reduce((s,x)=>s+x,0)/a.length;
  const dv=avg(vs.slice(-k))-avg(vs.slice(0,k));
  const arrow=dv>1.5?'↗':(dv<-1.5?'↘':'→');
  const col=dv<-1.5?'var(--warn)':(dv>1.5?'var(--ok)':'var(--accent)');
  return `<span class="spark" title="${pts.length} samples · now ${esc(last[1])} dB">`+
    `<svg width="${W}" height="${H}"><path d="${d}" fill="none" stroke="${col}" stroke-width="2" `+
    `stroke-linejoin="round" stroke-linecap="round"/><circle cx="${sx(last[0]).toFixed(1)}" `+
    `cy="${sy(last[1]).toFixed(1)}" r="2.5" fill="${col}"/></svg>`+
    `<span style="color:${col}">${arrow} ${esc(last[1])}</span></span>`;
}
async function tick(){
 let d; try{d=await (await fetch(DIR+'api/state',{cache:'no-store'})).json();}
 catch(e){$('#conn').className='pill bad';$('#conn').textContent='dashboard offline';return;}
 const st=d.status||{}, m=st.metrics||{}, node=st.node||{}, rp=d.responder||{};
 const on=st.connected;
 $('#conn').className='pill '+(on?'ok':'bad');
 $('#conn').textContent=on?'● radio connected':'● radio down';
 $('#sub').textContent=`${node.longName||'?'} (${node.shortName||'?'}) · ${node.id||''} · fw ${st.firmware||'?'}`;
 const live=rp.enabled==='true';
 $('#tiles').innerHTML=[
   tile('Battery', batteryLabel(m), m.voltage!=null?m.voltage.toFixed(2)+'V':''),
   tile('Bridge', (d.bridge.state==='running'?'running':'stopped'), d.bridge.pid?('pid '+d.bridge.pid):''),
   tile('Uptime', st.uptime_s!=null?fmtDur(st.uptime_s):'—'),
   tile('Responder', `<span class="dot ${live?'on':'off'}"></span>${live?'live':'off'}`,
        (rp.model?rp.model.replace('claude-','').replace(/-\d+$/,''):'')+' · '+(rp.allow_count||0)+' allowed'),
   tile('Sent / Received', `${(d.totals&&d.totals.sent)??0} / ${(d.totals&&d.totals.recv)??0}`),
   tile('Ch util', m.chUtil!=null?m.chUtil.toFixed(1)+'%':'—', m.airUtilTx!=null?('air '+m.airUtilTx.toFixed(2)+'%'):''),
 ].join('');
 const cfg=d.config||{}, active=(st.transport||cfg.TRANSPORT||'serial');
 $('#active-t').textContent = on ? 'live' : 'down';
 $('#active-t').className = 'badge right ' + (on ? 'ok' : 'warn');
 const SEGS=[['serial','USB'],['tcp','WiFi']];
 $('#trans').innerHTML =
   `<div class="seg" role="group" aria-label="radio transport">`
   + SEGS.map(([k,lab])=>`<span class="sg${active===k?' on':''}"`
       + (active===k?' aria-current="true"':'') + `>${lab}</span>`).join('')
   + `</div><div class="segd">`
   + (on ? `Carrying traffic over <b>${active==='tcp'?'WiFi':'USB'}</b>`
         : `Not connected &mdash; last configured for <b>${active==='tcp'?'WiFi':'USB'}</b>`)
   + (active==='tcp'&&cfg.HOST?` to <code>${esc(cfg.HOST)}</code>`:'')
   + `. The other is idle, which is not the same as broken.</div>`;
 SELF={id:node.id||null, name:node.shortName||node.longName||null};
 lastNodes=(d.nodes&&d.nodes.nodes)||[];
 const xs=d.exchanges||[];
 const dms=d.dm_exchanges||[];
 $('#xc-n').textContent=xs.length;
 $('#dm-n').textContent=dms.length;
 // Only touch the DOM when the content actually changed. Cheap, and it stops the 3s refresh
 // from fighting the reader (lost text selection, scroll jump) when nothing has happened.
 const sig=JSON.stringify([xs,dms,SELF,lastNodes.map(n=>[n.id,n.short])]);
 if(sig!==lastXsig){
   lastXsig=sig;
   XBYKEY.clear(); xs.forEach(x=>XBYKEY.set(xkey(x),x));
   dms.forEach(x=>XBYKEY.set(xkey(x),x));
   $('#exchanges').innerHTML=xs.length?xs.map(exchangeHtml).join('')
     :'<div class="empty">nothing on air yet — mesh is quiet or awaiting first inbound</div>';
   // Same renderer, deliberately. A second one would drift from the first, and the whole
   // point of the trace is that what it shows and what happened cannot diverge.
   $('#dm-exchanges').innerHTML=dms.length?dms.map(exchangeHtml).join('')
     :'<div class="empty">no direct messages yet</div>';
   hydrateOpen();
 }
 $('#nn').textContent=lastNodes.length;
 renderNodes();
 renderLearning(d.learning||{});
}
function shaLink(c,pushed){
  if(!c) return '<span class="lwarn">not committed</span>';
  const url='https://github.com/deanssamclaw/cal-mesh/commit/'+encodeURIComponent(c);
  // "pushed" is not decoration: a commit only on the Mac is work nobody else can see, and
  // saying "armed" without saying that would overstate it.
  return `<a class="lsha" href="${url}" target="_blank" rel="noopener noreferrer">${esc(c)}</a>`
       + (pushed?'':' <span class="lwarn">local only</span>');
}
function renderLearning(L){
  const sb=L.scoreboard||{};
  // The distiller writes once a day; this ran four innerHTML writes every 3s regardless,
  // against the rule the exchange stream states two functions up — it fights the reader for
  // text selection and scroll position, and buys nothing.
  const lsig=JSON.stringify(L);
  if(lsig===lastLsig) return;
  lastLsig=lsig;
  // A bare count, like the two tabs beside it. "19 open" put a unit inside one badge of three.
  $('#lrn-untriaged').textContent=sb.untriaged??0;
  // Emphasis is reserved for the two numbers that mean something went wrong. by_loop being 0
  // is the honest state of a new loop, not an alarm, and colouring it as one cries wolf.
  $('#lrn-stats').innerHTML=[
    tile('needs an oracle', sb.untriaged??0),
    tile('armed', sb.armed??0),
    tile('recurred after arming', sb.recurred??0, '', (sb.recurred||0)>0?'alarm':''),
    tile('corrections', sb.corrections??0, '', (sb.corrections||0)>0?'alarm':''),
    tile('found by', (sb.by_loop??0)+' / '+(sb.by_hand??0), 'loop / by hand'),
  ].join('');
  const A=L.armed||[];
  $('#lrn-armed').innerHTML=A.length?A.map(a=>
    `<div class="lrow${a.recurred?' bad':''}"><div class="lask">${esc(a.ask)}</div>`
    +`<div class="lmeta">${a.source?esc(a.source):'no source recorded'}</div>`
    +`<div class="lmeta">${shaLink(a.commit,a.pushed)}`
    +(a.armed?` · armed ${daystamp(a.armed)}`:'')
    +(a.corrections?` · <span class="lwarn">${a.corrections} correction${a.corrections>1?'s':''}</span>`:'')
    +(a.recurred?' · <span class="lwarn">still reaching the model</span>':'')
    +`</div></div>`).join(''):'<div class="empty">nothing armed from the queue yet</div>';
  const Q=L.untriaged||[];
  $('#lrn-queue').innerHTML=Q.length?Q.map(q=>
    `<div class="lrow split"><div class="lask">${esc(q.ask)}</div>`
    +`<div class="lmeta">seen ${q.count}×${q.last?' · last '+daystamp(q.last):''}</div></div>`
   ).join(''):'<div class="empty">queue is empty — every ask has a verdict</div>';
  const C=L.corrections||[];
  $('#lrn-corr').innerHTML=C.length?C.map(c=>
    `<div class="lrow"><div class="lask">${esc(c.ask)}</div>`
    +`<div class="lmeta">${daystamp(c.ts)} — ${esc(c.what)}</div></div>`
   ).join(''):'<div class="empty">none yet</div>';
}
// 'toggle' does not bubble, so listen in the capture phase on the container. Survives every
// re-render because the listener is on #exchanges, not on the details elements themselves.
// resolve the retired-version links against the app root, so they work at "/" and under a
// funnel path prefix alike
document.querySelectorAll('#oldlink,#oldlink2').forEach(a=>{a.href=DIR+'old-4';});
// A closed trace is not built. Every exchange used to render its full trace on every 3s pass
// whether or not anyone had opened it, which put a hard ceiling on how rich a trace could get.
// Bodies are now filled on first open and rebuilt by the normal render while they stay open.
// The assembly runs once per OPEN and is keyed separately from OPEN itself, because the 3s
// refresh rebuilds an open trace's markup — without this it would restart every three seconds.
// Nothing is measured from layout any more: the boxes share a centre line, so the arrows are
// straight and the geometry is the grid's problem, not ours.
const ANIMATED=new Set();
function hydrate(el,k,animate){
  const tp=el.querySelector('.tp'); if(!tp) return;
  if(!animate||ANIMATED.has(k)) return;
  ANIMATED.add(k);
  tp.classList.add('anim');
  tp.querySelectorAll('.arw').forEach((a,i)=>{a.style.animationDelay=(60+i*160)+'ms';});
  tp.querySelectorAll('.stg>.sdot').forEach((d,i)=>{d.style.animationDelay=(420+i*120)+'ms';});
}
function hydrateOpen(){
  document.querySelectorAll('#exchanges details.tr[open]').forEach(el=>hydrate(el,el.dataset.k,false));
}
// Tabs. Bound once at load, never from tick(), and the panes are hidden rather than
// rebuilt — so a refresh mid-read cannot switch the tab out from under you.
$('#xtabs').addEventListener('click', e=>{
  const b=e.target.closest('.tab'); if(!b) return;
  document.querySelectorAll('#xtabs .tab').forEach(t=>{
    const on = t===b;
    t.setAttribute('aria-selected', on?'true':'false');
    const pane=document.getElementById(t.getAttribute('aria-controls'));
    if(pane) pane.hidden = !on;
  });
});
$('#xtabs').addEventListener('keydown', e=>{
  if(e.key!=='ArrowLeft'&&e.key!=='ArrowRight') return;
  const tabs=[...document.querySelectorAll('#xtabs .tab')];
  const cur=tabs.findIndex(t=>t.getAttribute('aria-selected')==='true');
  const nxt=tabs[(cur+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length];
  nxt.click(); nxt.focus(); e.preventDefault();
});
[$('#exchanges'),$('#dm-exchanges')].forEach(c=>c.addEventListener('toggle', e=>{
  const el=e.target;
  if(!el.matches||!el.matches('details.tr')) return;
  const k=el.dataset.k;
  if(!k) return;
  if(!el.open){ OPEN.delete(k); ANIMATED.delete(k);
    // the class must come off, or re-adding it on reopen is a no-op and nothing replays
    const tpc=el.querySelector('.tp'); if(tpc) tpc.classList.remove('anim');
    return; }
  OPEN.add(k);
  const body=el.querySelector('.tpwrap');
  const x=XBYKEY.get(k);
  if(body&&!body.firstChild&&x) body.innerHTML=traceHtml(x);
  hydrate(el,k,true);
}, true));
(function(){
  const m=location.pathname.match(/\/(old-\d+)\/?$/);
  if(!m) return;
  const cur=location.pathname.replace(/\/old-\d+\/?$/,'/');
  const b=document.createElement('div');
  b.style.cssText='background:#fff8c5;color:#9a6700;border-bottom:1px solid #d4a72c;'+
    'padding:9px 22px;font-size:13px;text-align:center';
  b.innerHTML='This is <b>'+m[1]+'</b>, a retired version of the dashboard, kept for reference. '+
    '<a href="'+cur+'" style="color:#0a63c9;font-weight:600">Go to the current page &rarr;</a>';
  document.body.insertBefore(b, document.body.firstChild);
})();
loadSnr(); loadRoutes(); tick(); setInterval(tick,3000);
setInterval(loadSnr,30000); setInterval(loadRoutes,30000);
</script></body></html>"""


# --- page routing ------------------------------------------------------------------
# "/" is always the current page. A retired page keeps a PERMANENT /old-N slot, numbered by
# the order it was RETIRED — old-1 is the first version retired and stays that page forever.
# Renumbering on each release would make a published link silently change meaning, so don't.
#
# To promote the next version: build PAGE_V6, move PAGE_V5 into RETIRED_PAGES as "old-5",
# point CURRENT_PAGE at PAGE_V6, and drop the stale entry from LEGACY_ALIASES.
CURRENT_PAGE = PAGE_V5
RETIRED_PAGES = {
    "old-1": PAGE_V1,      # v1 — two-column inbound/outbound. Retired 2026-08-09.
    "old-2": PAGE_V2,      # v2 — exchanges + flat decision trace. Retired 2026-08-12.
    "old-3": PAGE_V3,      # v3 — depth, on a fully light palette. Retired 2026-08-19.
    "old-4": PAGE_V4,      # v4 — dark trace panel; build queue as a loose card. Retired 2026-08-21.
}
# A trial URL points at the page it NAMES, not at whatever is current — /v2 must not silently
# become v3. Relative Location so it resolves under a funnel path prefix as well as at the root.
ALIAS_REDIRECTS = {"v2": "old-2", "v3": "old-3"}
# Trial URLs that should keep resolving to whatever they were promoted into, so a link handed
# out during the trial doesn't 404. Retire an alias when its version does.
LEGACY_ALIASES = set()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Defense in depth: even if markup were injected, block external loads and
        # cross-origin exfil. connect-src 'self' keeps the /api/* fetches working.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; connect-src 'self'; "
                         "img-src 'self' data:; base-uri 'none'; form-action 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        slug = path.strip("/")
        if slug in ALIAS_REDIRECTS:
            self.send_response(302)
            self.send_header("Location", ALIAS_REDIRECTS[slug])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if slug in ("", "index.html") or slug in LEGACY_ALIASES:
            self._send(200, CURRENT_PAGE.encode(), "text/html; charset=utf-8")
            return
        if slug in RETIRED_PAGES:
            self._send(200, RETIRED_PAGES[slug].encode(), "text/html; charset=utf-8")
            return
        # API endpoints do file I/O — cap concurrency so a flood can't spawn unbounded work
        if not _SEM.acquire(blocking=False):
            self._send(503, b"busy", "text/plain")
            return
        try:
            if path == "/api/state":
                self._send(200, json.dumps(cached("state", 2, build_state)).encode(), "application/json")
            elif path == "/api/snr":
                self._send(200, json.dumps(cached("snr", 5, build_snr)).encode(), "application/json")
            elif path == "/api/routes":
                self._send(200, json.dumps(cached("routes", 10, build_routes)).encode(), "application/json")
            elif path == "/api/stats":
                self._send(200, json.dumps(cached("stats", 10, build_decision_stats)).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")
        finally:
            _SEM.release()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server((BIND, PORT), Handler) as srv:
        print(f"cal-mesh dashboard on http://{BIND}:{PORT}", flush=True)
        srv.serve_forever()
