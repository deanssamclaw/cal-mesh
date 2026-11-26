#!/usr/bin/env python3
"""eval_test_exchange.py -- a report inside a test exchange gets a measured signal report
(sigreport.test_exchange, SIGREPORT_TEST_EXCHANGE_ENABLED; Dean's ruling 2026-11-26).

Positives are real messages from the inbox. Negatives are the false fires the replay found
while the rule was being written ("I heard about that show") plus the guards it must keep.
Expected sets are written by hand, never derived from the matcher.

Run:  python3 evals/eval_test_exchange.py               (exit 0 = pass)
      python3 evals/eval_test_exchange.py --self-test   also proves the checks can FAIL
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(HERE) == "evals":
    HERE = os.path.dirname(HERE)
sys.path.insert(0, HERE)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sigreport = _load("sigreport")
responder = _load("responder")
responder.sigreport = sigreport

FAILS = []


def ck(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}  {detail}")


REPORTS = [  # real, from inbox.jsonl
    "Received in OP", "Received OP 103rd/Metcalf", "Heard near Santa Fe and Parker", "3 of 3 hops",
    "2 hops from Overland Park 435 Metcalf", "3 hops to Olathe.", "Test received. 2 hops.",
    "87b3 1 hop", "F082 test received, 3 hops to 87th/I35", "Test from Raytown",
    "Test - 87th & Acuff", "e260... Rcvd Claycomo, 2 hops", "Go chiefs! Rec 7 hops",
    "direct contact-98dbm to independence", "I got you 902c at 3 hops", "Heard in S Olathe",
]
NOT_REPORTS = [  # each refused for a stated reason
    ("I heard about that show. Never saw it", "'heard about' is not a report"),
    ("Who knows I heard the NWS is down too lol", "'heard' mid-sentence"),
    ("Never heard of JUNC either but welcome!", "'heard of'"),
    ("Looks like I'm not the only one testing AI agents in mesh", "'testing' mid-sentence"),
    ("Wow, went from 3 to 6 hops, maybe the weather", "another doer's word"),
    ("how many hops are you?", "a question"),
    ("3 hops to Olathe?", "a question -- only the question rule refuses this one"),
    ("Had one 4 minutes ago, 6 hops and 17 miles away. Had a 7 hop that went further", "over 12 words"),
    ("Good copy", "a bare acknowledgement that replies to nothing"),
    ("Gotcha", "a bare acknowledgement that replies to nothing"),
    ("", "empty"),
]


def rec(text, to="^all", ch=0, reply_to=None, **kw):
    r = {"text": text, "from": "!aaaaaaaa", "to": to, "channel": ch, "snr": 6.0, "rssi": -40,
         "hops": 2, "hop_start": 7, "hop_limit": 5, "reaction": False, "reply_to": reply_to}
    r.update(kw)
    return r


OURS = "!cccccccc"
OWN = frozenset({777})
CFG = {"RESPONDER_ENABLED": "true", "SIGREPORT_ENABLED": "true", "TRIGGER_WORD": "cal",
       "SIGREPORT_TEST_EXCHANGE_ENABLED": "true"}
_STATUS = os.path.join(tempfile.mkdtemp(prefix="evaltx-"), "status.json")
json.dump({"metrics": {"chUtil": 5.0}}, open(_STATUS, "w"))
responder.STATUS = _STATUS


def plan(r, cfg=None, st=None):
    return responder.plan_sigreport(dict(CFG, **(cfg or {})), st if st is not None else {}, r,
                                    OURS, ts=time.time(), own_ids=OWN)


def gates(p):
    return {g["gate"]: g["pass"] for g in (p[5] or [])}


def run():
    del FAILS[:]
    os.utime(_STATUS)
    for t in REPORTS:
        ck(f"report: {t!r}", sigreport.test_exchange(t))
    for t, why in NOT_REPORTS:
        ck(f"refused ({why}): {t!r}", not sigreport.test_exchange(t))
    ck("a bare ack IS a report inside an exchange", sigreport.test_exchange("Copy", is_reply=True))
    ck("not text refuses", not sigreport.test_exchange(None))

    ck("defaults OFF", responder.DEFAULTS["SIGREPORT_TEST_EXCHANGE_ENABLED"] == "false")
    off = plan(rec("Received in OP", reply_to=555), cfg={"SIGREPORT_TEST_EXCHANGE_ENABLED": "false"})
    ck("flag off: a reply to another station is refused as before",
       off[0] is False and off[1] == "sigreport_reply_to_other_station", off[:2])
    ok = plan(rec("Received in OP", reply_to=555))
    ck("flag on: a report in another's test is answered", ok[0] is True and ok[1] == "sigreport", ok[:2])
    ck("it goes to the channel, not a DM", ok[2] == "^all", ok[2])
    ck("it carries the measurement", ok[4] and "2 hops" in ok[4], ok[4])
    gt = gates(ok)
    ck("the trace names the exception", gt.get("test_exchange_reply") is True, gt)
    ck("the trace never claims the reply gate passed", "reply_not_to_other_station" not in gt, gt)
    chat = plan(rec("ha the Chiefs looked great", reply_to=555))
    ck("a chatter reply to another station stays refused",
       chat[0] is False and chat[1] == "sigreport_reply_to_other_station", chat[:2])
    dm = plan(rec("Received in OP", to=OURS, reply_to=555))
    ck("a DM reply to another station's packet is not this rule", dm[0] is False, dm[:2])
    own = plan(rec("Copy in Olathe", reply_to=777))
    ck("a reply to Cal's own packet still passes the reply gate",
       gates(own).get("reply_not_to_other_station") is True, gates(own))
    plain = plan(rec("87b3 1 hop"))
    ck("a hop report naming a station is answered", plain[0] is True, plain[:2])
    ck("and the trace says why", gates(plain).get("test_exchange_report") is True, gates(plain))
    plain_off = plan(rec("87b3 1 hop"), cfg={"SIGREPORT_TEST_EXCHANGE_ENABLED": "false"})
    ck("flag off: the hop report is not a test", plain_off[0] is False and plain_off[1] == "not_a_test",
       plain_off[:2])
    ack = plan(rec("Gotcha"))
    ck("a bare ack replying to nothing stays silent", ack[0] is False, ack[:2])

    # every later gate still applies
    react = plan(rec("Received in OP", reply_to=555, reaction=True))
    ck("a tapback is never a report", react[0] is False, react[:2])
    kill = plan(rec("Received in OP", reply_to=555), cfg={"RESPONDER_ENABLED": "false"})
    ck("kill switch first", kill[0] is False and kill[1] == "disabled", kill[:2])
    nomeas = plan(rec("Received in OP", reply_to=555, snr=None, rssi=None, hops=None,
                      hop_start=None, hop_limit=None))
    ck("no measurement: silent, never vague", nomeas[0] is False, nomeas[:2])
    cool = plan(rec("Received in OP", reply_to=555), st={"sig_per_sender": {"!aaaaaaaa": time.time()}})
    ck("per-sender cooldown still applies", cool[0] is False and "cooldown" in cool[1], cool[:2])
    day = time.strftime("%Y-%m-%d", time.gmtime())
    spent = plan(rec("Received in OP", reply_to=555), st={"sig_day": {day: 10 ** 6}})
    ck("daily budget still applies", spent[0] is False and "budget" in spent[1], spent[:2])
    return FAILS[:]


MUTATIONS = {
    "hop rule removed": lambda: setattr(sigreport, "_TX_HOPS", re.compile(r"(?!x)x")),
    "lead word anywhere": lambda: setattr(sigreport, "_TX_LEAD", re.compile(
        r"\b(?:heard|received|rcvd|rec|copy|copied|gotcha|test(?:ing)?|ack)\b", re.I)),
    "questions allowed": lambda: setattr(sigreport, "test_exchange",
        (lambda f: lambda t, is_reply=False: f(t.replace("?", ""), is_reply) if isinstance(t, str) else f(t, is_reply))(sigreport.test_exchange)),
    "word cap removed": lambda: setattr(sigreport, "_TX_MAX_WORDS", 10 ** 6),
    "bare acks anywhere": lambda: setattr(sigreport, "_TX_BARE_ACK", re.compile(r"(?!x)x")),
    "other doers allowed": lambda: setattr(sigreport, "_claims_other_doer", lambda s: False),
}
# "flag ignored" needs a real mutation of the gate, not of config: rewrite the source check.
_SRC = os.path.join(HERE, "responder.py")


def _flag_ignored():
    src = open(_SRC, encoding="utf-8").read()
    needle = 'tx_flag = str(cfg.get("SIGREPORT_TEST_EXCHANGE_ENABLED", "false")).lower() == "true"'
    assert needle in src, "self-test anchor missing"
    spec = importlib.util.spec_from_loader("responder_mut", loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = _SRC
    exec(compile(src.replace(needle, "tx_flag = True"), _SRC, "exec"), mod.__dict__)
    mod.sigreport = sigreport
    mod.STATUS = _STATUS
    return mod


MUTATIONS["flag ignored"] = None

if __name__ == "__main__":
    fails = run()
    for f in fails:
        print("  FAIL " + f)
    if "--self-test" in sys.argv:
        print("self-test -- each mutation must be CAUGHT:")
        keep = {n: getattr(sigreport, n) for n in ("_TX_HOPS", "_TX_LEAD", "test_exchange",
                                                   "_TX_MAX_WORDS", "_TX_BARE_ACK", "_claims_other_doer")}
        real_responder = responder
        for name, mutate in MUTATIONS.items():
            if name == "flag ignored":
                globals()["responder"] = _flag_ignored()
            else:
                mutate()
            caught = bool(run())
            print(f"  [{'CAUGHT' if caught else 'SURVIVED'}] {name}")
            if not caught:
                fails.append("self-test: " + name)
            for n, v in keep.items():
                setattr(sigreport, n, v)
            globals()["responder"] = real_responder
    print(f"eval_test_exchange: {len(fails)} failed")
    sys.exit(1 if fails else 0)
