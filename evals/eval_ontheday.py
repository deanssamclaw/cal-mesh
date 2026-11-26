#!/usr/bin/env python3
"""eval_ontheday.py -- "on this day" is a public broadcast, so the table, the gates and the
delivery are tested, and the checks are proved able to fail.

Run:  python3 evals/eval_ontheday.py               (exit 0 = pass)
      python3 evals/eval_ontheday.py --self-test   also proves the checks can FAIL
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(HERE) == "evals":
    HERE = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


otd = _load("ontheday")
sys.modules["ontheday"] = otd
hol = _load("usholidays")
sys.modules["usholidays"] = hol
presence = _load("presence")

FAILS = []


def ck(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}  {detail}")


def E(date_, year, event, local=False, takeaway="Have a way to get warnings without power.", **kw):
    e = {"date": date_, "year": year, "place": "x", "event": event, "takeaway": takeaway,
         "event_source": "https://www.weather.gov/x", "event_quote": "q" * 30,
         "takeaway_source": "https://www.ready.gov/x", "takeaway_quote": "q" * 30, "local": local}
    e.update(kw)
    return e


TZ = ZoneInfo("America/Chicago")
DAY = date(2026, 5, 20)                       # a date with entries, not a holiday
CFG = {"RESPONDER_ENABLED": "true", "ON_THIS_DAY_ENABLED": "true", "PRESENCE_ENABLED": "true"}


def run():
    del FAILS[:]
    d = tempfile.mkdtemp(prefix="evalotd-")
    try:
        p = os.path.join(d, "t.json")
        json.dump([E("05-20", 1999, "A national event happened.", local=False),
                   E("05-20", 1957, "A Kansas City event happened.", local=True),
                   E("05-20", 1960, "Second national event.", local=False),
                   E("05-21", 2000, "No source.", event_source="http://x"),
                   E("05-22", 2000, "No lesson source.", takeaway_source=""),
                   E("13-40", 2000, "Bad date."), E("05-23", "1999", "Year as text."),
                   E("05-24", 2001, "x" * 150, takeaway="y" * 150),
                   {"not": "an entry"}, 7], open(p, "w"))
        es = otd.load(p)
        ck("load: only valid, sourced, packet-sized entries", set(es) == {"05-20"}, sorted(es))
        ck("load: Kansas City first", es["05-20"][0]["local"] is True, es["05-20"][0])
        ck("load: then oldest first", [e["year"] for e in es["05-20"]] == [1957, 1960, 1999],
           [e["year"] for e in es["05-20"]])
        ck("load: a missing file is an empty table", otd.load(os.path.join(d, "none.json")) == {})
        open(os.path.join(d, "bad.json"), "w").write("{not json")
        ck("load: a broken file is an empty table", otd.load(os.path.join(d, "bad.json")) == {})
    finally:
        shutil.rmtree(d, ignore_errors=True)

    t = otd.text(E("05-20", 1957, "A tornado struck.", takeaway="Know your shelter."))
    ck("text", t == "On this day in 1957: A tornado struck. Know your shelter.", t)
    ck("text: oversized is refused, never truncated", otd.text(E("05-20", 1957, "x" * 190)) is None)

    todays = es["05-20"]
    ids = [otd.entry_id(e) for e in todays]
    ck("choose: Kansas City first", otd.choose(todays, []) is todays[0])
    ck("choose: then the next unused", otd.choose(todays, ids[:1]) is todays[1])
    ck("choose: a full round starts over", otd.choose(todays, ids) is todays[0])
    ck("choose: nothing to choose", otd.choose([], []) is None)

    P = lambda cfg=CFG, day=DAY, hour=12, posted=frozenset(), heard=None, util=5.0, last=None, hol_=False: \
        otd.plan_post(cfg, day, hour, set(posted), heard, util, 1e9, 900, 25, last_send_ts=last,
                      holiday_today=hol_, entries=es)
    ck("plan: posts at noon on a date with entries", P()[1] == "post", P()[1])
    ck("plan: master switch", P(cfg={**CFG, "RESPONDER_ENABLED": "false"})[0] is None)
    ck("plan: its own flag", P(cfg={**CFG, "ON_THIS_DAY_ENABLED": "false"})[0] is None)
    ck("plan: presence switch", P(cfg={**CFG, "PRESENCE_ENABLED": "false"})[0] is None)
    ck("plan: no entry, no post", P(day=date(2026, 5, 21))[1] == "no_entry_today")
    ck("plan: once a day", P(posted={DAY.isoformat()})[1] == "already_posted")
    ck("plan: unreadable history refuses", otd.plan_post(CFG, DAY, 12, None, None, 5.0, 1e9, 900, 25,
                                                         entries=es)[0] is None)
    ck("plan: stands down on a holiday", P(hol_=True)[1] == "holiday_today")
    ck("plan: not before noon", P(hour=11)[0] is None)
    ck("plan: 13:00 is still midday", P(hour=13)[1] == "post")
    ck("plan: not from 14:00", P(hour=14)[0] is None)
    ck("plan: two hours after Cal's last post", P(last=1e9 - 3600)[1] == "too_soon_after_last_post")
    ck("plan: not into a conversation", P(heard=1e9 - 60)[0] is None)
    ck("plan: unknown channel waits", P(heard="unknown")[0] is None)
    ck("plan: busy channel waits", P(util=40.0)[0] is None)
    ck("plan: unknown utilization waits", P(util=None)[0] is None)
    ck("pending: true on an entry date", otd.pending(CFG, DAY, set(), entries=es) is True)
    ck("pending: false once posted", otd.pending(CFG, DAY, {DAY.isoformat()}, entries=es) is False)
    ck("pending: false on a holiday", otd.pending(CFG, DAY, set(), holiday_today=True, entries=es) is False)
    ck("pending: false without an entry", otd.pending(CFG, date(2026, 5, 21), set(), entries=es) is False)

    # --- end to end through presence, in a temp station ------------------------------------
    base = tempfile.mkdtemp(prefix="evalotdpres-")
    try:
        os.makedirs(os.path.join(base, "data"))
        json.dump([E("05-20", 1957, "A Kansas City event happened.", local=True),
                   E("05-20", 1999, "A national event happened.")],
                  open(os.path.join(base, "data", "on_this_day.json"), "w"))
        json.dump({}, open(os.path.join(base, "data", "holiday_facts.json"), "w"))
        open(os.path.join(base, "config"), "w").write(
            "RESPONDER_ENABLED=true\nON_THIS_DAY_ENABLED=true\nPRESENCE_ENABLED=true\nPRESENCE_CHANCE=1\n"
            "PRESENCE_MIN_GAP_H=0\n")
        json.dump({"sends": [], "custom_key": "kept"}, open(os.path.join(base, "presence-state.json"), "w"))
        open(os.path.join(base, "inbox.jsonl"), "w").write("")

        def at(h, m=0, send=True):
            now = datetime(2026, 5, 20, h, m, tzinfo=TZ).timestamp()
            json.dump({"ts": datetime.fromtimestamp(now - 30).isoformat(), "metrics": {"chUtil": 5.0}},
                      open(os.path.join(base, "status.json"), "w"))
            with contextlib.redirect_stdout(io.StringIO()):
                presence.main(["--send"] if send else [], base=base, now=now)

        def outbox():
            ob = os.path.join(base, "outbox")
            return sorted(json.load(open(os.path.join(ob, f)))["text"] for f in os.listdir(ob)
                          if not f.startswith(".")) if os.path.isdir(ob) else []

        def state():
            try:
                return json.load(open(os.path.join(base, "presence-state.json")))
            except (OSError, ValueError):
                return {}

        at(8)
        ck("e2e: the morning greeting holds off on an entry date", outbox() == [], outbox())
        at(12, 5, send=False)
        ck("e2e: a dry run writes nothing", outbox() == [] and state().get("onthisday") is None)
        at(12, 5)
        ck("e2e: posts the Kansas City entry at midday",
           outbox() == ["On this day in 1957: A Kansas City event happened. Have a way to get "
                        "warnings without power."], outbox())
        st = state()
        ck("e2e: recorded as posted", st.get("onthisday") == ["2026-05-20"], st)
        ck("e2e: the entry is marked used", len(st.get("onthisday_used", {}).get("05-20", [])) == 1, st)
        ck("e2e: other keys survive", st.get("custom_key") == "kept", st)
        at(13, 30)
        at(15)
        ck("e2e: never twice, and no ordinary greeting after it the same day", len(outbox()) == 1, outbox())
        ck("e2e: the record survives the ordinary writer", state().get("onthisday") == ["2026-05-20"])
    finally:
        shutil.rmtree(base, ignore_errors=True)

    # --- the real table ----------------------------------------------------------------------
    raw = json.load(open(os.path.join(HERE, "data", "on_this_day.json"), encoding="utf-8"))
    ck("table: every row loads", sum(len(v) for v in otd.load(os.path.join(HERE, "data", "on_this_day.json")).values())
       == len(raw), len(raw))
    for i, e in enumerate(raw):
        tag = f"{e.get('date')}/{e.get('year')}"
        ck(f"{tag}: both sources https", str(e.get("event_source", "")).startswith("https://")
           and str(e.get("takeaway_source", "")).startswith("https://"))
        ck(f"{tag}: both excerpts carried", len(str(e.get("event_quote", ""))) >= 20
           and len(str(e.get("takeaway_quote", ""))) >= 20)
        ck(f"{tag}: event and lesson each <= 90", 0 < len(e.get("event", "")) <= 90
           and 0 < len(e.get("takeaway", "")) <= 90)
        ck(f"{tag}: fits one packet", otd.text(e) is not None)
        ck(f"{tag}: plain text, no URL", "http" not in e.get("event", "") + e.get("takeaway", ""))
        ck(f"{tag}: a real past year", 1700 <= e.get("year", 0) <= 2026)
    return FAILS[:]


MUTATIONS = {
    "local not preferred": lambda: setattr(otd, "load", (lambda f: lambda path=None: {
        k: sorted(v, key=lambda e: e["year"], reverse=True) for k, v in f(path).items()})(otd.load)),
    "unsourced lessons allowed": lambda: setattr(otd, "_ok", (lambda f: lambda e: f(dict(e, takeaway_source="https://x"))
                                                               if isinstance(e, dict) else False)(otd._ok)),
    "no midday window": lambda: setattr(otd, "WINDOW", (0, 24)),
    "posts on holidays": lambda: setattr(otd, "plan_post", (lambda f: lambda *a, **k: f(*a, **dict(k, holiday_today=False)))(otd.plan_post)),
    "no spacing after a post": lambda: setattr(otd, "MIN_GAP_S", 0),
    "greeting not held": lambda: setattr(otd, "pending", lambda *a, **k: False),
    "rotation forgets": lambda: setattr(otd, "choose", lambda entries, used: entries[0] if entries else None),
}

if __name__ == "__main__":
    fails = run()
    for f in fails:
        print("  FAIL " + f)
    if "--self-test" in sys.argv:
        print("self-test -- each mutation must be CAUGHT:")
        keep = {n: getattr(otd, n) for n in ("load", "_ok", "WINDOW", "plan_post", "MIN_GAP_S",
                                             "pending", "choose")}
        for name, mutate in MUTATIONS.items():
            mutate()
            caught = bool(run())
            print(f"  [{'CAUGHT' if caught else 'SURVIVED'}] {name}")
            if not caught:
                fails.append("self-test: " + name)
            for n, v in keep.items():
                setattr(otd, n, v)
    print(f"eval_ontheday: {len(fails)} failed")
    sys.exit(1 if fails else 0)
