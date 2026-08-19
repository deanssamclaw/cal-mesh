#!/usr/bin/env python3
"""Runtime eval for the page's render functions — it EXECUTES them, it does not just parse them.

WHY THIS FILE EXISTS
--------------------
On 2026-08-12 three adversarial reviews measured what the existing evals actually cover, and the
answer was: almost none of this.

  * `eval_page.py` is a SYNTAX GATE. A valid-syntax ReferenceError injected into tick() produced
    HTTP 200, 80 KB, zero exchanges, zero neighbours — and a green "radio connected" pill — while
    `eval_page.py` exited 0. Its own docstring says "200 and the string is present is not evidence
    the page works". That is true one level up: **parses is not evidence either.**
  * `eval_weather.py` covers wants_weather()'s booleans and nothing else. Under 12 mutations, 9
    SURVIVED — including escaping removed from the chips, trigger_match dropped before it reaches
    the page, and a fixed reply again claiming a model wrote it.

The render functions (flowHtml / spineHtml / linkSvg / gauge / stage) are pure string builders —
they touch no DOM — so they can be called directly under node with a small shim for the handful of
globals the script touches at load time. That is what this does: build records that match what
responder.py actually writes, render them, and assert on the HTML.

Every check here corresponds to a real defect that shipped, or to a mutation that survived.

Run:  python3 eval_render.py        (exit 0 = pass; skips cleanly if node is unavailable)
      python3 eval_render.py --self-test   also proves the checks can FAIL (negative controls)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "dashboard.py")).read()

node = shutil.which("node")
if not node:
    print("SKIP: node not found — cannot execute the render functions")
    sys.exit(0)


def page_script(name):
    m = re.search(name + r' = r"""(.*?)"""\n', SRC, re.S)
    if not m:
        return None
    s = re.search(r"<script>(.*?)</script>", m.group(1), re.S)
    return s.group(1) if s else None


# Enough of a browser for the module to finish loading. Deliberately tiny: if the page ever needs
# more than this at load time, that is itself worth knowing about.
SHIM = r"""
const __el = () => new Proxy(function(){}, {
  get(t, k){ if(k==='style') return {}; if(k==='dataset') return {};
             if(k==='classList') return {add(){},remove(){},contains(){return false}};
             if(k==='textContent'||k==='innerHTML') return '';
             return __el(); },
  set(){ return true; }, apply(){ return __el(); } });
globalThis.document = { querySelector: () => __el(), querySelectorAll: () => [],
  createElement: () => __el(), body: { insertBefore(){}, firstChild: null } };
globalThis.location = { pathname: '/' };
globalThis.addEventListener = () => {};
globalThis.setInterval = () => 0;
globalThis.fetch = () => Promise.reject(new Error('no network in eval'));
"""

# Records shaped exactly as responder.py writes them, then filtered the way correlate() filters.
# READ the whitelist out of dashboard.py rather than mirroring it. A duplicated copy meant a key
# could be dropped from the dashboard and this eval would keep exercising it from its own list —
# the mutation "remove 'calc' from the dashboard whitelist" survived exactly that way.
_WL = re.search(r'rec\["trace"\] = \{k: dec\.get\(k\) for k in\s*\((.*?)\)', SRC, re.S)
if _WL is None:
    print("FAIL: could not read the trace whitelist out of dashboard.py")
    sys.exit(1)
TRACE_KEYS = tuple(re.findall(r'"([a-z_]+)"', _WL.group(1)))
if "calc" not in TRACE_KEYS:
    print("FAIL: dashboard.py trace whitelist is missing 'calc' — the handler never reaches the page")
    sys.exit(1)

GATES_OK = [{"gate": g, "pass": True} for g in
            ("not_self", "fresh", "responder_enabled", "sender_allowed", "addressed", "within_rate")]
GATES_BLOCKED = ([{"gate": g, "pass": True} for g in ("not_self", "fresh", "responder_enabled")]
                 + [{"gate": "sender_allowed", "pass": False}])


def correlate(d):
    return {k: d[k] for k in TRACE_KEYS if d.get(k) is not None}


def rec(**kw):
    """An exchange as /api/state emits it."""
    base = dict(kind="exchange", ts="2026-08-12T12:00:00+00:00", **{"from": "!aaaaaaaa"},
                to="^all", channel="0", text="", reply=None, verdict="replied",
                snr=5.5, rssi=-41, hops=0, capability=None, gen_ms=1000)
    base.update({k: v for k, v in kw.items() if k != "trace"})
    base["trace"] = correlate(kw.get("trace", {}))
    return base


SAN_PUNCT = {"in_chars": 46, "out_chars": 45, "sentence_trim": "punctuation", "dropped_chars": 0}
SAN_LEGACY = {"in_chars": 30, "out_chars": 29, "sentence_trimmed": True}   # pre-sentence_trim record
SAN_CONTENT = {"in_chars": 90, "out_chars": 30, "sentence_trim": "content", "dropped_chars": 60}
TM_STRONG = {"via": "strong", "strong": ["heat index"], "weak": ["heat"], "question": True}
TM_WEAKQ = {"via": "weak_plus_question", "strong": [], "weak": ["rain"], "question": True}

CASES = {
    # A capability answer: the full chain, the boundary, the model credited.
    "weather": rec(text="Cal, whats the heat index?", reply="95F, clear skies, south wind 8 mph",
                   capability="weather", trace=dict(
                       gates=GATES_OK, sanitize=SAN_PUNCT, prompt_kind="weather",
                       model="claude-haiku-4-5-20251001", injected_fact="95F, Clear, wind S 8 mph",
                       weather_ok=True, obs_station="KOJC", obs_age_s=1080, dest="^all",
                       trigger_match=TM_STRONG)),
    # A refused forecast: capability MATCHED, nothing fetched, NO model ran.
    "forecast": rec(text="Cal, is it going to rain tomorrow?",
                    reply="Only current conditions, no forecast yet.", capability="weather",
                    gen_ms=None, trace=dict(
                        gates=GATES_OK, sanitize=SAN_PUNCT, prompt_kind="general",
                        forecast_asked=True, gen_status="fixed_forecast_refused", dest="^all",
                        trigger_match=TM_WEAKQ)),
    # Fetch failure: matched, attempted, failed, no model.
    "fetchfail": rec(text="Cal, whats the temperature?", reply="Can't reach weather right now.",
                     capability="weather", gen_ms=None, trace=dict(
                         gates=GATES_OK, sanitize=SAN_PUNCT, prompt_kind="general",
                         weather_ok=False, gen_status="fixed_weather_unavailable", dest="^all",
                         trigger_match=TM_STRONG)),
    # No capability: the message really is what the model received.
    "general": rec(text="Cal, hows the link holding up?", reply="Link's solid and steady over here",
                   trace=dict(gates=GATES_OK, sanitize=SAN_PUNCT, prompt_kind="general",
                              model="claude-haiku-4-5-20251001", dest="^all")),
    # Gated out: no flow at all, spine stops.
    "skipped": rec(text="Hi", reply=None, verdict="skipped", reason="sender_not_allowed",
                   gen_ms=None, trace=dict(gates=GATES_BLOCKED, sanitize=None)),
    # An older record that cannot say WHICH was trimmed.
    "legacysan": rec(text="Cal, hows the link holding up?", reply="Link's solid",
                     trace=dict(gates=GATES_OK, sanitize=SAN_LEGACY, prompt_kind="general",
                                model="m", dest="^all")),
    # Routing genuinely unknown — must not draw as "direct".
    "nullhops": rec(text="Cal, weather?", reply="Warm and clear", hops=None, capability="weather",
                    trace=dict(gates=GATES_OK, sanitize=SAN_PUNCT, prompt_kind="weather", model="m",
                               injected_fact="80F, Clear", weather_ok=True, dest="^all",
                               trigger_match=TM_STRONG)),
    # Attacker-controlled tokens reaching the public page.
    "xss": rec(text="<script>alert(1)</script>", reply="<img src=x onerror=alert(1)>",
               capability="weather", trace=dict(
                   gates=GATES_OK, sanitize=SAN_PUNCT, prompt_kind="weather", model="m",
                   injected_fact="<b>x</b>", weather_ok=True, obs_station="\"><svg onload=1>",
                   dest="^all", trigger_match={"via": "strong", "strong": ["<i>heat</i>"],
                                               "weak": [], "question": True})),
    # A measurement that is not a number must not draw a reading.
    "badnum": rec(text="Cal, weather?", reply="Warm", capability="weather", snr="abc", rssi=None,
                  trace=dict(gates=GATES_OK, sanitize=SAN_PUNCT, prompt_kind="weather", model="m",
                             injected_fact="80F", weather_ok=True, obs_age_s="nope", dest="^all",
                             trigger_match=TM_STRONG)),
    "content": rec(text="a. b.", reply="ok", trace=dict(
        gates=GATES_OK, sanitize=SAN_CONTENT, prompt_kind="general", model="m", dest="^all")),
    # An off-list greeting ack: matched deterministically, NOTHING fetched, NO model. Both
    # pre-existing branches would misdescribe it — the capability branch is weather-shaped,
    # the general branch credits the model with reading the message.
    "greeting": rec(text="Good morning", reply="Good morning", capability="greeting",
                    reason="greeting_ack", gen_ms=None, trace=dict(
                        gates=GATES_BLOCKED, prompt_kind="fixed", dest="^all",
                        gen_status="fixed_greeting_ack",
                        greeting_gates=[{"gate": "greeting_enabled", "pass": True},
                                        {"gate": "bare_greeting", "pass": True}])),
    # A COMPUTED answer: parsed by Python, nothing fetched, no model. The weather-shaped branch
    # would claim a failed lookup for a reply that never touched the network.
    "calc": rec(text="cal wavelength at 915 MHz",
                reply="915 MHz: wavelength 32.8 cm, quarter-wave 8.2 cm (free space)",
                capability="calc", reason="addressed", gen_ms=None, trace=dict(
                    gates=GATES_OK, sanitize=SAN_PUNCT,
                    prompt_kind="fixed", dest="!aaaaaaaa",
                    gen_status="fixed_calc",
                    calc={"handler": "wavelength", "refused": None})),
    # Attacker text in a calc question is still rendered on a public page.
    "calcxss": rec(text="cal <script>alert(1)</script> 2*2", reply="2*2 = 4",
                   capability="calc", reason="addressed", gen_ms=None, trace=dict(
                       gates=GATES_OK, sanitize=SAN_PUNCT,
                       prompt_kind="fixed", dest="!aaaaaaaa", gen_status="fixed_calc",
                       calc={"handler": "arith", "refused": None})),
    # A different SENDER. The harvested path is looked up by who sent the message, and drawing
    # one node's measured path under another node's message would look entirely plausible.
    "othersender": rec(text="Cal, hows the link?", reply="Solid", **{"from": "!bbbbbbbb"},
                       trace=dict(gates=GATES_OK, sanitize=SAN_PUNCT, prompt_kind="general",
                                  model="m", dest="^all")),
    # An attacker-shaped greeting: the ack is fixed, but their TEXT is still drawn.
    "greetxss": rec(text="<script>alert(1)</script>", reply="Good morning",
                    capability="greeting", reason="greeting_ack", gen_ms=None, trace=dict(
                        gates=GATES_BLOCKED, prompt_kind="fixed", dest="^all",
                        gen_status="fixed_greeting_ack")),
}

# (case, must-contain, must-NOT-contain, why it exists)
CHECKS = [
    # ---- the fixed-path cluster: the layout was chosen from injected_fact ----
    ("forecast", ["what Cal sent", "no model ran", "nothing"],
     ["what the model wrote", "sanitized, then given", "only this crosses", "wrote a reply from it"],
     "a refused forecast took the GENERAL layout and claimed the model was handed the message"),
    ("forecast", ["what the software recognised", "rain"], [],
     "the recognition step was unreachable on exactly the path it was written for"),
    ("fetchfail", ["the lookup failed", "what Cal sent"], ["only this crosses", "what the model wrote"],
     "a failed fetch is not a model answering, and nothing crossed a boundary"),
    ("weather", ["only this crosses", "what the model wrote", "National Weather Service"], [],
     "the capability path must keep its boundary, its attribution and its source"),
    ("general", ["sanitized, then given"], ["only this crosses", "what the software recognised"],
     "with no lookup the message really is what the model got — the picture must invert"),
    ("skipped", [], ["1 &middot; the question", "class=\"flow\""],
     "nothing was generated, so there is no chain to draw"),
    # ---- absent is not failed ----
    ("forecast", ["not attempted"], ["fetch FAILED"],
     "weather_ok is ABSENT on the forecast path; reading undefined as FAILED asserted a failure"),
    ("fetchfail", ["fetch FAILED"], ["not attempted"], "a real failure must still read as failed"),
    ("weather", ["fetch ok"], ["FAILED", "not attempted"], "success must read as success"),
    # ---- the sanitizer must never invent a content drop ----
    ("legacysan", ["predates"], ["rest dropped", "first sentence kept"],
     "the legacy fallback GUESSED 'content' and printed 'rest dropped' as though it knew"),
    ("weather", ["no content dropped"], ["rest dropped"], "a punctuation trim must say so"),
    ("content", ["first sentence kept", "60 chars dropped"], [],
     "a real content drop must still be reported, with its size"),
    # ---- the drawing must not contradict the prose ----
    # Assert on the DRAWING, not the prose: "routing not recorded" also appears in the spine's
    # summary, so checking for that string passes even when the diagram box is gone — which is
    # precisely the picture-vs-prose defect this check exists to catch.
    ("nullhops", ["stroke-dasharray", "routing not recorded"], [],
     "a null hop count drew IDENTICALLY to a direct hop while the row said 'unknown'"),
    ("weather", ["heard direct"], ["stroke-dasharray"],
     "a known-direct message must not draw a dashed unknown box"),
    # ---- escaping on a public page ----
    ("xss", ["&lt;script&gt;", "&lt;i&gt;heat&lt;/i&gt;"],
     ["<script>alert", "<img src=x", "<svg onload", "<i>heat</i>"],
     "message, reply, station and the trigger chips are all attacker-influenced"),
    # ---- a non-number must not be drawn as a reading ----
    ("badnum", [], ["left:NaN", "NaN%"], "NaN in a style landed the marker at the WEAK end of the scale"),
    # ---- the greeting ack is a third shape, not either existing one ----
    ("greeting", ["what Cal sent", "Nothing was looked up and no model ran", "a greeting, and nothing else"],
     ["what the model wrote", "what Cal looked up", "which fact to look up",
      "sanitized, then given", "wrote a reply from it", "only this crosses",
      "predates Cal keeping the matched words", "National Weather Service"],
     "setting capability='greeting' sent it down the WEATHER branch: it claimed a lookup that "
     "never happened and excused the missing word-match as an old record"),
    ("greeting", ["not on Cal's reply list"], [],
     "the point of the ack is that the sender is off-list — the trace must say so"),
    # ---- a computed answer is a FOURTH shape ----
    ("calc", ["what Cal computed", "nothing was fetched and no model ran",
              "not in the number path"],
     ["what the model wrote", "what Cal looked up", "which fact to look up",
      "the lookup failed", "weather service could not be reached",
      "only this crosses", "National Weather Service"],
     "capability='calc' fell into the weather-shaped branch: with no injected_fact it told "
     "readers a weather lookup had been attempted and failed, for a reply that never "
     "touched the network"),
    ("calc", ["wavelength"], [],
     "the trace must name WHICH handler parsed the question — the responder records it and "
     "the dashboard whitelist was dropping it before it reached the page"),
    ("calcxss", ["&lt;script&gt;"], ["<script>alert"],
     "a calc question is attacker-controlled text and is rendered on a public page"),
    ("greetxss", ["&lt;script&gt;"], ["<script>alert"],
     "the ack is fixed but the stranger's own text is still rendered on a public page"),
    # ---- a harvested path is a SEPARATE measurement, never this message's path ----
    ("weather", ["measured path to this node", "not this message", "traceroute 4 min ago"], [],
     "a path drawn without its own age and without saying it is a different measurement reads "
     "as the route this message took, which is a fabrication dressed as a measurement"),
    ("weather", ["6.25 dB", "-3.5 dB", "6.75 dB"], [],
     "both directions must be shown, and they differ — that asymmetry is the whole point of "
     "having per-link SNR at all"),
    ("weather", [">out<", ">back<"], [],
     "the two directions are measured separately and must not be merged into one chain"),
    # A path is looked up by the SENDER. Drawing one node's path against another node's message
    # is the failure this lookup can have, and it would look completely plausible.
    ("othersender", [], ["measured path to this node", "!deadbeef"],
     "the path is looked up by SENDER — a different node's measured path must not be drawn "
     "under this message, which would look entirely plausible and be wrong"),
    ("general", ["measured path to this node"], [],
     "...while the sender that DOES have one still gets it, or the check above passes by "
     "the feature being broken"),
]

failures, checked = [], 0


def run(script, extra):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(SHIM + script + "\n" + extra)
        path = f.name
    try:
        return subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)


def render_all(script):
    driver = ('\n// Harvested-path fixture. Without this, every check below runs with ROUTES empty and pathHtml\n// returns \'\' — so the whole feature would be "covered" by assertions that never reach it.\nROUTES = {me:"!cccccccc", ours:{\n  "!aaaaaaaa": {ts:new Date(Date.now()-240000).toISOString(),\n    path:["!cccccccc","!deadbeef","!aaaaaaaa"],\n    snr_towards:[6.25,-3.5], snr_back:[6.75,-4.25],\n    snr_towards_complete:true, snr_back_complete:true,\n    route_back:["!deadbeef"], links:2, witness:"addressed",\n    requester:"!cccccccc", traced:"!aaaaaaaa"}\n}, others:[]};\n' + "const OUT={};" +
              "".join(f"OUT[{json.dumps(k)}]=traceHtml({json.dumps(v)});" for k, v in CASES.items()) +
              "console.log(JSON.stringify(OUT));")
    r = run(script, driver)
    if r.returncode != 0:
        # Report the ERROR, not node's version banner: the last stderr line is the footer.
        lines = [l.strip() for l in r.stderr.strip().splitlines() if l.strip()]
        err = next((l for l in lines if re.search(r"(Error|Exception):", l)), None)
        return None, err or (lines[0] if lines else "(no detail)")
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), None
    except Exception as e:                                    # noqa: BLE001
        return None, f"could not parse render output: {e}"


script = page_script("PAGE_V4")
if script is None:
    print("FAIL: PAGE_V4 not found")
    sys.exit(1)

rendered, err = render_all(script)
if rendered is None:
    # This is the case eval_page.py cannot see: it parses, but it throws when executed.
    print(f"FAIL: the page script threw when its render functions were EXECUTED — {err}")
    sys.exit(1)

for case, must, mustnt, why in CHECKS:
    html = rendered[case]
    for token in must:
        checked += 1
        if token not in html:
            failures.append(f"[{case}] missing {token!r} — {why}")
    for token in mustnt:
        checked += 1
        if token in html:
            failures.append(f"[{case}] contains {token!r} — {why}")

# A check that cannot fail is not evidence. Prove these can, by breaking the code on purpose.
if "--self-test" in sys.argv:
    MUTATIONS = [
        ("capability from injected_fact",
         "const capability=!!(x.capability||(t.trigger_match&&t.trigger_match.via));",
         "const capability=!!t.injected_fact;"),
        ("escaping removed from chips",
         "chips=words.map(w=>`<span class=\"chip\">${esc(w)}</span>`).join('');",
         "chips=words.map(w=>`<span class=\"chip\">${w}</span>`).join('');"),
        ("absent weather_ok reads as failed",
         "const fstate = ok===true?'ok' : (ok===false?'FAILED':'not attempted');",
         "const fstate = ok?'ok':'FAILED';"),
        ("legacy sanitize guesses content",
         "(q.sentence_trimmed?'unknown':'none')", "(q.sentence_trimmed?'content':'none')"),
        ("null hops draws as direct",
         "  if(hops==null) stops.push({lab:'?', sub:'routing not recorded', dim:true, dash:true});\n",
         ""),
    ]
    print("\n--- self-test: each mutation must be CAUGHT ---")
    for name, orig, mut in MUTATIONS:
        if orig not in script:
            print(f"  ?? {name}: anchor not found — this control is stale")
            failures.append(f"self-test anchor missing: {name}")
            continue
        mrend, merr = render_all(script.replace(orig, mut, 1))
        if mrend is None:
            print(f"  ok {name}: CAUGHT (threw: {merr[:60]})")
            continue
        caught = any((t not in mrend[c]) for c, ms, _, _ in CHECKS for t in ms if c in mrend) or \
                 any((t in mrend[c]) for c, _, mn, _ in CHECKS for t in mn if c in mrend)
        print(f"  {'ok' if caught else 'XX'} {name}: {'CAUGHT' if caught else 'SURVIVED'}")
        if not caught:
            failures.append(f"MUTATION SURVIVED: {name} — the checks above cannot detect it")

for f in failures:
    print("FAIL " + f)
print(f"\n{checked} assertion(s) over {len(CASES)} rendered record shapes; {len(failures)} problem(s)")
sys.exit(1 if failures else 0)
