#!/usr/bin/env python3
"""DRAFTS — what Cal would have said, for every message that was not his own.

WHY. Cal hears far more than he answers: 206 of 284 records in a 28-day window got silence.
Whether that silence is right is currently unknowable, because nothing anywhere records what
Cal WOULD have said. This drafts it, stores it, and publishes it on its own tab. Nothing here
transmits, and the point is to look before deciding whether Cal should speak more.

THE DRAFT IS CAL'S REASONING, NOT THE MODEL'S. cal_reply() runs the SAME ladder the live
responder runs, in the same order and out of the same functions: sigreport, then the doer plan
(calc, sunmoon, weather, capabilities), then the off-list greeting ack, and a model prompt only
when nothing else claimed the message. Until 2026-09-08 it did none of that -- it called the
model for every message and separately LABELLED which doer would have answered, so the tab
published Cal declining a capability he has ("Can't check live weather try online") beside a
note that the weather doer matched. 13 of 143 stored rows were wrong that way.

Transmission gates -- cooldown, the daily sigreport budget, channel utilisation -- are
deliberately NOT applied: they decide whether Cal speaks, not what he would say, and the row
already carries the true answer to "did he speak" in `why_silent`. ALLOW_FROM is consulted
only to place the greeting arm where production mounts it; it does NOT suppress a draft. So an
off-list row with no doer match shows what Cal COULD have said, not what he would have sent.

THREE STRUCTURAL RULES, all graded by eval_drafts.py:

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

  3. A DOER'S ANSWER IS REPLAYED, NEVER RECONSTRUCTED. sigreport is built entirely out of snr,
     rssi, hops and relay_byte, which live on the PACKET (inbox.jsonl) and not on the decision
     (decisions.jsonl). Without the packet, a "report" would be invented numbers presented as
     measurements, so the join refuses when it cannot identify the packet unambiguously and
     sigreport declines. Validated against an oracle, not by inspection: for the 4 records where
     Cal really sent a report and the text still matches today's rules, this path reproduces the
     transmitted string byte for byte.

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
SCHEMA = 1                      # bump when a row's meaning changes, not merely its fields
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


def regime(cfg):
    """WHAT CODE PRODUCED THIS ROW. Computed once per run and stamped on every row.

    This is the one thing here that cannot be added later. A row keeps its text for a year; the
    conditions that produced it are destroyed at write time. The gap is not hypothetical -- on
    2026-09-06 the input contract changed mid-afternoon (drafts began sanitizing before the
    model, as the responder always had) and the only way to tell an old row from a new one was
    the ACCIDENTAL absence of a key. A 365-day corpus lost regime-legibility on day one.

    Four fields, because four things change what a draft would have been: the code, the model,
    which capabilities were armed (an armed doer means the model never sees that ask), and the
    row schema itself. `armed` is a sorted digest rather than the list, so it stays one short
    field and still changes whenever the set does.
    """
    import hashlib
    # learn.git_head() already does this, is tested, and also reports whether the sha is on
    # origin. Writing a second `rev-parse` here would have added a subprocess site to a module
    # whose whole safety story is that it shells out to nothing -- and the eval caught exactly
    # that, which is the check doing its job rather than getting in the way.
    try:
        sha, pushed = _learn.git_head()
    except Exception:
        sha, pushed = "", False
    armed = sorted(k for k, v in cfg.items()
                   if k.endswith("_ENABLED") and str(v).lower() == "true")
    return {"schema": SCHEMA,
            "commit": sha or "",
            "pushed": bool(pushed),
            "model": cfg.get("RESPONDER_MODEL", ""),
            "armed": hashlib.sha256(",".join(armed).encode()).hexdigest()[:12],
            "armed_n": len(armed)}


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


# --- the radio's own measurements -------------------------------------------------------------
# decisions.jsonl records what Cal DECIDED; it does not carry snr, rssi, hops or relay_byte,
# because those belong to the packet rather than the decision. sigreport is built entirely out
# of them, so without the packet its replay is not "a different answer" -- it is a FABRICATION,
# and sigreport correctly refuses with no_measurements. inbox.jsonl has all four on all rows.
#
# THE JOIN IS DELIBERATELY REFUSABLE. The two files stamp different clocks (the bridge's receive
# time and the responder's decision time), so no exact key exists: measured across 322 records,
# (from, ts) matches 0 and (from, text) matches all 322 -- but (from, text) is NOT unique, with
# 28 keys covering 73 rows. Picking the nearest of several is how the trace panel published one
# exchange's measurements against another's message. So: nearest within a bound, and when more
# than one candidate sits inside that bound, return nothing and let sigreport refuse.
#
# Validated against an oracle rather than by inspection: 8 records show Cal really sent a
# sigreport, and for the 4 whose text still matches, this join plus sigreport.report() reproduces
# the transmitted string BYTE FOR BYTE, relay name and all.
_JOIN_WINDOW_S = 60.0
_PACKETS = None


def _packet_index():
    global _PACKETS
    if _PACKETS is None:
        _PACKETS = {}
        try:
            for ln in open(os.path.join(BASE, "inbox.jsonl"), encoding="utf-8"):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                _PACKETS.setdefault((r.get("from"), r.get("text")), []).append(r)
        except OSError:
            pass
    return _PACKETS


def _when(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def packet_for(rec, window=_JOIN_WINDOW_S):
    """The inbound packet behind a decision, or None when it cannot be identified UNAMBIGUOUSLY."""
    t = _when(rec.get("ts"))
    if t is None:
        return None
    near = [c for c in _packet_index().get((rec.get("from"), rec.get("text")), [])
            if (_when(c.get("ts")) is not None
                and abs((_when(c.get("ts")) - t).total_seconds()) <= window)]
    return near[0] if len(near) == 1 else None


def cal_reply(cfg, rec, our, dry=False):
    """WHAT CAL WOULD HAVE SAID, through his own ladder rather than a model wearing his name.

    Mirrors the live order exactly -- sigreport (responder.py:1430), the doer/model plan
    (:1476), then the off-list greeting ack (:1583) -- and calls each arm's OWN content
    function instead of restating its logic here. Restating it is how the two copies drift,
    and this module is a read-only observer with no standing to hold a second opinion about
    what Cal says.

    TRANSMISSION GATES ARE DELIBERATELY NOT APPLIED. Cooldown, the daily sigreport budget and
    channel utilisation decide whether Cal SPEAKS; none of them changes what he would have
    said. The row already carries the true answer to "did he speak" in `why_silent`, copied
    from the responder's own record at the time. Re-deciding it here would answer a settled
    question using TODAY's channel instead of that day's -- a draft that flips because the
    airwaves are busy this morning is measuring the wrong thing.

    ALLOW_FROM IS CONSULTED, BUT NOT OBEYED, and the distinction matters. In production it
    stops an off-list sender from ever reaching generated prose, so a strict reading would draft
    SILENCE for most of this corpus -- 103 of 143 stored rows are off-list asks no doer claims,
    and the tab would go blank exactly where it is most worth reading. So it decides only where
    the GREETING arm mounts (production mounts it in the off-list branch, and for a stranger the
    fixed ack is the only thing Cal ever sends, so falling through to the model there would
    invent conversation he does not offer). It does NOT suppress the model draft.

    The consequence, stated plainly because the row does not otherwise say it: for an off-list
    sender with no doer match, the draft is what Cal COULD have said, not what he would have
    sent -- he would have said nothing. `why_silent` carries "sender_not_allowed" on exactly
    those rows, copied from the responder's own record, and that is where the truth lives.

    Returns (reply, gen_status, via).
    """
    text = rec.get("text") or ""
    trig = cfg.get("TRIGGER_WORD", "cal")
    allow = [a.strip() for a in (cfg.get("ALLOW_FROM") or "").split(",") if a.strip()]
    off_list = rec.get("from") not in allow

    # 1. SIGREPORT. The measurements ride on the record, so this arm replays EXACTLY: the
    #    numbers are the ones the radio actually heard that day, not today's.
    #    try_answer() joins match+report and threads `index`, which the separate two-call path
    #    once dropped -- the 2026-08-23 defect described at responder.py:1283. `relay_name` is
    #    passed because production passes it; eval_drafts asserts the two agree on real records.
    if (cfg.get("SIGREPORT_ENABLED", "false").lower() == "true"
            and rec.get("from") != our
            and rec.get("reaction") in (None, False)):
        pkt = packet_for(rec)
        if pkt is not None and pkt.get("reaction") in (None, False):
            sr, _m = _sig.try_answer(
                text, pkt,
                max_chars=_int_cfg(cfg, "SIGREPORT_MAX_CHARS",
                                   _r.DEFAULTS["SIGREPORT_MAX_CHARS"]),
                trigger=trig,
                relay_name=_r.resolve_relay(pkt.get("relay_byte")))
            if sr:
                return sr, "ok", "sigreport"

    # 2. THE DOER LADDER, then the model. plan_response is pure by contract, so calling it and
    #    then declining to use the prompt costs nothing but the weather fetch -- which a bare
    #    greeting can never trigger.
    plan = _r.plan_response(cfg, rec.get("from"), text)
    if plan["mode"] == "fixed":
        return (plan["fixed_reply"], "ok",
                plan.get("capability") or plan.get("fixed_kind") or "fixed")

    # 3. GREETING, off-list only -- which is where production mounts it. For a stranger this is
    #    the ONLY thing Cal ever sends, so falling through to the model here would misrepresent
    #    him in the direction that flatters: inventing conversation he does not actually offer.
    if (off_list and cfg.get("GREETING_ENABLED", "false").lower() == "true"
            and rec.get("to") in ("^all", None)
            and rec.get("reaction") in (None, False)):
        g = _r.greeting_reply(text, cfg.get("GREET_TEXT", ""))
        if g:
            return g, "ok", "greeting"

    # DRY: the arm is decided by everything above this line, all of it deterministic. --audit
    # only needs to know WHICH arm would answer, so it stops here rather than spending a model
    # call per banked row. The ladder above is the real one either way -- an audit that
    # restated the order would be a second opinion about what Cal says, which is exactly what
    # this module's docstring forbids.
    if dry:
        return None, "dry", ("model+" + plan["capability"] if plan.get("capability") else "model")
    reply, why = _r.run_claude(cfg, plan["prompt"])
    # The weather path is NEITHER arm: plan_response returns mode=generate with
    # capability="weather" because the harness fetched a real observation and injected it, and
    # the model only phrases it. "model" would hide the fact; a doer name would claim a
    # determinism it lacks -- rerun tomorrow and the temperature differs. Name it for what it is.
    via = "model+" + plan["capability"] if plan.get("capability") else "model"
    return reply, why, via


# Arms whose injected fact is fetched LIVE at generation time and therefore cannot be replayed
# for a message from last week. sigreport is deliberately absent: it is rebuilt from snr/rssi on
# the PACKET, so it is contemporaneous by construction, which is the whole reason that path
# refuses when it cannot identify the packet.
_TIME_DEPENDENT = ("weather", "sunmoon")


def faithful_draft(rec, via):
    """Is this draft a faithful account of what Cal WOULD have said, at the time?

    Two ways it is not, and only the first was checked until 2026-09-12:

      1. Cal actually replied. Then the real reply is on the record and a draft beside it is a
         guess about a question already answered.
      2. A LIVE, TIME-DEPENDENT FACT went into it. A weather draft made today for an August
         message carries today's observation. Measured: an 2026-08-21 heat-index ask and a
         2026-09-10 storm ask hold the IDENTICAL fact, twenty days apart, and one row answered
         "Moderate rain" with "75F clear south wind 7 mph". Both rendered with no caveat,
         because `faithful` was keyed on (1) alone and Cal had never replied to either.

    This mislabelling is not cosmetic: it produced two wrong verdicts in the 2026-09-12 review,
    where three heat-index drafts were graded "never answered the heat index" while the real
    on-air replies had answered it ("Clear skies, 88F, heat index 98F"). A grading surface that
    manufactures defects is worse than one that reports none.
    """
    if rec.get("reply"):
        return False
    return not any(k in (via or "") for k in _TIME_DEPENDENT)


def _rec_for(row):
    """Rebuild the responder record a banked row was drafted from.

    Only the fields the ladder reads. `reply` is restored from `sent_reply` because
    faithful_draft() keys on it, and dropping it would silently reclassify every row Cal
    actually answered.
    """
    return {"text": row.get("text"), "from": row.get("from"), "to": row.get("to") or "^all",
            "reaction": None, "reply": row.get("sent_reply"), "id": row.get("packet_id"),
            "ts": row.get("ts")}


def audit(cfg, rows=None, our=None):
    """Which banked rows would a DIFFERENT arm answer today? Read-only, no model calls.

    The failure this exists for is the one session 150 paid for in the gap ledger: drafts.jsonl
    is cumulative and `run()` skips anything already drafted (`if did in done: continue`), so a
    capability armed later corrects every FUTURE row and cannot reach one already banked. On
    2026-09-12 that was 40 of 44 graded defects still showing the old Cal, with nothing in the
    repo able to say so.

    Compares the RECORDED arm against the arm the live ladder would choose now. It does not
    rewrite anything -- `--redraft` does that, and only when asked.
    """
    rows = _load_rows() if rows is None else rows
    our = _learn.our_id() if our is None else our
    # Two different unknowns, counted apart because they mean different things. A row with no
    # `armed` hash predates regime stamping; a row with no `via` predates arm recording, and it
    # is the second one that makes the tab's "each row says which arm answered" untrue.
    drift, no_regime, no_arm = [], 0, 0
    for row in rows:
        if not row.get("armed"):
            no_regime += 1
        if not row.get("via"):
            no_arm += 1
        was = row.get("via")
        try:
            _, _, now_arm = cal_reply(cfg, _rec_for(row), our, dry=True)
        except Exception:                                          # noqa: BLE001
            continue
        # A row with no recorded arm predates via-stamping. It is not drift -- nothing is known
        # to have changed -- so it is counted separately rather than inflating the number.
        if was and now_arm != was:
            drift.append({"draft_id": row.get("draft_id"), "text": row.get("text"),
                          "was": was, "now": now_arm, "draft": row.get("draft")})
    return {"total": len(rows), "drift": drift,
            "no_regime": no_regime, "no_arm": no_arm}


def redraft(cfg, ids=None, drifted_only=True, with_model=False, now=None):
    """Re-run banked rows under the code running NOW and rewrite them in place.

    Re-stamps `armed`/`commit` on a rewritten row, and ONLY on a rewritten row: those fields
    say which Cal produced the text, so stamping a row this did not regenerate would assert
    something false about it.

    A row whose new arm is the MODEL needs a model call to get text, so it is skipped unless
    --with-model is passed. Deterministic arms cost nothing and are rewritten by default; that
    is also the direction that matters, since the point of arming a doer is to stop improvising.
    """
    rows = _load_rows()
    our = _learn.our_id()
    reg = regime(cfg)
    changed = skipped = 0
    for row in rows:
        if ids and row.get("draft_id") not in ids:
            continue
        try:
            _, _, now_arm = cal_reply(cfg, _rec_for(row), our, dry=True)
        except Exception:                                          # noqa: BLE001
            continue
        if drifted_only and row.get("via") and now_arm == row.get("via"):
            continue
        if now_arm.startswith("model") and not with_model:
            skipped += 1
            continue
        reply, why, via = cal_reply(cfg, _rec_for(row), our)
        row.update({"draft": reply, "gen_status": why, "via": via,
                    "shape": shape(reply), "chars": len(reply or ""),
                    "doers": doer_coverage(row.get("text") or ""),
                    "faithful": faithful_draft(_rec_for(row), via), **reg})
        changed += 1
    if changed:
        save(rows)
    return {"changed": changed, "skipped_need_model": skipped}


def run(cfg, limit=None, now=None):
    rows = prune(_load_rows(), now)
    done = already(rows)
    our = _learn.our_id()
    cap = limit if limit is not None else _int_cfg(cfg, "DRAFTS_MAX_PER_RUN",
                                                  DEFAULTS["DRAFTS_MAX_PER_RUN"])
    made = 0
    reg = regime(cfg)
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
        # SANITIZE FIRST. build_prompt's contract is "msg_text must already be sanitized", and
        # the responder honours it at responder.py:634 before every live generation. This module
        # did not, so a stranger's raw text was interpolated straight into the user turn --
        # newline truncation and the injected-instruction redaction both bypassed. Measured when
        # it was found: 85 of 100 stored rows came from senders NOT on ALLOW_FROM, 44 distinct
        # strangers, and the resulting prose is published on a public page.
        #
        # The allow list gates GENERATED PROSE. Simulating generation for everyone quietly
        # demoted it to a gate on TRANSMISSION, which was never the deal. Sanitizing restores
        # the responder's actual input path, so the draft is also a more faithful counterfactual,
        # not a less faithful one.
        # THE REAL LADDER lives in cal_reply(); see it for the order and for which gates are
        # deliberately not applied. What it replaces here was a direct
        # sanitize -> build_prompt -> run_claude that reached the model every time and skipped
        # every doer. 13 of 143 stored rows were for messages a doer owns, and the model,
        # having no weather tool, DECLINED -- "Can't check live weather try online" -- against a
        # live weather doer that answers with a temperature. The tab published Cal refusing a
        # capability he has.
        #
        # `clean`/`flagged` are recomputed here rather than returned by cal_reply: they describe
        # what the SANITIZER did to the inbound, which is a property of the message and true on
        # every arm, including the ones that never build a prompt at all.
        clean, flagged = _r.sanitize_inbound(text)
        reply, why, via = cal_reply(cfg, rec, our)
        rows.append({
            "ts": rec.get("ts"),
            "draft_id": did,
            "from": rec.get("from"),
            "text": text,
            "draft": reply,
            "gen_status": why,
            # WHICH ARM ANSWERED: a doer name, or "model". The single most useful field on the
            # row -- it separates "Cal knew this" from "Cal improvised", and a doer reply needs
            # no grading at all.
            "via": via,
            # The responder's OWN recorded reason, copied not recomputed, so the row says which
            # gate actually stopped it rather than which gate would stop it now.
            "why_silent": rec.get("reason"),
            "sent_reply": rec.get("reply"),
            # A draft made later is not what the responder would have said: the weather fact,
            # sun/moon times and DM memory are all read live at generation time.
            "faithful": faithful_draft(rec, via),
            # What the sanitizer did, so the tab can show that a message was redacted rather
            # than silently publishing a draft of something that never reached the model whole.
            "flagged": flagged,
            "shape": shape(reply),
            "doers": doer_coverage(text),
            "chars": len(reply or ""),
            "gen_ms": int((time.time() - t0) * 1000),
            **reg,
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
    ap.add_argument("--audit", action="store_true",
                    help="read-only: which banked rows would a different arm answer now?")
    ap.add_argument("--redraft", action="store_true",
                    help="re-run banked rows under the current code and rewrite them")
    ap.add_argument("--all", action="store_true",
                    help="with --redraft: every row, not only the drifted ones")
    ap.add_argument("--with-model", action="store_true",
                    help="with --redraft: also rewrite rows whose new arm is the model "
                         "(one model call per row)")
    args = ap.parse_args()

    if args.grade:
        return cmd_grade(args)

    if args.audit:
        a = audit(load_cfg())
        print("drafts audit: %d banked · %d would answer differently now · "
              "%d with no recorded arm · %d with no regime stamp"
              % (a["total"], len(a["drift"]), a["no_arm"], a["no_regime"]))
        for d in a["drift"]:
            print("  %-14s -> %-14s %r" % (d["was"], d["now"], (d["text"] or "")[:56]))
        if a["drift"]:
            print("\n  `--redraft` rewrites these. Deterministic arms cost nothing; a row whose")
            print("  new arm is the model is skipped unless --with-model.")
        # Exit codes mirror learn.py --check so this composes with the same alerting path:
        # 0 nothing to do, 1 drift found. A row with no recorded arm is NOT drift -- nothing is
        # known to have changed about it -- so it never trips the alert on its own.
        return 1 if a["drift"] else 0

    if args.redraft:
        cfg = load_cfg()
        r = redraft(cfg, drifted_only=not args.all, with_model=args.with_model)
        print("redrafted %d row(s); %d skipped (new arm is the model, needs --with-model)"
              % (r["changed"], r["skipped_need_model"]))
        return 0

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
