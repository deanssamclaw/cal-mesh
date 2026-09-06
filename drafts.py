#!/usr/bin/env python3
"""DRAFTS — what Cal would have said, for every message that was not his own.

WHY. Cal hears far more than he answers: 206 of 284 records in a 28-day window got silence.
Whether that silence is right is currently unknowable, because nothing anywhere records what
Cal WOULD have said. This drafts it, stores it, and publishes it on its own tab. Nothing here
transmits, and the point is to look before deciding whether Cal should speak more.

TWO STRUCTURAL RULES, both graded by eval_drafts.py:

  1. THE MODEL CALL IS THE RESPONDER'S. This module calls responder.run_claude() and never
     builds an argv. That argv is a lockdown -- `--permission-mode plan` (tools cannot execute,
     on the box that owns the radio), `--setting-sources ""` (Dean's private CLAUDE.md is ABSENT,
     not merely persona-guarded), `--output-format text`. Every eval that guards those flags
     asserts against `_claude_argv` BY NAME, so a second call site would inherit none of them and
     nothing in the repo would notice.

  2. IT CANNOT TRANSMIT, AND THAT IS TESTED AT THE FILESYSTEM. bridge.py drains ~/cal-mesh/outbox/
     and broadcasts any file dropped there -- a plain text file goes to ^all on channel 0. So the
     transmit boundary is a DIRECTORY, not an import, and "this module does not import enqueue"
     is a check that cannot fail: it passes for open(OUTBOX + "/x", "w"). The eval instead runs
     this module under a wrapper that fails on any write resolving inside outbox/.

WHAT LEARNS FROM IT. Deliberately split, because a check that scores its own output cannot fail:

  * AUTOMATIC, needs nobody. Per draft: did Cal produce something, was it GENERIC boilerplate
    (learn._GENERIC_SMELL, already tested), and would a deterministic DOER have answered the
    message a gate refused? The last one is the strongest signal available because it involves no
    judgment at all -- and measured on today's log it is nearly silent (7 of 206, six of them
    historical range tests from before sigreport was armed). It earns its place by staying quiet.
  * HUMAN, optional, never required. `--grade` records a person's verdict. The automatic counters
    accumulate whether or not anyone ever grades, which is exactly the property the triage queue
    lacks: that one sat unworked for 13 consecutive scheduled runs.

Cal never grades Cal on QUALITY. The automatic layer measures shape, not correctness.

Run:  python3 drafts.py            (draft the new records)
      python3 drafts.py --check    (counts only, writes nothing)
      python3 drafts.py --grade <id> --verdict good|wrong|harmful --note "..."
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import responder as _r          # noqa: E402  -- run_claude, load_config, clean_reply, build_prompt
import learn as _learn          # noqa: E402  -- normalize, iter_decisions, _GENERIC_SMELL
import calc as _calc            # noqa: E402
import weather as _weather      # noqa: E402
import capabilities as _caps    # noqa: E402
import sunmoon as _sunmoon      # noqa: E402
import sigreport as _sig        # noqa: E402

DRAFTS = os.path.join(BASE, "drafts.jsonl")
STATE = os.path.join(BASE, "drafts-state.json")
GRADES = os.path.join(BASE, "draft-grades.json")

RETAIN_DAYS = 365               # Dean's call 2026-09-06. ~3,000 records, about a megabyte.
DEFAULTS = {"DRAFTS_ENABLED": "false", "DRAFTS_MAX_PER_RUN": "40", "DRAFTS_MAX_PER_DAY": "120"}
VERDICTS = ("good", "wrong", "harmful")


def _int_cfg(cfg, key, dflt):
    """Fail CLOSED: an unparseable budget becomes the default, never unlimited."""
    try:
        return int(str(cfg.get(key, dflt)).strip())
    except (TypeError, ValueError):
        return int(dflt)


def load_cfg():
    """The responder's config, plus this module's OWN keys.

    responder.load_config() starts from responder.DEFAULTS and keeps a line only `if k in cfg`,
    so a key it has never heard of is silently dropped. DRAFTS_ENABLED lives here, not there, so
    reading the config through that function alone meant the flag could be set to true in the
    file and this module would still report DISARMED -- unreachable, with nothing saying why.

    It failed CLOSED, which is the right direction to fail, and is exactly why nothing noticed:
    the eval asserted the default was off and the module dutifully stayed off. Found by arming
    it for real and watching it refuse.

    The responder is deliberately not modified to know about these keys: it owns the transmit
    path, and a read-only observer has no business adding fields to it.
    """
    cfg = dict(DEFAULTS)
    cfg.update(_r.load_config())
    try:
        for ln in open(os.path.join(BASE, "config")):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                if k.strip() in DEFAULTS:
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    return cfg


def _load(path, dflt):
    try:
        return json.load(open(path))
    except Exception:
        return dflt


def draft_id(rec):
    """Stable per RECORD, not per cluster: the same ask arriving twice is two drafts, because
    the interesting question is whether Cal would answer it the same way both times."""
    return "%s:%s" % (rec.get("ts", "")[:19], rec.get("id") or rec.get("from") or "?")


def doer_coverage(text):
    """Which deterministic doers WOULD have answered this. No model, no judgment.

    Each matcher is read through the field it actually sets, not truthiness: try_answer returns
    a (reply, meta) TUPLE that is truthy even when it declined, and the explain_* helpers return
    an explanation dict either way whose `via` is the real answer. Reading these wrong produced
    a confident '27 of 27' on 2026-09-05 that meant nothing."""
    hits = []
    try:
        reply, meta = _calc.try_answer(text)
        if reply is not None:
            hits.append("calc:" + str(meta.get("handler")))
    except Exception:
        pass
    for name, fn in (("weather", lambda t: _weather.explain_weather_match(t)),
                     ("capabilities", lambda t: _caps.explain_match(t, "cal")),
                     ("sunmoon", lambda t: _sunmoon.explain_match(t)),
                     ("sigreport", lambda t: _sig.match(t, trigger="cal"))):
        try:
            m = fn(text)
            if isinstance(m, dict) and m.get("via"):
                hits.append(name)
        except Exception:
            pass
    return hits


def shape(draft):
    """SHAPE, never quality. 'answered' means Cal produced prose, not that the prose is right --
    that distinction is the whole reason the automatic layer is trustworthy without a human."""
    if not draft:
        return "silent"
    if _learn._GENERIC_SMELL.search(draft):
        return "generic"
    return "answered"


def prune(rows, now=None):
    """Retention is by TIMESTAMP, not by line count. decisions.jsonl is trimmed to its last 5,000
    LINES, which has no relationship to age -- inheriting that would have made the one-year
    promise mean whatever the traffic rate happened to be."""
    now = now or datetime.now(timezone.utc)
    cut = (now - timedelta(days=RETAIN_DAYS)).isoformat()
    return [r for r in rows if (r.get("ts") or "") >= cut]


def already(rows):
    return {r.get("draft_id") for r in rows}


def candidates(our):
    """Every inbound that is not Cal's own, NEWEST FIRST. Emoji, tapbacks, other people's
    conversations, and the ones he answered -- all of it. A message Cal replied to still gets a
    draft, marked unfaithful, because 'would he answer that differently today' is worth seeing;
    it is stored BESIDE the real reply and never replaces it.

    ORDER IS THE WHOLE POINT OF A BOUNDED RUN. iter_decisions() yields oldest first, so taking
    the first N unprocessed drafted the OLDEST twenty of 285 and the tab showed 2026-08-08 to
    08-14 while three weeks of newer traffic sat untouched. A review surface has to answer
    "what is Cal not saying lately", so a run that cannot cover everything must cover the
    RECENT end. The backlog is then filled in by running it again, from the new end backwards.
    """
    rows = [r for r in _learn.iter_decisions()
            if r.get("from") and r.get("from") != our]
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return rows


def run(cfg, limit=None, now=None):
    rows = prune(_load_rows(), now)
    done = already(rows)
    our = _learn.our_id()
    cap = limit if limit is not None else _int_cfg(cfg, "DRAFTS_MAX_PER_RUN",
                                                  DEFAULTS["DRAFTS_MAX_PER_RUN"])
    made = 0
    for rec in candidates(our):
        if made >= cap:
            break
        did = draft_id(rec)
        if did in done:
            continue
        text = rec.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        t0 = time.time()
        prompt = _r.build_prompt((rec.get("from") or "")[-4:], text)
        reply, why = _r.run_claude(cfg, prompt)
        rows.append({
            "ts": rec.get("ts"),
            "draft_id": did,
            "from": rec.get("from"),
            "text": text,
            "draft": reply,
            "gen_status": why,
            # The responder's OWN recorded reason, copied not recomputed, so the row says which
            # gate actually stopped it rather than which gate would stop it now.
            "why_silent": rec.get("reason"),
            "sent_reply": rec.get("reply"),
            # A draft made later is not what the responder would have said: the weather fact,
            # sun/moon times and DM memory are all read live at generation time.
            "faithful": not bool(rec.get("reply")),
            "shape": shape(reply),
            "doers": doer_coverage(text),
            "chars": len(reply or ""),
            "gen_ms": int((time.time() - t0) * 1000),
        })
        made += 1
    return rows, made


def _load_rows():
    out = []
    try:
        for ln in open(DRAFTS, encoding="utf-8"):
            ln = ln.strip()
            if ln:
                out.append(json.loads(ln))
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass
    return out


def save(rows):
    tmp = DRAFTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, DRAFTS)


def summary(rows):
    """The automatic layer. Counts shape and doer coverage; never quality."""
    sh, doers, unfaithful = {}, {}, 0
    for r in rows:
        sh[r.get("shape")] = sh.get(r.get("shape"), 0) + 1
        if not r.get("faithful"):
            unfaithful += 1
        for d in r.get("doers") or []:
            doers[d] = doers.get(d, 0) + 1
    graded = _load(GRADES, {})
    return {"drafts": len(rows), "shape": sh, "doer_would_have_answered": doers,
            "unfaithful": unfaithful, "graded": len(graded),
            "verdicts": {v: sum(1 for g in graded.values() if g.get("verdict") == v)
                         for v in VERDICTS}}


def cmd_grade(args):
    """The optional human layer. A verdict lives in its OWN file, never in triage.json: any dict
    under a cluster key there reads as a triage verdict to four consumers, so a grade written
    there would drop the cluster out of the build queue and lower the untriaged count on the
    public page within 60 seconds -- asserting a gap was triaged when nothing was built."""
    if args.verdict not in VERDICTS:
        print("verdict must be one of: " + ", ".join(VERDICTS))
        return 2
    g = _load(GRADES, {})
    g[args.grade] = {"verdict": args.verdict, "note": args.note or "",
                     "by": args.by or "dean", "ts": datetime.now(timezone.utc).isoformat()}
    json.dump(g, open(GRADES, "w"), ensure_ascii=False, indent=2, sort_keys=True)
    print("graded %s = %s" % (args.grade, args.verdict))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Draft the reply Cal did not send.")
    ap.add_argument("--check", action="store_true", help="counts only; writes nothing")
    ap.add_argument("--limit", type=int, help="cap this run (default from config)")
    ap.add_argument("--grade", metavar="DRAFT_ID")
    ap.add_argument("--verdict", choices=VERDICTS)
    ap.add_argument("--note")
    ap.add_argument("--by")
    args = ap.parse_args()

    if args.grade:
        return cmd_grade(args)

    if args.check:
        print(json.dumps(summary(prune(_load_rows())), indent=2))
        return 0

    cfg = load_cfg()
    if str(cfg.get("DRAFTS_ENABLED", DEFAULTS["DRAFTS_ENABLED"])).lower() != "true":
        print("drafts: DISARMED (DRAFTS_ENABLED is not true) — nothing drafted")
        return 0

    rows, made = run(cfg, args.limit)
    save(rows)
    s = summary(rows)
    print("drafted %d new · %d held (retention %dd) · shape %s · doers %s"
          % (made, s["drafts"], RETAIN_DAYS, s["shape"], s["doer_would_have_answered"]))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
