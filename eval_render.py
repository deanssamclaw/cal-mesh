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


def current_page_name():
    """The template CURRENT_PAGE points at, resolved from source.

    Hardcoding the version number here means every promotion silently retargets these checks at
    a RETIRED page — which still passes, because the retired page was correct when it froze.
    The audit would go on reporting green about a page nobody is served."""
    m = re.search(r"^CURRENT_PAGE = (PAGE_V\d+)", SRC, re.M)
    return m.group(1) if m else None



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
    # An unanswered greeting whose MAIN ladder stopped at sender_allowed but whose GREETING
    # ladder ran on past it (the ack is deliberately open to off-list senders) and stopped for
    # its own reason. The published cause must be the one that actually governed the outcome.
    "greetcooldown": rec(text="Morning", reply=None, verdict="skipped",
                         reason="sender_not_allowed", gen_ms=None, trace=dict(
                             gates=GATES_BLOCKED, greeting_reason="greeting_sender_cooldown",
                             greeting_gates=[{"gate": "greeting_enabled", "pass": True},
                                             {"gate": "not_self", "pass": True},
                                             {"gate": "broadcast", "pass": True},
                                             {"gate": "not_a_reaction", "pass": True},
                                             {"gate": "bare_greeting", "pass": True},
                                             {"gate": "sender_cooldown", "pass": False}])),
    # A GENUINE generation failure. The negative control for the fix below: deleting the stop
    # branch outright would make every "deterministic answer is not a failure" check pass while
    # silently un-reporting the failures the branch exists for.
    "genfail": rec(text="Cal, hows the link?", reply=None, verdict="replied", gen_ms=None,
                   trace=dict(gates=GATES_OK, sanitize=SAN_PUNCT, prompt_kind="general",
                              model="claude-haiku-4-5-20251001", gen_status="gen_timeout")),
    # An attacker-shaped greeting: the ack is fixed, but their TEXT is still drawn.
    "greetxss": rec(text="<script>alert(1)</script>", reply="Good morning",
                    capability="greeting", reason="greeting_ack", gen_ms=None, trace=dict(
                        gates=GATES_BLOCKED, prompt_kind="fixed", dest="^all",
                        gen_status="fixed_greeting_ack")),
}

# (case, must-contain, must-NOT-contain, why it exists)
# SIGREPORT — added 2026-08-23 after the panel published a false account of itself. With no
# branch, a range test fell through to the WEATHER story and the public page reported `Test 15`
# as "a weather question" whose "lookup failed — the weather service could not be reached".
# Every claim in that sentence was untrue of the message. 101 assertions over 15 shapes did not
# catch it for the only reason that matters: none of the shapes was a sigreport.
CASES["sigreport_relayed"] = rec(
    text="Test 15", reply="Copy 15: 1 hop via MDNO, last leg RSSI -61, SNR 7.0",
    # hops on the RECORD as well as in the meta: they come from the same packet in real life,
    # and the link diagram above the flow reads the record. A fixture where they disagree
    # renders "relayed 1 hop" beside "heard direct" — which is how this line got written.
    capability="sigreport", gen_ms=None, channel="1", hops=1,
    trace=dict(gates=GATES_OK, prompt_kind="fixed", dest="^all", gen_status="fixed_sigreport",
               sigreport=dict(hops=1, snr=7.0, rssi=-61, relay_name="MDNO",
                              parts=["1 hop via MDNO", "last leg RSSI -61", "SNR 7.0"])))
CASES["sigreport_direct"] = rec(
    text="Test 16", reply="Copy 16: direct, RSSI -35, SNR 6.0",
    capability="sigreport", gen_ms=None,
    trace=dict(gates=GATES_OK, prompt_kind="fixed", dest="^all", gen_status="fixed_sigreport",
               sigreport=dict(hops=0, snr=6.0, rssi=-35, relay_name=None,
                              parts=["direct", "RSSI -35", "SNR 6.0"])))
# THE FOUR RECORDS ALREADY ON THE PUBLIC PAGE have no meta — they were written before the
# responder kept it. An absent hop count must not read as "direct": that is the same
# absent-reads-as-present failure the module itself was fixed for twice.
CASES["sigreport_legacy"] = rec(
    text="Test 12", reply="Copy: SNR 5.8, RSSI -63, 2 hops", capability="sigreport",
    gen_ms=None, hops=2,
    trace=dict(gates=GATES_OK, prompt_kind="fixed", dest="^all", gen_status="fixed_sigreport"))

# AND THE GENERAL CASE OF THAT BUG: a capability this page has never heard of. The default must
# be to say what is known, not to assert the nearest mechanism it has prose for.
CASES["unknown_capability"] = rec(
    text="whatever", reply="something", capability="somethingnew", gen_ms=None,
    trace=dict(gates=GATES_OK, prompt_kind="fixed", dest="^all"))

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
    # The words the false trace used are the ones that must never appear on this path.
    ("sigreport_relayed",
     ["what Cal measured", "no model ran", "last leg only", "MDNO", "received on air"],
     ["a weather question", "the lookup failed", "weather service", "National Weather Service",
      "what Cal looked up", "only this crosses"],
     "a range test was published as a weather question whose lookup failed — none of it true"),
    ("sigreport_relayed", ["relayed 1 hop"], ["heard direct"],
     "a relayed packet must not be described as heard direct"),
    ("sigreport_direct",
     ["heard direct", "the sender&rsquo;s own signal", "what Cal measured"],
     ["last leg only", "a weather question", "the lookup failed"],
     "a direct packet IS the sender's signal — the last-leg qualifier would be wrong here"),
    ("sigreport_legacy", ["what Cal measured", "Routing <b>not recorded</b>"],
     ["The packet arrived direct", "last leg only", "a weather question"],
     "a record with no measurement meta must not claim the packet arrived direct"),
    ("unknown_capability",
     ["no trace panel written", "the trace is missing, not the mechanism", "somethingnew"],
     ["a weather question", "the lookup failed", "what Cal looked up", "trigger_match"],
     "an unrecognised capability must say what is known, never assert the nearest mechanism"),
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

    # ---- an answer written by code is not a failed generation --------------------------
    # `gen_status` names WHICH PATH produced the reply; it is not a health field. Everything
    # from a doer carries a fixed_* value, and the spine drew any non-"ok" status as a red
    # stop stage — then drew "sent" underneath it. 21 real records rendered that way: every
    # greeting ack, calc, sigreport and forecast refusal published as a breakdown.
    ("calc", ["answered from code"], ["fixed_calc"],
     "a computed answer was drawn as a stopped generation, with the internal status enum as "
     "its summary, immediately above a green 'sent' stage"),
    ("greeting", ["answered from code"], ["fixed_greeting_ack"],
     "the greeting ack is the whole point of the deterministic path and it published as a failure"),
    ("forecast", ["answered from code"], ["fixed_forecast_refused"],
     "a deliberate refusal is a decision the code made, not a generation that broke"),
    ("sigreport_direct", ["answered from code"], ["fixed_sigreport"],
     "the readback never runs a model at all — there is no generation to fail"),
    # ...and the branch must still report the failures it was written for.
    ("genfail", ["class=\"stg stop\"", "gen_timeout"], ["answered from code"],
     "a real generation failure must still stop the spine — deleting the branch would make "
     "every check above pass while silently un-reporting the case it exists for"),

    # ---- the ladder headline must not contradict the chips under it --------------------
    # `stopped` is read off the verdict, so a MATCHED record with a failed gate printed
    # "all N checks passed" (N counting only the passers) above a red cross. 10 real records:
    # every off-list greeting ack, where the main ladder genuinely failed and the greeting
    # ladder answered anyway — which is the interesting fact and was nowhere on the page.
    # The wrong string is specific: GATES_BLOCKED is 3 passes and 1 failure, and the old
    # headline counted the passers and called that number "all". Forbidding "checks passed"
    # outright would also forbid the greeting ladder's own headline, which is CORRECT here --
    # a blunt negative that fails on the fix is not a stronger check, it is a wrong one.
    ("greeting", ["bare_greeting", "answered by another path", "the greeting path"],
     ["all 3 checks passed"],
     "the headline counted only the gates that passed, so a ladder with a red cross in it "
     "announced that everything had passed -- and the ladder that DID answer was never drawn"),

    # ---- name the cause that actually governed the outcome -----------------------------
    # Assert the SENTENCE, not the word. "cooldown" also appears as a gate chip in the
    # greeting ladder drawn just above, so a substring check here passed with the fix
    # disabled -- the mutation harness caught that, which is what it is for.
    ("greetcooldown", ["already been greeted inside the cooldown window"],
     ["all 3 checks passed"],
     "the greeting ack is deliberately open to off-list senders, so sender_allowed did not "
     "decide this outcome — its own cooldown did, and the page named the other one"),
]

failures, checked = [], 0


def row_problem(html):
    """The chain's declared column count vs the boxes it actually draws, or None.

    Three legitimate shapes: the weather layout on the 7-column `.flow`, a `.flow.gen.g<N>`
    chain, and no chain at all (a skipped message has no reply to draw). A `.flow.gen` with
    NO count is the broken one, and it is broken silently -- it renders, it just wraps.
    """
    boxes = len(re.findall(r'class="fb\b', html))
    m = re.search(r'class="flow gen g(\d)"', html)
    if m:
        if int(m.group(1)) != boxes:
            return f"declares g{m.group(1)} but draws {boxes} boxes — the grid wraps the chain"
        return None if boxes >= 2 else f"a chain needs at least two boxes, got {boxes}"
    if re.search(r'class="flow gen"', html):
        return f"a .flow.gen chain with no column count, drawing {boxes} boxes"
    return None



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


CUR = current_page_name()
script = page_script(CUR) if CUR else None
if script is None:
    print(f"FAIL: {CUR} not found")
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
        ("sigreport falls through to the weather story again",
         "if(x.capability==='sigreport'){", "if(false){"),
        ("an unknown capability is described as weather",
         "if(capName!=='weather'){", "if(false){"),
        ("unknown hops claims the packet arrived direct",
         "+(hops==null", "+(false"),
        ("a relayed report claims the sender's own signal",
         "const relayed=(hops!=null&&hops>0);", "const relayed=false;"),
        ("the four-box chain goes back into the three-column grid",
         'return `<div class="flow gen g${boxes.length}">${out.join(\'\')}</div>`;',
         'return `<div class="flow gen">${out.join(\'\')}</div>`;'),
        ("null hops draws as direct",
         "  if(hops==null) stops.push({lab:'?', sub:'routing not recorded', dim:true, dash:true});\n",
         ""),
        # --- the four defects fixed 2026-08-25. Each mutation is the code as it actually
        # shipped, so a regression to the live behaviour is what these catch.
        ("a deterministic answer is published as a failed generation again",
         "    if(/^fixed_/.test(t.gen_status))", "    if(false)"),
        ("the ladder headline counts only the gates that passed",
         "      s+=stage('pass','gated',`stopped at <b>${esc(fails[0].gate)}</b> &mdash; answered by another path`,",
         "      s+=stage('pass','gated',`all ${t.gates.filter(g=>g.pass).length} checks passed`,"),
        ("the ladder that actually answered is not drawn",
         "  if(altl&&altl[1].length)", "  if(false)"),
        ("the greeting path's own stop reason is ignored again",
         "    const gw=GREETWHY[t.greeting_reason];", "    const gw=null;"),
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
                 any((t in mrend[c]) for c, _, mn, _ in CHECKS for t in mn if c in mrend) or \
                 any(row_problem(h) for h in mrend.values())
        print(f"  {'ok' if caught else 'XX'} {name}: {'CAUGHT' if caught else 'SURVIVED'}")
        if not caught:
            failures.append(f"MUTATION SURVIVED: {name} — the checks above cannot detect it")

# --- the channel chip -------------------------------------------------------------------
#
# WHY THIS IS HERE AND NOT IN eval_channel.py. That file guards the RESPONDER's channel gate --
# whether a message arriving on Cal's own channel counts as addressed, and the fail-open value
# that would drop the trigger requirement for every node at once. This is the other end: what
# the PAGE draws once that decision has been made. Different module, different failure.
#
# The failure this exists to catch is a chip that says one channel and is coloured as another.
# Colour here is reinforcement -- the chip spells out "ch0"/"ch1" -- so a mismatch is not a
# safety bug, it is a page that quietly lies about which traffic was public. The neutral case
# is the one worth stating aloud: records written before the responder recorded a channel have
# no channel at all, and they must NOT be painted as either one.
CH_CASES = [
    ("open channel",      0,      ' class="tag ch c0"', ['c1', 'ch?']),
    ("Cal's own channel", 1,      ' class="tag ch c1"', ['c0', 'ch?']),
    ("some other index",  4,      ' class="tag ch"',    ['c0', 'c1']),
    ("channel not recorded", None, ' class="tag ch"',   ['c0', 'c1']),
]


def render_ch(scr):
    driver = ("const CH={};"
              + "".join(f"CH[{json.dumps(n)}]=chTag({json.dumps(v)});" for n, v, _, _ in CH_CASES)
              + "console.log(JSON.stringify(CH));")
    r = run(scr, driver)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:                                          # noqa: BLE001
        return None


ch = render_ch(script)
if ch is None:
    failures.append("chTag threw or produced nothing when EXECUTED")
else:
    for name, val, must, mustnt in CH_CASES:
        html = ch[name]
        checked += 1
        if must not in html:
            failures.append(f"[chip:{name}] missing {must!r} in {html!r}")
        for tok in mustnt:
            checked += 1
            if tok in html:
                failures.append(f"[chip:{name}] must not contain {tok!r} — got {html!r}")
    # The label is the discriminator that survives any colour decision, so assert it directly.
    for name, val, _, _ in CH_CASES:
        checked += 1
        want = "ch?" if val is None else f"ch{val}"
        if want not in ch[name]:
            failures.append(f"[chip:{name}] label should read {want!r} — got {ch[name]!r}")

if "--self-test" in sys.argv:
    print("\n--- self-test: channel chip mutations must be CAUGHT ---")
    CH_MUT = [
        ("every chip painted as the open channel",
         "const cls = c===0 ? ' c0' : c===1 ? ' c1' : '';", "const cls = ' c0';"),
        ("unknown channel painted as Cal's own",
         "const cls = c===0 ? ' c0' : c===1 ? ' c1' : '';",
         "const cls = c===0 ? ' c0' : ' c1';"),
        ("the two channels swapped",
         "const cls = c===0 ? ' c0' : c===1 ? ' c1' : '';",
         "const cls = c===0 ? ' c1' : c===1 ? ' c0' : '';"),
        ("missing channel renders a bare chip again",
         "ch${c==null?'?':esc(c)}", "ch${esc(c)}"),
    ]
    for name, orig, mut in CH_MUT:
        if orig not in script:
            print(f"  ?? {name}: anchor not found — this control is stale")
            failures.append(f"self-test anchor missing: {name}")
            continue
        m = render_ch(script.replace(orig, mut, 1))
        if m is None:
            print(f"  ok {name}: CAUGHT (threw)")
            continue
        caught = False
        for cname, val, must, mustnt in CH_CASES:
            h = m.get(cname, "")
            want = "ch?" if val is None else f"ch{val}"
            if must not in h or any(t in h for t in mustnt) or want not in h:
                caught = True
        print(f"  {'ok' if caught else 'XX'} {name}: {'CAUGHT' if caught else 'SURVIVED'}")
        if not caught:
            failures.append(f"MUTATION SURVIVED: {name}")

# --- the chain is ONE ROW ----------------------------------------------------------------
# `.flow.gen` defines three grid columns. A branch passing four boxes and three arrows through
# it does not overflow -- it WRAPS, into a two-row block with one box squeezed narrow and its
# note running a dozen lines tall. It renders, nothing throws, and it was wrong from the day
# calc was armed. So the box count and the declared column class are checked against each
# other for every shape, which is the only way this stays fixed.
for case, html in sorted(rendered.items()):
    checked += 1
    problem = row_problem(html)
    if problem:
        failures.append(f"[{case}] {problem}")

for f in failures:
    print("FAIL " + f)
print(f"\n{checked} assertion(s) over {len(CASES)} rendered record shapes; {len(failures)} problem(s)")
sys.exit(1 if failures else 0)
