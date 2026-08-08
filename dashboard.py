#!/usr/bin/env python3
"""cal-mesh dashboard — a read-only view of every lever behind Cal on the mesh.

Serves:
    /            single-page dashboard (auto-refresh)
    /api/state   JSON aggregate of bridge status, transports, sent/recv logs, neighbors

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


# Only these config keys are ever exposed on the (public) API. Everything else —
# including ALLOW_FROM node IDs and any future secret — is withheld by default.
# PORT is deliberately excluded — the serial path embeds the device MAC.
PUBLIC_CONFIG_KEYS = ("TRANSPORT", "HOST", "RESPONDER_ENABLED", "RESPONDER_MODEL")


def build_state():
    cfg = read_config()
    safe_cfg = {k: cfg[k] for k in PUBLIC_CONFIG_KEYS if k in cfg}
    status = read_json(STATUS, {})
    status.pop("port", None)   # MAC-bearing serial path — never publish
    return {
        "status": status,
        "config": safe_cfg,
        "bridge": launchd_running(),
        "nodes": read_json(NODES, {"nodes": [], "count": 0}),
        "sent": tail_jsonl(SENT_LOG, 40),
        "inbox": tail_jsonl(INBOX, 40),
        "totals": {"sent": count_lines(SENT_LOG), "recv": count_lines(INBOX)},
        "responder": {
            "enabled": cfg.get("RESPONDER_ENABLED", "false"),
            "model": cfg.get("RESPONDER_MODEL", ""),
            # count only — never publish which node IDs are Dean's trusted fleet
            "allow_count": len([a for a in cfg.get("ALLOW_FROM", "").split(",") if a.strip()]),
            "decisions": tail_jsonl(DECISIONS, 30),
        },
    }


PAGE = r"""<!doctype html>
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
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px 16px;font-size:13px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.5px}
td.snr-good{color:var(--ok)} td.snr-bad{color:var(--warn)}
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
    <div style="overflow-x:auto"><table id="nodes"><thead><tr>
      <th>Short</th><th>Name</th><th>HW</th><th>Hops</th><th>SNR</th><th>1h SNR trend</th></tr></thead><tbody></tbody></table></div>
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
  </div>
  <div style="margin-top:16px" class="card" id="changelog"><h2>Changelog</h2>
    <div class="clog">
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
   tile('Battery', m.battery!=null?m.battery+'%':'—', m.voltage!=null?m.voltage.toFixed(2)+'V':''),
   tile('Ch util', m.chUtil!=null?m.chUtil.toFixed(1)+'%':'—', m.airUtilTx!=null?('air '+m.airUtilTx.toFixed(2)+'%'):''),
   tile('Sent / Received', `${(d.totals&&d.totals.sent)??0} / ${(d.totals&&d.totals.recv)??0}`),
   tile('Responder', rp.enabled==='true'?'● live':'○ off',
        rp.model?rp.model.replace('claude-','').replace(/-\d+$/,''):''),
 ].join('');
 // transports
 const cfg=d.config||{}, active=(st.transport||cfg.TRANSPORT||'serial');
 $('#active-t').textContent='active: '+active;
 $('#trans').innerHTML=[
   `<div class="t ${active==='serial'?'active':''}"><div class="lbl"><span class="dot ${active==='serial'?'on':'off'}"></span>USB · serial</div><div class="val">${esc(cfg.PORT||'local USB')}</div></div>`,
   `<div class="t ${active==='tcp'?'active':''}"><div class="lbl"><span class="dot ${active==='tcp'?'on':'off'}"></span>WiFi · tcp</div><div class="val">${esc(cfg.HOST||'')}:4403</div></div>`,
 ].join('');
 // sent
 $('#tx-n').textContent=(d.sent||[]).length;
 $('#sent').innerHTML=(d.sent&&d.sent.length)?d.sent.map(x=>`
   <div class="msg"><div class="meta"><span class="tag tx">TX</span>
     <span>${hhmmss(x.ts)}</span><span>→ ${esc(x.dest)}</span>
     <span class="tag ch">ch${esc(x.channel)}</span><span>${esc(x.bytes)}B</span><span>${esc(x.transport)}</span>
     ${x.source==='responder'?'<span class="tag" style="background:#3a2a12;color:#d29922">AUTO</span>':''}</div>
   <div class="body">${esc(x.text)}</div></div>`).join(''):'<div class="empty">nothing sent yet</div>';
 // inbox
 $('#rx-n').textContent=(d.inbox||[]).length;
 $('#inbox').innerHTML=(d.inbox&&d.inbox.length)?d.inbox.map(x=>`
   <div class="msg"><div class="meta"><span class="tag rx">RX</span>
     <span>${hhmmss(x.ts)}</span><span>${esc(x.from)} → ${esc(x.to)}</span>
     <span class="tag ch">ch${esc(x.channel)}</span>${x.snr!=null?`<span>snr ${esc(x.snr)}</span>`:''}</div>
   <div class="body">${esc(x.text)}</div></div>`).join(''):'<div class="empty">nothing received yet — mesh is quiet or awaiting first inbound</div>';
 // responder decisions
 const dec=(rp.decisions)||[];
 $('#rstate').textContent=(rp.enabled==='true'?'live':'disabled')+' · '+(rp.allow_count||0)+' allowed';
 $('#decisions').innerHTML=dec.length?dec.map(x=>`
   <div class="msg"><div class="meta">
     <span class="tag ${x.matched?'tx':'rx'}" style="${x.matched?'':'background:#2a2f38;color:#8b98a9'}">${x.matched?'REPLIED':'SKIP'}</span>
     <span>${hhmmss(x.ts)}</span><span>${esc(x.from)}</span>
     ${x.matched?'':`<span class="tag ch">${esc(x.reason)}</span>`}</div>
   <div class="body">${esc(x.text)}${x.reply?` <span style="color:var(--tx)">→ ${esc(x.reply)}</span>`:''}</div></div>`).join(''):'<div class="empty">no inbound evaluated yet</div>';
 // nodes
 const nodes=(d.nodes&&d.nodes.nodes)||[];
 $('#nn').textContent=nodes.length;
 $('#nodes').querySelector('tbody').innerHTML=nodes.map(n=>{
   const sg=(n.snr!=null&&n.snr>0)?'snr-good':'snr-bad';
   return `<tr><td>${esc(n.short)}</td><td>${esc(n.long)}</td><td>${esc(n.hw)}</td>
     <td>${n.hops==null?'—':esc(n.hops)}</td><td class="${sg}">${n.snr==null?'—':esc(n.snr)}</td>
     <td>${sparkline((SNR[n.id]||{}).points, n.hops)}</td></tr>`;}).join('');
}
function fmtDur(s){s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),ss=s%60;
 return h?`${h}h ${m}m`:(m?`${m}m ${ss}s`:`${ss}s`);}
loadSnr(); tick(); setInterval(tick,3000); setInterval(loadSnr,30000);
</script></body></html>"""


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
        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
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
