#!/usr/bin/env python3
"""Eval for kb.py -- radio and mesh definitions, verbatim from a vetted table.

Three things are graded: the TABLE (every entry sourced, dated, short, unambiguous), the MATCHER
(closed-world: a definition question about exactly one listed term, nothing else), and the
LADDER (below calc and sun/moon, above weather; reachable by strangers, no model anywhere).
The phrasings below were written by hand for this file, not generated from the table.

Run:  python3 evals/eval_kb.py            (exit 0 = pass)
      python3 evals/eval_kb.py --self-test (also runs the mutants)
"""
import io
import contextlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(HERE) == "evals":
    HERE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import kb
import responder as R

FAILS = []


def ck(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


# (message, the entry it must define). Hand-written; the table is not consulted to make these.
MUST = [("What's tropo?", "tropo"), ("cal what is POTA", "pota"), ("Cal, what does 73 mean?", "73"),
        ("what's a repeater", "repeater"), ("define SNR", "snr"), ("what is RACES", "races"),
        ("whats LoRa", "lora"), ("hey cal what's QTH?", "qth"), ("what is a Fresnel zone", "fresnel_zone"),
        ("what does QSL stand for", "qsl"), ("meaning of CQ", "cq"), ("what is a phonetic alphabet", "phonetic_alphabet"),
        ("what is ham radio", "ham_radio"), ("what is a grid square", "grid_square"),
        ("what is a tornado watch", "watch"), ("what's ducting", "tropo"), ("what is SKYWARN", "skywarn"),
        ("what's GMRS?", "gmrs"), ("what's MQTT", "mqtt"), ("what's a hop limit", "hop_limit"),
        # Review 2026-09-24 misses, now answered:
        ("what's tropo, cal?", "tropo"), ("@cal what's tropo", "tropo"), ("cal what's QSL mean", "qsl"),
        ("what's the meaning of 73", "73"), ("cal meaning of 73", "73"), ("what are repeaters", "repeater"),
        ("cal explain snr", "snr")]
# Must NOT match anything. Each is a real trap: a live question, a personal question, a bare
# common word, a number, a report, or chatter that merely contains a term.
MUST_NOT = ["tropo", "is tropo happening?", "is there tropo tonight", "what's your QTH", "what's my callsign",
            "what is cal's callsign", "the router is down", "what's net income", "what are the car races",
            "what races are on", "got you at 3 hops", "is there a tornado warning?", "what's the weather",
            "what's up", "what's 73", "what is a router", "what's a net", "whats 915", "what's a channel",
            "what is ham", "what's for dinner", "any POTA activators out?", "73 everyone", "what's your SNR",
            "what snr do you get from me", "tropo is great tonight", "what is tropo like in Olathe",
            # Review 2026-09-24 false fires: "the" asks for a CURRENT value, not a definition.
            "cal what's the snr", "what is the rssi", "what's the channel utilization", "what's the airtime",
            "what's the traceroute", "cal what's the modem preset", "what's the tornado warning",
            "what's the severe thunderstorm warning", "what's the callsign", "what's the qth",
            "what's the grid square", "what's the short name", "what is the current snr",
            "cal whats the weather warning", "what's the weather watch", "what is the weather radio",
            "cal what is AQ", "what is a tornado", "what are races"]


def suite():
    print("\n== the table ==")
    entries, index = kb.load()
    ck("the pack has entries", len(entries) >= 40, str(len(entries)))
    for e in entries:
        k = e.get("key")
        ok = (isinstance(e.get("answer"), str) and 0 < len(e["answer"]) <= kb.MAX_CHARS
              and str(e.get("source", "")).startswith("http") and e.get("quote")
              and re.match(r"^\d{4}-\d{2}-\d{2}$", str(e.get("verified_on", ""))) and e.get("aliases"))
        if not ok:
            ck(f"entry {k}: answer <= {kb.MAX_CHARS}, source, quote, date, aliases", False, json.dumps(e)[:160])
    ck("every entry: answer <= 120 chars, an http source, a quote, a verified date, aliases",
       all(0 < len(e["answer"]) <= kb.MAX_CHARS and e["source"].startswith("http") and e["quote"]
           and e["aliases"] for e in entries))
    ck("no two entries share an alias (load() refuses)", len(index) >= len(entries))
    bare = {"router", "net", "channel", "client", "ham", "grid", "races", "915", "watch", "warning"}
    ck("no bare common word is an alias", not (bare & {a.lower() for e in entries for a in e["aliases"]
                                                      if not a.isupper()}))
    ck("no alias is a bare number", not any(a.replace(" ", "").isdigit() and a != "73"
                                           for e in entries for a in e["aliases"]))
    allowed = ("meshtastic.org", "arrl.org", "ecfr.gov", "weather.gov", "github.com/meshcore-dev",
               "reticulum.network", "parksontheair.com", "fcc.gov")
    ck("every source is on an authoritative domain",
       all(any(d in e["source"] for d in allowed) for e in entries),
       str([e["source"] for e in entries if not any(d in e["source"] for d in allowed)]))
    ck("every answer survives the reply cleaner unchanged (same bytes on every path)",
       all(R.clean_reply(e["answer"], cap=180) == e["answer"] for e in entries),
       str([e["key"] for e in entries if R.clean_reply(e["answer"], cap=180) != e["answer"]]))
    ck("no security claims to strangers: no encryption or private-DM entry",
       not any(e["key"] in ("encryption", "pkc_dm") for e in entries))
    ck("no answer mentions this station's own details",
       not any(re.search(r"\b(Cal|Olathe|!27ca|Dean)\b", e["answer"]) for e in entries))

    print("\n== the matcher ==")
    for t, key in MUST:
        got = kb.match(t)
        ck(f"defines {key}: {t!r}", got is not None and got["key"] == key, repr(got and got["key"]))
    for t in MUST_NOT:
        got = kb.match(t)
        ck(f"silent: {t!r}", got is None, repr(got and got["key"]))
    # The personal-question wall is the one exact matching cannot supply on its own: it matters
    # the day an alias contains "your" or "my". A one-entry table makes that day happen now.
    import tempfile
    tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"entries": [{"key": "x", "aliases": ["your qth", "my callsign"], "answer": "private",
                            "source": "http://x", "quote": "q", "verified_on": "2026-09-24"}]}, tmp)
    tmp.close()
    ck("a question about the asker or Cal never matches, whatever the table holds",
       kb.match("what's your QTH", path=tmp.name) is None and kb.match("what is my callsign", path=tmp.name) is None)
    os.unlink(tmp.name)
    reply, meta = kb.answer("what's tropo?")
    entry = [e for e in kb.load()[0] if e["key"] == "tropo"][0]
    ck("the reply is the stored answer, verbatim", reply == entry["answer"])
    ck("an over-long answer is refused, not trimmed", kb.answer("what's tropo?", max_chars=10)[0] is None)
    ck("non-text is a miss, not a crash", kb.match(None) is None and kb.match(5) is None)

    print("\n== the ladder ==")
    c = dict(R.DEFAULTS)
    c.update({"KB_ENABLED": "true", "CALC_ENABLED": "true", "SUNMOON_ENABLED": "true",
              "CAPS_ENABLED": "true", "WEATHER_ENABLED": "true", "WEATHER_POINT": "39.0,-95.0"})
    dead = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline"))
    p = R.plan_response(c, "!a", "cal what's tropo?", get=dead)
    ck("kb answers a definition, fixed, no model", p["capability"] == "kb" and p["mode"] == "fixed", repr(p["capability"]))
    ck("off by default", R.DEFAULTS["KB_ENABLED"] == "false")
    p = R.plan_response(dict(c, KB_ENABLED="false"), "!a", "cal what's tropo?", get=dead)
    ck("KB_ENABLED=false: not answered from the pack", p["capability"] != "kb")
    ck("calc keeps numbers", R.plan_response(c, "!a", "cal 5 mi in km", get=dead)["capability"] == "calc")
    ck("sun/moon keeps sky times", R.plan_response(c, "!a", "cal when is sunset", get=dead)["capability"] == "sunmoon")
    ck("a definition outranks weather: 'what is a tornado watch'",
       R.plan_response(c, "!a", "cal what is a tornado watch", get=dead)["capability"] == "kb")
    ck("a live weather question is not a definition",
       R.plan_response(c, "!a", "cal is there a tornado watch", get=dead)["capability"] != "kb")
    ck("strangers can reach it (no model in its path)", "kb" in R.STRANGER_CAPS)
    R.channel_busy = lambda cfg, ts=None: (False, 5.0, "quiet")
    sc = dict(c, RESPONDER_ENABLED="true", STRANGER_DOERS_ENABLED="true", TRIGGER_WORD="cal")
    ok = R.plan_stranger(sc, {}, {"from": "!bbbbbbbb", "to": "^all", "channel": 0,
                                  "text": "cal what's POTA?", "reaction": False}, "!cccccccc")
    ck("a stranger asking a definition gets the vetted answer", ok[0] and ok[6] == "kb", repr(ok[:2]))


suite()
if FAILS:
    print(f"\neval_kb: {len(FAILS)} FAILED")
    sys.exit(1)

if "--self-test" in sys.argv:
    print("\n== mutations (each must be caught) ==")
    SRC = open(os.path.join(HERE, "kb.py")).read()
    MUTANTS = {
        "frames tried in the wrong order": ("_PERSONAL = re.compile", "_FRAMES = _FRAMES[::-1]\n_PERSONAL = re.compile"),
        "personal questions answered": ("if not raw or _PERSONAL.search(raw):", "if not raw:"),
        "bare numbers answered": ('if raw.replace(" ", "").isdigit() and n == 2:', "if False:"),
        "contains instead of equals": ("    hit = idx.get(_norm(raw))",
                                       "    hit = next((v for k, v in idx.items() if k.strip('=') in _norm(raw)), None)"),
        "no definition frame required": ("    else:\n        return None\n    raw", "    else:\n        m = re.match(r'(?P<t>.+)', s); n = 2\n    raw"),
        "the 'the' frame is back (answers live questions)": ("(?P<t>(?!the\\b).+?)$", "(?:the\\s+)?(?P<t>.+?)$"),
        "name at the end not stripped": ('    s = _TAIL.sub("", _LEAD.sub("", text.strip(), count=1))',
                                         '    s = _LEAD.sub("", text.strip(), count=1)'),
        "over-long answers trimmed": ('return None, {"matched": True, "key": e["key"], "refused": "too_long"}',
                                      'reply = reply[:max_chars]'),
    }
    fixups = {}
    real = (kb.match, kb.answer)
    escaped = []
    for name, (a, b) in MUTANTS.items():
        assert a in SRC, name
        src = SRC.replace(a, b, 1)
        if name in fixups:
            src = src.replace(*fixups[name], 1)
        ns = {"__file__": kb.__file__, "__name__": "kb_mut"}
        exec(compile(src, "kb_mut", "exec"), ns)
        kb.match, kb.answer = ns["match"], ns["answer"]
        FAILS.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                suite()
            except Exception:
                FAILS.clear()
        kb.match, kb.answer = real
        caught = bool(FAILS)
        print(f"  {'ok  ' if caught else 'FAIL'} mutant caught: {name}")
        if not caught:
            escaped.append(name)
    FAILS.clear()
    if escaped:
        print(f"\neval_kb: {len(escaped)} mutant(s) not caught")
        sys.exit(1)
print("\neval_kb: all checks pass")
