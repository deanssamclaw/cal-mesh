#!/usr/bin/env python3
"""Eval for presence.py -- the occasional greeting / radio check on the channel.

Every rule in presence.plan makes Cal QUIETER. Each one is checked here, and each check is paired
with an in-process mutant that removes the rule and must turn the suite red.

Run:  python3 evals/eval_presence.py            (exit 0 = pass)
      python3 evals/eval_presence.py --self-test (also runs the mutants)
"""
import contextlib
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(HERE) == "evals":
    HERE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import presence as P

NOW = 1_800_000_000.0
DAY = 86400.0
FAILS = []


def ck(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def cfg(**over):
    c = dict(P.DEFAULTS, RESPONDER_ENABLED="true", PRESENCE_ENABLED="true", PRESENCE_CHANCE="1")
    c.update(over)
    return c


ALWAYS = lambda: 0.0          # the chance roll passes, and the first choice is taken
NEVER = lambda: 0.999


def run(c=None, history=(), heard=None, util=5.0, hour=8, rng=ALWAYS):
    return P.plan(cfg() if c is None else c, list(history), heard, util, now=NOW,
                  local_hour=hour, rng=rng)


def suite():
    print("\n== switches ==")
    ck("off by default", P.DEFAULTS["PRESENCE_ENABLED"] == "false")
    ck("PRESENCE_ENABLED=false is silent", run(cfg(PRESENCE_ENABLED="false"))[0] is None)
    ck("the master switch off is silent, whatever PRESENCE_ENABLED says",
       run(cfg(RESPONDER_ENABLED="false"))[1] == "responder_disabled")
    ck("an absent master switch is off", P.plan(dict(P.DEFAULTS, PRESENCE_ENABLED="true",
       PRESENCE_CHANCE="1"), [], None, 5.0, now=NOW, local_hour=8, rng=ALWAYS)[0] is None)
    ck("everything clear: it sends", run()[1] == "send", repr(run()))

    print("\n== time windows ==")
    for h, want in ((7, "PRESENCE_MORNING"), (9, "PRESENCE_MORNING"), (12, "PRESENCE_AFTERNOON"),
                    (15, "PRESENCE_AFTERNOON"), (18, "PRESENCE_EVENING"), (20, "PRESENCE_EVENING")):
        got = run(hour=h)[0]
        ck(f"{h:02}:00 greets with {want}", got and got["key"] == want, repr(got))
    for h in (0, 3, 6, 10, 11, 16, 17, 21, 23):
        ck(f"{h:02}:00 is outside every window", run(hour=h)[1] == "outside_windows")

    print("\n== rare ==")
    ck("inside the minimum gap: silent", run(history=[{"ts": NOW - 35 * 3600, "key": "x"}])[1] == "min_gap")
    ck("just past the gap: allowed", run(history=[{"ts": NOW - 37 * 3600, "key": "x"}])[1] == "send")
    three = [{"ts": NOW - 6.5 * DAY, "key": "a"}, {"ts": NOW - 4.5 * DAY, "key": "b"},
             {"ts": NOW - 2.5 * DAY, "key": "c"}]
    ck("the weekly cap holds", run(history=three)[1] == "weekly_cap")
    ck("a send older than a week no longer counts",
       run(history=[{"ts": NOW - 8 * DAY, "key": "a"}] + three[1:])[1] == "send")
    loose = cfg(PRESENCE_MIN_GAP_H="0", PRESENCE_MAX_PER_WEEK="20")
    ck("one per calendar day, even when the gap and weekly cap allow more",
       run(loose, history=[{"ts": NOW - 60, "key": "x"}])[1] == "daily_cap")
    ck("yesterday's send does not use today's slot",
       run(loose, history=[{"ts": NOW - 30 * 3600, "key": "x"}])[1] == "send")
    ck("the chance roll can say not now", run(cfg(PRESENCE_CHANCE="0.08"), rng=NEVER)[1] == "not_this_time")
    ck("the default chance is small (occasional, not scheduled)", float(P.DEFAULTS["PRESENCE_CHANCE"]) <= 0.2)

    print("\n== never into a conversation, never into a busy channel ==")
    ck("text heard 5 min ago: waits", run(heard=NOW - 300)[1] == "channel_in_conversation")
    ck("text heard an hour ago: fine", run(heard=NOW - 3600)[1] == "send")
    ck("busy channel: waits", run(util=40.0)[1] == "channel_busy_or_unknown")
    ck("unknown utilization: waits (fails closed)", run(util=None)[1] == "channel_busy_or_unknown")

    print("\n== what it says ==")
    last_morning = [{"ts": NOW - 5 * DAY, "key": "PRESENCE_MORNING"}]
    got = run(history=last_morning)[0]
    ck("never the same phrase twice in a row", got and got["key"] != "PRESENCE_MORNING", repr(got))
    got = run(cfg(PRESENCE_MORNING="Morning from the operator's phrase"))[0]
    ck("the operator's phrase is what goes out", got and got["text"] == "Morning from the operator's phrase")
    ck("an empty phrase is never sent", run(cfg(PRESENCE_MORNING=""))[1] == "bad_phrase")
    ck("an over-long phrase is never sent", run(cfg(PRESENCE_MORNING="x" * 80))[1] == "bad_phrase")
    src = open(os.path.join(HERE, "presence.py")).read()
    defaults_block = src[src.index("DEFAULTS = {"):src.index("}", src.index("DEFAULTS = {"))]
    ck("the public defaults name no place (location lives in the operator's config)",
       not re.search(r"\b(Olathe|Kansas|KC|Lenexa|Overland)\b", defaults_block))
    for k in ("PRESENCE_MORNING", "PRESENCE_AFTERNOON", "PRESENCE_EVENING", "PRESENCE_CHECK"):
        ck(f"default {k} fits a mesh message", 0 < len(P.DEFAULTS[k]) <= P.MAX_CHARS)
    ck("junk config numbers fall back to the defaults, not to zero",
       run(cfg(PRESENCE_MIN_GAP_H="banana"), history=[{"ts": NOW - 3600, "key": "x"}])[1] == "min_gap")

    print("\n== the real run: main() on a scratch station ==")
    import json as _j, tempfile as _tf, shutil as _sh
    from datetime import datetime as _dt, timezone as _tz
    base = _tf.mkdtemp()
    try:
        now = _dt(2026, 9, 24, 13, 30, tzinfo=_tz.utc).timestamp()      # 08:30 Central
        def iso(t):
            return _dt.fromtimestamp(t, _tz.utc).isoformat()
        def setup(status_age=30, util=5.0, inbox=True, heard_ago=7200, state=None, extra="", at=None):
            at = now if at is None else at
            for f in ("presence-state.json", "inbox.jsonl", "status.json"):
                try:
                    os.unlink(os.path.join(base, f))
                except OSError:
                    pass
            _sh.rmtree(os.path.join(base, "outbox"), ignore_errors=True)
            open(os.path.join(base, "config"), "w").write(
                "RESPONDER_ENABLED=true\nPRESENCE_ENABLED=true\nPRESENCE_CHANCE=1\n" + extra)
            if status_age is not None:
                _j.dump({"ts": iso(at - status_age), "metrics": {"chUtil": util}},
                        open(os.path.join(base, "status.json"), "w"))
            if inbox:
                open(os.path.join(base, "inbox.jsonl"), "w").write(_j.dumps(
                    {"ts": iso(at - heard_ago), "channel": 0, "text": "hi", "reaction": False}) + "\n")
            if state is not None:
                open(os.path.join(base, "presence-state.json"), "w").write(state)
        def sent():
            d = os.path.join(base, "outbox")
            return [f for f in os.listdir(d)] if os.path.isdir(d) else []
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            setup(); P.main([], base=base, now=now)
        ck("a dry run (no --send) writes nothing", sent() == [])
        with contextlib.redirect_stdout(quiet):
            setup(); P.main(["--send"], base=base, now=now)
        ck("--send writes one outbox entry", len(sent()) == 1, str(sent()))
        rec = _j.load(open(os.path.join(base, "presence-state.json")))
        ck("and remembers it, so the caps hold next run", len(rec.get("sends", [])) == 1)
        with contextlib.redirect_stdout(quiet):
            P.main(["--send"], base=base, now=now + 1800)
        ck("the next run, 30 min later, sends nothing", len(sent()) == 1, str(sent()))
        cases = [("text heard 5 minutes ago (read from the real inbox)", dict(heard_ago=300)),
                 ("a stale radio status (bridge may be down)", dict(status_age=3 * 86400)),
                 ("no radio status at all", dict(status_age=None)),
                 ("no inbox at all", dict(inbox=False)),
                 ("NaN channel use", dict(util=float("nan"))),
                 ("negative channel use", dict(util=-50)),
                 ("a corrupt state file", dict(state="{not json")),
                 ("a state file of the wrong shape", dict(state='{"sends": "x"}')),
                 ("an unknown timezone", dict(extra="PRESENCE_TZ=Mars/Olympus\n"))]
        for name, kw in cases:
            with contextlib.redirect_stdout(quiet):
                setup(**kw); P.main(["--send"], base=base, now=now)
            ck(f"silent with {name}", sent() == [], str(sent()))
        early = _dt(2026, 9, 24, 7, 30, tzinfo=_tz.utc).timestamp()
        with contextlib.redirect_stdout(quiet):
            setup(at=early); P.main(["--send"], base=base, now=early)
        ck("07:30 UTC is 02:30 Central: silent (the operator's clock, not the host's)", sent() == [])
        # ...and the same fixture DOES send inside the window, so the silence above is the clock's.
        with contextlib.redirect_stdout(quiet):
            setup(at=early + 6 * 3600); P.main(["--send"], base=base, now=early + 6 * 3600)
        ck("the same fixture at 08:30 Central sends (control)", len(sent()) == 1, str(sent()))
    finally:
        _sh.rmtree(base, ignore_errors=True)


suite()
if FAILS:
    print(f"\neval_presence: {len(FAILS)} FAILED")
    sys.exit(1)

if "--self-test" in sys.argv:
    print("\n== mutations (each must be caught) ==")
    real = (P.plan, P.main)
    SRC = open(os.path.join(HERE, "presence.py")).read()
    MUTANTS = {
        "master switch ignored": ('if str(cfg.get("RESPONDER_ENABLED", "false")).lower() != "true":',
                                  'if False:'),
        "own flag ignored": ('if str(cfg.get("PRESENCE_ENABLED", "false")).lower() != "true":', 'if False:'),
        "no windows": ("    if wkey is None:\n        return None, \"outside_windows\"",
                       "    if wkey is None:\n        wkey = \"PRESENCE_CHECK\""),
        "no min gap": ('return None, "min_gap"', 'pass'),
        "no weekly cap": ('return None, "weekly_cap"', 'pass'),
        "no daily cap": ('return None, "daily_cap"', 'pass'),
        "talks into a conversation": ('return None, "channel_in_conversation"', 'pass'),
        "busy or unknown channel transmits": ('return None, "channel_busy_or_unknown"', 'pass'),
        "dry run transmits": ('if "--send" not in argv:', 'if False:'),
        "sends are not remembered": ('history = history + [{"ts": now, "key": got["key"]}]', 'pass'),
        "stale radio status trusted": ('if not (0 <= age <= _num(cfg, "PRESENCE_STATUS_MAX_AGE_S")):', 'if False:'),
        "inbox never read": ('    path = os.path.join(base, "inbox.jsonl")\n    try:', '    return None\n    try:'),
        "host clock instead of the operator's": ('ZoneInfo(str(cfg.get("PRESENCE_TZ", DEFAULTS["PRESENCE_TZ"])))', 'ZoneInfo("UTC")'),
        "corrupt state read as never sent": ('state = None                      # unreadable', 'state = {}                      # unreadable'),
        "no chance roll": ('if rng() >= _num(cfg, "PRESENCE_CHANCE"):', 'if False:'),
        "repeats itself": ('if k != last_key]', 'if True]'),
    }
    import types
    escaped = []
    for name, (a, b) in MUTANTS.items():
        assert a in SRC, name
        m = types.ModuleType("presence_mut")
        m.__file__ = P.__file__
        exec(compile(SRC.replace(a, b, 1), "presence_mut", "exec"), m.__dict__)
        m.UNKNOWN = P.UNKNOWN            # one sentinel: main() and plan() must agree on it
        P.plan, P.main = m.plan, m.main
        FAILS.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                suite()
            except Exception:                # a crash is not a catch
                FAILS.clear()
        P.plan, P.main = real
        caught = bool(FAILS)
        print(f"  {'ok  ' if caught else 'FAIL'} mutant caught: {name}")
        if not caught:
            escaped.append(name)
    FAILS.clear()
    if escaped:
        print(f"\neval_presence: {len(escaped)} mutant(s) not caught")
        sys.exit(1)
print("\neval_presence: all checks pass")
