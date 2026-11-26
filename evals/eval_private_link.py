#!/usr/bin/env python3
"""eval_private_link.py -- a DM or Cal's-channel question about the sender's own link gets a
measured signal report (sigreport.private_link_ask, SIGREPORT_PRIVATE_ASK_ENABLED).

The two asks that motivated it are in the log: "Hows the radio holding up?" over DM, twice,
answered by the model with "signal's solid" while the measured SNR was on disk. The expected
sets below are written by hand, never derived from the matcher.

Run:  python3 evals/eval_private_link.py               (exit 0 = pass)
      python3 evals/eval_private_link.py --self-test   also proves the checks can FAIL
"""
import importlib.util
import os
import sys
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
# plan_sigreport reaches the module through responder's own import; point it at this copy.
responder.sigreport = sigreport

FAILS = []


def ck(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}  {detail}")


OURS = "!cccccccc"
CFG = {"RESPONDER_ENABLED": "true", "SIGREPORT_ENABLED": "true", "TRIGGER_WORD": "cal",
       "SIGREPORT_PRIVATE_ASK_ENABLED": "true", "CAL_CHANNEL": "1"}

ASK = [  # real (first two) and plausible variants: the sender asking about THEIR link to Cal
    "Hows the radio holding up?",
    "Cal, hows the link holding up?",
    "how's my signal",
    "Cal how is the link",
    "whats my snr?",
    "Hey Cal, how's reception?",
    "is my signal getting through?",
    "how is my radio coming in",
]
NOT_ASK = [  # each refused for a stated reason
    ("how are you doing", "no link noun"),
    ("how am i doing?", "no link noun -- first person, so only the noun rule refuses it"),
    ("my radio is holding up fine", "a statement, not a question"),
    ("the link was great today", "a statement"),
    ("how's jim's radio holding up?", "a third party"),
    ("how is the signal from Lawrence?", "a place"),
    ("hows the radio club meeting going?", "not link vocabulary"),
    ("whats the weather radio saying?", "another doer's subject"),
    ("how's the link to the 435 router?", "a station"),
    ("", "empty"),
    # adversarial review 2026-11-25: each of these got a report before the fix
    ("is that radio getting through?", "someone else's radio"),
    ("how is that signal?", "someone else's signal"),
    ("hows the signal over there?", "someone else's signal"),
    ("what is rssi?", "a definition"),
    ("whats snr?", "a definition"),
    ("what's a good snr?", "advice"),
    ("how do i get a good signal?", "advice"),
    ("is the radio on?", "about Cal"),
    ("are you on the radio?", "about Cal"),
    ("is that you on the radio?", "about Cal"),
    ("how is your radio holding up?", "about Cal's radio"),
]


def rec(text, to=OURS, ch=0, **kw):
    r = {"text": text, "from": "!aaaaaaaa", "to": to, "channel": ch, "snr": 6.0, "rssi": -40,
         "hops": 1, "hop_start": 7, "hop_limit": 6, "reaction": False}
    r.update(kw)
    return r


def plan(r, cfg=None, st=None):
    return responder.plan_sigreport(dict(CFG, **(cfg or {})), st if st is not None else {}, r,
                                    OURS, ts=time.time())


import json
import tempfile

# The quiet-channel gate reads chUtil from status.json; a fresh, quiet one lets the rest of the
# path be tested. Written to a temp dir, never next to the live state.
_STATUS = os.path.join(tempfile.mkdtemp(prefix="evalprivlink-"), "status.json")
json.dump({"metrics": {"chUtil": 5.0}}, open(_STATUS, "w"))
responder.STATUS = _STATUS


def run():
    del FAILS[:]
    os.utime(_STATUS)                 # keep it fresh across a long self-test
    for t in ASK:
        ck(f"asks about own link: {t!r}", sigreport.private_link_ask(t))
    for t, why in NOT_ASK:
        ck(f"refused ({why}): {t!r}", not sigreport.private_link_ask(t))
    ck("not text refuses", not sigreport.private_link_ask(None))

    # the mount
    ck("defaults OFF", responder.DEFAULTS["SIGREPORT_PRIVATE_ASK_ENABLED"] == "false")
    off = plan(rec("Hows the radio holding up?"), cfg={"SIGREPORT_PRIVATE_ASK_ENABLED": "false"})
    ck("flag off: a DM link ask is not a test", off[0] is False and off[1] == "not_a_test", off[:2])
    dm = plan(rec("Hows the radio holding up?"))
    ck("DM link ask gets a report", dm[0] is True and dm[1] == "sigreport", dm[:2])
    ck("the report goes back as a DM", dm[2] == "!aaaaaaaa", dm[2])
    ck("the report carries the measurement", dm[4] and "6" in dm[4], dm[4])
    ck("the trace names the rule", any(g["gate"] == "private_link_ask" and g["pass"] for g in dm[5]),
       dm[5])
    calch = plan(rec("Hows the radio holding up?", to="^all", ch=1))
    ck("Cal's channel counts as private", calch[0] is True and calch[2] == "^all", calch[:3])
    pub = plan(rec("Hows the radio holding up?", to="^all", ch=0))
    ck("PUBLIC channel: never fires here", pub[0] is False and pub[1] == "not_a_test", pub[:2])
    nocal = plan(rec("Hows the radio holding up?", to="^all", ch=1), cfg={"CAL_CHANNEL": "-1"})
    ck("Cal's channel disarmed: channel 1 is public", nocal[0] is False, nocal[:2])
    zero = plan(rec("whats my snr?", to="^all", ch=0), cfg={"CAL_CHANNEL": "0"})
    ck("CAL_CHANNEL=0 never makes the primary channel private", zero[0] is False, zero[:2])
    other = plan(rec("Hows the radio holding up?", to="!bbbbbbbb"))
    ck("a DM to someone else is not ours", other[0] is False, other[:2])
    chat = plan(rec("how are you doing"))
    ck("DM chit-chat is not a test", chat[0] is False and chat[1] == "not_a_test", chat[:2])

    # every downstream gate still applies
    nomeas = plan(rec("Hows the radio holding up?", snr=None, rssi=None, hops=None,
                      hop_start=None, hop_limit=None))
    ck("no measurement: silent, never vague", nomeas[0] is False, nomeas[:2])
    st = {"sig_per_sender": {"!aaaaaaaa": time.time()}}
    cool = plan(rec("Hows the radio holding up?"), st=st)
    ck("per-sender cooldown still applies", cool[0] is False and "cooldown" in cool[1], cool[:2])
    kill = plan(rec("Hows the radio holding up?"), cfg={"RESPONDER_ENABLED": "false"})
    ck("kill switch still first", kill[0] is False and kill[1] == "disabled", kill[:2])
    sigoff = plan(rec("Hows the radio holding up?"), cfg={"SIGREPORT_ENABLED": "false"})
    ck("SIGREPORT_ENABLED still governs", sigoff[0] is False, sigoff[:2])
    react = plan(rec("Hows the radio holding up?", reaction=True))
    ck("a tapback is not an ask", react[0] is False, react[:2])
    return FAILS[:]


MUTATIONS = {
    "vocabulary wall removed":
        lambda: setattr(sigreport, "own_link_only", lambda t: True),
    "noun requirement removed":
        lambda: setattr(sigreport, "_LINK_NOUN", __import__("re").compile(r"")),
    "elsewhere guard removed":
        lambda: setattr(sigreport, "_ELSEWHERE", __import__("re").compile(r"(?!x)x")),
    "this-link requirement removed":
        lambda: setattr(sigreport, "_THIS_LINK", __import__("re").compile(r"")),
    "channel 0 treated as private":
        lambda: setattr(responder, "cal_channel", lambda cfg: 0),
    "question shape removed":
        lambda: setattr(sigreport, "_ASKED", __import__("re").compile(r"")),
    "mounted on public traffic":
        lambda: setattr(responder, "_private_link_ask",
                        lambda cfg, r, o: str(cfg.get("SIGREPORT_PRIVATE_ASK_ENABLED")) == "true"
                        and sigreport.private_link_ask(r.get("text", ""))),
    "flag ignored":
        lambda: setattr(responder, "_private_link_ask",
                        lambda cfg, r, o: r.get("to") == o and sigreport.private_link_ask(r.get("text", ""))),
    "defaults ON":
        lambda: responder.DEFAULTS.__setitem__("SIGREPORT_PRIVATE_ASK_ENABLED", "true"),
}

if __name__ == "__main__":
    fails = run()
    for f in fails:
        print("  FAIL " + f)
    if "--self-test" in sys.argv:
        print("self-test -- each mutation must be CAUGHT:")
        saved = {(sigreport, n): getattr(sigreport, n)
                 for n in ("own_link_only", "_LINK_NOUN", "_ASKED", "_ELSEWHERE", "_THIS_LINK")}
        saved[(responder, "cal_channel")] = responder.cal_channel
        saved[(responder, "_private_link_ask")] = responder._private_link_ask
        dflt = responder.DEFAULTS["SIGREPORT_PRIVATE_ASK_ENABLED"]
        for name, mutate in MUTATIONS.items():
            mutate()
            caught = bool(run())
            print(f"  [{'CAUGHT' if caught else 'SURVIVED'}] {name}")
            if not caught:
                fails.append("self-test: " + name)
            for (mod, n), v in saved.items():
                setattr(mod, n, v)
            responder.DEFAULTS["SIGREPORT_PRIVATE_ASK_ENABLED"] = dflt
    n = len(ASK) + len(NOT_ASK) + 18
    print(f"eval_private_link: {n} checks, {len(fails)} failed")
    sys.exit(1 if fails else 0)
