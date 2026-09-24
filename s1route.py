#!/usr/bin/env python3
"""S1ROUTE — a second opinion on routing, asked only where the ladder has already given up.

(Named `jevroute` until 2026-09-23: it was built against TypeSafe's Jev, and what runs today is
a frozen Qwen3.5-4B scored by SemIf on jlab. "System One" is the model class, not a vendor.)

WHY. Every doer here claims a message with a regex, and the ladder's fallback is the language
model. A message no regex claims therefore reaches the one component that can invent. That is
not hypothetical: `Cal, hows the link holding up?` was answered "Link's solid and steady over
here" with no number behind it (sigreport.py records that), and 10 of 14 natural present-tense
weather asks ("is it raining", "how cold is it") match no weather trigger at all. Widening the
regexes is the fix that has broken something every round it was tried (README, session 126).

A System One model (TypeSafe's Jev in the cloud, or the local scorer on jlab) answers one typed
question -- which service should answer this? -- with a probability for every option. It does not write text and it computes
nothing. So it can only ever send a message to a doer that already exists; the doer still owns
the answer, and every number on air still comes from Python or the radio.

MEASURED (2026-09-21, jev-1.13.0, the 319 unique non-reaction inbox messages, disagreements
adjudicated against each doer's documented scope, ambiguous cases counted against Jev; this
exact configuration, three calls per message):
    all 319, as if every message were eligible      ladder 245 -> 254   (+9, 0 broken)
    addressed, public channel  (the default)        ladder  23 ->  25   (+2, 0 broken, n=25)
    + DMs and Cal's channel    (S1_PRIVATE_OK)     ladder  47 ->  50   (+3, 0 broken, n=56)
No message flipped between act and no-act across the three calls. REVIEW 2026-09-24: the labels
side with Jev on every disputed addressed message (relabelled, the all-319 row is +6/-3), and the
default row is 7 fallthrough messages, 2 fixes -- a direction, not a rate. Every fix is a link/signal
ask ("Cal, hows the link holding up?") the model used to answer with no number behind it.
Small on purpose: an earlier hybrid that also routed greetings and un-addressed chatter scored
far higher (293/319) and is NOT this module -- that widening is a separate decision (airtime,
and every public message leaving the house). docs/proposals/jev-routing.md.

THE RULES:
1. ASKED ONLY ON THE FALLTHROUGH. `eligible()` requires that no doer claimed the message and
   that it is about to go to the model. A message the ladder answers never leaves this machine,
   so a regex that works is never second-guessed and never overruled.
2. PRIVATE TRAFFIC STAYS HOME BY DEFAULT. Every DM and everything on Cal's own channel is
   excluded unless S1_PRIVATE_OK=true; an unlocked DM is excluded even then, and so is any
   message the sanitizer flagged (Jev's own docs say adversarial text can move it).
3. RESCUES ONLY INTO A DOER THAT CAN STILL REFUSE. `RESCUABLE` is weather, caps and sigreport.
   Each keeps its own fail-safe: weather says "Can't reach weather" rather than invent, a
   forecast ask is still refused, sigreport still requires measurements, cooldown and budget.
   Two GUARDS, asked in the same call, gate the two rescues that can mislead: weather needs
   `weather_now` (past-tense asks were being answered with the current reading), sigreport
   needs `other_station` low AND no node named in the text (a question about a repeater was
   being answered with the sender's own link numbers).
   calc is excluded because a failed parse means there is nothing to compute; greeting because
   answering more greetings is a policy change, not a routing fix.
4. FAIL OPEN TO TODAY. Any error, timeout, 4xx/5xx, malformed body or unknown route returns a
   result with `route` None, and the caller does exactly what it did before this module existed.
   S1_TIMEOUT_S is a WALL-CLOCK bound on the whole request, and a network failure OR a malformed
   answer backs off for S1_BACKOFF_S, so a broken scorer costs one timeout, not one per message.
   A 4xx rejects one request and does not back off -- unless S1_REJECTS_MAX arrive in a row,
   which is a scorer rejecting everything, not a message it cannot read.
5. THE MODEL IS PINNED. (Cloud path. The local service names the model it ran in `model`.) `jev-latest` moves on every release and the threshold was measured on
   jev-1.13.0. Moving is a deliberate change with a re-run of the measurement, not an alias flip.
6. THE KEY NEVER TRAVELS. It is read from S1_KEY_FILE at call time and appears in no return
   value, log line or trace. Only the route, its confidence and the model id are recorded.
"""
import json, math, os, threading, time, urllib.request

MODEL = "jev-1.13.0"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"

# TWO BACKENDS, ONE DECISION. `typesafe` is the cloud service; `local` is a scorer on our own
# hardware (system_one_server.py on jlab: a frozen Qwen3.5-4B scored by SemIf with one fitted
# temperature). Measured on the same 319 messages they make the SAME rescues and break nothing;
# the local one keeps message text in the house and costs ~13 s instead of 0.25 s. See
# docs/proposals/local-system-one.md.
#
# THE LOCAL PROMPT IS NOT THE CLOUD PROMPT, and that is deliberate. The measurement that licenses
# this was taken with the state as PLAIN TEXT and a question that does not name a state field.
# Sending the cloud shape to the local scorer moved "Cal, hows the link holding up?" from 0.90 to
# 0.79 -- across the floor. These constants are the measured wording; changing either means
# re-running tools/local-system-one before trusting the threshold.
LOCAL_INSTRUCTIONS = ("Which service should answer this message, sent to a station named Cal on a "
                      "LoRa mesh radio network?")
LOCAL_GUARDS = {
    "weather_now": "Is this message asking about the weather conditions right now? Not about past "
                   "weather and not a forecast.",
    "other_station": "Does this message ask about the signal, link or reception of a specific "
                     "other station, repeater, router or node, rather than the link between the "
                     "sender and Cal?",
}
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

# Two guards asked IN THE SAME CALL (speculative fan-out; they cost input tokens, not latency).
# Each closes a hole the adversarial review reproduced with the real model:
#   weather_now   -- "what was the high yesterday" was routed to weather at 0.98 and would have
#                    been answered with the current reading. Past and future both score ~0.02.
#   other_station -- "how is the signal from the Olathe repeater" was routed to sigreport at 0.99
#                    and would have been answered with the SENDER's link numbers. Third-party
#                    asks score 0.93-0.97; genuine self-link asks <= 0.23 (measured 2026-09-21).
GUARDS = {
    "weather_now": "Is the message in `mesh_message` asking about the weather conditions right "
                   "now? Not about past weather and not a forecast.",
    "other_station": "Does the message in `mesh_message` ask about the signal, link or reception "
                     "of a specific other station, repeater, router or node, rather than the link "
                     "between the sender and Cal?",
}
OTHER_STATION_MAX = 0.5     # sigreport is refused at or above this; the regex backs it up

DEFAULTS = {
    "S1_ROUTE_ENABLED": "false",
    "S1_MIN_CONF": "0.8",          # measured: 0 breaks from 0.7 up; start above it
    "S1_TIMEOUT_S": "2",
    "S1_KEY_FILE": "~/.credentials/typesafe",
    # DMs and Cal's own (PSK) channel are PRIVATE traffic. Off by default: arming the router does
    # not by itself send private messages to a third party; this is a second, separate decision.
    "S1_PRIVATE_OK": "false",
    # After a failed call, skip Jev for this long, so an outage costs one timeout, not one per
    # message. The overall deadline is S1_TIMEOUT_S, enforced on the whole request.
    "S1_BACKOFF_S": "300",
    # typesafe | local. The flag above still decides whether ANY of this runs.
    "S1_BACKEND": "typesafe",
    # Set this to the scorer's TAILNET address. A MagicDNS name resolves to the PUBLIC relay
    # address on any host that has Funnel enabled -- `jlab` resolves to a 199.x, not a 100.x --
    # so a hostname here can silently point the router off the tailnet. Loopback by default so an
    # unset config reaches nothing rather than something wrong.
    "S1_LOCAL_URL": "http://127.0.0.1:8799/v1/systemone",
    # The local scorer is a CPU doing three forward passes; measured ~13 s for a route plus both
    # guards. It sits on the path that was about to call the language model anyway.
    "S1_LOCAL_TIMEOUT_S": "30",
    # A local scorer that answers "too hot" is not broken, so it gets its own shorter window.
    "S1_BUSY_BACKOFF_S": "120",
    # Consecutive 4xx rejections before a rejection counts as an outage. One unreadable message
    # (the scorer 422s some emoji) must not silence the router; a scorer that answers EVERY
    # request with a slow 4xx must not cost up to S1_LOCAL_TIMEOUT_S on every message either
    # (review 2026-09-24: a 25 s 422 backed off 0 s, forever).
    "S1_REJECTS_MAX": "3",
    # none | typesafe. A SECOND scorer, asked only when the local one could not answer (hot, busy,
    # down, timed out, rejected, malformed) or is sitting out a backoff. OFF by default, because
    # turning it on reverses the decision that armed this router: that no message text leaves the
    # house. PUBLIC traffic only, always -- a DM or Cal's channel never goes to the fallback, even
    # with S1_PRIVATE_OK=true (that flag was a decision about the scorer on our own hardware).
    # Measured: the cloud path answers in ~0.25 s on the pinned jev-1.13.0 (2026-09-24), and the
    # floor, guards and walls are the same code on both.
    "S1_FALLBACK": "none",
}
# The PRIMARY scorer's failure state at top level; the fallback keeps its own, so an outage of
# one never silences the other.
_state = {"backoff_until": 0.0, "rejects": 0,
          "fallback": {"backoff_until": 0.0, "rejects": 0}}


def _cfg(cfg, key):
    return (cfg or {}).get(key, DEFAULTS[key])


def enabled(cfg):
    return str(_cfg(cfg, "S1_ROUTE_ENABLED")).lower() == "true"


def backend(cfg):
    """typesafe | local | None. An unrecognised value is None and the router does not run.
    It used to fall back to typesafe, and the config loader keeps an inline comment as part of
    the value -- so `S1_BACKEND=local  # typesafe | local` silently sent message text to the
    cloud (review 2026-09-24). A misconfigured backend fails CLOSED, never to the other one."""
    b = str(_cfg(cfg, "S1_BACKEND")).strip().lower()
    return b if b in ("typesafe", "local") else None


def fallback_ok(cfg, private=False, now=None):
    """True if the cloud may be asked for THIS message when the local scorer cannot answer.
    Unknown values are none (fail closed), and only a local primary has a fallback."""
    if str(_cfg(cfg, "S1_FALLBACK")).strip().lower() != "typesafe":
        return False
    if backend(cfg) != "local" or private:
        return False
    return (time.time() if now is None else now) >= _state["fallback"]["backoff_until"]


def eligible(cfg, plan, private=False, now=None):
    """(ok, reason). The whole of rule 1 and rule 2 -- one place, so it cannot drift.
    `private` is decided by the caller from the packet: a DM, or Cal's own channel."""
    if not enabled(cfg):
        return False, "s1_disabled"
    if backend(cfg) is None:
        return False, "bad_backend"
    if plan.get("capability") is not None or plan.get("mode") != "generate":
        return False, "claimed_by_ladder"
    if plan.get("unlocked"):
        return False, "private_dm"
    if plan.get("flagged"):
        return False, "injection_flagged"
    if private and str(_cfg(cfg, "S1_PRIVATE_OK")).lower() != "true":
        return False, "private_traffic"
    if not (plan.get("clean") or "").strip():
        return False, "empty"
    if ((time.time() if now is None else now) < _state["backoff_until"]
            and not fallback_ok(cfg, private, now)):
        return False, "backoff"
    return True, "fallthrough"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urlopen follows 3xx by default. A scorer that answers with a redirect is either
    misconfigured or not ours, and following it would send the next request somewhere this
    config never named -- with the cloud path, carrying an Authorization header. Refuse."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http_post(url, body, headers, timeout):
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read(65536).decode("utf-8"))


def _with_deadline(fn, deadline):
    """Run fn() with a hard WALL-CLOCK limit. urlopen's timeout bounds each socket operation, not
    the request: a server trickling one byte every half second held a 2 s call for 40 s in
    review. The worker is a daemon thread; if it overruns, it is abandoned and we move on."""
    box = {}
    def run():
        try:
            box["v"] = fn()
        except Exception as e:           # surfaced to the caller below
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(deadline)
    if t.is_alive():
        raise TimeoutError("deadline")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _prob(v):
    """A probability or ValueError. Rejects bool, strings, NaN/inf and out-of-range values --
    `confidence: true` and `"0.95"` were both accepted before review."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("not a number")
    v = float(v)
    if not math.isfinite(v) or not (0.0 <= v <= 1.0):
        raise ValueError("out of range")
    return v


def _backoff(cfg, key, now, st=None):
    st = _state if st is None else st
    try:
        back = float(_cfg(cfg, key))
        if not math.isfinite(back):
            raise ValueError
    except ValueError:
        back = float(DEFAULTS[key])
    # Capped at a day: `S1_BACKOFF_S=1e18` is finite and would have kept the router off until
    # the next restart with nothing on the page saying why.
    st["backoff_until"] = (time.time() if now is None else now) + min(86400.0, max(0.0, back))
    st["rejects"] = 0


def classify(cfg, text, post=None, now=None, slot=None):
    """{'route','conf','weather_now','other_station','model','ms','error'}; route None on ANY
    failure (rule 4). `post(url, body, headers, timeout)` is injectable so the eval never touches
    the network. A network-shaped failure starts the backoff window."""
    st = _state if slot is None else _state[slot]
    res = {"route": None, "conf": None, "weather_now": None, "other_station": None,
           "model": None, "ms": None, "error": None, "backend": backend(cfg)}
    if res["backend"] is None:
        res["error"] = "bad_backend"
        return res
    local = res["backend"] == "local"
    key = ""
    if not local:
        try:
            with open(os.path.expanduser(_cfg(cfg, "S1_KEY_FILE")), encoding="utf-8") as f:
                key = f.read().strip()
        except (OSError, UnicodeDecodeError, ValueError):
            key = ""
        if not key:
            res["error"] = "no_key"
            return res
    if local:
        questions = {"route": {"type": "choice", "instructions": LOCAL_INSTRUCTIONS,
                               "criteria": CRITERIA}}
        questions.update({k: {"type": "noul", "instructions": v} for k, v in LOCAL_GUARDS.items()})
        body = {"state": text, "questions": questions}          # plain text: the measured shape
        headers = {"Content-Type": "application/json"}          # no key leaves this machine
    else:
        questions = {"route": {"type": "choice", "instructions": INSTRUCTIONS, "criteria": CRITERIA}}
        questions.update({k: {"type": "noul", "instructions": v} for k, v in GUARDS.items()})
        body = {"model": MODEL, "state": {"mesh_message": text}, "questions": questions}
        headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    try:
        timeout = float(_cfg(cfg, "S1_LOCAL_TIMEOUT_S" if local else "S1_TIMEOUT_S"))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except ValueError:
        timeout = float(DEFAULTS["S1_TIMEOUT_S"])
    t0 = time.time()
    try:
        url = _cfg(cfg, "S1_LOCAL_URL") if local else ENDPOINT
        r = _with_deadline(lambda: (post or _http_post)(url, body, headers, timeout), timeout)
        a = r["answers"]
        route = a["route"]["choice"]
        if not isinstance(route, str) or route not in CRITERIA:
            raise ValueError("bad route")
        conf = _prob(a["route"]["confidence"])
        wn = _prob(a["weather_now"]["noul"])
        oth = _prob(a["other_station"]["noul"])
        model = r.get("model")
        model = model if isinstance(model, str) and 0 < len(model) <= 40 else None
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
        # A malformed answer -- garbage JSON included, since JSONDecodeError is a ValueError --
        # is a broken scorer, not a message it could not read: the scorer's own refusals come
        # back as 4xx. It backs off like an outage. It did not, and a scorer answering every
        # request with a slow malformed body cost up to the full timeout on every message.
        res["ms"] = round((time.time() - t0) * 1000)
        res["error"] = "bad_answer"
        _backoff(cfg, "S1_BACKOFF_S", now, st)
        return res
    except Exception as e:                     # network-shaped: timeout, HTTP, DNS
        res["ms"] = round((time.time() - t0) * 1000)
        res["error"] = type(e).__name__[:40]
        # A 4xx says THIS REQUEST was rejected, not that the scorer is down, so ONE must not
        # silence the router for the outage window. Found by running: the local scorer refuses
        # input whose GGUF and reference tokenizations disagree (some emoji) with a 422, and one
        # such message would otherwise have taken the router off the air for S1_BACKOFF_S.
        # S1_REJECTS_MAX in a row is a scorer rejecting everything, and that does back off.
        # 429 is excluded: being rate-limited IS a reason to wait.
        # 3xx is included: a redirect from the scorer is a rejected request, not an outage.
        code = getattr(e, "code", None)
        if isinstance(code, int) and 300 <= code < 500 and code != 429:
            res["error"] = f"http_{code}"
            st["rejects"] += 1
            try:
                most = int(_cfg(cfg, "S1_REJECTS_MAX"))
            except ValueError:
                most = int(DEFAULTS["S1_REJECTS_MAX"])
            if st["rejects"] >= min(100, max(1, most)):
                _backoff(cfg, "S1_BACKOFF_S", now, st)
            return res
        # A local scorer refusing because the CPU is hot (or because it is already scoring
        # something else) is a healthy answer, not an outage: a shorter window.
        busy = local and code == 503
        if busy:
            res["error"] = "busy"
        _backoff(cfg, "S1_BUSY_BACKOFF_S" if busy else "S1_BACKOFF_S", now, st)
        return res
    st["rejects"] = 0
    res["ms"] = round((time.time() - t0) * 1000)
    # Unrounded: decide() compares these against the floor, and rounding first let 0.7999 act
    # at a 0.8 floor. The trace rounds for display; the decision never sees a rounded value.
    res.update({"route": route, "conf": conf, "weather_now": wn, "other_station": oth,
                "model": model})
    return res


def consult(cfg, text, private=False, primary=None, fallback=None, now=None):
    """The primary scorer, then -- only if it could not answer and fallback_ok -- the cloud one.
    `primary(cfg, text)` / `fallback(cfg, text)` are injectable for the eval. The result says
    which backend answered, and `fallback_from` names the local failure that sent it there."""
    primary = primary or classify
    fallback = fallback or (lambda c, x: classify(c, x, slot="fallback"))
    t = time.time() if now is None else now
    if t < _state["backoff_until"]:
        res = {"route": None, "error": "backoff", "backend": backend(cfg), "ms": 0}
    else:
        res = primary(cfg, text)
    if res.get("route") is not None or res.get("error") in (None, "bad_backend", "no_key"):
        return res
    if not fallback_ok(cfg, private, now):
        return res
    fb = fallback(dict(cfg, S1_BACKEND="typesafe"), text)
    # No key means no request was made: nothing left the house, so the result must not say it
    # did (review 2026-09-24 -- the page printed "sent to Jev" for a call that never happened).
    if fb.get("error") in ("no_key", "bad_backend"):
        return res
    fb["fallback_from"] = {"backend": res.get("backend"), "error": res.get("error"), "ms": res.get("ms")}
    return fb


def decide(cfg, res):
    """The route to act on, or None. Below threshold, a guard that fails, or not a doer that
    can refuse, is None -- which is today's path."""
    if not res or res.get("route") not in RESCUABLE:
        return None
    try:
        floor = float(_cfg(cfg, "S1_MIN_CONF"))
        if not math.isfinite(floor):
            raise ValueError
    except ValueError:
        floor = float(DEFAULTS["S1_MIN_CONF"])
    # Every comparison is written so that NaN, None or a non-number REFUSES. `x < floor` is
    # False for NaN, so `if conf < floor: return None` let a NaN act (review 2026-09-24);
    # classify's _prob is the first wall, this is the second.
    def num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
    conf, wn, oth = num(res.get("conf")), num(res.get("weather_now")), num(res.get("other_station"))
    if not (conf is not None and conf >= floor):
        return None
    if res["route"] == "weather" and not (wn is not None and wn >= floor):
        return None
    if res["route"] == "sigreport" and not (oth is not None and oth < OTHER_STATION_MAX):
        return None
    return res["route"]
