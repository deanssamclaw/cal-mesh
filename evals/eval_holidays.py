#!/usr/bin/env python3
"""eval_holidays.py -- holidays are a public broadcast, so the dates, the facts and the gates
are tested against things outside this repo.

THE ORACLES. Federal dates are pinned to OPM's published holiday schedules for 2026-2028
(opm.gov/policy-data-oversight/pay-leave/federal-holidays, fetched 2026-11-26), including its
observed-day shifts: an entry OPM marks as moved for a weekend must sit on the weekday next to
the date this code computes, and every other entry must equal it exactly. Easter is pinned to an
outside published table (EASTER below; source recorded beside it), never to the computus.

Run:  python3 evals/eval_holidays.py               (exit 0 = pass)
      python3 evals/eval_holidays.py --self-test   also proves the checks can FAIL
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(HERE) == "evals":
    HERE = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hol = _load("holidays")
sys.modules["holidays"] = hol
responder = _load("responder")
presence = _load("presence")
learn = _load("learn")

FAILS = []


def ck(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}  {detail}")


# OPM, as published: (date OPM lists, holiday key, shifted-for-a-weekend?)
OPM = {
    2026: [("2026-01-01", "new_years_day", 0), ("2026-01-19", "mlk_day", 0),
           ("2026-02-16", "washingtons_birthday", 0), ("2026-05-25", "memorial_day", 0),
           ("2026-06-19", "juneteenth", 0), ("2026-07-03", "independence_day", 1),
           ("2026-09-07", "labor_day", 0), ("2026-10-12", "columbus_day", 0),
           ("2026-11-11", "veterans_day", 0), ("2026-11-26", "thanksgiving", 0),
           ("2026-12-25", "christmas", 0)],
    2027: [("2027-01-01", "new_years_day", 0), ("2027-01-18", "mlk_day", 0),
           ("2027-02-15", "washingtons_birthday", 0), ("2027-05-31", "memorial_day", 0),
           ("2027-06-18", "juneteenth", 1), ("2027-07-05", "independence_day", 1),
           ("2027-09-06", "labor_day", 0), ("2027-10-11", "columbus_day", 0),
           ("2027-11-11", "veterans_day", 0), ("2027-11-25", "thanksgiving", 0),
           ("2027-12-24", "christmas", 1)],
    2028: [("2027-12-31", "new_years_day", 1), ("2028-01-17", "mlk_day", 0),
           ("2028-02-21", "washingtons_birthday", 0), ("2028-05-29", "memorial_day", 0),
           ("2028-06-19", "juneteenth", 0), ("2028-07-04", "independence_day", 0),
           ("2028-09-04", "labor_day", 0), ("2028-10-09", "columbus_day", 0),
           ("2028-11-10", "veterans_day", 1), ("2028-11-23", "thanksgiving", 0),
           ("2028-12-25", "christmas", 0)],
}
EASTER = {}          # filled from evals/easter_oracle.json (see there for the source)
_EO = os.path.join(HERE, "evals", "easter_oracle.json")
if os.path.exists(_EO):
    EASTER = json.load(open(_EO))["dates"]

D = lambda s: date.fromisoformat(s)


def _jload(path):
    """A JSON file, or {} if a mutation stopped it being written -- the checks then fail cleanly."""
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def run():
    del FAILS[:]
    # --- calendar vs OPM -------------------------------------------------------------------
    for year, rows in OPM.items():
        ds = hol.dates(year)
        for opm, key, shifted in rows:
            got = ds[key]
            if shifted:
                ok = abs((D(opm) - got).days) == 1 and got.weekday() in (5, 6) and D(opm).weekday() in (0, 4)
            else:
                ok = D(opm) == got
            ck(f"OPM {year} {key}", ok, f"OPM {opm} (shifted={shifted}) vs computed {got}")
    ck("Easter oracle present", len(EASTER) >= 10, len(EASTER))
    for y, d in EASTER.items():
        ck(f"Easter {y}", hol.easter(int(y)).isoformat() == d, f"{hol.easter(int(y))} vs {d}")
    ck("Mother's Day 2026 is May 10", hol.dates(2026)["mothers_day"] == date(2026, 5, 10))
    ck("Father's Day 2026 is June 21", hol.dates(2026)["fathers_day"] == date(2026, 6, 21))
    ck("every holiday has a date every year", all(len(hol.dates(y)) == len(hol.HOLIDAYS)
                                                  for y in range(2024, 2050)))
    ck("on() finds today's", hol.on(date(2026, 11, 26)) == ["thanksgiving"])
    ck("on() finds nothing on an ordinary day", hol.on(date(2026, 11, 27)) == [])

    # --- wishes ----------------------------------------------------------------------------
    T = date(2026, 11, 26)
    for t in ("Happy Thanksgiving everyone!", "happy thanksgiving from Lenexa \U0001F983",
              "Happy Turkey Day mesh", "HAPPY THANKSGIVING", "Happy Thanksgiving, all"):
        ck(f"wish: {t!r}", hol.wished(t, T) == "thanksgiving", hol.wished(t, T))
    for t, why in (("is it thanksgiving?", "a question"),
                   ("do people say happy thanksgiving on here?", "a question holding a wish"),("Thanksgiving dinner was great", "no wish"),
                   ("Merry Christmas", "not today or tomorrow"),
                   ("Happy Thanksgiving " + "word " * 15, "too long"),
                   ("unhappy thanksgiving", "not the phrase"), ("", "empty")):
        ck(f"not a wish ({why}): {t[:30]!r}", hol.wished(t, T) is None, hol.wished(t, T))
    ck("the eve counts: Christmas wished on the 24th", hol.wished("Merry Christmas!", date(2026, 12, 24)) == "christmas")
    ck("New Year wished on New Year's Eve", hol.wished("Happy New Year!", date(2026, 12, 31)) == "new_years_day")
    ck("the day after does not", hol.wished("Happy Thanksgiving", date(2026, 11, 27)) is None)

    # --- the deck --------------------------------------------------------------------------
    d = tempfile.mkdtemp(prefix="evalhol-")
    try:
        sp = os.path.join(d, "s.json")
        facts = {"thanksgiving": ["f1", "f2", "f3", "f4"]}
        import random as _r
        seq = [hol.draw("thanksgiving", facts=facts, state_path=sp, rng=_r.Random(i)) for i in range(12)]
        ck("deck: every fact once per round", all(sorted(seq[i:i + 4]) == ["f1", "f2", "f3", "f4"]
                                                  for i in (0, 4, 8)), seq)
        ck("deck: never the same fact twice in a row", all(a != b for a, b in zip(seq, seq[1:])), seq)
        ck("deck: no facts, no draw", hol.draw("easter", facts=facts, state_path=sp) is None)
        ck("deck: an unwritable state refuses", hol.draw("thanksgiving", facts=facts,
                                                         state_path=os.path.join(d, "nope", "s.json")) is None)
        bad = os.path.join(d, "f.json")
        json.dump({"thanksgiving": [{"fact": "sourced", "source": "https://x.gov/a", "quote": "q"},
                                    {"fact": "unsourced", "source": ""},
                                    {"fact": "http only", "source": "http://x"}],
                   "not_a_holiday": [{"fact": "x", "source": "https://x"}]}, open(bad, "w"))
        lf = hol.load_facts(bad)
        ck("facts: only https-sourced entries load", lf == {"thanksgiving": ["sourced"]}, lf)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    ck("reply text", hol.reply_text("thanksgiving", "x") == "Happy Thanksgiving! x")
    ck("announce text", hol.announce_text("thanksgiving", "x") == "Happy Thanksgiving, mesh! x")
    ck("an oversized line is refused, never truncated", hol.reply_text("thanksgiving", "x" * 300) is None)

    # --- the real facts file ---------------------------------------------------------------
    raw = json.load(open(os.path.join(HERE, "data", "holiday_facts.json"), encoding="utf-8"))
    ck("facts file: only known holidays", set(raw) <= set(hol.HOLIDAYS), set(raw) - set(hol.HOLIDAYS))
    ck("facts file: every holiday has at least 3", all(len(raw.get(k, [])) >= 3 for k in hol.HOLIDAYS),
       {k: len(raw.get(k, [])) for k in hol.HOLIDAYS if len(raw.get(k, [])) < 3})
    for k, items in raw.items():
        for i, it in enumerate(items):
            f = it.get("fact", "")
            ck(f"{k}#{i} sourced over https", str(it.get("source", "")).startswith("https://"), it.get("source"))
            ck(f"{k}#{i} carries its excerpt", len(str(it.get("quote", "")).strip()) >= 20)
            ck(f"{k}#{i} at most 110 chars", 0 < len(f) <= 110, len(f))
            ck(f"{k}#{i} fits a packet as a reply and an announcement",
               hol.reply_text(k, f) is not None and hol.announce_text(k, f) is not None)
            ck(f"{k}#{i} plain text", all(ord(c) < 0x2000 or c in "–—’" for c in f), f)
            ck(f"{k}#{i} no URL", "http" not in f and "www." not in f)

    # --- the announcement plan -------------------------------------------------------------
    on_cfg = {"RESPONDER_ENABLED": "true", "HOLIDAY_ANNOUNCE_ENABLED": "true"}
    P = lambda cfg=on_cfg, day=T, hour=10, ann=frozenset(), heard=None, util=5.0: hol.plan_announce(
        cfg, day, hour, set(ann), heard, util, 1e9, 900, 25)
    ck("announce: due on the day at 10", P() == ("thanksgiving", "announce"), P())
    ck("announce: master switch", P(cfg={"HOLIDAY_ANNOUNCE_ENABLED": "true"})[0] is None)
    ck("announce: its own flag", P(cfg={"RESPONDER_ENABLED": "true"})[0] is None)
    ck("announce: not before 9", P(hour=8)[0] is None)
    ck("announce: at 9", P(hour=9)[0] == "thanksgiving")
    ck("announce: not from 21", P(hour=21)[0] is None)
    ck("announce: once", P(ann={"2026-11-26:thanksgiving"})[0] is None)
    ck("announce: not on an ordinary day", P(day=date(2026, 11, 27))[0] is None)
    ck("announce: unreadable history refuses", hol.plan_announce(on_cfg, T, 10, None, None, 5.0, 1e9, 900, 25)[0] is None)
    ck("announce: not into a conversation", P(heard=1e9 - 60)[0] is None)
    ck("announce: unknown channel waits", P(heard="unknown")[0] is None)
    ck("announce: busy channel waits", P(util=40.0)[0] is None)
    ck("announce: unknown utilization waits", P(util=None)[0] is None)

    # --- the reply plan --------------------------------------------------------------------
    st_path = os.path.join(tempfile.mkdtemp(prefix="evalholst-"), "status.json")
    json.dump({"metrics": {"chUtil": 5.0}}, open(st_path, "w"))
    responder.STATUS = st_path
    ts = datetime(2026, 11, 26, 16, 0, tzinfo=responder.ZoneInfo("America/Chicago")).timestamp()
    os.utime(st_path, (ts, ts))
    CFG = {"RESPONDER_ENABLED": "true", "HOLIDAY_REPLY_ENABLED": "true"}
    OURS, OWN = "!cccccccc", frozenset({777})
    fake = lambda k: "A sourced fact."

    def rec(text="Happy Thanksgiving everyone!", **kw):
        r = {"text": text, "from": "!aaaaaaaa", "to": "^all", "channel": 0, "reaction": False}
        r.update(kw)
        return r

    def plan(r=None, cfg=None, st=None, draw=fake):
        return responder.plan_holiday(dict(CFG, **(cfg or {})), st if st is not None else {},
                                      r or rec(), OURS, ts=ts, own_ids=OWN, draw=draw)

    ck("reply: defaults OFF", responder.DEFAULTS["HOLIDAY_REPLY_ENABLED"] == "false")
    ok = plan()
    ck("reply: a wish gets the wish and a fact", ok[0] is True and ok[4] == "Happy Thanksgiving! A sourced fact.", ok[:5])
    ck("reply: to the channel", ok[2] == "^all")
    ck("reply: meta names the holiday and the day", ok[6] == {"holiday": "thanksgiving", "day": "2026-11-26"}, ok[6])
    ck("reply: flag off", plan(cfg={"HOLIDAY_REPLY_ENABLED": "false"})[0] is False)
    ck("reply: kill switch", plan(cfg={"RESPONDER_ENABLED": "false"})[1] == "disabled")
    ck("reply: a tapback is not a wish", plan(rec(reaction=True))[0] is False)
    ck("reply: not by DM", plan(rec(to=OURS))[0] is False)
    ck("reply: not inside another station's exchange",
       plan(rec(reply_to=555))[1] == "holiday_reply_to_other_station")
    ck("reply: a reply to Cal's own announcement is Cal's", plan(rec(reply_to=777))[0] is True)
    ck("reply: not from Cal itself", plan(rec(**{"from": OURS}))[0] is False)
    ck("reply: not a wish", plan(rec("Thanksgiving dinner was great"))[1] == "not_a_holiday_wish")
    ck("reply: once per sender per day",
       plan(st={"holiday_per_sender": {"!aaaaaaaa": "2026-11-26"}})[1] == "holiday_sender_today")
    ck("reply: yesterday's reply does not block today",
       plan(st={"holiday_per_sender": {"!aaaaaaaa": "2026-11-25"}})[0] is True)
    ck("reply: daily budget", plan(st={"holiday_day": {"2026-11-26": 8}})[1] == "holiday_budget_spent")
    ck("reply: no fact, no reply", plan(draw=lambda k: None)[1] == "holiday_no_fact")
    os.utime(st_path, (ts - 10 ** 6, ts - 10 ** 6))
    ck("reply: stale radio status waits", plan()[0] is False)
    os.utime(st_path, (ts, ts))
    st = {}
    responder.commit_holiday(st, "!aaaaaaaa", "2026-11-26")
    ck("commit: spends the sender and the day", st == {"holiday_per_sender": {"!aaaaaaaa": "2026-11-26"},
                                                       "holiday_day": {"2026-11-26": 1}}, st)
    responder.commit_holiday(st, "!bbbbbbbb", "2026-11-27")
    ck("commit: old days are dropped", st["holiday_day"] == {"2026-11-27": 1}
       and st["holiday_per_sender"] == {"!bbbbbbbb": "2026-11-27"}, st)

    # --- learn sees it as an answered doer, not an ask, not a tripwire ----------------------
    r = {"ts": "2026-11-26T16:00:00+00:00", "matched": True, "reply": "x", "capability": "holiday",
         "gen_status": "fixed_holiday", "prompt_kind": "fixed", "text": "Happy Thanksgiving"}
    ck("learn: a holiday reply is a HIT", learn.classify(r, OURS) == "HIT", learn.classify(r, OURS))
    ck("learn: and not an ask", learn.is_ask(r, OURS) is False)
    kbr = dict(r, capability="kb", gen_status="fixed_kb")
    ck("learn: a kb answer is a HIT", learn.classify(kbr, OURS) == "HIT", learn.classify(kbr, OURS))

    # --- presence: the announcement end to end, in a temp base ------------------------------
    base = tempfile.mkdtemp(prefix="evalholpres-")
    try:
        os.makedirs(os.path.join(base, "data"))
        json.dump({"thanksgiving": [{"fact": "Fact one.", "source": "https://x.gov/1", "quote": "q" * 20},
                                    {"fact": "Fact two.", "source": "https://x.gov/2", "quote": "q" * 20}]},
                  open(os.path.join(base, "data", "holiday_facts.json"), "w"))
        open(os.path.join(base, "config"), "w").write(
            "RESPONDER_ENABLED=true\nHOLIDAY_ANNOUNCE_ENABLED=true\nPRESENCE_ENABLED=true\nPRESENCE_CHANCE=1\n")
        now = datetime(2026, 11, 26, 10, 0, tzinfo=responder.ZoneInfo("America/Chicago")).timestamp()
        json.dump({"ts": datetime.fromtimestamp(now - 30).isoformat(), "metrics": {"chUtil": 5.0}},
                  open(os.path.join(base, "status.json"), "w"))
        open(os.path.join(base, "inbox.jsonl"), "w").write("")
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            presence.main([], base=base, now=now)
        ck("presence: a dry run writes nothing", not os.path.exists(os.path.join(base, "outbox"))
           and not os.path.exists(os.path.join(base, "presence-state.json")))
        with contextlib.redirect_stdout(io.StringIO()):
            presence.main(["--send"], base=base, now=now)
        ob = os.path.join(base, "outbox")
        out = [f for f in os.listdir(ob) if not f.startswith(".")] if os.path.isdir(ob) else []
        ck("presence: one announcement", len(out) == 1, out)
        msg = json.load(open(os.path.join(base, "outbox", out[0]))) if out else {}
        ck("presence: holiday source, not presence", msg.get("source") == "holiday", msg)
        ck("presence: the text", msg.get("text") in ("Happy Thanksgiving, mesh! Fact one.",
                                                    "Happy Thanksgiving, mesh! Fact two."), msg)
        ps = _jload(os.path.join(base, "presence-state.json"))
        ck("presence: recorded as announced", ps.get("holidays") == ["2026-11-26:thanksgiving"], ps)
        with contextlib.redirect_stdout(io.StringIO()):
            presence.main(["--send"], base=base, now=now + 3 * 3600)
        out2 = [f for f in os.listdir(ob) if not f.startswith(".")] if os.path.isdir(ob) else []
        ck("presence: never twice, and no Morning Mesh after it", len(out2) == 1, out2)
        ps2 = _jload(os.path.join(base, "presence-state.json"))
        ck("presence: the record survives the next run", ps2.get("holidays") == ["2026-11-26:thanksgiving"], ps2)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return FAILS[:]


MUTATIONS = {
    "Thanksgiving on the 3rd Thursday": lambda: hol.NTH.__setitem__("thanksgiving", (11, 3, 3)),
    "Memorial Day on the first Monday": lambda: hol.NTH.__setitem__("memorial_day", (5, 0, 1)),
    "Easter off by a week": lambda: setattr(hol, "easter", (lambda f: lambda y: f(y) + timedelta(days=7))(hol.easter)),
    "questions accepted as wishes": lambda: setattr(hol, "wished",
        (lambda f: lambda t, d: f((t or "").replace("?", ""), d))(hol.wished)),
    "deck forgets": lambda: setattr(hol, "draw",
        (lambda f: lambda k, facts=None, state_path=None, rng=None: f(k, facts, state_path + ".x" + str(time.time_ns()) if state_path else None, rng))(hol.draw)),
    "announce any hour": lambda: setattr(hol, "ANNOUNCE_FROM_H", 0),
    "reply ignores the flag": lambda: responder.DEFAULTS.__setitem__("HOLIDAY_REPLY_ENABLED", "true"),
    "unsourced facts allowed": lambda: setattr(hol, "load_facts",
        (lambda f: lambda path=None: {k: v + ["unsourced"] for k, v in f(path).items()})(hol.load_facts)),
}

if __name__ == "__main__":
    fails = run()
    for f in fails:
        print("  FAIL " + f)
    if "--self-test" in sys.argv:
        import copy
        print("self-test -- each mutation must be CAUGHT:")
        keep = {n: getattr(hol, n) for n in ("easter", "wished", "draw", "ANNOUNCE_FROM_H", "load_facts")}
        keep_nth = copy.deepcopy(hol.NTH)
        keep_def = dict(responder.DEFAULTS)
        for name, mutate in MUTATIONS.items():
            mutate()
            caught = bool(run())
            print(f"  [{'CAUGHT' if caught else 'SURVIVED'}] {name}")
            if not caught:
                fails.append("self-test: " + name)
            for n, v in keep.items():
                setattr(hol, n, v)
            hol.NTH.clear(); hol.NTH.update(keep_nth)
            responder.DEFAULTS.clear(); responder.DEFAULTS.update(keep_def)
    print(f"eval_holidays: {len(fails)} failed")
    sys.exit(1 if fails else 0)
