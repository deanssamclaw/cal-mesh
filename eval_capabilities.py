#!/usr/bin/env python3
"""Offline eval for the CAPABILITIES doer.

No radio, no model, no network.

WHY THIS EXISTS. On 2026-08-18 07:12 CT a real inbound — "List for me the categories or
topics of information you know." — was claimed by NO capability, fell through to the
language model, and was answered "I can help with coding, technical questions, writing,
research, analysis, and general knowledge." That is a chat assistant answering a handheld
radio: none of it is true over LoRa in seven words, and the reply describes a product
rather than this node. The failure class is the one the whole architecture exists to
prevent — an unrecognised question reaching the model, which then invents.

The doer answers from CONFIG, never from the model, so the list cannot drift from what is
actually armed. Three halves:

  POSITIVE  — capability questions get the fixed, config-derived string.
  ORDERING  — the doer sits BELOW every real capability and must never steal from one.
              This is the half that matters: a doer that answers "what's the temperature"
              with a capability list is worse than the bug it replaces.
  NEGATIVE  — ordinary traffic is untouched and still reaches the model.

Run: python3 eval_capabilities.py [-v]
"""
import sys

import capabilities as C
import responder as R

V = "-v" in sys.argv
PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        if V:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s: got %r want %r" % (name, got, want))


def truthy(name, got):
    check(name, bool(got), True)


def falsy(name, got):
    check(name, bool(got), False)


def cfg(**over):
    """Everything armed, as deployed on 2026-08-18."""
    c = dict(R.DEFAULTS)
    c.update({"CAPS_ENABLED": "true", "CALC_ENABLED": "true", "SUNMOON_ENABLED": "true",
              "WEATHER_ENABLED": "true", "TRIGGER_WORD": "cal",
              "WEATHER_POINT": "39.0,-95.0", "SUNMOON_POINT": "39.0,-95.0",
              "SUNMOON_TZ": "America/Chicago"})
    c.update(over)
    return c


def plan(text, **over):
    """plan_response with the network stubbed. get= returns None so no fetch can occur;
    any test that reaches weather is a test that FAILED to be claimed by the doer."""
    return R.plan_response(cfg(**over), "n0de", text, get=lambda *a, **k: None)


# ---------------------------------------------------------------- POSITIVE
# The live defect, verbatim off the wire, plus the phrasings a person actually reaches for.
POSITIVE = [
    "List for me the categories or topics of information you know.",   # the 07:12 defect
    "list the topics of information you know",
    "what can you do",
    "cal what can you do?",
    "what do you know",
    "what are your capabilities",
    "what can you help with",
    "what do you know about",
    "what topics do you know",
    "what are you for",
    "what can i ask you",
    "what can i ask",
    "commands?",
    "cal help",
    "help",
    "what services do you offer",
]

for t in POSITIVE:
    truthy("match: %r" % t, C.explain_match(t)["via"])
    p = plan(t)
    check("claims: %r" % t, p["capability"], "capabilities")
    check("fixed: %r" % t, p["mode"], "fixed")
    truthy("no model: %r" % t, p["prompt"] is None)


# ------------------------------------------------------- ORDERING (the half that matters)
# Every one of these must be claimed by a REAL capability, never by the list. A doer that
# answers a temperature question with a menu is a worse bug than the one being fixed.
STEALING = [
    ("cal whats the temperature?",              "weather"),
    ("what is the weather",                     "weather"),
    ("can you tell me the temperature",         "weather"),
    ("do you know the weather",                 "weather"),
    ("what do you know about the weather",      "weather"),
    ("whats the temp",                          "weather"),
    ("when is sunset",                          "sunmoon"),
    ("what time does it get dark",              "sunmoon"),
    ("do you know when sunset is",              "sunmoon"),
    ("what is 12*12",                           "calc"),
    ("cal 2+2",                                 "calc"),
    ("what is the wavelength at 915 mhz",       "calc"),
]

for t, want in STEALING:
    p = plan(t)
    check("ordering %r -> %s" % (t, want), p["capability"], want)


# ------------------------------------------- PRE-EXISTING GAPS (verified NOT caused by this)
# These three reach NO capability and fall through to the model. A control run on 2026-08-18
# with CAPS_ENABLED=false returned byte-identical results, so this doer neither causes nor
# fixes them. Two are DELIBERATE: calc refuses prose containing an expression on purpose
# ("box 5 * 3" is a box), a refusal Dean's own corpus validated in session 126. The third is a
# genuine sunmoon trigger gap — logged, real, and out of scope for this change.
#
# What this doer IS responsible for: not making them worse by claiming them.
PRE_EXISTING = [
    "can you tell me when the sun sets",      # sunmoon trigger gap — real, unfixed
    "can you tell me what 40*3 is",           # calc prose refusal — deliberate
    "do you know what .5 mi is in km",        # calc prose refusal — deliberate
]

for t in PRE_EXISTING:
    check("pre-existing %r not claimed as capabilities" % t,
          plan(t)["capability"] == "capabilities", False)


# ---------------------------------------------------------------- NEGATIVE
# Ordinary traffic must be untouched and must still reach the model.
NEGATIVE = [
    "hello",
    "hi cal",
    "range test",
    "how are you today",
    "hows the radio holding up",
    "got you here in olathe",
    "thanks",
    "are you familiar with the pocket ref book",   # a book question, not a menu question
    "i know a guy who can help",
    "let me know what you find",
    "do you know joe",
    "what a day",
    "help me lift this",                           # 'help' as a verb with an object
    "can you help me move the antenna",
]

for t in NEGATIVE:
    falsy("no match: %r" % t, C.explain_match(t)["via"])
    p = plan(t)
    check("falls through: %r" % t, p["capability"], None)


# ---------------------------------------------------------------- CONFIG-DERIVED
# The list must be built from the flags, never hardcoded — otherwise it drifts from reality
# the first time a capability is disarmed, and the node lies about itself.
r_all = C.answer(cfg())
for word in ("math", "sun", "weather"):
    truthy("all-armed reply mentions %s: %r" % (word, r_all), word in r_all.lower())

r_nowx = C.answer(cfg(WEATHER_ENABLED="false"))
falsy("weather disarmed -> not offered: %r" % r_nowx, "weather" in r_nowx.lower())
truthy("weather disarmed -> still offers math", "math" in r_nowx.lower())

r_none = C.answer(cfg(CALC_ENABLED="false", SUNMOON_ENABLED="false", WEATHER_ENABLED="false"))
falsy("nothing armed -> offers nothing", any(w in r_none.lower() for w in ("math", "sun", "weather")))
truthy("nothing armed -> still a reply", len(r_none) > 0)

# Airtime budget. LoRa is shared; a menu is exactly the reply that wants to sprawl.
for label, r in (("all", r_all), ("no-weather", r_nowx), ("none", r_none)):
    truthy("budget %s (%d <= 120): %r" % (label, len(r), r), len(r) <= 120)

# The reply must never claim what the node cannot do. This is the actual content of the bug.
for banned in ("coding", "writing", "research", "analysis", "general knowledge"):
    falsy("never claims %r" % banned, banned in r_all.lower())


# ---------------------------------------------------------------- KILL SWITCH
p_off = plan("what can you do", CAPS_ENABLED="false")
check("disarmed -> no claim", p_off["capability"], None)
truthy("disarmed -> reaches model", p_off["prompt"] is not None)


print("eval_capabilities: %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
