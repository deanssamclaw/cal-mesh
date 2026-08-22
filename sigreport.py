#!/usr/bin/env python3
"""SIGREPORT — answer a range or signal test with what the receiver actually measured.

WHY. On 2026-08-22 Dean read the greeting analysis and made the call: "any kind of range test
or signal test deserves a response if we can successfully make that work." The log agrees with
him. Strangers had sent `Range test` four times, plus `Tange test`, `Test test` and a
`latency test`, and every one of them was met with silence, because a range test is not a
greeting and its sender is not on the allow list. A range test is the one message on a mesh
that is explicitly ASKING a listener to say something back — silence is the single least
useful reply available, and it is indistinguishable from being out of range, which is exactly
the thing being measured.

THE ORACLE, WHICH IS WHY THIS IS A DOER AND NOT A PROMPT. Every number in the reply is the
receiver's own measurement OF THE PACKET BEING ANSWERED: `snr`, `rssi` and the hop count are
recorded by bridge.py from the radio's own report on arrival. Nothing here is derived,
estimated, or looked up, so there is no model answer to launder and no external table to be
wrong about. The strongest form of this: when the model previously answered `Cal, hows the
link holding up?` with "Link's solid and steady over here", it had no access to a number at
all. This module can only ever say what the radio said.

WHAT IT MUST NEVER DO. Estimate a DISTANCE. SNR and RSSI are a range proxy, and a distance
published from an unnamed origin is a trilateration fix on Cal — the standing design rule that
already shelved the distance feature and that eval_sunmoon still violates in one line. The
residual here is real and is stated rather than traded silently: a patient stranger who range
tests from several positions and collects these reports can estimate where Cal is, because
signal peaks where the receiver is. That is a slower and noisier version of the same leak,
Cal HT broadcasts no position precisely to avoid it, and it is Dean's call at arm time, not
this module's to make quietly.

FAIL SILENT, NEVER FAIL VAGUE. If the record carries no measurements at all, this returns
None and Cal says nothing. A signal report with no signal in it is not a report, and
"Copy, strong signal" with nothing behind it is the invented-self-description failure that
capabilities.py exists to prevent, wearing a different hat.
"""
import re

# --- what counts as a test ------------------------------------------------------------------
#
# MATCHED BY SHAPE, NOT BY A VOCABULARY LIST. The first draft carried a qualifier whitelist
# (range|signal|radio|rf|link|mesh|latency|...) and it fails the way every whitelist here has
# failed: the real log contains `Tange test`, a typo for Range that no list will ever hold.
# The wavelength doer had already paid for that lesson once, when a cut-length branch gated on
# the literal word "antenna" and a live ask spelled it "anyenna".
#
# The shape that actually separates them is POSITION AND LENGTH: on a mesh channel, a short
# message whose LAST word is "test" or "check" is a radio test. `Range test`, `Tange test`,
# `Test test`, `Cal test`, `latency test`, `radio check`, `mic check` all satisfy it without
# naming any of them; "the range test we ran yesterday failed" does not, because it is long
# and does not end there.
#
# The cost asymmetry is what licenses the loose end of this. A false fire spends one short,
# true, polite sentence of airtime. A miss is silence at the exact moment somebody is asking
# whether they can be heard. That is the opposite of the calc/torque asymmetry, where a wrong
# number is worse than no number, and it is why this trigger is allowed to be the broader one.
_MAX_WORDS_TAIL = 3

# Spelled-out forms that end in something other than the head word. Kept explicit and short:
# these are whole-message idioms, not a vocabulary to grow.
_WHOLE = (
    r"(?:this|that|it)\s+is\s+(?:a|an|another|the)\s+(?:\w+\s+)?test",
    r"(?:radio|comms?|signal|mic|sound)\s+check",
    r"check\s+check(?:\s+check)?",
    r"(?:do\s+you\s+|you\s+)?copy(?:\s+me)?",
    r"how(?:'s|s)?\s+(?:my\s+)?copy",
    r"(?:signal|sig)\s+report",
    r"(?:how|hows|how's)\s+(?:my\s+)?signal",
    r"anyone\s+(?:copy|read|hear)\s+(?:me|this)",
    r"testing(?:\s+\d+)*",
)
_WHOLE_RE = re.compile(r"^(?:" + r"|".join(_WHOLE) + r")$", re.I)

# The head-word rule. Deliberately NOT anchored on a qualifier — see above.
_TAIL_RE = re.compile(r"^(?:[\w'/-]+\s+){0,%d}(?:test|check)$" % (_MAX_WORDS_TAIL - 1), re.I)


def _normalize(text):
    """Lowercase, collapse whitespace, drop trailing punctuation. A question mark is KEPT and
    is not disqualifying here — unlike a greeting, `you copy?` is the ordinary spelling of a
    radio check, and refusing it would refuse the commonest form of the thing."""
    s = (text or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" .!,;:-–—\"'?")


def match(text, trigger="cal"):
    """Return {"via": ...} for a range/signal test, else None. Pure text shape, no I/O.

    The trigger word is stripped FIRST so `Cal range test` and `range test` are the same
    message. It is stripped only from the front, and only as a whole word: a node named
    "Calibration Test" must not be filleted into a match.
    """
    s = _normalize(text)
    if not s:
        return None
    trig = (trigger or "").strip().lower()
    if trig:
        s = re.sub(r"^%s\b[\s,:-]*" % re.escape(trig), "", s).strip()
        # "Cal" alone, once stripped, leaves nothing. That is a hail, not a test.
        if not s:
            return None
    if _WHOLE_RE.match(s):
        return {"via": "phrase", "text": s}
    if _TAIL_RE.match(s):
        # A bare "test" is still a test — one word, ends in test.
        return {"via": "tail", "text": s}
    return None


# --- what the radio measured ----------------------------------------------------------------

def _num(v):
    """A measurement or None. Bool is rejected explicitly: JSON `true` converts to 1 and would
    ship as a plausible SNR pointing the reassuring direction, which is the exact shape of the
    heat-index bug (`true` became 34F and aired)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f or f in (float("inf"), float("-inf")) else f


def hops_of(rec):
    """Hop count as the bridge recorded it, or None.

    `hops` is written by bridge.py; hop_start/hop_limit are kept as the fallback because
    `hopLimit` is OMITTED by MessageToDict when it is 0 (proto3 has no presence for scalars),
    which is what once recorded every fully-relayed packet as "unknown". Absent stays absent —
    a missing hop count is never rendered as "direct", which would be a claim rather than a
    measurement.
    """
    h = _num(rec.get("hops"))
    if h is not None and h >= 0:
        return int(h)
    start, limit = _num(rec.get("hop_start")), _num(rec.get("hop_limit"))
    if start is None or limit is None:
        return None
    d = int(start) - int(limit)
    return d if d >= 0 else None


def report(rec, max_chars=64):
    """Build the reply, or (None, meta) when there is nothing measured to report.

    Field-by-field degradation: a malformed snr costs the snr and nothing else. Only the
    complete absence of every measurement refuses outright.
    """
    meta = {"snr": None, "rssi": None, "hops": None, "parts": [], "refused": None}
    snr = _num(rec.get("snr"))
    rssi = _num(rec.get("rssi"))
    hops = hops_of(rec)
    # RSSI is a negative dBm figure at the receiver. A positive one is not a stronger signal,
    # it is a corrupt field, and airing it would read as a spectacular link.
    if rssi is not None and rssi > 0:
        rssi = None
    # SNR on LoRa lives roughly in -20..+15 dB. Outside that is an instrument fault, not a
    # remarkable link, and the honest move is to drop the field rather than transmit it.
    if snr is not None and not (-30.0 <= snr <= 30.0):
        snr = None
    meta["snr"], meta["rssi"], meta["hops"] = snr, rssi, hops

    parts = []
    if snr is not None:
        parts.append(f"SNR {snr:.1f}")
    if rssi is not None:
        parts.append(f"RSSI {int(round(rssi))}")
    if not parts:
        # Hops alone is not a signal report. It says the packet arrived, which the sender can
        # already infer from getting an answer at all.
        meta["refused"] = "no_measurements"
        return None, meta
    if hops is not None:
        parts.append("direct" if hops == 0 else f"{hops} hop" + ("s" if hops != 1 else ""))
    meta["parts"] = list(parts)
    text = "Copy: " + ", ".join(parts)
    if len(text) > max_chars:
        # Drop from the RIGHT, which sheds routing before it sheds signal: the two numbers are
        # the report, the hop count is context. Never mid-field — a truncated "RSSI -3" is a
        # different and better-looking measurement than "RSSI -32", and every wrong answer this
        # codebase has aired took exactly that shape.
        while len(parts) > 1 and len("Copy: " + ", ".join(parts)) > max_chars:
            parts.pop()
        text = "Copy: " + ", ".join(parts)
        meta["parts"] = list(parts)
        if len(text) > max_chars:
            meta["refused"] = "too_long"
            return None, meta
    return text, meta


def try_answer(text, rec, max_chars=64, trigger="cal"):
    """match + report in one call. Returns (reply|None, meta)."""
    m = match(text, trigger=trigger)
    if not m:
        return None, {"matched": False}
    reply, meta = report(rec, max_chars=max_chars)
    meta.update({"matched": True, "via": m["via"]})
    return reply, meta
