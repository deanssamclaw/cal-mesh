#!/usr/bin/env python3
"""cal-mesh dashboard — a read-only view of every lever behind Cal on the mesh.

Serves:
    /            the CURRENT dashboard (v2 — one "Exchanges" stream + per-exchange trace)
    /old-1       v1, retired 2026-08-09 (two-column inbound/outbound). See PAGES below:
                 an old-N slot is assigned once and never renumbered, so a link keeps
                 pointing at the same page forever.
    /api/state   JSON aggregate of bridge status, transports, sent/recv logs, neighbors
    /api/snr     per-node SNR time series (last hour)
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
                         "obs_station", "obs_age_s", "forecast_asked")
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


def build_state():
    cfg = read_config()
    safe_cfg = {k: cfg[k] for k in PUBLIC_CONFIG_KEYS if k in cfg}
    status = read_json(STATUS, {})
    status.pop("port", None)   # MAC-bearing serial path — never publish
    # Pull decisions once and use it for both the decisions feed and the in/out correlation.
    # Read deeper than the feeds so a reply near the window edge still finds its partner.
    decisions = tail_jsonl(DECISIONS, 120)
    inbox, sent = correlate(tail_jsonl(INBOX, 40), tail_jsonl(SENT_LOG, 40), decisions)
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
        "totals": {"sent": count_lines(SENT_LOG), "recv": count_lines(INBOX)},
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
header{display:flex;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);
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
const DIR=location.pathname.endsWith('/')?location.pathname:location.pathname+'/';
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
  const m={sender_not_allowed:'sender is not on the allow-list — the message was received fine, Cal just may not answer it',
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
<title>cal-mesh — levers (v2)</title>
<style>
:root{--bg:#0c0f14;--card:#151a22;--card2:#1b222c;--line:#232c39;--fg:#e6edf3;
--dim:#8b98a9;--accent:#4ea1ff;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--tx:#a371f7;--rx:#3fb950;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:linear-gradient(180deg,#0c0f14,#0c0f14ee);backdrop-filter:blur(6px);z-index:5}
header h1{font-size:17px;margin:0;letter-spacing:.3px}
header .sub{color:var(--dim);font-size:12px}
.pill{margin-left:12px;padding:5px 12px;border-radius:999px;font-weight:600;font-size:12px}
.pill.ok{background:#12351f;color:var(--ok);border:1px solid #1c5c30}
.pill.bad{background:#3a1618;color:var(--bad);border:1px solid #6e2327}
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
.tag.tx{background:#241a3a;color:var(--tx)} .tag.rx{background:#12351f;color:var(--rx)}
.tag.ch{background:#1a2740;color:var(--accent)} .tag.auto{background:#3a2a12;color:var(--warn)}
.tag.offlist{background:#3a2f12;color:var(--warn);border:1px solid #6b5416}
.tag.quiet{background:#2a2f38;color:var(--dim)}
/* --- exchanges --- */
.xc{padding:14px 16px;border-bottom:1px solid var(--line)}
.xc:last-child{border-bottom:0}
.xc .meta{color:var(--dim);font-size:11px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:5px}
.xc .ask{font-size:15px;word-break:break-word;max-width:78ch}
.rep .txt,.norep{max-width:78ch}
.xc.unprompted{background:#12161d}
.rep{margin:9px 0 0 16px;padding:8px 12px;border-left:2px solid var(--tx);background:#171320;
border-radius:0 8px 8px 0}
.rep .who{color:var(--dim);font-size:11px;display:block;margin-bottom:2px}
.rep .txt{color:var(--tx);font-size:14px}
.norep{margin:8px 0 0 16px;padding:7px 12px;border-left:2px solid var(--line);background:#11161d;
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
details.tr summary:hover{border-color:var(--accent);background:#1f2836}
.tp{margin-top:7px;background:#10151c;border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.link-d{margin:2px 0 10px;max-width:620px}
.link-d svg{width:100%;height:auto;display:block}
.trow{display:flex;gap:10px;padding:3px 0;font-size:12px;align-items:baseline}
.tk{color:var(--dim);min-width:78px;flex-shrink:0;text-transform:uppercase;font-size:10px;letter-spacing:.5px}
.tv{color:var(--fg);word-break:break-word}
.tv code{background:var(--card2);padding:1px 5px;border-radius:4px;font-size:11.5px}
.gate{display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:4px;font-size:11px}
.gp{background:#12351f;color:var(--ok)} .gf{background:#3a1618;color:var(--bad)}
.tnote{margin-top:8px;padding-top:7px;border-top:1px solid var(--line);color:var(--dim);font-size:11px;line-height:1.5}
.tnone{color:var(--dim);font-size:12px}
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
.faq h3{margin:0;padding:14px 16px 8px;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--accent);border-bottom:1px solid var(--line);background:#12161d}
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
      Known limitation, stated plainly: a station can be several miles away, and its reading can differ
      from the estimate for a specific spot by <b>five degrees or more</b>. What Cal reports is a real
      measurement of somewhere nearby, not a forecast for where you're standing.
      <br><a href="https://github.com/deanssamclaw/cal-mesh/blob/main/docs/proposals/level3-weather-point-accuracy.md" target="_blank" rel="noopener noreferrer">The write-up, including the fix ↗</a></div></details>
    <details><summary>What is an "exchange"?</summary><div class="a">
      Almost everything Cal transmits is a response to being prompted, so the page is organised that
      way: the incoming message is the head, and Cal's reply is indented beneath it. Two things don't
      fit that shape and are marked separately — <b>unprompted</b> sends (an operator message, with no
      ask above it) and messages overheard but never addressed to Cal.</div></details>
    <details><summary>What's in the decision trace?</summary><div class="a">
      Open <b>trace</b> on any exchange for exactly how the reply came to exist: the <b>gate ladder</b>
      (which checks passed, which one stopped it), what the <b>sanitizer</b> did to the incoming text,
      whether a <b>capability</b> fired and the exact <b>fact</b> that was injected, which weather
      station it came from and how old the reading was, plus the model and how long it took. It is the
      machinery, not a narration — see below.</div></details>
    <details><summary>Why doesn't the trace show Cal's "thinking"?</summary><div class="a">
      Because there isn't any to show, and inventing some would be worse than showing nothing. Reply
      generation returns plain text — there's no hidden reasoning being discarded. We could ask the
      model to narrate why it chose a reply, but that narration <b>wouldn't be a faithful account of
      the computation</b>, and publishing it as though it were would present a plausible story as
      mechanism. It would also put unbounded, model-authored text — influenced by whatever a stranger
      transmitted — onto a public page, which is what the rest of the design works to prevent.</div></details>
    <details><summary>What's the diagram at the top of a trace?</summary><div class="a">
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
      <div class="ci"><span class="cd">2026-08-11</span><b>Two capture bugs, and a caption that confidently explained one of them wrongly.</b> Every message received since 2026-08-09 was showing "hops unknown — this message predates routing capture". The messages did not predate anything: the hop count is <i>hop_start</i> minus <i>hop_limit</i>, and the radio library builds its packet view with a converter that omits any number equal to zero — so a message that used its <b>entire</b> hop budget arrived with <i>hop_limit</i> missing and was recorded as "no data", indistinguishable from a message that carried no routing at all. The most-relayed messages were the ones being thrown away. Worse was the caption: one asserted cause printed for a blank that has several. It now states only what the record supports, and older messages that genuinely predate the feature still say so.</div>
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
  const m={sender_not_allowed:'sender is not on the allow-list — the message was received fine, Cal just may not answer it',
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
  const senderC = offlist ? {fill:'#3a2f12', stroke:'#6b5416'}      // matches the OFF-LIST tag
                : quiet   ? {fill:'#11161d', stroke:'#3d4a5c'}
                          : {fill:'#11161d', stroke:'#3fb950'};
  const stops=[x.from
    ? {lab:esc(fitLabel(nodeName(x.from),15)), sub:esc(fitLabel(x.from,15)), fill:senderC.fill, stroke:senderC.stroke}
    : {lab:'unknown', sub:'no id recorded', fill:senderC.fill, stroke:senderC.stroke}];
  // A hop is a REBROADCAST, so N hops means N relays stood between the sender and Cal — and the
  // firmware only ever tells us the last one. Drawing a single relay box for N>1 quietly implied
  // we knew the whole path. The ones we cannot name are now counted and drawn dashed, so the
  // diagram shows the size of what it does not know instead of hiding it.
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
      svg+=`<line x1="${x1}" y1="${by+bh/2}" x2="${x2-6}" y2="${by+bh/2}" stroke="#3d4a5c" stroke-width="2"/>`
         +`<path d="M${x2-7} ${by+bh/2-4.5} L${x2} ${by+bh/2} L${x2-7} ${by+bh/2+4.5}z" fill="#3d4a5c"/>`;
    }
    const fill=s.fill||(s.self?'#171320':'#11161d');
    const stroke=s.stroke||(s.self?'#a371f7':(s.dim?'#3d4a5c':'#3fb950'));
    svg+=`<rect x="${bx}" y="${by}" width="${bw}" height="${bh}" rx="8" fill="${fill}" stroke="${stroke}" `
       +`stroke-width="1.5"${s.dash?' stroke-dasharray="5 4"':''}/>`
       +`<text x="${bx+bw/2}" y="${by+21}" fill="#e6edf3" font-size="13" font-weight="600" text-anchor="middle">${s.lab}</text>`
       +`<text x="${bx+bw/2}" y="${by+37}" fill="#8b98a9" font-size="10.5" text-anchor="middle">${s.sub}</text>`;
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
  return diagram+rows;
}
function traceHtml(x){
  const t=x.trace||{};
  if(!t.gates&&!t.sanitize&&!t.model)
    return `<div class="tp">${x.kind==='exchange'?linkSvg(x):''}`+
      '<div class="tnone">No decision trace recorded — this message predates it.</div></div>';
  let h=(x.kind==='exchange'?linkSvg(x):'');
  if(t.gates&&t.gates.length)
    h+=row('gates', t.gates.map(g=>`<span class="gate ${g.pass?'gp':'gf'}">${g.pass?'✓':'✗'} ${esc(g.gate)}</span>`).join('')
        +(x.verdict==='skipped'?' <span style="color:var(--dim)">— ladder stops at the first failure</span>':''));
  if(t.sanitize){const s=t.sanitize,b=[`${s.in_chars}→${s.out_chars} chars`];
    // trailing '?' and a dropped sentence hit the same code path — say which actually happened
    const tk=s.sentence_trim!=null?s.sentence_trim:(s.sentence_trimmed?'content':'none');
    if(tk==='content') b.push(`first sentence kept (${s.dropped_chars!=null?s.dropped_chars+' chars':'rest'} dropped)`);
    else if(tk==='punctuation') b.push('trailing punctuation trimmed, no content dropped');
    if(s.length_capped) b.push('length capped');
    if(s.redactions) b.push(`${s.redactions} redaction${s.redactions>1?'s':''}`);
    if(s.flagged) b.push('injection-shaped tokens flagged');
    if(b.length===1) b.push('unchanged');
    h+=row('sanitizer', esc(b.join(' · ')));}
  if(t.forecast_asked) h+=row('refused', 'asked about a FUTURE condition — the capability holds '
    +'current observations only, so a fixed reply was sent and no lookup was made');
  if(t.prompt_kind) h+=row('prompt', t.prompt_kind==='weather'
      ? 'capability template — <b>the message itself is not included</b>'
      : 'general template — the sanitized message is quoted to the model');
  if(x.capability) h+=row('capability', `${esc(x.capability)} · fetch ${t.weather_ok?'ok':'FAILED'}`);
  if(t.injected_fact) h+=row('fact in', `<code>${esc(t.injected_fact)}</code>`);
  if(t.obs_station||t.obs_age_s!=null){
    const age=t.obs_age_s!=null?`${Math.round(t.obs_age_s/60)} min old`:'age unknown';
    h+=row('measured at', `station <code>${esc(t.obs_station||'?')}</code> · reading ${esc(age)}`
      +` <span style="color:var(--dim)">— a real observation from the nearest station, not an`
      +` estimate for any particular spot</span>`);}
  if(t.model) h+=row('model', `<code>${esc(t.model)}</code>`+(x.gen_ms!=null?` · ${secs(x.gen_ms)}`:''));
  if(t.gen_status&&t.gen_status!=='ok') h+=row('generation', `<code>${esc(t.gen_status)}</code>`);
  if(t.dest) h+=row('sent to', `<code>${esc(t.dest)}</code>`);
  h+='<div class="tnote">This is the machinery, not the model\'s reasoning. Generation returns plain '
   +'text with no chain of thought, and asking for a narration would produce a plausible story rather '
   +'than an account of what actually happened — so it is not shown.</div>';
  return `<div class="tp">${h}</div>`;
}
// The page re-renders every 3s, which would wipe any <details> the reader had opened. Track
// open traces by a stable key and restore the attribute on every render, so an expanded trace
// stays expanded until it is clicked shut. (Toggle doesn't bubble — the listener captures.)
const OPEN=new Set();
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
    <details class="tr" data-k="${esc(xkey(x))}"${OPEN.has(xkey(x))?' open':''}><summary>trace</summary>${traceHtml(x)}</details></div>`;
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
$('#exchanges').addEventListener('toggle', e=>{
  const el=e.target;
  if(!el.matches||!el.matches('details.tr')) return;
  const k=el.dataset.k;
  if(!k) return;
  el.open ? OPEN.add(k) : OPEN.delete(k);
}, true);
loadSnr(); tick(); setInterval(tick,3000); setInterval(loadSnr,30000);
</script></body></html>"""


# --- page routing ------------------------------------------------------------------
# "/" is always the current page. A retired page keeps a PERMANENT /old-N slot, numbered by
# the order it was RETIRED — old-1 is the first version retired and stays that page forever.
# Renumbering on each release would make a published link silently change meaning, so don't.
#
# To promote the next version: build PAGE_V3, move PAGE_V2 into RETIRED_PAGES as "old-2",
# point CURRENT_PAGE at PAGE_V3, and drop the stale entry from LEGACY_ALIASES.
CURRENT_PAGE = PAGE_V2
RETIRED_PAGES = {
    "old-1": PAGE_V1,      # v1 — two-column inbound/outbound. Retired 2026-08-09.
}
# Trial URLs that should keep resolving to whatever they were promoted into, so a link handed
# out during the trial doesn't 404. Retire an alias when its version does.
LEGACY_ALIASES = {"v2"}


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
