#!/usr/bin/env python3
"""console.py — the instrument for the packets that are not conversations.

The exchange stream on the main page renders TEXT_MESSAGE_APP. Over the census this
bridge has kept since 2026-08-19 that is well under 1% of what the radio decodes. This
module builds the other view: what the air is carrying, how the node hears it, how far
it reaches, and how the two answering paths differ in latency.

Every function here reads a file that already exists and is already written. Nothing
new is collected. The rules that were expensive to learn, kept as code:

  * The census is a MEMORY counter that resets with the bridge. A total is a sum of
    session finals and undercounts by whatever arrived in the seconds before each
    restart. `sessions` travels with the total so a reader cannot take it as continuous.
  * Absence is not zero. A node with no hop count is `unknown`, returned beside the
    histogram and never as a `0` bucket. A receipt class with no observation is None,
    which the page must render as an em-dash, not as 0.
  * A latency average over both answering paths is meaningless: a doer that never ran a
    model contributes 0 ms. The two populations are returned separately and never summed.
  * Probe TARGETS are never published. routes.jsonl already reaches the public through
    /api/routes, so a node that ANSWERED is not new exposure; a node that stayed SILENT
    is only in the gitignored ledger, and publishing that list would hand out a map of
    who is reachable from here. Counts only.
"""
import json, os, re, time

BASE = os.path.expanduser("~/cal-mesh")
BRIDGE_LOG = os.path.join(BASE, "bridge.log")
NODES      = os.path.join(BASE, "nodes.json")
SNR_HIST   = os.path.join(BASE, "snr-history.jsonl")
ROUTES_LOG = os.path.join(BASE, "routes.jsonl")
TRACER_LOG = os.path.join(BASE, "tracer.log")
DECISIONS  = os.path.join(BASE, "decisions.jsonl")
ACKS       = os.path.join(BASE, "acks.jsonl")

# Meshtastic firmware src/airtime.h. Read from the source, not from a blog.
# These gate a node's OWN housekeeping traffic (telemetry, position, nodeinfo) via
# isTxAllowedChannelUtil(). FloodingRouter/NextHopRouter::perhapsRebroadcast never
# consult them -- high utilisation only widens the CSMA backoff (getTxDelayMsec()),
# so it DELAYS a rebroadcast and never suppresses one. The caption must say that.
CHUTIL_POLITE = 25
CHUTIL_MAX    = 40


def _epoch(s):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _quantile(sorted_vals, q):
    """Linear-interpolated quantile. Returns None for an empty list rather than raising,
    because every caller here is describing data that may not exist yet."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac


def parse_census(path=BRIDGE_LOG):
    """PORT CENSUS lines -> per-session finals plus the current session's sample trace.

    Counters are cumulative WITHIN a bridge session and reset to zero on restart, so a
    drop in the total is the session boundary. Returns finals per session (for the sum)
    and the deltas of the newest session (for the packets/minute trace)."""
    sessions, cur, cur_samples = [], None, []
    try:
        with open(path, errors="replace") as f:
            for ln in f:
                if "PORT CENSUS" not in ln:
                    continue
                try:
                    head, body = ln.split(" PORT CENSUS ", 1)
                except ValueError:
                    continue
                ts = _epoch(head.strip())
                d = {}
                for tok in body.split():
                    if "=" in tok:
                        k, v = tok.rsplit("=", 1)
                        try:
                            d[k] = int(v)
                        except ValueError:
                            pass
                if not d:
                    continue
                tot = sum(d.values())
                if cur is None or tot < cur["total"]:
                    if cur is not None:
                        sessions.append(cur)
                        cur_samples = []
                    cur = {"total": tot, "ports": d, "start_ts": ts, "ts": ts}
                    cur_samples = [(ts, tot)]
                else:
                    cur["total"], cur["ports"], cur["ts"] = tot, d, ts
                    cur_samples.append((ts, tot))
    except Exception:
        return {"sessions": 0, "totals": {}, "grand_total": 0, "trace": [],
                "span_s": None, "current_total": 0, "ok": False}
    if cur is not None:
        sessions.append(cur)

    totals = {}
    for s in sessions:
        for k, v in s["ports"].items():
            totals[k] = totals.get(k, 0) + v

    # packets/minute between consecutive samples of the CURRENT session only
    trace = []
    for (t0, v0), (t1, v1) in zip(cur_samples, cur_samples[1:]):
        if t0 is None or t1 is None or t1 <= t0:
            continue
        trace.append({"ts": t1, "ppm": round((v1 - v0) * 60.0 / (t1 - t0), 3)})
    span = None
    if cur_samples and cur_samples[0][0] and cur_samples[-1][0]:
        span = cur_samples[-1][0] - cur_samples[0][0]
    ppm = sorted(x["ppm"] for x in trace)
    return {
        "sessions": len(sessions),
        "totals": totals,
        "grand_total": sum(totals.values()),
        "current_total": cur["total"] if cur else 0,
        "trace": trace[-160:],
        "span_s": span,
        "ppm_median": _quantile(ppm, 0.5),
        "ppm_p10": _quantile(ppm, 0.10),
        "ppm_p90": _quantile(ppm, 0.90),
        "ok": True,
    }


def build_hops(path=NODES):
    """Hop-count histogram. `unknown` is returned SEPARATELY and must be drawn outside
    the axis -- a node that never reported a hop count has not been measured at zero.

    The spike at 3 is partly the Meshtastic default hop limit, i.e. partly a SETTING
    rather than a shape of the mesh. The caption says so."""
    try:
        nodes = (json.load(open(path)) or {}).get("nodes", [])
    except Exception:
        return {"hist": {}, "unknown": 0, "total": 0, "default_hop_limit": 3}
    hist, unknown = {}, 0
    for n in nodes:
        h = n.get("hops")
        if h is None:
            unknown += 1
        else:
            hist[int(h)] = hist.get(int(h), 0) + 1
    return {"hist": hist, "unknown": unknown, "total": len(nodes), "default_hop_limit": 3}


def build_snr_dist(path=SNR_HIST, top=10, floor=-24.0, ceil=10.0):
    """Per-talker SNR quantiles on ONE shared axis, plus the all-sample band behind them.

    V5 draws a sparkline per node scaled to that node's own min/max, so a node that
    wandered a quarter of a decibel draws the same dramatic zigzag as one that dropped
    to -21.5 dB. Ten charts on ten axes are not small multiples. One axis, and the
    reader sees that almost every sample lives inside a 2 dB band.

    SNR is the LAST LEG only. It is never a distance and the page must say so."""
    series = {}
    try:
        with open(path) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                node, snr = r.get("node"), r.get("snr")
                if node is None or snr is None:
                    continue
                series.setdefault(node, []).append(float(snr))
    except Exception:
        return {"talkers": [], "band": {}, "n": 0, "axis": [floor, ceil], "nodes": 0}

    short = {}
    try:
        for n in (json.load(open(NODES)) or {}).get("nodes", []):
            short[n.get("id")] = n.get("short")
    except Exception:
        pass

    allv = sorted(v for vs in series.values() for v in vs)
    talkers = []
    for node, vs in sorted(series.items(), key=lambda kv: -len(kv[1]))[:top]:
        sv = sorted(vs)
        talkers.append({
            "node": node, "short": short.get(node), "n": len(sv),
            "min": sv[0], "max": sv[-1],
            "q1": _quantile(sv, 0.25), "med": _quantile(sv, 0.5), "q3": _quantile(sv, 0.75),
            "went_negative": sv[0] < 0,
        })
    return {
        "talkers": talkers,
        "band": {"p5": _quantile(allv, 0.05), "p50": _quantile(allv, 0.5),
                 "p95": _quantile(allv, 0.95)},
        "n": len(allv), "nodes": len(series), "axis": [floor, ceil],
        "negatives": sum(1 for v in allv if v < 0),
    }


def _own_fleet(path=os.path.join(BASE, "config")):
    """ALLOW_FROM = the operator's own hardware. Used ONLY to count how many responders
    were strangers; the ids never leave this function."""
    try:
        for ln in open(path):
            if ln.startswith("ALLOW_FROM="):
                return {x.strip() for x in ln.split("=", 1)[1].strip().split(",") if x.strip()}
    except Exception:
        pass
    return set()


def _ledger_counts(path=os.path.join(BASE, "route-ledger.md")):
    """Parse ONLY the two counts out of the tracer's own ledger. The file is gitignored
    because it names third-party nodes; nothing but integers is returned."""
    try:
        txt = open(path, errors="replace").read()
    except Exception:
        return None
    m = re.search(r"asked\s+\*\*(\d+)\*\*\s+node\(s\),\s+\*\*(\d+)\*\*\s+answered", txt)
    if not m:
        return None
    return {"asked": int(m.group(1)), "answered": int(m.group(2))}


def build_tracer_score(routes=ROUTES_LOG):
    """Traceroute score. Counts only -- no target ids, ever.

    Two sources, deliberately not reconciled in code:

      * routes.jsonl is the EVIDENCE -- a node that answered is in it, and it already
        reaches the public through /api/routes, so counting it here is not new exposure.
      * route-ledger.md is the tracer's own SCOREBOARD, and it is written BEFORE each
        probe goes out, so it is permanently one cycle behind its own best result.

    When they disagree the page says so and shows both. It does not average them, pick
    one, or clamp one to the other -- an earlier draft of this function raised `asked` to
    match `answered` when its source returned nothing and published a 100% response rate,
    which is the exact defect class this project keeps finding: a repair that manufactures
    a plausible number out of a broken input. Reject, do not repair."""
    own = _own_fleet()
    answered_nodes, third_party = set(), set()
    try:
        for ln in open(routes):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if r.get("kind") != "response" or r.get("witness") != "addressed":
                continue
            t = r.get("traced")
            if not t:
                continue
            answered_nodes.add(t)
            if t not in own:
                third_party.add(t)
    except Exception:
        pass

    led = _ledger_counts()
    evidence_answered = len(answered_nodes)
    asked = led["asked"] if led else None
    disagrees = bool(led and led["answered"] != evidence_answered)
    return {
        # asked is None when the ledger cannot be read. The panel then reports the
        # answered count with NO rate, rather than inventing a denominator.
        "asked": asked,
        "answered": evidence_answered,
        "ledger_answered": led["answered"] if led else None,
        "third_party": len(third_party),
        "own": evidence_answered - len(third_party),
        "rate": round(evidence_answered / asked, 3) if asked else None,
        "disagrees": disagrees,
        "cells": [True] * evidence_answered + [False] * max(0, (asked or evidence_answered) - evidence_answered),
    }


def build_latency(path=DECISIONS):
    """Two populations, never one average.

    A record with gen_ms == 0 and model == null did not run a model; it was answered from
    code. Averaging those into model latency understates it. /api/stats made exactly this
    mistake and published 11,499 ms against 13,985 ms true, unseen because nothing on the
    page fetched that endpoint. Returned apart, and the model's values are returned as
    individual points because n is small enough that a smoothed histogram would invent a
    shape the data has not earned."""
    model_ms, fixed = [], 0
    try:
        for ln in open(path):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            g = r.get("gen_ms")
            if g is None:
                continue
            if r.get("model") and g > 0:
                model_ms.append(float(g))
            else:
                fixed += 1
    except Exception:
        pass
    sv = sorted(model_ms)
    return {
        "model": {"n": len(sv), "points": sv,
                  "median": _quantile(sv, 0.5), "p90": _quantile(sv, 0.90),
                  "mean": round(sum(sv) / len(sv), 1) if sv else None},
        "fixed": {"n": fixed, "ms": 0},
    }


def build_receipts(path=ACKS):
    """The four-way delivery classifier, with an HONEST EMPTY.

    A class with no observation returns None, which the page renders as an em-dash. A
    zero is a measurement; this is the absence of one, and they must not look alike.
    No percentage is computed: only a fraction of sends ever set wantAck, so the
    denominator that would make one meaningful does not exist."""
    classes = ["delivered", "no_ack", "error", "unknown"]
    counts = {c: None for c in classes}
    total = 0
    try:
        for ln in open(path):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            st = str(r.get("status") or "unknown")
            if st not in counts:
                st = "unknown"
            counts[st] = (counts[st] or 0) + 1
            total += 1
    except Exception:
        pass
    return {"counts": counts, "total": total, "classes": classes}


# Signals this project collects and this page deliberately does NOT draw, with the reason.
# On the page, not only in a proposal -- same instinct as the changelog.
DARK_SIGNALS = [
    ("battery-history.jsonl", "417 samples, last written 2026-08-13. A live-looking battery "
     "trend from a stale file is a lie with a timestamp on it. Restart the sampler or leave "
     "the chart off."),
    ("Any distance, range or bearing", "Refused permanently. Repeated true signal reports "
     "from moving positions are a slow trilateration fix on a node that broadcasts no "
     "position on purpose."),
    ("Per-node SNR good/fair/bad", "Meshtastic rates SNR against the active modem preset's "
     "limit, and this project never records which preset the radio is on. The page does not "
     "know the limit and will not guess one."),
    ("Which nodes were probed", "The score is published; the target list is not. A list of "
     "who answers from here is a reachability map of third parties."),
    ("Hardware-model breakdown", "81 of 256 nodes report UNSET. A chart whose largest class "
     "is 'we don't know' is a chart about the protocol, not the mesh."),
    ("Who talks most", "A leaderboard of third parties on a public page. Out of scope and "
     "out of taste."),
]


def build_console():
    return {
        "census": parse_census(),
        "hops": build_hops(),
        "snr": build_snr_dist(),
        "tracer": build_tracer_score(),
        "latency": build_latency(),
        "receipts": build_receipts(),
        "chutil": {"polite": CHUTIL_POLITE, "max": CHUTIL_MAX},
        "dark": [{"name": n, "why": w} for n, w in DARK_SIGNALS],
        "built_at": time.time(),
    }


if __name__ == "__main__":
    print(json.dumps(build_console(), indent=1)[:4000])


# --- the page -----------------------------------------------------------------------
# A SUPPLEMENT, not a replacement. The main page keeps its slot and its design; this is
# a deeper dive linked from it. Ordinal ramp is one hue light-to-dark, validated against
# the white card surface this page actually uses. No red-amber-green anywhere: every
# quantity here is ordered, and a rainbow on an ordered quantity is a lie about type.
PAGE_CONSOLE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>cal-mesh — the console</title>
<style>
:root{--bg:#f6f8fa;--card:#ffffff;--card2:#eef1f5;--line:#d6dce4;--fg:#1a1f26;
--dim:#5c6672;--accent:#0a63c9;
--r1:#86b6ef;--r2:#5598e7;--r3:#2a78d6;--r4:#184f95;--axis:#898781;--grid:#e1e0d9;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:16px 22px;
border-bottom:1px solid var(--line);position:sticky;top:0;
background:linear-gradient(180deg,#f6f8fa,#f6f8faee);backdrop-filter:blur(6px);z-index:5}
header h1{font-size:17px;margin:0;letter-spacing:.3px}
header .sub{color:var(--dim);font-size:12px}
.navlinks{margin-left:auto;display:inline-flex;gap:14px;align-items:center}
.faqlink{color:var(--accent);text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
.faqlink:hover{text-decoration:underline}
.wrap{max-width:1080px;margin:0 auto;padding:20px 22px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;margin-bottom:18px}
.card h2{font-size:15px;margin:0 0 2px}
.q{color:var(--dim);font-size:12.5px;margin:0 0 14px}
.cap{color:var(--dim);font-size:12.5px;margin-top:12px;line-height:1.6}
.hero{font-size:34px;font-weight:700;letter-spacing:-.5px;line-height:1.1}
.hero small{font-size:13px;font-weight:500;color:var(--dim);letter-spacing:0}
.badge{display:inline-block;padding:2px 9px;border-radius:999px;background:var(--card2);
border:1px solid var(--line);color:var(--dim);font-size:11.5px;font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-top:14px}
.tile{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile .n{font-size:22px;font-weight:700}
.tile .l{font-size:11.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.4px}
.tile .qq{font-size:12px;color:var(--dim);margin-top:4px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.4px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
details{margin-top:12px}
summary{cursor:pointer;color:var(--accent);font-size:12.5px;font-weight:600}
.scroll{overflow-x:auto}
.em{color:var(--dim);font-style:italic}
.warn{background:#fff8e6;border:1px solid #e6d5a8;border-radius:8px;padding:10px 12px;
font-size:12.5px;margin-top:12px}
svg{display:block;max-width:100%}
.lgd{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--dim);margin-top:10px}
.lgd i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;
vertical-align:-1px}
</style></head><body>
<header>
  <div><h1>📻 cal-mesh <span class="sub">— the console</span></h1>
  <div class="sub" id="sub">connecting…</div></div>
  <span class="navlinks">
    <a class="faqlink" id="backlink" href="./">← the exchanges</a>
    <a class="faqlink" href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">GitHub ↗</a>
  </span>
</header>
<div class="wrap">

<div class="card">
  <h2>Why this page exists</h2>
  <p class="q">The main page publishes the conversations. This one publishes everything else.</p>
  <div id="why"></div>
</div>

<div class="card">
  <h2>Traffic</h2>
  <p class="q">How much is the radio decoding, and how steadily?</p>
  <div id="anchor"></div>
</div>

<div class="card">
  <h2>What the air is carrying</h2>
  <p class="q">Of everything decoded, what kind of packet was it?</p>
  <div id="census"></div>
</div>

<div class="card">
  <h2>How the node hears its neighbours</h2>
  <p class="q">Is one talker really stronger than another, or is that noise?</p>
  <div id="snr"></div>
</div>

<div class="card">
  <h2>Reach, in hops</h2>
  <p class="q">How far away is the mesh this node can see? Topology, never geography.</p>
  <div id="hops"></div>
</div>

<div class="card">
  <h2>Traceroute</h2>
  <p class="q">When this node asks a stranger to describe the path back, does anyone answer?</p>
  <div id="tracer"></div>
</div>

<div class="card">
  <h2>How long a reply takes</h2>
  <p class="q">Two answering paths, never one average.</p>
  <div id="lat"></div>
</div>

<div class="card">
  <h2>Delivery receipts</h2>
  <p class="q">Of the messages sent with acknowledgement requested, what came back?</p>
  <div id="rcpt"></div>
</div>

<div class="card">
  <h2>Collected, and deliberately not drawn</h2>
  <p class="q">Signals this project records and this page refuses to chart, with the reason.</p>
  <div id="dark"></div>
</div>

</div>
<script>
const DIR=(function(){let p=location.pathname.replace(/\/(console)\/?$/,'/');
 return p.endsWith('/')?p:p+'/';})();
const R=['#86b6ef','#5598e7','#2a78d6','#184f95'];
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>n==null?'—':n.toLocaleString();
const f1=n=>n==null?'—':(Math.round(n*10)/10).toLocaleString();
function el(id){return document.getElementById(id)}

function svgEl(w,h,inner,label){
  return '<div class="scroll"><svg viewBox="0 0 '+w+' '+h+'" width="'+w+'" height="'+h+
    '" role="img" aria-label="'+esc(label)+'">'+inner+'</svg></div>';
}

/* anchor: packets/min over the current bridge session, with the middle-80% band behind */
function anchor(d){
  const c=d.census, t=c.trace||[];
  const hrs=c.span_s?(c.span_s/3600):null;
  let head='<div class="hero">'+f1(c.ppm_median)+' <small>packets / minute, median</small></div>';
  if(!t.length){el('anchor').innerHTML=head+'<p class="cap">No census samples in this bridge session yet.</p>';return}
  const W=1000,H=170,P=34;
  const xs=(i)=>P+(W-P-14)*(i/Math.max(1,t.length-1));
  const vals=t.map(p=>p.ppm);
  const top=Math.max(1,Math.max.apply(null,vals)*1.12);
  const ys=(v)=>H-24-(H-46)*(v/top);
  let g='';
  for(let k=0;k<=4;k++){const v=top*k/4;g+='<line x1="'+P+'" y1="'+ys(v)+'" x2="'+(W-14)+'" y2="'+ys(v)+
    '" stroke="var(--grid)" stroke-width="1"/><text x="'+(P-6)+'" y="'+(ys(v)+4)+
    '" text-anchor="end" font-size="10" fill="var(--axis)">'+f1(v)+'</text>'}
  if(c.ppm_p10!=null&&c.ppm_p90!=null){
    g+='<rect x="'+P+'" y="'+ys(c.ppm_p90)+'" width="'+(W-P-14)+'" height="'+Math.max(1,ys(c.ppm_p10)-ys(c.ppm_p90))+
      '" fill="'+R[0]+'" opacity=".34"/>'}
  let path='';
  t.forEach((p,i)=>{path+=(i?'L':'M')+xs(i).toFixed(1)+' '+ys(p.ppm).toFixed(1)+' '});
  g+='<path d="'+path+'" fill="none" stroke="'+R[2]+'" stroke-width="1.8" stroke-linejoin="round"/>';
  g+='<line x1="'+P+'" y1="'+(H-24)+'" x2="'+(W-14)+'" y2="'+(H-24)+'" stroke="var(--axis)" stroke-width="1"/>';
  el('anchor').innerHTML=head+
    '<div class="tiles"><div class="tile"><div class="l">This session</div><div class="n">'+fmt(c.current_total)+
      '</div><div class="qq">packets decoded'+(hrs?' in '+f1(hrs)+' h':'')+'</div></div>'+
    '<div class="tile"><div class="l">Middle 80%</div><div class="n">'+f1(c.ppm_p10)+'–'+f1(c.ppm_p90)+
      '</div><div class="qq">packets / minute</div></div>'+
    '<div class="tile"><div class="l">Politeness</div><div class="n">'+d.chutil.polite+'% <small style="font-size:13px;color:var(--dim)">of '+d.chutil.max+'%</small>'+
      '</div><div class="qq">firmware channel-utilisation marks</div></div></div>'+
    svgEl(W,H,g,'Packets per minute over the current bridge session')+
    '<p class="cap">The line is this bridge session only ('+(hrs?f1(hrs)+' hours, ':'')+t.length+
    ' samples); the band behind it is the middle 80% of those samples. It does not extrapolate past a restart. '+
    'The two firmware marks above gate <em>this node\'s own</em> housekeeping chatter — telemetry, position, nodeinfo. '+
    'They do not stop it relaying other people\'s packets; above them a busy channel only widens the backoff before a rebroadcast.</p>';
}

function why(d){
  const c=d.census,tot=c.grand_total||0;
  const txt=(c.totals&&c.totals.TEXT_MESSAGE_APP)||0;
  const pct=tot?(txt*100/tot):0;
  el('why').innerHTML='<div class="tiles">'+
    '<div class="tile"><div class="l">Decoded, all sessions</div><div class="n">'+fmt(tot)+'</div>'+
      '<div class="qq">across '+c.sessions+' bridge sessions</div></div>'+
    '<div class="tile"><div class="l">Were text messages</div><div class="n">'+fmt(txt)+'</div>'+
      '<div class="qq">'+(Math.round(pct*100)/100)+'% of the total</div></div>'+
    '<div class="tile"><div class="l">Everything else</div><div class="n">'+fmt(tot-txt)+'</div>'+
      '<div class="qq">had no face on this site until now</div></div></div>'+
    '<p class="cap">The exchange stream renders the text messages, and that is the point of the project — '+
    'but a radio that has decoded '+fmt(tot)+' packets and shows you '+fmt(txt)+' is not a console. '+
    'This page is the other instrument, built entirely from files the bridge was already writing and nothing was reading.</p>';
}

/* census: horizontal bars, ONE hue, sorted, every bar direct-labelled */
function census(d){
  const c=d.census, tot=c.grand_total||0;
  const rows=Object.entries(c.totals||{}).sort((a,b)=>b[1]-a[1]);
  if(!rows.length){el('census').innerHTML='<p class="em">No census lines yet.</p>';return}
  const W=1000,rh=30,H=rows.length*rh+16,LB=210,BW=W-LB-150;
  const mx=rows[0][1]||1;
  let g='';
  rows.forEach((r,i)=>{
    const y=i*rh+8,w=Math.max(2,BW*(r[1]/mx));
    const nm=r[0]==='ENCRYPTED'?'(encrypted — no portnum)':r[0];
    g+='<text x="'+(LB-10)+'" y="'+(y+15)+'" text-anchor="end" font-size="12" fill="var(--fg)">'+esc(nm)+'</text>';
    g+='<rect x="'+LB+'" y="'+(y+3)+'" width="'+w+'" height="'+(rh-12)+'" rx="3" fill="'+R[2]+'"/>';
    g+='<text x="'+(LB+w+8)+'" y="'+(y+15)+'" font-size="12" fill="var(--dim)" font-variant-numeric="tabular-nums">'+
       fmt(r[1])+'  ·  '+(Math.round(r[1]*10000/tot)/100)+'%</text>';
  });
  let tbl='<table><thead><tr><th>Port</th><th class="num">Packets</th><th class="num">Share</th></tr></thead><tbody>';
  rows.forEach(r=>{tbl+='<tr><td>'+esc(r[0])+'</td><td class="num">'+fmt(r[1])+'</td><td class="num">'+
    (Math.round(r[1]*10000/tot)/100)+'%</td></tr>'});
  tbl+='</tbody></table>';
  el('census').innerHTML='<span class="badge">sum of '+c.sessions+' session finals · '+fmt(tot)+' packets</span>'+
    svgEl(W,H,g,'Packets decoded by port type')+
    '<p class="cap">A portnum is a name, not a magnitude, so the bars are one hue and length carries the whole quantity. '+
    '<strong>(encrypted — no portnum)</strong> is not a port: it is the bucket for packets that arrived with no readable '+
    'contents, so their kind is <em>unknown</em>, not zero. These counters live in the bridge\'s memory and reset when it '+
    'restarts, so this total is a sum of '+c.sessions+' session finals and undercounts by whatever arrived in the seconds '+
    'before each restart. It is not a continuous total and must not be read as one.</p>'+
    '<details><summary>Table view</summary>'+tbl+'</details>';
}

/* SNR: one shared axis for every talker, all-sample band behind */
function snr(d){
  const s=d.snr,T=s.talkers||[];
  if(!T.length){el('snr').innerHTML='<p class="em">No SNR samples recorded.</p>';return}
  const [lo,hi]=s.axis,W=1000,rh=26,H=T.length*rh+54,LB=92,RB=118,PW=W-LB-RB;
  const X=v=>LB+PW*((v-lo)/(hi-lo));
  let g='';
  for(let v=lo;v<=hi;v+=4){g+='<line x1="'+X(v)+'" y1="14" x2="'+X(v)+'" y2="'+(H-30)+
    '" stroke="var(--grid)" stroke-width="1"/><text x="'+X(v)+'" y="'+(H-14)+
    '" text-anchor="middle" font-size="10" fill="var(--axis)">'+v+'</text>'}
  if(s.band&&s.band.p5!=null){
    g+='<rect x="'+X(s.band.p5)+'" y="14" width="'+Math.max(1,X(s.band.p95)-X(s.band.p5))+'" height="'+(H-44)+
      '" fill="'+R[0]+'" opacity=".30"/>'}
  T.forEach((t,i)=>{
    const y=i*rh+30;
    g+='<text x="'+(LB-10)+'" y="'+(y+4)+'" text-anchor="end" font-size="11.5" fill="var(--fg)">'+
       esc(t.short||'(unnamed)')+'</text>';
    g+='<line x1="'+X(t.min)+'" y1="'+y+'" x2="'+X(t.max)+'" y2="'+y+'" stroke="'+R[1]+'" stroke-width="1.4"/>';
    g+='<rect x="'+X(t.q1)+'" y="'+(y-5)+'" width="'+Math.max(1.5,X(t.q3)-X(t.q1))+'" height="10" rx="2" fill="'+R[2]+'" opacity=".55"/>';
    g+='<circle cx="'+X(t.med)+'" cy="'+y+'" r="3.6" fill="'+R[3]+'"/>';
    if(t.went_negative) g+='<circle cx="'+X(t.min)+'" cy="'+y+'" r="4.2" fill="none" stroke="'+R[3]+'" stroke-width="1.6"/>';
    g+='<text x="'+(W-RB+10)+'" y="'+(y+4)+'" font-size="11" fill="var(--dim)" font-variant-numeric="tabular-nums">med '+
       f1(t.med)+' · n='+t.n+'</text>';
  });
  let tbl='<table><thead><tr><th>Talker</th><th class="num">n</th><th class="num">min</th><th class="num">q1</th><th class="num">median</th><th class="num">q3</th><th class="num">max</th></tr></thead><tbody>';
  T.forEach(t=>{tbl+='<tr><td>'+esc(t.short||'(unnamed)')+'</td><td class="num">'+t.n+'</td><td class="num">'+f1(t.min)+
    '</td><td class="num">'+f1(t.q1)+'</td><td class="num">'+f1(t.med)+'</td><td class="num">'+f1(t.q3)+
    '</td><td class="num">'+f1(t.max)+'</td></tr>'});
  tbl+='</tbody></table>';
  el('snr').innerHTML=svgEl(W,H,g,'Signal-to-noise distribution per talker on one shared axis')+
    '<div class="lgd"><span><i style="background:'+R[2]+';opacity:.55"></i>middle half</span>'+
    '<span><i style="background:'+R[3]+';border-radius:50%"></i>median</span>'+
    '<span><i style="background:'+R[1]+'"></i>worst to best</span>'+
    '<span><i style="background:'+R[0]+';opacity:.5"></i>middle 90% of all '+fmt(s.n)+' samples</span>'+
    '<span>○ worst sample went negative</span></div>'+
    '<p class="cap">One axis for every talker, which is the whole point: '+
    'the band shows almost every sample across '+fmt(s.nodes)+' nodes sitting inside a couple of decibels, and only '+
    fmt(s.negatives)+' of '+fmt(s.n)+' samples ever went negative. Drawn per-node on its own scale, a talker that '+
    'wandered a quarter of a decibel looks exactly as dramatic as one that dropped off a cliff. '+
    '<strong>This is the last leg only.</strong> A packet relayed from a neighbour close by arrives strong no matter how '+
    'far the original sender is, so a strong reading is never a short distance, and nothing here is a range.</p>'+
    '<details><summary>Table view</summary>'+tbl+'</details>';
}

/* hops: histogram, with "no hop count" OUTSIDE the axis */
function hops(d){
  const h=d.hops,keys=Object.keys(h.hist||{}).map(Number).sort((a,b)=>a-b);
  if(!keys.length){el('hops').innerHTML='<p class="em">No hop counts recorded.</p>';return}
  const W=1000,H=210,P=40,BW=(W-P-170)/keys.length;
  const mx=Math.max.apply(null,keys.map(k=>h.hist[k]));
  let g='';
  keys.forEach((k,i)=>{
    const v=h.hist[k],ht=(H-64)*(v/mx),x=P+i*BW;
    g+='<rect x="'+(x+6)+'" y="'+(H-38-ht)+'" width="'+(BW-12)+'" height="'+Math.max(1,ht)+'" rx="3" fill="'+
       (k===h.default_hop_limit?R[3]:R[2])+'"/>';
    g+='<text x="'+(x+BW/2)+'" y="'+(H-44-ht)+'" text-anchor="middle" font-size="11.5" fill="var(--dim)" font-variant-numeric="tabular-nums">'+v+'</text>';
    g+='<text x="'+(x+BW/2)+'" y="'+(H-20)+'" text-anchor="middle" font-size="11.5" fill="var(--fg)">'+k+'</text>';
  });
  g+='<line x1="'+P+'" y1="'+(H-38)+'" x2="'+(W-170)+'" y2="'+(H-38)+'" stroke="var(--axis)" stroke-width="1"/>';
  g+='<text x="'+((W-170+P)/2)+'" y="'+(H-4)+'" text-anchor="middle" font-size="11" fill="var(--axis)">hops away</text>';
  const cx=W-140;
  g+='<rect x="'+cx+'" y="'+(H-108)+'" width="126" height="54" rx="8" fill="none" stroke="var(--axis)" stroke-width="1.4" stroke-dasharray="5 4"/>';
  g+='<text x="'+(cx+63)+'" y="'+(H-84)+'" text-anchor="middle" font-size="17" font-weight="700" fill="var(--fg)">'+h.unknown+'</text>';
  g+='<text x="'+(cx+63)+'" y="'+(H-68)+'" text-anchor="middle" font-size="10.5" fill="var(--dim)">no hop count</text>';
  el('hops').innerHTML=svgEl(W,H,g,'Neighbour count by hop distance')+
    '<p class="cap">'+fmt(h.total)+' nodes seen. The '+h.unknown+' with no hop count sit in the dashed chip '+
    '<em>outside</em> the axis on purpose — a node that never reported a hop count has not been measured at zero, '+
    'and drawing it as a zero bar would turn missing data into a reading. The tallest bar is at '+h.default_hop_limit+
    ', which is also the Meshtastic default hop limit, so part of that spike is a <em>setting</em> rather than a shape of the mesh.</p>';
}

function tracer(d){
  const t=d.tracer;
  const W=1000,cs=30;
  let g='';
  (t.cells||[]).forEach((a,i)=>{
    const x=24+i*cs;
    g+=a?'<circle cx="'+x+'" cy="26" r="9" fill="'+R[3]+'"/>'
        :'<circle cx="'+x+'" cy="26" r="9" fill="none" stroke="var(--axis)" stroke-width="1.8"/>';
  });
  const rate=t.rate!=null?Math.round(t.rate*1000)/10+'%':'—';
  let warn='';
  if(t.disagrees){
    warn='<div class="warn"><strong>Two sources disagree, and this page will not pick one.</strong> '+
      'The evidence file records <strong>'+t.answered+'</strong> nodes that answered. The tracer\'s own ledger says '+
      '<strong>'+t.ledger_answered+'</strong>. The ledger is written <em>before</em> each probe goes out, so it is '+
      'permanently one cycle behind its own best result. Until that is fixed, both numbers are shown and neither is '+
      'called "the" rate.</div>';
  }
  el('tracer').innerHTML='<div class="tiles">'+
    '<div class="tile"><div class="l">Asked</div><div class="n">'+fmt(t.asked)+'</div><div class="qq">nodes probed</div></div>'+
    '<div class="tile"><div class="l">Answered</div><div class="n">'+fmt(t.answered)+'</div><div class="qq">'+rate+' of those asked</div></div>'+
    '<div class="tile"><div class="l">Not the operator\'s own</div><div class="n">'+fmt(t.third_party)+'</div>'+
      '<div class="qq">strangers, of '+fmt(t.answered)+' responders</div></div></div>'+
    svgEl(W,56,g,'One mark per node probed; filled means it answered')+
    '<div class="lgd"><span>● answered</span><span>○ silent</span></div>'+warn+
    '<p class="cap">Filled versus hollow is a <em>shape</em> difference, so this survives any colour vision and a '+
    'monochrome printout without needing a second hue. The marks are a tally, not a timeline, and carry no identity: '+
    'a node that answered is already public in the path records, but the list of who stayed silent is a map of who is '+
    'reachable from here, so the score is published and the targets never are. '+
    'The count that matters is the third one — a reply from the operator\'s own hardware measures the bench, not the mesh.</p>';
}

function lat(d){
  const m=d.latency.model,fx=d.latency.fixed;
  let g='';
  if(m.n){
    const W=1000,H=120,P=40,mx=Math.max.apply(null,m.points)*1.08;
    const X=v=>P+(W-P-20)*(v/mx);
    for(let k=0;k<=4;k++){const v=mx*k/4;
      g+='<line x1="'+X(v)+'" y1="18" x2="'+X(v)+'" y2="78" stroke="var(--grid)"/>'+
         '<text x="'+X(v)+'" y="96" text-anchor="middle" font-size="10" fill="var(--axis)">'+Math.round(v/1000)+'s</text>'}
    m.points.forEach(v=>{g+='<circle cx="'+X(v)+'" cy="48" r="4.6" fill="'+R[2]+'" opacity=".62"/>'});
    if(m.median!=null){g+='<line x1="'+X(m.median)+'" y1="24" x2="'+X(m.median)+'" y2="72" stroke="'+R[3]+'" stroke-width="2.2"/>'+
      '<text x="'+X(m.median)+'" y="16" text-anchor="middle" font-size="10.5" fill="'+R[3]+'" font-weight="700">median</text>'}
    if(m.p90!=null){g+='<line x1="'+X(m.p90)+'" y1="30" x2="'+X(m.p90)+'" y2="66" stroke="var(--axis)" stroke-width="1.6" stroke-dasharray="4 3"/>'+
      '<text x="'+X(m.p90)+'" y="16" text-anchor="middle" font-size="10.5" fill="var(--axis)">p90</text>'}
    g=svgEl(W,H,g,'Model reply latency, one dot per reply');
  }
  el('lat').innerHTML='<div class="tiles">'+
    '<div class="tile"><div class="l">Answered by a model</div><div class="n">'+f1(m.median/1000)+'s</div>'+
      '<div class="qq">median of '+m.n+' replies · p90 '+f1(m.p90/1000)+'s</div></div>'+
    '<div class="tile"><div class="l">Answered from code</div><div class="n">0 ms</div>'+
      '<div class="qq">'+fx.n+' replies · no model ran</div></div></div>'+g+
    '<p class="cap">These are two populations and averaging them together produces a number that describes neither. '+
    'A reply written by a deterministic answerer contributes a literal zero, so folding those in drags the mean toward '+
    'zero and understates how long the model actually takes. Each dot is one real reply — with n='+m.n+
    ' a smoothed curve would invent a shape the data has not earned.</p>';
}

function rcpt(d){
  const r=d.receipts;
  let rows='';
  r.classes.forEach(c=>{
    const v=r.counts[c];
    rows+='<tr><td>'+esc(c.replace('_',' '))+'</td><td class="num">'+
      (v==null?'<span class="em">—</span>':v)+'</td></tr>';
  });
  el('rcpt').innerHTML='<table><thead><tr><th>Outcome</th><th class="num">Count</th></tr></thead><tbody>'+
    rows+'</tbody></table>'+
    '<p class="cap">An em-dash is not a zero. A zero would mean this outcome was looked for and never happened; '+
    '<span class="em">—</span> means it has not been observed at all, and the two must not look alike on a page whose '+
    'whole currency is the difference. No percentage is shown because only a fraction of messages are ever sent asking '+
    'for acknowledgement, so the denominator that would make one meaningful does not exist yet.</p>';
}

function dark(d){
  let rows='';
  (d.dark||[]).forEach(x=>{rows+='<tr><td style="width:34%"><strong>'+esc(x.name)+'</strong></td><td>'+esc(x.why)+'</td></tr>'});
  el('dark').innerHTML='<table><tbody>'+rows+'</tbody></table>'+
    '<p class="cap">This table is on the page, not only in a design document. A dashboard that quietly drops what it '+
    'cannot justify drawing looks identical to one that never had the data.</p>';
}

async function tick(){
  try{
    const r=await fetch(DIR+'api/console',{cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    const d=await r.json();
    why(d);anchor(d);census(d);snr(d);hops(d);tracer(d);lat(d);rcpt(d);dark(d);
    el('sub').textContent='built '+new Date(d.built_at*1000).toLocaleTimeString();
  }catch(e){ el('sub').textContent='could not load: '+e.message }
}
document.getElementById('backlink').href=DIR;
tick();setInterval(tick,30000);
</script>
</body></html>
"""
