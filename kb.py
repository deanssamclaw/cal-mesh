#!/usr/bin/env python3
"""KB — radio and mesh terms, answered from a vetted table, never from a model.

WHY (Dean, 2026-09-24: build what the traffic asks for). "What's tropo?" was asked on this mesh
and a human answered it. The roadmap's rule for knowledge is that the model never free-recalls:
a definition on air is either an operator-vetted sentence with a source behind it, or nothing.

THE TABLE (data/radio_kb.json). Each entry: the answer returned VERBATIM, the authoritative page
it comes from (Meshtastic docs, ARRL, eCFR, NWS), the quote on that page that supports it, and the
date it was verified. Nothing is paraphrased at runtime and nothing is interpolated.

THE MATCHER IS CLOSED-WORLD, the same lesson as sigreport.own_link_only. It fires only when BOTH:
  1. the message is a definition question -- "what's X", "what is (a|an) X", "explain X",
     "what does X mean / stand for", "define X", "meaning of X"; and
  2. X, normalised, EQUALS one alias. Not contains: equals.
So "the router is down", "net income", "car races", "got you at 3 hops", "is there a tornado
warning" and "is tropo happening" can never fire -- none is a definition question about exactly
one listed term. A question about Cal or the asker -- "what's your QTH", "what's my callsign" --
never matches: that is a different intent, and some of those answers are private.

A miss returns None and the message takes its usual path. A wrong-sense hit is bounded by the
answers themselves, which carry their domain ("On Meshtastic...", "In ham radio...").
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data", "radio_kb.json")
MAX_CHARS = 120

_GREET = r"(?:hey|hi|hello|yo|ok|okay)"
_LEAD = re.compile(r"^\s*(?:%s[\s,]+)?(?:@?cal\b[\s,:;!.-]*)?" % _GREET, re.I)
# The name can come last too: "what's tropo, cal?"
_TAIL = re.compile(r"[\s,;:-]+@?cal\s*[?!.]*\s*$", re.I)
# Ordered: "what does X mean" must be tried before the plain "what X" frame, or it reads as the
# term "does X mean" and misses.
#
# NO "the" IN THE PLAIN FRAME (review 2026-09-24). "What's the SNR", "what's the tornado warning",
# "what's the callsign" are questions about a CURRENT value -- a reading, an alert, this station --
# and a textbook definition is the wrong answer to every one of them. "a"/"an" ask what a thing
# IS; "the" asks which one or how much. "The" survives only where the frame already says
# "mean" or "meaning".
_FRAMES = (
    re.compile(r"^(?:what(?:'s|’s|s)?|whats)\s+(?:does\s+|do\s+|is\s+)?(?:an?\s+|the\s+)?(?P<t>.+?)\s+(?:mean|stand\s+for)$", re.I),
    re.compile(r"^(?:define|explain|meaning\s+of|what(?:'s|’s|\s+is)\s+the\s+meaning\s+of)\s+(?:an?\s+|the\s+)?(?P<t>.+?)$", re.I),
    re.compile(r"^(?:what(?:'s|’s|s)?|whats)\s+(?:is\s+|are\s+)?(?:an?\s+)?(?P<t>(?!the\b).+?)$", re.I),
)
_PERSONAL = re.compile(r"\b(your|my|our|his|her|their|cal'?s|cal’s|you|me)\b", re.I)


def _norm(s):
    s = s.replace("’", "'").lower()
    s = re.sub(r"[^\w\s'.-]", " ", s)
    return re.sub(r"\s+", " ", s).strip(" .'-")


def load(path=DATA):
    """(entries, index). index maps a normalised alias -> entry; a case-sensitive alias (all
    capitals, e.g. RACES, LOS) is keyed by its exact spelling with a '=' prefix instead."""
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)["entries"]
    index = {}
    for e in entries:
        for a in e.get("aliases", []):
            key = ("=" + a) if (a.isupper() and len(a) <= 6 and a.isalpha()) else _norm(a)
            if key in index and index[key] is not e:
                raise ValueError(f"alias {a!r} is claimed by {index[key]['key']} and {e['key']}")
            index[key] = e
    return entries, index


_CACHE = {}


def _index(path=DATA):
    if path not in _CACHE:
        _CACHE[path] = load(path)[1]
    return _CACHE[path]


def match(text, path=DATA):
    """The entry this message asks to define, or None."""
    if not isinstance(text, str):
        return None
    s = _TAIL.sub("", _LEAD.sub("", text.strip(), count=1))
    s = s.rstrip(" ?!.")
    for n, fr in enumerate(_FRAMES):
        m = fr.match(s)
        if m:
            break
    else:
        return None
    raw = m.group("t").strip(" ?!.")
    if not raw or _PERSONAL.search(raw):
        return None
    # A bare number is calc's ("what's 73" is a sum waiting to happen). A number is a term only
    # when the question says so: "what does 73 mean".
    if raw.replace(" ", "").isdigit() and n == 2:
        return None
    idx = _index(path)
    exact = idx.get("=" + raw)
    if exact:
        return exact
    hit = idx.get(_norm(raw))
    # A plural of a listed term ("what are repeaters") -- only when the singular is itself listed.
    if hit is None and _norm(raw).endswith("s") and len(_norm(raw)) > 4:
        hit = idx.get(_norm(raw)[:-1])
    return hit


def answer(text, max_chars=MAX_CHARS, path=DATA):
    """(reply, meta) or (None, meta). The reply is the stored answer, verbatim."""
    e = match(text, path)
    if not e:
        return None, {"matched": False}
    reply = e["answer"]
    if len(reply) > max_chars:
        return None, {"matched": True, "key": e["key"], "refused": "too_long"}
    return reply, {"matched": True, "key": e["key"], "source": e["source"],
                   "verified_on": e["verified_on"]}
