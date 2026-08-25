#!/usr/bin/env python3
"""anatomy.py — a reply, split into things that EXISTED and things that RAN.

WHY. The live trace panel draws one flat chain of numbered boxes, which reads as
"everything flowed through everything", and the page then spends a caption walking that
back. The sentence the caption is fighting is the most important one on the site:

    the model was never given your question; it was given the observation.

That is hard to draw when every box is the same box. W3C PROV-O separates prov:Entity
("a thing with some fixed aspects") from prov:Activity ("something that occurs over a
period of time and acts upon or with entities"), and links them with prov:used and
prov:wasGeneratedBy. Under that split the sentence stops being prose: the question is one
entity, the observation is another, and what the generate activity USED is a fact you can
look at. If the question's line does not reach the model, it did not reach the model.

WHAT IS DELIBERATELY NOT TAKEN. Not PROV's colours (a 2013 spec palette that fails a
contrast check), not prov:Agent (there is one agent and drawing it adds a node type that
never varies), not the general graph (which needs a layout engine; this chain is linear),
and not the vocabulary on the page -- a reader gets "a thing" and "something that ran".

THE HONEST LIMIT, stated here and on the page. check_spec() proves a spec is internally
COHERENT: every id resolves, nothing is orphaned, a generation names its inputs. It cannot
prove the spec is TRUE OF WHAT RAN. This module reads a decision record and reconstructs
what must have happened; it is not emitted by responder.py at the point the model call is
built. So the page says "this trace does not contradict itself" and never "this trace is
correct." Closing that gap means moving spec emission into the responder, which is a
larger change than a page.
"""
import json, os

BASE = os.path.expanduser("~/cal-mesh")

# Closed vocabularies. An unknown kind is a DEFECT and renders as one; it may never
# inherit another capability's narrative. The weather branch was once the fall-through
# for anything unrecognised, which is how a range test was published as "a weather
# question" whose "lookup failed".
ENTITY_KINDS = {
    "received":    "the message as it arrived on air",
    "sanitized":   "the message after the inbound filter",
    "measurement": "the receiver's own reading of the packet",
    "computed":    "a value worked out in code",
    "observation": "a fact fetched from outside",
    "context":     "curated text injected for an authenticated sender",
    "reply":       "the text that was transmitted",
}
ACTIVITY_KINDS = {
    "match":     "decided which capability, if any, claimed this",
    "sanitize":  "filtered the inbound text",
    "measure":   "read the packet's own signal metadata",
    "compute":   "worked the answer out deterministically",
    "fetch":     "asked an outside service for a fact",
    "generate":  "ran a language model",
    "transmit":  "put the reply on air",
}


def _ent(eid, kind, label, detail=None):
    return {"id": eid, "kind": kind, "label": label, "detail": detail}


def _act(aid, kind, label, used, produced, detail=None):
    return {"id": aid, "kind": kind, "label": label, "used": list(used),
            "produced": produced, "detail": detail}


def spec_for(rec):
    """Reconstruct entities + activities from one decision record.

    Every branch is driven by a field the responder actually wrote. There is NO default
    branch: a record that matches nothing produces a spec with no capability chain and
    says so, rather than borrowing a story."""
    ents, acts = [], []
    cap = rec.get("capability")
    model = rec.get("model")
    san = rec.get("sanitize")
    reply = rec.get("reply")

    ents.append(_ent("1", "received", "the message", rec.get("text")))
    acts.append(_act("a1", "match", "matched the message", ["1"], None,
                     ("claimed by " + cap) if cap else (rec.get("reason") or "nothing claimed it")))

    src = "1"
    if isinstance(san, dict):
        ents.append(_ent("2", "sanitized", "the filtered message",
                         "%d redaction(s), %s" % (san.get("redactions") or 0,
                          "trimmed" if san.get("sentence_trimmed") else "not trimmed")))
        acts.append(_act("a2", "sanitize", "filtered it", ["1"], "2"))
        src = "2"

    nid = 3
    reply_from = None

    if cap == "calc":
        c = rec.get("calc") or {}
        ents.append(_ent(str(nid), "computed", "the computed value",
                         "handler: %s" % (c.get("handler") or "unknown")))
        acts.append(_act("a3", "compute", "worked it out in code", [src], str(nid)))
        reply_from = str(nid); nid += 1
    elif cap == "sigreport":
        ents.append(_ent(str(nid), "measurement", "the packet's own reading",
                         "signal-to-noise, RSSI and hop count of the packet that carried it"))
        acts.append(_act("a3", "measure", "read the packet metadata", ["1"], str(nid)))
        reply_from = str(nid); nid += 1
    elif cap == "greeting":
        ents.append(_ent(str(nid), "reply", "the acknowledgement",
                         "mirrored from a closed table; no model in this path"))
        acts.append(_act("a3", "compute", "picked the mirrored greeting", ["1"], str(nid)))
        reply_from = str(nid); nid += 1
    elif cap == "sunmoon":
        ents.append(_ent(str(nid), "computed", "the computed time",
                         "solar/lunar geometry for the node's own location"))
        acts.append(_act("a3", "compute", "worked it out in code", [src], str(nid)))
        reply_from = str(nid); nid += 1

    used_by_model = []
    if rec.get("injected_fact") or rec.get("obs_station"):
        obs = _ent(str(nid), "observation", "the fetched observation",
                   "station %s%s" % (rec.get("obs_station") or "unnamed",
                                     (", %s s old" % rec["obs_age_s"]) if rec.get("obs_age_s") is not None else ""))
        ents.append(obs)
        acts.append(_act("a4", "fetch", "looked it up", [], str(nid)))
        used_by_model.append(str(nid)); nid += 1

    if rec.get("dm_unlock"):
        ents.append(_ent(str(nid), "context", "the injected context",
                         "a bounded curated file, not the agent's memory"))
        used_by_model.append(str(nid)); nid += 1

    if model:
        # For weather the model is given the OBSERVATION and not the sender's message. That
        # is the whole claim, and it is visible here as an absence in `used`.
        if not used_by_model:
            used_by_model = [src]
        rid = str(nid); nid += 1
        ents.append(_ent(rid, "reply", "the reply", reply))
        acts.append(_act("a5", "generate", "ran the model", used_by_model, rid,
                         "model: %s" % model))
        reply_from = rid
    elif reply_from and ents[-1]["kind"] != "reply":
        rid = str(nid); nid += 1
        ents.append(_ent(rid, "reply", "the reply", reply))
        acts.append(_act("a6", "compute", "formatted the reply", [reply_from], rid))
        reply_from = rid
    elif reply_from is None and reply:
        rid = str(nid); nid += 1
        ents.append(_ent(rid, "reply", "the reply", reply))
        reply_from = rid

    if reply and reply_from:
        acts.append(_act("a7", "transmit", "put it on air", [reply_from], None,
                         "to %s" % (rec.get("dest") or "unknown")))

    gen = [a for a in acts if a["kind"] == "generate"]
    return {
        "entities": ents, "activities": acts,
        "model_ran": bool(model),
        "crossed": gen[0]["used"] if gen else [],
        "capability": cap,
        "verdict": rec.get("verdict") or ("replied" if reply else "no reply"),
        "gates": {k: rec.get(k) for k in
                  ("gates", "greeting_gates", "sigreport_gates",
                   "dm_unlock_gates", "dm_longer_gates") if rec.get(k)},
    }


def check_spec(spec):
    """Internal coherence only. See the module docstring: this cannot prove truth."""
    problems = []
    ids = {e["id"] for e in spec["entities"]}
    if len(ids) != len(spec["entities"]):
        problems.append("two entities share an id")
    for e in spec["entities"]:
        if e["kind"] not in ENTITY_KINDS:
            problems.append("entity %s has an undeclared kind '%s'" % (e["id"], e["kind"]))
    produced = set()
    gens = 0
    for a in spec["activities"]:
        if a["kind"] not in ACTIVITY_KINDS:
            problems.append("activity %s has an undeclared kind '%s'" % (a["id"], a["kind"]))
        if a["kind"] == "generate":
            gens += 1
            if not a["used"]:
                problems.append("a model ran with nothing named crossing to it")
        for u in a["used"]:
            if u not in ids:
                problems.append("activity %s uses entity %s, which does not exist" % (a["id"], u))
        if a["produced"]:
            if a["produced"] not in ids:
                problems.append("activity %s produced entity %s, which does not exist" % (a["id"], a["produced"]))
            produced.add(a["produced"])
    if gens > 1:
        problems.append("more than one generation in one reply")
    if not spec["entities"] or spec["entities"][0]["kind"] != "received":
        problems.append("the chain does not begin with the received message")
    for e in spec["entities"][1:]:
        if e["id"] not in produced:
            problems.append("entity %s (%s) was produced by nothing" % (e["id"], e["label"]))
    if spec["model_ran"] and not spec["crossed"]:
        problems.append("a model ran but no entity is recorded as crossing to it")
    return problems


def dead_ends(spec):
    """Entities that were produced and then fed nothing.

    This is NOT a defect and an early version of this module wrongly reported it as one.
    On a weather reply the sanitized message is produced and never used, because the model
    is handed the fetched observation INSTEAD of the sender's words -- which is the single
    most important claim this page makes. Rendering that as a red defect strip would have
    flagged the architecture working exactly as designed, on every weather record, which is
    the same species of bug as a correct doer reply drawn with a red stop dot.

    So it is returned separately and drawn as what it is: a thing that existed and went
    nowhere."""
    used_any = {u for a in spec["activities"] for u in a["used"]}
    return [e["id"] for e in spec["entities"]
            if e["kind"] != "reply" and e["id"] not in used_any]


def build_anatomy(limit=6):
    """Real, current records -- one per distinct shape, newest first."""
    from dashboard import build_state  # same process, already cached
    st = build_state()
    out, seen = [], set()
    for e in st.get("exchanges", []):
        t = dict(e.get("trace") or {})
        t.update({"text": e.get("text"), "reply": e.get("reply"),
                  "capability": e.get("capability"), "verdict": e.get("verdict"),
                  "reason": e.get("reason")})
        key = (t.get("capability") or t.get("reason") or "none", bool(t.get("model")))
        if key in seen:
            continue
        seen.add(key)
        spec = spec_for(t)
        spec["problems"] = check_spec(spec)
        spec["dead_ends"] = dead_ends(spec)
        spec["ts"] = e.get("ts")
        out.append(spec)
        if len(out) >= limit:
            break
    return {"records": out,
            "entity_kinds": ENTITY_KINDS, "activity_kinds": ACTIVITY_KINDS,
            "built_at": __import__("time").time()}


PAGE_ANATOMY = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>cal-mesh — the anatomy of a reply</title>
<style>
:root{--bg:#f6f8fa;--card:#ffffff;--card2:#eef1f5;--line:#d6dce4;--fg:#1a1f26;
--dim:#5c6672;--accent:#0a63c9;
--soft:#2a78d6;--softbg:#eaf2fd;--model:#8a6400;--modelbg:#fdf6e6;--modelln:#e6d3a3;
--net:#1a6b3c;--netbg:#eef8f1;--stop:#8c3a2b;--stopbg:#fdf3f1;--stopln:#e8cfc9;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:16px 22px;
border-bottom:1px solid var(--line);position:sticky;top:0;
background:linear-gradient(180deg,#f6f8fa,#f6f8faee);backdrop-filter:blur(6px);z-index:5}
header h1{font-size:17px;margin:0;letter-spacing:.3px}
header .sub{color:var(--dim);font-size:12px}
.navlinks{margin-left:auto;display:inline-flex;gap:14px;align-items:center}
.faqlink{color:var(--accent);text-decoration:none;font-size:13px;font-weight:600;white-space:nowrap}
.wrap{max-width:980px;margin:0 auto;padding:20px 22px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 3px}
.q{color:var(--dim);font-size:12.5px;margin:0 0 12px}
.band{font-size:11px;font-weight:700;letter-spacing:.9px;text-transform:uppercase;
color:var(--dim);margin:0 0 8px}
.chain{display:flex;flex-wrap:wrap;align-items:stretch;gap:8px;margin:6px 0 4px}
.node{border:1px solid var(--line);background:var(--card2);padding:8px 12px;min-width:118px;
max-width:230px;font-size:12.5px;line-height:1.4}
.ent{border-radius:999px;background:var(--softbg);border-color:#bcd8f7}
.ent .n{font-weight:700;font-size:11px;color:var(--soft);letter-spacing:.4px}
.act{border-radius:6px;background:var(--card);border-style:solid}
.act .n{font-weight:700;font-size:11px;color:var(--dim);letter-spacing:.4px;text-transform:uppercase}
.act.generate{background:var(--modelbg);border-color:var(--modelln)}
.act.generate .n{color:var(--model)}
.act.fetch{background:var(--netbg);border-color:#bfe0c8}
.act.fetch .n{color:var(--net)}
.act.transmit{background:var(--netbg);border-color:#bfe0c8}
.act.transmit .n{color:var(--net)}
.ent.crossed{background:var(--modelbg);border-color:var(--modelln)}
.ent.crossed .n{color:var(--model)}
.ent.dead{background:var(--card2);border-color:var(--line);opacity:.72;
border-style:dashed}
.arrow{align-self:center;color:var(--dim);font-size:15px}
.det{color:var(--dim);font-size:11.5px;margin-top:3px;word-break:break-word}
.expo{background:var(--card2);border:1px solid var(--line);border-radius:8px;
padding:11px 13px;margin-top:12px;font-size:13px}
.expo b{color:var(--model)}
.lgd{display:flex;flex-wrap:wrap;gap:16px;font-size:12.5px;color:var(--dim);margin-top:12px}
.sw{margin-top:12px}
.ladder{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 10px}
.g{font-size:11.5px;padding:2px 8px;border-radius:5px;border:1px solid var(--line);
background:var(--card2);color:var(--dim)}
.g.p{background:#eef8f1;border-color:#bfe0c8;color:var(--net)}
.g.f{background:var(--stopbg);border-color:var(--stopln);color:var(--stop)}
.who{font-size:12px;font-weight:600;color:var(--fg);margin-bottom:2px}
.defect{background:var(--stopbg);border:1px solid var(--stopln);border-left:3px solid var(--stop);
border-radius:8px;padding:10px 13px;margin-bottom:10px;font-size:12.5px}
.tag{font-size:10.5px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;
padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim);
background:var(--card2)}
.rec h3{margin:0 0 10px;font-size:14.5px;display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.msg{font-size:13px;background:var(--card2);border:1px solid var(--line);border-radius:6px;
padding:7px 10px;margin-bottom:10px}
.caution{background:#fdf6e6;border:1px solid var(--modelln);border-radius:8px;
padding:12px 14px;font-size:13px;margin-top:12px}
</style></head><body>
<header>
  <div><h1>📻 cal-mesh <span class="sub">— the anatomy of a reply</span></h1>
  <div class="sub" id="sub">connecting…</div></div>
  <span class="navlinks">
    <a class="faqlink" id="backlink" href="./">← the exchanges</a>
    <a class="faqlink" id="caplink" href="capabilities">Can &amp; cannot →</a>
    <a class="faqlink" id="conlink" href="console">The console →</a>
  </span>
</header>
<div class="wrap">

<div class="card">
  <h2>Two shapes, and one sentence they exist to make visible</h2>
  <p class="lead" style="font-size:14.5px">Most drawings of a pipeline imply that everything
  flowed through everything. The hardest and most important thing to say about this node is
  the opposite: <strong>when Cal answers a weather question, the language model is never
  given your message — it is given the fetched observation instead.</strong></p>
  <p class="q" style="margin-top:10px">So the chain below separates <em>things that existed</em>
  from <em>things that ran</em>. If a thing's line does not reach the model, it did not reach
  the model. That is a fact you can look at rather than a caption you have to trust.</p>
  <div class="lgd" id="legend"></div>
  <div id="wxnote"></div>
</div>

<div id="records"></div>

<div class="card">
  <h2>What the check on each chain does and does not prove</h2>
  <p class="q" style="margin:0">Each chain is checked for internal contradictions: every
  reference resolves, nothing is orphaned, a generation names its inputs, and no chain begins
  anywhere but the received message. That proves a chain <strong>does not contradict
  itself</strong>. It does <strong>not</strong> prove the chain is true of what actually ran —
  this page reconstructs the chain from the decision record afterwards, rather than the
  responder emitting it at the moment the model call is built. Closing that gap means
  changing the responder, not the page, and until it is done this distinction is the honest
  thing to publish. A green chain here means coherent, never verified.</p>
</div>

</div>
<script>
const DIR=(function(){let p=location.pathname.replace(/\/(anatomy|capabilities|console)\/?$/,'/');
 return p.endsWith('/')?p:p+'/';})();
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function el(i){return document.querySelector('#'+i)}

function chain(sp){
  const byId={}; sp.entities.forEach(e=>byId[e.id]=e);
  const crossed=new Set(sp.crossed||[]), dead=new Set(sp.dead_ends||[]);
  let out='', drawn=new Set();
  sp.activities.forEach((a,i)=>{
    a.used.forEach(u=>{
      if(drawn.has(u)) return;
      drawn.add(u);
      const e=byId[u]; if(!e) return;
      out+=entHtml(e,crossed,dead)+'<span class="arrow">→</span>';
    });
    out+='<div class="node act '+esc(a.kind)+'"><div class="n">'+esc(a.kind)+'</div>'+
         esc(a.label)+(a.detail?'<div class="det">'+esc(a.detail)+'</div>':'')+'</div>';
    if(a.produced&&!drawn.has(a.produced)){
      drawn.add(a.produced);
      const e=byId[a.produced]; if(e) out+='<span class="arrow">→</span>'+entHtml(e,crossed,dead);
    }
    if(i<sp.activities.length-1) out+='<span class="arrow">→</span>';
  });
  sp.entities.forEach(e=>{ if(!drawn.has(e.id)) out+='<span class="arrow">→</span>'+entHtml(e,crossed,dead); });
  return '<div class="chain">'+out+'</div>';
}
function entHtml(e,crossed,dead){
  const cls='ent'+(crossed.has(e.id)?' crossed':'')+(dead.has(e.id)?' dead':'');
  return '<div class="node '+cls+'"><div class="n">'+esc(e.id)+' · '+esc(e.kind)+'</div>'+
    esc(e.label)+(e.detail?'<div class="det">'+esc(e.detail)+'</div>':'')+'</div>';
}

function switchboard(g){
  const names={gates:'the main responder',greeting_gates:'the greeting path',
    sigreport_gates:'the signal readback',dm_unlock_gates:'the authenticated-DM tier',
    dm_longer_gates:'the DM length budget'};
  const keys=Object.keys(g||{});
  if(!keys.length) return '';
  return '<div class="sw"><p class="band">Which paths were offered this message</p>'+
    keys.map(k=>{
      const l=(g[k]||[]);
      return '<div class="who">'+esc(names[k]||k)+'</div><div class="ladder">'+
        l.map(x=>'<span class="g '+(x.pass?'p':'f')+'">'+(x.pass?'✓':'✗')+' '+esc(x.gate)+'</span>').join('')+
        '</div>';
    }).join('')+'</div>';
}

function exposure(sp){
  if(!sp.model_ran)
    return '<div class="expo">Nothing was shown to a language model. <b>No model ran for this '+
      'reply</b> — it was produced entirely in code, so there is no boundary on this chain to cross.</div>';
  const names=sp.entities.filter(e=>(sp.crossed||[]).includes(e.id)).map(e=>e.id+' '+e.label);
  const deadn=sp.entities.filter(e=>(sp.dead_ends||[]).includes(e.id)).map(e=>e.label);
  let s='<div class="expo">Shown to the language model: <b>'+esc(names.join(', ')||'nothing named')+
    '</b>, and nothing else.';
  if(deadn.length) s+=' <strong>'+esc(deadn.join(', '))+'</strong> existed and went nowhere — '+
    'it was produced and then fed to nothing, which on this chain is the point rather than a fault.';
  return s+'</div>';
}

function record(sp){
  const defect=(sp.problems&&sp.problems.length)
    ? '<div class="defect"><strong>This chain contradicts itself.</strong><ul style="margin:6px 0 0;padding-left:18px">'+
      sp.problems.map(p=>'<li>'+esc(p)+'</li>').join('')+'</ul></div>' : '';
  const first=sp.entities[0]||{};
  return '<div class="card rec"><h3>'+esc(sp.capability||'no capability claimed it')+
    ' <span class="tag">'+esc(sp.verdict)+'</span>'+
    (sp.model_ran?'<span class="tag">a model ran</span>':'<span class="tag">no model ran</span>')+
    '</h3>'+defect+
    (first.detail?'<div class="msg">'+esc(first.detail)+'</div>':'')+
    chain(sp)+exposure(sp)+switchboard(sp.gates)+'</div>';
}

async function tick(){
  try{
    const r=await fetch(DIR+'api/anatomy',{cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    const d=await r.json();
    el('legend').innerHTML=
      '<span><span class="node ent" style="display:inline-block;min-width:0;padding:3px 11px">a thing that existed</span></span>'+
      '<span><span class="node act" style="display:inline-block;min-width:0;padding:3px 11px">something that ran</span></span>'+
      '<span><span class="node ent crossed" style="display:inline-block;min-width:0;padding:3px 11px">was shown to the model</span></span>'+
      '<span><span class="node ent dead" style="display:inline-block;min-width:0;padding:3px 11px">existed, went nowhere</span></span>';
    const hasWx=d.records.some(r=>r.capability==='weather');
    el('wxnote').innerHTML=hasWx?'':
      '<div class="caution"><strong>No weather exchange is in the current window, so the '+
      'example above is not demonstrated on this page right now.</strong> The chains below '+
      'show every shape that has occurred recently. Saying this is cheaper than letting a '+
      'claim stand with nothing under it — when a weather question next arrives, its chain '+
      'will appear here with the observation crossing and the message going nowhere.</div>';
    el('records').innerHTML=d.records.map(record).join('');
    el('sub').textContent=d.records.length+' shapes · built '+new Date(d.built_at*1000).toLocaleTimeString();
  }catch(e){ el('sub').textContent='could not load: '+e.message }
}
(function(){const a=el('backlink'); if(a) a.href=DIR;
  const b=el('caplink'); if(b) b.href=DIR+'capabilities';
  const c=el('conlink'); if(c) c.href=DIR+'console';})();
tick();setInterval(tick,30000);
</script>
</body></html>
"""
