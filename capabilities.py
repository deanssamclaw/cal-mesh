#!/usr/bin/env python3
"""CAPABILITIES — a fixed, config-derived answer to "what can you do".

WHY. On 2026-08-18 07:12 CT this arrived on the open channel:

    "List for me the categories or topics of information you know."

No capability claimed it, so it fell through to the language model, which answered:

    "I can help with coding, technical questions, writing, research, analysis, and
     general knowledge. What do you need?"

That is a chat assistant answering a handheld radio. None of it is available over LoRa in
seven words, and the reply describes a product rather than this node. It is the exact
failure class the architecture exists to prevent — an unrecognised question reaching the
model, which then invents — and it is worse than an invented number because it invents a
*self-description*, which a stranger has no way to check.

TWO DESIGN RULES, both learned the hard way elsewhere in this codebase.

1. THE ANSWER IS BUILT FROM CONFIG, NEVER WRITTEN DOWN. A hardcoded menu is correct on the
   day it is written and lies the first time a capability is disarmed. The flags are the
   only thing that knows what is actually armed, so the flags compose the sentence. The
   model is not in this path at all.

2. IT SITS AT THE BOTTOM OF THE LADDER. This doer must never take a message a real
   capability would answer: a menu that outranked weather would answer "what's the
   temperature" with a list of topics, which is a worse bug than the one being fixed.
   Placement below weather/calc/sunmoon is not a detail — it is most of the correctness,
   and eval_capabilities.py spends its largest section on exactly that.

Matching is deliberately narrow. "do you know" is NOT a trigger, because "do you know joe"
is not a menu question; the family requires an interrogative "what". Bare "help" is a menu
question, but "help me lift this" is not, so "help" only counts as the entire message.
"""
import re

# Each pattern is anchored on an interrogative or an explicit menu noun. Nothing here fires
# on a bare verb, which is what keeps ordinary traffic out.
_PATTERNS = (
    (re.compile(r"\bwhat can (?:you|i) (?:do|ask|help|offer)\b"), "what_can"),
    (re.compile(r"\bwhat do you (?:do|know)\b"),                  "what_do_you"),
    (re.compile(r"\bwhat (?:topics|categories|subjects|kinds?|types?|sorts?)\b.{0,30}?"
                r"\b(?:know|do|help|offer|answer|cover)\b"),      "topics"),
    (re.compile(r"\blist\b.{0,40}?\b(?:topics|categories|subjects|information|things)\b"),
                                                                  "list"),
    (re.compile(r"\bcapabilit(?:y|ies)\b"),                       "capabilities"),
    (re.compile(r"\bwhat are you for\b"),                         "what_for"),
    (re.compile(r"\bwhat services\b"),                            "services"),
    (re.compile(r"\bcommands?\b"),                                "commands"),
)

# "help" is a menu ask only when it is the whole message. As a verb with an object
# ("help me lift this") it is ordinary traffic and must reach the model.
_BARE_HELP = re.compile(r"^(?:help|menu|options)$")


def _normalize(text, trigger="cal"):
    """Lowercase, drop the trigger word, strip punctuation and collapse whitespace."""
    t = (text or "").lower()
    if trigger:
        t = re.sub(r"\b%s\b" % re.escape(trigger.lower()), " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def explain_match(text, trigger="cal"):
    """{'via': <rule name or None>, 'norm': <normalized text>} — the trace wants the rule."""
    norm = _normalize(text, trigger)
    if not norm:
        return {"via": None, "norm": norm}
    if _BARE_HELP.match(norm):
        return {"via": "bare_help", "norm": norm}
    for rx, name in _PATTERNS:
        if rx.search(norm):
            return {"via": name, "norm": norm}
    return {"via": None, "norm": norm}


def answer(cfg, max_chars=120):
    """Compose the reply FROM THE FLAGS. Never a stored string, so it cannot drift from
    what is actually armed. Ordered resilient-first: what works with no network comes
    first, because that ordering is the point of the whole node."""
    def on(key):
        return str(cfg.get(key, "false")).lower() == "true"

    parts = []
    if on("CALC_ENABLED"):
        parts.append("math, units, RF")
    if on("SUNMOON_ENABLED"):
        parts.append("sun and twilight times")
    if on("WEATHER_ENABLED"):
        parts.append("current weather")

    if not parts:
        return "Nothing armed right now."

    reply = "I can do " + "; ".join(parts) + "."
    # Airtime is shared. A menu is exactly the reply that wants to sprawl, so it is bounded
    # here rather than trusted to stay short as capabilities are added.
    if len(reply) > max_chars:
        reply = reply[:max_chars - 1].rstrip(" ,;") + "."
    return reply
