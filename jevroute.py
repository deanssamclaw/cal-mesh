#!/usr/bin/env python3
"""JEVROUTE — a second opinion on routing, asked only where the ladder has already given up.

WHY. Every doer here claims a message with a regex, and the ladder's fallback is the language
model. A message no regex claims therefore reaches the one component that can invent. That is
not hypothetical: `Cal, hows the link holding up?` was answered "Link's solid and steady over
here" with no number behind it (sigreport.py records that), and 10 of 14 natural present-tense
weather asks ("is it raining", "how cold is it") match no weather trigger at all. Widening the
regexes is the fix that has broken something every round it was tried (README, session 126).

Jev (TypeSafe AI, a "System One" model) answers one typed question -- which service should
answer this? -- with a probability for every option. It does not write text and it computes
nothing. So it can only ever send a message to a doer that already exists; the doer still owns
the answer, and every number on air still comes from Python or the radio.

MEASURED BEFORE BUILT (2026-09-21, jev-1.13.0, every unique non-reaction message in the inbox,
319 of them, against each doer's documented scope, ambiguous cases counted against Jev):
    current regex ladder            245/319
    Jev alone                       306/319
    ladder first, Jev on fallthrough, acting at confidence >= 0.7   293/319, fixes 48, breaks 0
BUT 44 of those 48 were channel chatter nobody addressed to Cal. Among the 56 messages that
were addressed to Cal, the ladder misrouted 4 and Jev fixed all 4. This module is the
ADDRESSED-ONLY version; widening it to broadcast traffic is a different decision (airtime, and
every public message leaving the house) and it is not made here. docs/proposals/jev-routing.md.

THE RULES:
1. ASKED ONLY ON THE FALLTHROUGH. `eligible()` requires that no doer claimed the message and
   that it is about to go to the model. A message the ladder answers never leaves this machine,
   so a regex that works is never second-guessed and never overruled.
2. NEVER A PRIVATE DM. An unlocked DM carries operator context; it is excluded by `eligible()`.
   Neither is a message the sanitizer flagged: Jev's own docs say adversarial text can move it.
3. RESCUES ONLY INTO A DOER THAT CAN STILL REFUSE. `RESCUABLE` is weather, caps and sigreport.
   Each keeps its own fail-safe: weather says "Can't reach weather" rather than invent, a
   forecast ask is still refused, sigreport still requires measurements, cooldown and budget.
   calc is excluded because a failed parse means there is nothing to compute; greeting because
   answering more greetings is a policy change, not a routing fix.
4. FAIL OPEN TO TODAY. Any error, timeout, 4xx/5xx, malformed body or unknown route returns a
   result with `route` None, and the caller does exactly what it did before this module existed.
5. THE MODEL IS PINNED. `jev-latest` moves on every release and the threshold was measured on
   jev-1.13.0. Moving is a deliberate change with a re-run of the measurement, not an alias flip.
6. THE KEY NEVER TRAVELS. It is read from JEV_KEY_FILE at call time and appears in no return
   value, log line or trace. Only the route, its confidence and the model id are recorded.
"""
import json, os, time, urllib.request

MODEL = "jev-1.13.0"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
RESCUABLE = ("weather", "caps", "sigreport")

# The question and its options live here and nowhere else, so review reads one block.
# Option text is each doer's documented scope, not the corpus it was measured on.
INSTRUCTIONS = ("Which service should answer the message in `mesh_message`, sent to a station "
                "named Cal on a LoRa mesh radio network?")
CRITERIA = {
    "weather": "A question asking for current or forecast local weather information: rain, wind, "
               "temperature, snow, humidity, storms, whether it is hot or cold out. Not a "
               "statement, report or relayed alert about the weather",
    "sunmoon": "Asking for sunrise, sunset, dawn, dusk, twilight, moonrise, moonset or moon phase",
    "calc": "Asking for a computed answer: arithmetic, fractions, percentages, a unit conversion, "
            "bolt torque, concrete volume, acreage, a radio or electrical formula (wavelength, "
            "antenna length, dBm, ohms, path loss), or a navigation calculation such as a "
            "Maidenhead grid square or the distance and bearing between two places",
    "sigreport": "A radio range test, signal test, radio check or mic check, a contact report, or "
                 "asking how well their signal is being received (SNR, RSSI, 'how do you hear "
                 "me', 'copy?')",
    "caps": "Asking what this station can do, what topics it knows, or for a list of its "
            "commands or help",
    "greeting": "The whole message is only a hello or a wave (hi, hello, hey, good morning, good "
                "evening, a wave emoji) and asks nothing. Not a goodbye, not cheering, and not an "
                "emoji other than a wave",
    "conversation": "Anything else: chat, questions about the station or its operator, opinions, "
                    "general knowledge, statements, or radio talk that is not asking for one of "
                    "the specific services above",
}

DEFAULTS = {
    "JEV_ROUTE_ENABLED": "false",
    "JEV_MIN_CONF": "0.8",          # measured: 0 breaks from 0.7 up; start above it
    "JEV_TIMEOUT_S": "2",
    "JEV_KEY_FILE": "~/.credentials/typesafe",
}


def _cfg(cfg, key):
    return (cfg or {}).get(key, DEFAULTS[key])


def enabled(cfg):
    return str(_cfg(cfg, "JEV_ROUTE_ENABLED")).lower() == "true"


def eligible(cfg, plan):
    """(ok, reason). The whole of rule 1 and rule 2 -- one place, so it cannot drift."""
    if not enabled(cfg):
        return False, "jev_disabled"
    if plan.get("capability") is not None or plan.get("mode") != "generate":
        return False, "claimed_by_ladder"
    if plan.get("unlocked"):
        return False, "private_dm"
    if plan.get("flagged"):
        return False, "injection_flagged"
    if not (plan.get("clean") or "").strip():
        return False, "empty"
    return True, "fallthrough"


def _http_post(url, body, headers, timeout):
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(65536).decode("utf-8"))


def classify(cfg, text, post=None):
    """{'route', 'conf', 'model', 'ms', 'error'}. `route` is None on ANY failure (rule 4).
    `post(url, body, headers, timeout)` is injectable so the eval never touches the network."""
    res = {"route": None, "conf": None, "model": None, "ms": None, "error": None}
    try:
        with open(os.path.expanduser(_cfg(cfg, "JEV_KEY_FILE"))) as f:
            key = f.read().strip()
    except OSError:
        res["error"] = "no_key"
        return res
    if not key:
        res["error"] = "no_key"
        return res
    body = {"model": MODEL, "state": {"mesh_message": text},
            "questions": {"route": {"type": "choice", "instructions": INSTRUCTIONS,
                                    "criteria": CRITERIA}}}
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    try:
        timeout = float(_cfg(cfg, "JEV_TIMEOUT_S"))
    except ValueError:
        timeout = float(DEFAULTS["JEV_TIMEOUT_S"])
    t0 = time.time()
    try:
        r = (post or _http_post)(ENDPOINT, body, headers, timeout)
        a = r["answers"]["route"]
        route, conf = a["choice"], float(a["confidence"])
    except Exception as e:                     # rule 4: every failure is "no opinion"
        res["ms"] = round((time.time() - t0) * 1000)
        res["error"] = type(e).__name__
        return res
    res["ms"] = round((time.time() - t0) * 1000)
    if route not in CRITERIA or not (0.0 <= conf <= 1.0):
        res["error"] = "bad_answer"
        return res
    res.update({"route": route, "conf": round(conf, 3), "model": r.get("model")})
    return res


def decide(cfg, res):
    """The route to act on, or None. Below threshold, or not a doer that can refuse, is None."""
    if not res or res.get("route") not in RESCUABLE:
        return None
    try:
        floor = float(_cfg(cfg, "JEV_MIN_CONF"))
    except ValueError:
        floor = float(DEFAULTS["JEV_MIN_CONF"])
    return res["route"] if (res.get("conf") or 0.0) >= floor else None
