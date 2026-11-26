#!/usr/bin/env python3
"""policy.py -- grade every inbound message against the operator's rulings, with no human.

WHY. Grading drafts by hand stalled: 44 verdicts on 2026-09-12 and none since, and a model
grader tested blind against those 44 agreed on 57% at best and missed all 7 "harmful". The
verdicts themselves split in identical situations (a tapback: 5 "send a report", 3 "stay
silent"). Those were policy questions, not taste, so Dean ruled on them once (2026-11-26):

  P1  a tapback gets silence                        -- except a wave on Cal's OWN message,
                                                       which Cal mirrors (kept, 2026-10-19)
  P2  a bare check-in on the public channel gets a signal report, Cal named or not
  P3  a report inside another station's test exchange gets a signal report anyway
  P4  model-written words on the public channel only when Cal is named (doers are not words)

This file says, for each message, what Cal SHOULD have done under those rulings and compares it
with what Cal DID. A mismatch is a concrete defect with a line of code behind it.

INDEPENDENT OF THE CODE IT GRADES. The shapes below are this file's own reading of the
rulings, written separately from sigreport.test_exchange and deliberately a little broader. A
grader that called the matcher it grades would agree with it by construction and could never
fail. Where the two readings differ, the mismatch is listed for a human to look at once.

Read-only. Public-channel text only in the examples: that traffic is already published on the
Exchanges tab. DMs and Cal's own channel are counted, never quoted.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import learn                     # noqa: E402  -- iter_decisions, _iter_jsonl, our_id, _s

RULES = {
    "P1": "a tapback gets silence (a wave on Cal's own message is mirrored)",
    "P2": "a check-in on the public channel gets a signal report",
    "P3": "a report in another station's test exchange gets a signal report",
    "P4": "no model-written words on the public channel unless Cal is named",
}
RULED = "2026-11-26"
SENT = os.path.join(learn.BASE, "sent.jsonl")      # module globals, so a test can redirect them
WINDOW_DAYS = 14

_NAMED = re.compile(r"\bcal\b(?!['’]s)", re.I)
_KEYCAP = re.compile(r"^\s*(?:[0-9#*]️?⃣\s*)+$")
_HOPS = re.compile(r"\b\d+\s*(?:of\s+\d+\s+)?hops?\b|\bdirect\b", re.I)
_CHECKIN = re.compile(r"^\W*(?:\w{4}\W+)?(?:test\w*|radio\s+check|range\s+test|check(?:ing)?\s+in|"
                      r"heard|received|rcvd|rec|copy|copied|ack)\b"
                      r"|\b(?:heard|received|rcvd|got|copy)\s+(?:you|ya|u|in|near|from|at)\b"
                      r"|\btest\s+(?:received|heard)\b", re.I)
_WAVES = ("\U0001F44B",)


def _sent_ids():
    return {s.get("packet_id") for s in learn._iter_jsonl(SENT)
            if learn._key(s.get("packet_id")) is not None}


def checkin(text):
    """This file's own reading of 'a check-in or a report': short, and not a real question."""
    t = learn._s(text).strip()
    # A long question is a conversation; a short one ("Radio check?", "You copy??") is a check-in.
    if not t or len(t.split()) > 12 or ("?" in t and len(t.split()) > 4):
        return False
    return bool(_KEYCAP.match(t) or _HOPS.search(t) or _CHECKIN.search(t))


def expected(d, p, ours, sent, own_ch):
    """(action, rule) Cal should have taken. action: silent | sigreport | any."""
    text = learn._s(d.get("text"))
    public = d.get("to") in ("^all", None) and not (own_ch >= 0 and d.get("channel") == own_ch)
    if p.get("reaction") not in (None, False):
        if p.get("reply_to") in sent and text.strip() in _WAVES:
            return "wave", "P1"
        return "silent", "P1"
    if not public:
        return "any", ""                      # DMs and Cal's channel: the ladder decides
    if _NAMED.search(text):
        return "any", ""
    rt = p.get("reply_to")
    if rt and rt not in sent:
        return ("sigreport", "P3") if checkin(text) else ("silent", "P4")
    if checkin(text):
        return "sigreport", "P2"
    return "silent", "P4"


def actual(d):
    """What Cal did: silent | sigreport | doer | words."""
    if not d.get("reply"):
        return "silent"
    cap = d.get("capability")
    if cap == "sigreport":
        return "sigreport"
    if str(d.get("gen_status") or "").startswith("fixed_") or cap == "greeting":
        return "doer"
    return "words"


def conforms(exp, act, rule=""):
    if exp == "any":
        return True
    if exp == "wave":
        return act in ("doer", "silent")      # mirroring is allowed, not required
    if exp == "silent" and rule == "P4":
        # P4 governs MODEL words. A doer reply -- the greeting table, a signal report -- is not
        # prose, so it never breaks P4 (over-firing is sigreport's own eval's business).
        return act in ("silent", "doer", "sigreport")
    return act == exp                         # P1 silence is strict: no reply of any kind


def grade(now=None, days=WINDOW_DAYS, top=8):
    now = now or datetime.now(timezone.utc)
    cut = (now - timedelta(days=days)).isoformat()
    ours, own_ch = learn.our_id(), learn.cal_channel()
    sent = _sent_ids()
    pk = {}
    for p in learn._iter_jsonl(learn.INBOX):
        if learn._key(p.get("id")) is not None and learn._s(p.get("ts")) >= cut:
            pk[p["id"]] = p
    n = ok = 0
    by = {r: {"n": 0, "bad": 0} for r in RULES}
    examples = {r: [] for r in RULES}
    for d in learn.iter_decisions():
        if learn._s(d.get("ts")) < cut or d.get("from") == ours:
            continue
        p = pk.get(learn._key(d.get("id"))) or {}
        exp, rule = expected(d, p, ours, sent, own_ch)
        act = actual(d)
        n += 1
        good = conforms(exp, act, rule)
        ok += good
        if rule:
            by[rule]["n"] += 1
            if not good:
                by[rule]["bad"] += 1
                public = d.get("to") in ("^all", None) and not (own_ch >= 0 and d.get("channel") == own_ch)
                if public and len(examples[rule]) < top:
                    examples[rule].append({"ts": learn._s(d.get("ts"))[:16],
                                           "text": learn._s(d.get("text"))[:80],
                                           "expected": exp, "did": act})
    return {"days": days, "ruled": RULED, "rules": RULES, "n": n, "ok": ok, "bad": n - ok,
            "by_rule": by, "examples": examples}


if __name__ == "__main__":
    g = grade(days=int(sys.argv[1]) if len(sys.argv) > 1 else WINDOW_DAYS)
    print(f"last {g['days']} d: {g['n']} inbound, {g['ok']} follow the rulings, {g['bad']} do not")
    for r, v in g["by_rule"].items():
        print(f"  {r} {v['n']:4} seen, {v['bad']:3} broken -- {RULES[r]}")
        for e in g["examples"][r]:
            print(f"       {e['ts']}  expected {e['expected']}, did {e['did']}: {e['text']!r}")
