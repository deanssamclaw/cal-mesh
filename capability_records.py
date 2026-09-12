#!/usr/bin/env python3
"""capability_records.py — one addressable record per armed capability.

WHY THIS EXISTS. `capabilities.py` answers "what can you do" over the air in seven words,
composed from the config flags so it cannot drift from what is actually armed. That is the
right answer for a handheld radio and far too small for someone deciding whether to trust
this node. The public page has the same problem the over-air reply had before that module
was written: what Cal can do is a chip on a row, and what Cal CANNOT do is scattered across
a FAQ, a tab and a changelog.

A model card puts "what it should and should not be used for" ABOVE every metric, and is
explicit that the out-of-scope section is a warning label rather than a caveats footer.
That ordering is the whole borrowing here.

THE RULES, each one paid for by a bug in this repo:

1. THE ARMED SET COMES FROM THE FLAGS, NEVER FROM THIS FILE. A hand-maintained list is
   correct the day it is written. `capabilities.py` already had exactly this bug: three
   `if on(...)` blocks covering three of five armed doers, in a module whose stated premise
   is that the flags compose the sentence. So every row here is keyed on a config flag, and
   the eval asserts that every *_ENABLED flag the live config arms has a row.

2. A ROW MAY SAY "NOTHING KNOWN". It may not GUESS. An oracle is None unless the learning
   loop actually recorded one. A default that describes a mechanism describes the wrong one.

3. EVERY OUT-OF-SCOPE CLAIM NAMES THE CODE THAT ENFORCES IT. Prose asserting a limit is
   just prose; `where` points at a file and a symbol, and the eval greps for that symbol.
   When the code moves, the claim fails loudly instead of going quietly stale.

4. NO CONFIG VALUE IS EVER PUBLISHED -- only whether a flag is on. `config` holds a literal
   coordinate (WEATHER_POINT), the DM unlock node id, and a public-key fingerprint. The
   lazy version of this file ships the config dict and renders the true flags, which would
   publish the base station's location on a page whose stated rule is that no coordinate is
   ever stored or published. Booleans and hand-declared labels only.
"""
import ast, json, os, re

BASE = os.path.expanduser("~/cal-mesh")
CONFIG = os.path.join(BASE, "config")
TRIAGE = os.path.join(BASE, "triage.json")
RESPONDER = os.path.join(BASE, "responder.py")

# Budget keys that may appear in config. Values are read live; a key absent everywhere
# reports None rather than a remembered number.
_BUDGET_LABEL = {
    "GREET_MAX_PER_DAY": "acks per day, all senders",
    "GREET_SENDER_COOLDOWN_S": "seconds before the same node is acked again",
    "SIGREPORT_MAX_PER_DAY": "reports per day, all senders",
    "SIGREPORT_SENDER_COOLDOWN_S": "seconds between reports to one sender",
    "SIGREPORT_MAX_CH_UTIL": "percent channel use above which it stays quiet",
    "TRACEROUTE_MAX_CH_UTIL": "percent channel use above which it stays quiet",
    "TRACEROUTE_MIN_GAP_S": "seconds between probes",
    "TRACER_MAX_PER_DAY": "probes proposed per day",
    "TRACER_MAX_QUEUED": "probes allowed to sit in the queue",
    "CLARIFY_TTL_S": "seconds a follow-up stays answerable",
    "DM_LOCKED_MAX_CHARS": "reply character budget on a locked DM",
}


def responder_defaults(path=RESPONDER):
    """Pull the DEFAULTS dict out of responder.py WITHOUT importing it.

    The dashboard must never import the live responder as a side effect of rendering a
    page. ast.literal_eval on the assignment gives the same values with no execution."""
    try:
        tree = ast.parse(open(path).read())
    except Exception:
        return {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "DEFAULTS":
                    try:
                        return ast.literal_eval(node.value)
                    except Exception:
                        return {}
    return {}


def read_config(path=CONFIG):
    """Flags only. The VALUES of secret-bearing keys are never returned."""
    out = {}
    try:
        for ln in open(path):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ the registry
# kind: "doer"  answers a question put to it
#       "path"  a tier or gate that changes how a reply is produced, not a question it takes
#       "probe" something Cal sends outward; nobody asks for it
RECORDS = (
    {
        "flag": "CALC_ENABLED", "name": "calc", "kind": "doer", "module": "calc.py",
        "model_runs": False,
        "answers": "Arithmetic, unit conversion, RF wavelength and antenna cut lengths, "
                   "bolt torque, and concrete/lumber quantities.",
        "trigger": "A message whose text parses as one of those questions. No keyword list.",
        "who": "anyone",
        "out_of_scope": [
            {"limit": "SAE bolt grades 2, 5 and 8 only — any other grade is refused, not "
                      "interpolated.", "where": "calc.py:bolt_torque_ftlb"},
            {"limit": "Ambiguous units are refused rather than guessed: ton, gallon, cup, "
                      "pint, quart and oz have more than one convention and none was given.",
             "where": "calc.py:AMBIGUOUS"},
            {"limit": "A missing bolt grade produces a QUESTION, not a default. Torque "
                      "changes by more than 2x across grades.", "where": "calc.py:AskFor"},
        ],
        "oracle_key": "whats torque for a half inch grade 5 bolt",
    },
    {
        "flag": "SUNMOON_ENABLED", "name": "sun/moon", "kind": "doer", "module": "sunmoon.py",
        "model_runs": False,
        "answers": "Sunrise, sunset, twilight and moon phase for the node's own location.",
        "trigger": "A message asking for one of those times.",
        "who": "anyone",
        "out_of_scope": [
            {"limit": "One location only — the node's own. It will not compute times for a "
                      "place you name, and no coordinate is published with the answer.",
             "where": "sunmoon.py"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "WEATHER_ENABLED", "name": "weather", "kind": "doer", "module": "weather.py",
        "model_runs": True,
        "answers": "Current observed conditions from the nearest reporting station, with the "
                   "age of the observation attached.",
        "trigger": "A weather-shaped ask that is not forecast-shaped.",
        "who": "allow-list",
        "out_of_scope": [
            {"limit": "NO FORECASTS. A forecast-shaped ask is recognised and refused "
                      "deterministically; no lookup is attempted and the model is not asked "
                      "to improvise one.", "where": "weather.py:_FORECAST"},
            {"limit": "Present-tense readings that merely contain 'high' or 'low' ('high "
                      "winds', 'cpu high temp') are NOT forecasts — that over-refusal was a "
                      "real bug and the gate now distinguishes them.", "where": "weather.py"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "DRAFTS_ENABLED", "name": "simulated replies", "kind": "observer",
        "module": "drafts.py", "model_runs": True,
        "answers": "Nobody. It answers no one and transmits nothing. For every message that is "
                   "not Cal's own it drafts the reply he WOULD have sent and publishes it on "
                   "the Drafts tab, so 'should Cal say more?' can be looked at rather than "
                   "guessed at.",
        "trigger": "Every inbound message except Cal's own — answered or not, addressed or "
                   "not, emoji included. It runs on a schedule, after the fact, never in the "
                   "responder.",
        "who": "nobody — it is read, not sent",
        "out_of_scope": [
            {"limit": "It cannot transmit, and that is structural rather than promised: the "
                      "send boundary is the outbox DIRECTORY, since bridge.py broadcasts any "
                      "file dropped there. The eval executes the module with writes into "
                      "outbox/ trapped, and mutation-proves the trap.",
             "where": "eval_drafts.py"},
            {"limit": "It never builds a model argv. It calls the responder's locked one, so "
                      "--permission-mode plan and --setting-sources \"\" are inherited rather "
                      "than restated — a second call site would drop both and nothing would "
                      "notice.", "where": "responder.py:run_claude"},
            {"limit": "It never grades its own output. Shape only — silent, answered, or "
                      "generic boilerplate. Quality verdicts are a person's, in a separate "
                      "file, and are never required for the counters to mean something.",
             "where": "drafts.py:shape"},
            {"limit": "A draft made after the fact is not what the responder would have said: "
                      "the weather fact, sun/moon times and DM memory are read live. Those "
                      "rows are marked unfaithful rather than presented as equivalent.",
             "where": "drafts.py:run"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "CAPS_ENABLED", "name": "capabilities", "kind": "doer",
        "module": "capabilities.py", "model_runs": False,
        "answers": "What Cal can actually do, composed from the armed config FLAGS at the "
                   "moment of asking — never a stored sentence, so the list cannot drift away "
                   "from what is really armed.",
        "trigger": "A question about Cal's own capabilities or purpose. Mounted at the BOTTOM "
                   "of the ladder: a mutation test with a maximally greedy matcher still let "
                   "all four real capabilities win, so PLACEMENT is the guarantee and the "
                   "pattern is only defence in depth.",
        "who": "allow-listed senders — it sits below sender_allowed, so a stranger asking "
               "what Cal does still gets silence.",
        "out_of_scope": [
            {"limit": "It cannot describe a capability that is not armed, because it reads the "
                      "flags rather than a description of them. Arming a doer adds a row; "
                      "disarming one removes it, with no edit here.",
             "where": "capabilities.py:answer"},
            {"limit": "It is not a help system. It names what is armed and stops; it will not "
                      "explain how to phrase a request or what a doer's limits are.",
             "where": "capabilities.py"},
            {"limit": "Armed 2026-09-12 WITHOUT the independent adversarial review the watch "
                      "list asked for. Dean's explicit call; recorded here rather than left "
                      "implicit, because the review is still owed.",
             "where": "capabilities.py:answer"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "SIGREPORT_ENABLED", "name": "sigreport", "kind": "doer",
        "module": "sigreport.py", "model_runs": False,
        "answers": "The receiver's own reading of the packet that carried your test — hop "
                   "count, and the signal-to-noise and RSSI of the last leg.",
        "trigger": "A message shaped like a radio check, or a CONTACT REPORT - the sender telling Cal they heard him (Got you in Olathe), added 2026-09-12. A contact report must name no other node, or it is a report about a third party and Cal answering it barges into an exchange he was not part of. Matched by shape, not by a "
                   "vocabulary list, because the real log contains 'Tange test'.",
        "who": "anyone",
        "out_of_scope": [
            {"limit": "At more than zero hops the SNR and RSSI describe the LAST LEG, not "
                      "the journey. The report names the relay so the number cannot be read "
                      "as a measurement of the sender.", "where": "sigreport.py:report"},
            {"limit": "A relay name is published only on an unambiguous match and is "
                      "REJECTED rather than repaired if it does not clean up — a truncated "
                      "'MD<script>' became 'MDsc', a safe-looking name belonging to no node.",
             "where": "sigreport.py:clean_name"},
            {"limit": "Never a distance. Signal strength is not range, and nothing here "
                      "converts it into one.", "where": "sigreport.py:report"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "GREETING_ENABLED", "name": "greeting ack", "kind": "doer",
        "module": "responder.py", "model_runs": False,
        "answers": "Mirrors a bare greeting back, in register and never with the same words.",
        "trigger": "A message that is ENTIRELY a greeting. A question mark or any real "
                   "content and it stays silent.",
        "who": "anyone, including nodes not on the allow-list",
        "out_of_scope": [
            {"limit": "Deliberately NOT advertised in the over-air capability menu. The ack "
                      "is something done TO a greeting, not a service anyone asks for.",
             "where": "capabilities.py:_MENU"},
            {"limit": "No text loop-guard, by design — the ack IS a greeting, so such a rule "
                      "refuses the ordinary case. Budgets bound a loop instead.",
             "where": "responder.py"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "CLARIFY_FOLLOWUP_ENABLED", "name": "clarify follow-up", "kind": "path",
        "module": "responder.py", "model_runs": False,
        "answers": "Lets a short follow-up answer a question Cal just asked, instead of "
                   "being read as a new message.",
        "trigger": "A reply arriving inside the window, from the node that was asked.",
        "who": "the node that was asked",
        "out_of_scope": [
            {"limit": "Expires. After the window a bare answer is ordinary traffic again.",
             "where": "responder.py"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "DM_UNLOCK_ENABLED", "name": "DM unlock", "kind": "path",
        "module": "responder.py", "model_runs": True,
        "answers": "On an authenticated direct message from one specific node, swaps in a "
                   "curated context file so replies can be longer and better informed.",
        "trigger": "Requires ALL of: the flag, a direct message, a sender id, an encrypted "
                   "packet, and a public-key fingerprint matching the one on record.",
        "who": "exactly one node, not named here",
        "out_of_scope": [
            {"limit": "CONTENT ONLY. Exactly one argv argument differs — the system prompt. "
                      "Tool lockdown and the setting-source isolation are byte-identical, "
                      "and an eval asserts it.", "where": "responder.py"},
            {"limit": "Context is INJECTED from a bounded curated file, never loaded from "
                      "the agent's own memory.", "where": "responder.py"},
            {"limit": "Whatever goes in that file becomes public the first time Cal "
                      "references it, because this link is published like any other.",
             "where": "dm-context.txt"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "DM_LONGER_ENABLED", "name": "DM length budget", "kind": "path",
        "module": "responder.py", "model_runs": True,
        "answers": "A direct message gets a larger character budget than an open-channel "
                   "reply, because it costs one recipient rather than the whole channel.",
        "trigger": "Any direct message.", "who": "anyone who can DM the node",
        "out_of_scope": [
            {"limit": "Truncation backs off to a word boundary rather than cutting mid-word.",
             "where": "responder.py:clean_reply"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "DM_MEMORY_ENABLED", "name": "DM memory", "kind": "path",
        "module": "dm_memory.py", "model_runs": True,
        "answers": "Remembers a little across direct messages from one identity.",
        "trigger": "Pinned to the DM unlock tier; it does nothing without it.",
        "who": "the unlocked node only",
        "out_of_scope": [
            {"limit": "Identity is bound to the node id INSIDE the key, so a matching "
                      "fingerprint alone cannot address another node's memory.",
             "where": "dm_memory.py"},
            {"limit": "Requires the unlock flag as well as its own — disarming the tier "
                      "disarms this.", "where": "dm_memory.py"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "TRACEROUTE_ENABLED", "name": "traceroute", "kind": "probe",
        "module": "bridge.py", "model_runs": False,
        "answers": "Sends a traceroute and records the path if anyone answers.",
        "trigger": "Nobody asks for this. It is proposed by the tracer and gated on politeness.",
        "who": "n/a — outbound",
        "out_of_scope": [
            {"limit": "Unknown channel state fails CLOSED. If the node cannot measure how "
                      "busy the air is, it does not transmit.", "where": "bridge.py"},
            {"limit": "The list of nodes probed is never published. A node that ANSWERED is "
                      "already public in the path records; who stayed silent is a "
                      "reachability map of third parties.", "where": "console.py:build_tracer_score"},
        ],
        "oracle_key": None,
    },
    {
        "flag": "TRACER_ENABLED", "name": "tracer", "kind": "probe",
        "module": "tracer.py", "model_runs": False,
        "answers": "Decides which node to traceroute next and keeps score of who answers.",
        "trigger": "A timer. Propose-only.",
        "who": "n/a — outbound",
        "out_of_scope": [
            {"limit": "PROPOSE-ONLY. It writes one queue entry; the bridge's politeness gate "
                      "decides whether it is ever sent.", "where": "tracer.py"},
            {"limit": "Its ledger is written BEFORE each probe, so it is permanently one "
                      "cycle behind its own best result. Known, unfixed, and the console "
                      "shows the disagreement rather than hiding it.",
             "where": "console.py:build_tracer_score"},
        ],
        "oracle_key": None,
    },
)

# Permanent refusals that belong to the node rather than to any one capability.
STANDING_REFUSALS = (
    {"limit": "No coordinates, ever. The node broadcasts no position and none is stored or "
              "published anywhere on this site.", "where": "dashboard.py:PUBLIC_CONFIG_KEYS"},
    {"limit": "No map, and no distance or bearing derived from signal strength. Repeated "
              "true signal reports from moving positions are a slow trilateration fix.",
     "where": "console.py:DARK_SIGNALS"},
    {"limit": "The model's reasoning is never shown. The page publishes the machinery that "
              "chose a reply, not the model's account of itself.", "where": "dashboard.py"},
    {"limit": "Cal answers questions from three nodes. Anyone may read this page; almost "
              "nobody may make Cal speak, and most logged decisions are refusals to.",
     "where": "config:ALLOW_FROM"},
)


def build_records(cfg=None, triage=None, defaults=None):
    """Merge the registry with live flags, live budgets and the learning loop's record.

    Returns booleans and hand-declared labels. NO config value is copied through."""
    cfg = read_config() if cfg is None else cfg
    defaults = responder_defaults() if defaults is None else defaults
    if triage is None:
        try:
            triage = json.load(open(TRIAGE))
        except Exception:
            triage = {}

    def on(flag):
        return str(cfg.get(flag, "false")).lower() == "true"

    def budgets_for(rec):
        out = []
        pre = {"calc": (), "greeting ack": ("GREET_",), "sigreport": ("SIGREPORT_",),
               "traceroute": ("TRACEROUTE_",), "tracer": ("TRACER_",),
               "clarify follow-up": ("CLARIFY_",), "DM length budget": ("DM_LOCKED_",)}.get(rec["name"], ())
        for key, label in _BUDGET_LABEL.items():
            if not any(key.startswith(p) for p in pre):
                continue
            v = cfg.get(key, defaults.get(key))
            if v not in (None, ""):
                out.append({"key": key, "value": str(v), "means": label})
        return out

    allowed = len([x for x in (cfg.get("ALLOW_FROM") or "").split(",") if x.strip()])
    rows = []
    for r in RECORDS:
        t = triage.get(r["oracle_key"]) if r.get("oracle_key") else None
        t = t if isinstance(t, dict) else None
        rows.append({
            "name": r["name"], "kind": r["kind"], "flag": r["flag"], "module": r["module"],
            "armed": on(r["flag"]), "model_runs": r["model_runs"],
            "answers": r["answers"], "trigger": r["trigger"], "who": r["who"],
            "out_of_scope": r["out_of_scope"],
            # None, not a guess. Only the learning loop may supply these.
            "oracle": (t or {}).get("source"),
            "armed_on": (t or {}).get("armed"),
            "commit": (t or {}).get("commit"),
            "pushed": bool((t or {}).get("pushed")) if t else None,
            "corrections": [{"ts": c.get("ts"), "what": c.get("what")}
                            for c in (t or {}).get("corrections", []) or []],
            "budgets": budgets_for(r),
        })
    return {
        "records": rows,
        "standing": list(STANDING_REFUSALS),
        "allowed_count": allowed,
        "armed_count": sum(1 for r in rows if r["armed"]),
        "total": len(rows),
    }


def build_payload():
    d = build_records()
    d["unregistered"] = unregistered_flags()
    import time as _t
    d["built_at"] = _t.time()
    return d


def unregistered_flags(cfg=None):
    """Every *_ENABLED flag the live config arms that has NO record here.

    This is the check that keeps rule 1 true. It is exported rather than buried in the eval
    so the page itself can refuse to look complete when it is not."""
    cfg = read_config() if cfg is None else cfg
    known = {r["flag"] for r in RECORDS}
    # RESPONDER_ENABLED is the master switch for the whole responder, not a capability.
    exempt = {"RESPONDER_ENABLED"}
    return sorted(k for k, v in cfg.items()
                  if re.fullmatch(r"[A-Z0-9_]+_ENABLED", k)
                  and str(v).lower() == "true" and k not in known and k not in exempt)


if __name__ == "__main__":
    d = build_records()
    print("armed %d/%d, allow-list %d" % (d["armed_count"], d["total"], d["allowed_count"]))
    print("unregistered armed flags:", unregistered_flags() or "none")


# --- the page -----------------------------------------------------------------------
# Section order is fixed and taken from Model Cards for Model Reporting (Mitchell et al.,
# FAT* 2019): what it is, what it is for, WHAT IT IS NOT FOR, then everything else. The
# out-of-scope section is a warning label, so it is never collapsed, never a <details>,
# and never empty -- a capability with no stated limit says so in words.
PAGE_CAPABILITIES = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>cal-mesh — what it can and cannot do</title>
<style>
:root{--bg:#f6f8fa;--card:#ffffff;--card2:#eef1f5;--line:#d6dce4;--fg:#1a1f26;
--dim:#5c6672;--accent:#0a63c9;--stop:#8c3a2b;--stopbg:#fdf3f1;--stopln:#e8cfc9;
--go:#1a5f37;--gobg:#f0f8f2;}
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
.faqlink:hover{text-decoration:underline}
.wrap{max-width:900px;margin:0 auto;padding:20px 22px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 3px}
.q{color:var(--dim);font-size:12.5px;margin:0 0 14px}
.band{font-size:11px;font-weight:700;letter-spacing:.9px;text-transform:uppercase;
color:var(--dim);margin:0 0 8px}
.lead{font-size:14.5px;margin:0 0 10px}
.stop{background:var(--stopbg);border:1px solid var(--stopln);border-left:3px solid var(--stop);
border-radius:8px;padding:12px 14px;margin-top:12px}
.stop .band{color:var(--stop)}
.stop ul{margin:0;padding-left:18px}
.stop li{margin:6px 0}
.where{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
color:var(--dim);background:var(--card2);border:1px solid var(--line);border-radius:4px;
padding:1px 5px;white-space:nowrap}
.rec{border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:14px;
background:var(--card)}
.rec h3{margin:0;font-size:15px;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.tag{font-size:10.5px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;
padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--dim);
background:var(--card2)}
.tag.on{background:var(--gobg);color:var(--go);border-color:#bfe0c8}
.tag.off{background:var(--card2);color:var(--dim)}
.tag.model{background:#fdf6e8;color:#7a5a12;border-color:#e8d9b0}
.kv{margin:12px 0 0}
.kv .band{margin-bottom:3px}
.kv p{margin:0 0 10px}
.budg{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
.budg span{font-size:11.5px;background:var(--card2);border:1px solid var(--line);
border-radius:6px;padding:3px 8px;color:var(--dim)}
.budg b{color:var(--fg);font-variant-numeric:tabular-nums}
.corr{border-left:3px solid var(--stopln);padding-left:12px;margin-top:6px}
.corr .t{font-size:11.5px;color:var(--dim)}
.none{color:var(--dim);font-style:italic}
.grp{font-size:12px;font-weight:700;letter-spacing:.7px;text-transform:uppercase;
color:var(--dim);margin:22px 0 10px}
.alarm{background:#fdf3f1;border:1px solid #e8cfc9;border-left:3px solid var(--stop);
border-radius:8px;padding:12px 14px;margin-bottom:16px;font-size:13px}
.big{font-size:30px;font-weight:700;letter-spacing:-.4px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:12px}
.tile{background:var(--card2);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile .l{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.4px}
</style></head><body>
<header>
  <div><h1>📻 cal-mesh <span class="sub">— what it can and cannot do</span></h1>
  <div class="sub" id="sub">connecting…</div></div>
  <span class="navlinks">
    <a class="faqlink" id="backlink" href="./">← the exchanges</a>
    <a class="faqlink" id="conlink" href="console">The console →</a>
    <a class="faqlink" href="https://github.com/deanssamclaw/cal-mesh" target="_blank" rel="noopener noreferrer">GitHub ↗</a>
  </span>
</header>
<div class="wrap">
<div id="alarm"></div>

<div class="card">
  <p class="band">What this is</p>
  <p class="lead">A Meshtastic handheld running an agent that answers a few kinds of question
  over LoRa radio. Most of what it does is deterministic code with no language model in the
  path at all. This page lists every capability that is armed, what each one refuses, and
  which of them can put a model in front of your words.</p>
  <div id="top"></div>
</div>

<div class="card">
  <p class="band">What it cannot do — standing</p>
  <p class="q">These hold for the whole node, not for any one capability, and they do not expire.</p>
  <div id="standing"></div>
</div>

<div id="records"></div>

<div class="card">
  <h2>How to read this page</h2>
  <p class="q" style="margin:0">Every limit below names the file and symbol that enforces it,
  in a <span class="where">grey box</span>. That is not decoration: a limit written only as
  prose goes quietly stale when the code moves, and an eval checks that each of these symbols
  still exists. Where a field says <span class="none">not recorded</span>, nothing is known —
  it is deliberately not filled with a plausible value.</p>
</div>

</div>
<script>
const DIR=(function(){let p=location.pathname.replace(/\/(capabilities|console)\/?$/,'/');
 return p.endsWith('/')?p:p+'/';})();
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function el(i){return document.querySelector('#'+i)}
function when(s){ if(!s) return null; const d=new Date(s); return isNaN(d)?String(s):d.toISOString().slice(0,10); }

function limits(list){
  return '<ul>'+list.map(x=>'<li>'+esc(x.limit)+' <span class="where">'+esc(x.where)+'</span></li>').join('')+'</ul>';
}

function record(r){
  const tags='<span class="tag '+(r.armed?'on':'off')+'">'+(r.armed?'armed':'disarmed')+'</span>'+
    '<span class="tag">'+esc(r.kind)+'</span>'+
    (r.model_runs?'<span class="tag model">a model may run</span>':'<span class="tag">no model runs</span>');
  let prov='';
  if(r.oracle){
    prov='<div class="kv"><p class="band">Where the answer is pinned</p><p>'+esc(r.oracle)+'</p></div>';
  }else{
    prov='<div class="kv"><p class="band">Where the answer is pinned</p><p class="none">Not recorded. '+
      'Only the learning loop writes this field, and it has not written one for this capability.</p></div>';
  }
  let ident='';
  if(r.armed_on||r.commit){
    ident='<div class="kv"><p class="band">Armed</p><p>'+esc(when(r.armed_on)||'date not recorded')+
      (r.commit?' · commit <span class="where">'+esc(r.commit)+'</span>':'')+
      (r.pushed===true?' · pushed':(r.pushed===false?' · <strong>not pushed</strong>':''))+'</p></div>';
  }
  let corr='';
  if(r.corrections&&r.corrections.length){
    corr='<div class="kv"><p class="band">Corrections after arming</p>'+
      r.corrections.map(c=>'<div class="corr"><div class="t">'+esc(when(c.ts)||'')+
      '</div>'+esc(c.what)+'</div>').join('')+'</div>';
  }
  let budg='';
  if(r.budgets&&r.budgets.length){
    budg='<div class="kv"><p class="band">Budgets</p><div class="budg">'+
      r.budgets.map(b=>'<span><b>'+esc(b.value)+'</b> — '+esc(b.means)+'</span>').join('')+'</div></div>';
  }
  return '<div class="rec"><h3>'+esc(r.name)+' '+tags+'</h3>'+
    '<div class="kv"><p class="band">What it answers</p><p>'+esc(r.answers)+'</p></div>'+
    '<div class="kv"><p class="band">What triggers it</p><p>'+esc(r.trigger)+'</p></div>'+
    '<div class="kv"><p class="band">Who can trigger it</p><p>'+esc(r.who)+'</p></div>'+
    '<div class="stop"><p class="band">What it will not do</p>'+
      (r.out_of_scope&&r.out_of_scope.length?limits(r.out_of_scope)
        :'<p class="none">No limit has been written down for this capability. That is a gap in this page, not a claim that none exists.</p>')+
    '</div>'+prov+ident+budg+corr+'</div>';
}

async function tick(){
  try{
    const r=await fetch(DIR+'api/capabilities',{cache:'no-store'});
    if(!r.ok) throw new Error(r.status);
    const d=await r.json();

    el('alarm').innerHTML = (d.unregistered&&d.unregistered.length)
      ? '<div class="alarm"><strong>This page is incomplete and says so.</strong> '+
        d.unregistered.length+' capability flag(s) are armed in the running config with no record here: '+
        d.unregistered.map(esc).join(', ')+'. Until a record is written, this page is not a full '+
        'account of what the node can do.</div>' : '';

    const modelled=d.records.filter(r=>r.armed&&r.model_runs).length;
    el('top').innerHTML='<div class="tiles">'+
      '<div class="tile"><div class="l">Armed capabilities</div><div class="big">'+d.armed_count+'</div></div>'+
      '<div class="tile"><div class="l">Of those, a model may run</div><div class="big">'+modelled+'</div></div>'+
      '<div class="tile"><div class="l">Nodes Cal will answer</div><div class="big">'+d.allowed_count+'</div>'+
        '<div class="q" style="margin:4px 0 0">anyone may read; almost nobody may make it speak</div></div></div>';

    el('standing').innerHTML='<div class="stop"><p class="band">Permanent</p>'+limits(d.standing)+'</div>';

    const groups=[['doer','Capabilities you can ask for'],
                  ['path','Paths that change how a reply is made'],
                  ['probe','Things Cal sends outward, that nobody asks for']];
    el('records').innerHTML=groups.map(([k,title])=>{
      const rs=d.records.filter(r=>r.kind===k);
      if(!rs.length) return '';
      return '<div class="grp">'+esc(title)+'</div>'+rs.map(record).join('');
    }).join('');

    el('sub').textContent=d.armed_count+' armed · built '+new Date(d.built_at*1000).toLocaleTimeString();
  }catch(e){ el('sub').textContent='could not load: '+e.message }
}
(function(){const a=el('backlink'); if(a) a.href=DIR;
            const b=el('conlink'); if(b) b.href=DIR+'console';})();
tick();setInterval(tick,60000);
</script>
</body></html>
"""
