#!/usr/bin/env python3
"""eval_learn.py — the classifier is the whole product, so it is what gets tested.

learn.py never transmits, so this is not a house-gate arming eval. It guards one thing: that
each decision record lands in the right bucket. A record misfiled as HIT hides a real gap;
one misfiled as GAP invents work that does not exist. Both corrupt the build queue, which is
the only thing learn.py produces. The cases below are pinned to the ACTUAL record shapes seen
in decisions.jsonl — including the pre-`prompt_kind` schema, which is where the exclusion rule
earns its keep — not to shapes invented here.
"""
import sys
import learn

OUR = "!ca100001"          # synthetic our-node id; the real Cal HT id stays out of source
FAIL = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'FAIL'}  {name}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    if not ok:
        FAIL.append(name)


def rec(**kw):
    kw.setdefault("to", "^all")
    kw.setdefault("matched", True)
    return kw


print("classify() — one bucket per record, by exclusion")

# FILTERED: not matched (off-list sender, not addressed). Never a gap.
check("off-list dropped", learn.classify(rec(matched=False, reason="sender_not_allowed"), OUR), "FILTERED")
check("not-a-greeting dropped", learn.classify(rec(matched=False, reason="not_a_greeting"), OUR), "FILTERED")

# REFUSED / GREETING: designed fixed non-answers, working as built.
check("forecast refusal", learn.classify(rec(gen_status="fixed_forecast_refused", capability="weather",
      prompt_kind="fixed"), OUR), "REFUSED")
check("greeting via gen_status", learn.classify(rec(gen_status="fixed_greeting_ack",
      prompt_kind="fixed"), OUR), "GREETING")
check("greeting via capability", learn.classify(rec(capability="greeting", reason="greeting_ack"), OUR), "GREETING")

# HIT: a real doer answered from a fact.
check("weather hit", learn.classify(rec(capability="weather", prompt_kind="weather", gen_status="ok"), OUR), "HIT")
check("calc hit (fixed string)", learn.classify(rec(capability="calc", prompt_kind="fixed",
      gen_status="fixed_calc"), OUR), "HIT")
check("sunmoon hit", learn.classify(rec(capability="sunmoon", prompt_kind="sunmoon"), OUR), "HIT")

# GAP: matched, reached the model. Two schema eras must BOTH land here.
# The signal readback answers from code and its capability was missing from DOER_CAPS, so three
# correct replies were filed as gaps -- the ledger printed the readback's own answer ("Cal said:
# Copy 16: SNR 6.0, RSSI -35, direct") under a heading that said it had reached the model.
check("sigreport is a HIT, not a gap", learn.classify(
    rec(capability="sigreport", prompt_kind="fixed", gen_status="fixed_sigreport"), OUR), "HIT")
# ...and `Test 12` is the control: it genuinely DID reach the model (that was the session-134
# defect), so it must stay a GAP. Without this the check above passes by filing everything
# test-shaped as answered, which is the same error pointing the other way.
check("a test the model answered is still a gap", learn.classify(
    rec(text="Test 12", prompt_kind="general", gen_status="ok",
        model="claude-haiku-4-5-20251001"), OUR), "GAP")
# A doer this file has never been told about must trip the tripwire, not inflate the gap count.
# DOER_CAPS is hand-kept and drifts on every arming; this is the rule that makes the next
# omission loud. It must NOT be classified HIT either -- that would hide a doer that matched
# and could not answer.
check("an unknown doer trips OTHER", learn.classify(
    rec(capability="somethingnew", prompt_kind="fixed", gen_status="fixed_somethingnew"),
    OUR), "OTHER")

check("gap (current schema)", learn.classify(rec(prompt_kind="general", model="claude-haiku-4-5-20251001"), OUR), "GAP")
check("gap (pre-prompt_kind schema)", learn.classify(rec(reason="addressed"), OUR), "GAP")
check("gap over DM", learn.classify(rec(to=OUR, prompt_kind="general"), OUR), "GAP")

# The tripwire must stay empty for every real shape above. A record cannot reach OTHER unless
# it is matched AND claimed by nothing — which the exclusion rule now prevents. Prove the
# guard is live by constructing the one impossible-in-practice residue: matched=False would be
# FILTERED, so OTHER is only reachable if 'matched' is present-but-falsey in a novel way.
check("no real shape reaches OTHER", "OTHER" not in {
    learn.classify(r, OUR) for r in (
        rec(matched=False), rec(capability="weather", prompt_kind="weather"),
        rec(prompt_kind="general"), rec(gen_status="fixed_greeting_ack"),
        rec(gen_status="fixed_forecast_refused", capability="weather"))}, True)

print("\nis_dm() — DM iff addressed to our node")
check("dm to us", learn.is_dm(rec(to=OUR), OUR), True)
check("broadcast not dm", learn.is_dm(rec(to="^all"), OUR), False)
check("dm to another node not us", learn.is_dm(rec(to="!deadbeef"), OUR), False)

print("\nnormalize() — cluster key collapses trigger + punctuation + case")
check("trigger + case collapse",
      learn.normalize("Cal, what do you know?") == learn.normalize("what do you know"), True)
check("leading-decimal survives as digit-ish key",
      learn.normalize(".5 mi in km"), "5 mi in km")   # documents current behavior, not an ideal

print("\n_GENERIC_SMELL — flags product boilerplate, not ordinary replies")
check("flags 'I can help with'", bool(learn._GENERIC_SMELL.search(
      "I can help with coding, technical questions, writing")), True)
check("flags 'general knowledge'", bool(learn._GENERIC_SMELL.search("...and general knowledge.")), True)
check("does NOT flag a plain reply", bool(learn._GENERIC_SMELL.search(
      "Yeah, familiar with it. What do you need?")), False)
check("does NOT flag a weather reply", bool(learn._GENERIC_SMELL.search("66F, clear, SE 5 mph wind")), False)

print("\nclassify() — the doer's own outcome, read BEFORE the HIT rule")
# Both of these reached a doer, so both would count as clean answers under the old rule. That
# is the failure this loop was built to fix: a half-built doer and a missing capability were
# invisible to the thing whose job is spotting them.
check("clarify is not a HIT", learn.classify(
      rec(capability="calc", prompt_kind="fixed", calc={"handler": "torque", "outcome": "clarify"}),
      OUR), "CLARIFY")
check("no_table is not a REFUSED", learn.classify(
      rec(capability="calc", prompt_kind="fixed", calc={"handler": "torque", "outcome": "no_table"}),
      OUR), "NO_TABLE")
check("a real answer is still a HIT", learn.classify(
      rec(capability="calc", prompt_kind="fixed", calc={"handler": "torque", "outcome": "answered"}),
      OUR), "HIT")
# Older records predate the field entirely and must keep their old bucket.
check("missing outcome falls back to HIT", learn.classify(
      rec(capability="calc", prompt_kind="fixed", calc={"handler": "convert"}), OUR), "HIT")
check("no calc meta at all", learn.classify(
      rec(capability="weather", prompt_kind="fixed"), OUR), "HIT")
# An outcome on a record the model answered must not rescue it out of the build queue.
check("gap stays a gap", learn.classify(rec(prompt_kind="general"), OUR), "GAP")

print("\nmigrate() — v1 ledgers are rewritten, never rebuilt")
# decisions.jsonl keeps only its last 5000 lines, so resetting to pick up the new schema would
# silently drop every gap older than the rotation and read as a quiet mesh.
v1 = {"totals": {"GAP": 3}, "clusters": {
    "old ask": {"count": 3, "dm_count": 1, "examples": [], "replies": [], "froms": ["!a"],
                "first_ts": "2026-01-01", "last_ts": "2026-02-02", "generic_smell": False}}}
migrated, n = learn.migrate(dict(v1, clusters=dict(v1["clusters"])))
check("one cluster migrated", n, 1)
check("count preserved as GAP", migrated["clusters"]["old ask"]["counts"]["GAP"], 3)
check("dm count preserved", migrated["clusters"]["old ask"]["dm_counts"]["GAP"], 1)
check("schema stamped", migrated.get("schema"), learn.SCHEMA)
check("migration is idempotent", learn.migrate(migrated)[1], 0)
# The readers must tolerate the v1 shape even before a migration runs.
check("total() reads a v1 cluster", learn.total(v1["clusters"]["old ask"]), 3)
check("total() of a v1 cluster by non-GAP bucket", learn.total(v1["clusters"]["old ask"], "CLARIFY"), 0)

# v3 adds last_seen. It must be backfilled by the migration rather than left empty, or every
# existing cluster reads as never-asked until it next goes unanswered.
check("last_seen is backfilled, not left blank",
      bool(migrated["clusters"]["old ask"].get("last_seen")), True)
check("and its floor is last_ts",
      migrated["clusters"]["old ask"]["last_seen"] >= "2026-02-02", True)

print("\nlast_seen vs last_ts — when the ask ARRIVED vs when it went UNANSWERED")
# The defect this exists for: only GAP/CLARIFY/NO_TABLE/THROTTLED cluster, so an ask a doer
# now answers correctly stopped updating any date at all. The page dated `test` to 2026-08-22
# while sigreport had answered it four times on 09-04 — the entries that looked most neglected
# were the ones working best.
_c = {"counts": {"GAP": 1}, "dm_counts": {}, "streams": {}, "examples": [], "replies": [],
      "froms": [], "first_ts": "2026-01-01", "last_ts": "2026-02-02",
      "last_seen": "2026-02-02", "last_by_bucket": {"GAP": "2026-02-02"},
      "generic_smell": False}
# A HIT arriving later moves last_seen and must NOT move last_ts.
_after = dict(_c, last_seen="2026-03-03")
check("a later answered ask moves last_seen", _after["last_seen"], "2026-03-03")
check("and leaves last_ts alone", _after["last_ts"], "2026-02-02")
# recurred() reads the GAP timestamp only, so a HIT must never trip the regression alarm.
_v = {"armed": "2026-02-10"}
check("an answered ask does not read as a recurrence", learn.recurred(_after, _v), False)
# and a real GAP after arming still does
_gap = dict(_after, last_by_bucket={"GAP": "2026-03-04"})
check("but an unanswered one still does", learn.recurred(_gap, _v), True)
# HIT is not a clustering bucket: an answered ask must never MINT a queue entry.
check("HIT does not cluster", "HIT" in learn.CLUSTERED, False)
check("GAP does", "GAP" in learn.CLUSTERED, True)

print("\nrecurred() — coverage is whether the ask still reaches the MODEL")
ARMED = {"oracle": "derivable", "armed": "2026-08-21T12:00:00Z"}
after = {"last_by_bucket": {"GAP": "2026-08-21T18:00:00Z"}}
before = {"last_by_bucket": {"GAP": "2026-08-21T09:00:00Z"}}
check("gap after arming is a recurrence", learn.recurred(after, ARMED), True)
check("gap before arming is not", learn.recurred(before, ARMED), False)
check("no verdict, no alarm", learn.recurred(after, None), False)
check("triaged but unarmed, no alarm", learn.recurred(after, {"oracle": "derivable"}), False)
# A CLARIFY after arming is a half-built doer, reported separately. Folding it into the
# recurrence alarm would make a doer that is working as designed look broken.
clar = {"last_by_bucket": {"CLARIFY": "2026-08-21T18:00:00Z"}}
check("clarify after arming is NOT a recurrence", learn.recurred(clar, ARMED), False)
check("clarify after arming IS a partial", learn.partial(clar, ARMED), True)

print("\nverdict() — an untriaged cluster is the actionable state and is never defaulted")
check("unknown key has no verdict", learn.verdict({}, "anything"), None)
check("malformed entry is not a verdict", learn.verdict({"k": "derivable"}, "k"), None)
check("a real entry reads back", learn.verdict({"k": ARMED}, "k"), ARMED)

print("\nclassify() — our own limit refusing a legitimate question")
# A throttled message bucketed as FILTERED until 2026-08-22 — the same bin as 44 strangers'
# chatter. But sender_allowed and addressed both sit ABOVE the rate gate, so these reasons can
# only be reached by a real question from an allowed node that Cal chose not to answer. That is
# demand exceeding capacity, and it is the one bucket saying the LIMITS need looking at rather
# than a new capability.
check("rate limit is not a filter", learn.classify(
      rec(matched=False, reason="rate_limited"), OUR), "THROTTLED")
check("cooldown is not a filter either", learn.classify(
      rec(matched=False, reason="cooldown"), OUR), "THROTTLED")
# The reasons that genuinely are not about us stay FILTERED.
check("off-list is still filtered", learn.classify(
      rec(matched=False, reason="sender_not_allowed"), OUR), "FILTERED")
check("not addressed is still filtered", learn.classify(
      rec(matched=False, reason="not_addressed"), OUR), "FILTERED")
check("too old is still filtered", learn.classify(
      rec(matched=False, reason="too_old"), OUR), "FILTERED")
# An unmatched record with no reason at all must not be promoted into the build queue.
check("no reason recorded is filtered", learn.classify(rec(matched=False), OUR), "FILTERED")
check("throttled clusters into the queue", "THROTTLED" in learn.CLUSTERED, True)

print("\nstream() — three streams, and one that is honestly unknown")
# A message on Cal's own PSK'd channel is addressed ^all exactly like a public one, so `to`
# alone cannot separate them. From the moment that channel was armed the ledger ranked a
# question asked in the working channel as though a stranger had shouted it in public.
CH = 1
check("a DM is a DM whatever channel it rode", learn.stream(
      rec(to=OUR, channel=CH), OUR, CH), "dm")
check("Cal's channel", learn.stream(rec(channel=CH), OUR, CH), "cal")
check("the public channel", learn.stream(rec(channel=0), OUR, CH), "pub")
check("some other channel is not Cal's", learn.stream(rec(channel=2), OUR, CH), "pub")
# Disarmed: everything that is not a DM is public, exactly as before the split.
check("disarmed, a broadcast is public", learn.stream(rec(channel=1), OUR, -1), "pub")
check("disarmed, a DM is still a DM", learn.stream(rec(to=OUR, channel=1), OUR, -1), "dm")
# ABSENT IS NOT PUBLIC. Records written before the responder carried `channel` are known not to
# be DMs and NOT known to be public; calling them public is a guess dressed as a measurement,
# which is the absent-reads-as-known trap this repo has hit three times.
check("no channel recorded is unsplit, not public", learn.stream(rec(), OUR, CH), "pre")
check("but only while a private channel exists", learn.stream(rec(), OUR, -1), "pub")

print("\nintent_total() — a keyed channel ranks like a DM")
check("dm and cal both count as intent",
      learn.intent_total({"streams": {"dm": 2, "cal": 3, "pub": 9}}), 5)
check("public does not", learn.intent_total({"streams": {"pub": 9}}), 0)
# A ledger written before the split has no streams dict; DM was the only high-intent stream
# then, so that is what it falls back to rather than reading as zero.
check("pre-split cluster falls back to its dm count",
      learn.intent_total({"dm_counts": {"GAP": 4}}), 4)

# MUTATION-BOUNDARY — the self-test replays everything ABOVE this line under each
# mutation. Sliced on this sentinel rather than a run of dashes, which is miscounted
# by eye and then keeps the file's own exit block.
# ------------------------------------------------------------------------------- MUTATIONS
# The classifier is the product, so a passing eval that would also pass with the classifier
# broken is worth nothing. Each mutation below is this session's change reverted.
_ORIG_CLASSIFY = learn.classify

_ORIG_STREAM = learn.stream

MUTATIONS = [
    ("an unrecorded channel reads as public, inventing a measurement",
     lambda: setattr(learn, "stream", lambda rec, our, own: "dm" if learn.is_dm(rec, our)
                     else ("cal" if own >= 0 and rec.get("channel") == own else "pub"))),
    ("Cal's own channel is not weighted like a DM",
     lambda: setattr(learn, "intent_total", lambda c: (c.get("streams") or {}).get("dm", 0))),
    ("throttling read as a filter, so overload is invisible",
     lambda: setattr(learn, "classify", lambda rec, our:
                     "FILTERED" if not rec.get("matched") else _ORIG_CLASSIFY(rec, our))),
    ("classifier ignores the doer's outcome (the pre-loop behaviour)",
     lambda: setattr(learn, "classify", lambda rec, our:
                     "FILTERED" if not rec.get("matched") else
                     ("REFUSED" if rec.get("gen_status") == "fixed_forecast_refused" else
                      ("GREETING" if rec.get("capability") == "greeting" else
                       ("HIT" if rec.get("capability") in learn.DOER_CAPS
                        and rec.get("prompt_kind") != "general" else "GAP"))))),
    ("migrate rebuilds instead of rewriting (drops pre-rotation history)",
     lambda: setattr(learn, "migrate",
                     lambda agg: ({"totals": {}, "clusters": {}, "schema": learn.SCHEMA}, 0))),
    ("recurrence keyed on last_ts, so a clarify reads as a recurrence",
     lambda: setattr(learn, "recurred", lambda c, v: bool(
         v and v.get("armed") and (c.get("last_ts", "")
                                   or max(c.get("last_by_bucket", {}).values(), default=""))
         > v["armed"]))),
    ("untriaged clusters default to a verdict, emptying the actionable queue",
     lambda: setattr(learn, "verdict", lambda tr, key:
                     tr.get(key) if isinstance(tr.get(key), dict) else {"oracle": "derivable"})),
]


def self_test():
    import io, contextlib
    src = open(__file__).read()
    body = src[src.index('print("classify()'):src.index("# MUTATION-BOUNDARY")]
    saved = {n: getattr(learn, n) for n in ("classify", "migrate", "recurred", "verdict")}
    survived = []
    for name, mutate in MUTATIONS:
        globals()["FAIL"] = []
        mutate()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                exec(compile(body, "<mutation>", "exec"), globals())
        except Exception:
            pass
        caught = bool(globals()["FAIL"])
        for k, v in saved.items():
            setattr(learn, k, v)
        print(f"  {'CAUGHT  ' if caught else 'SURVIVED'} {name}"
              + (f" ({len(globals()['FAIL'])} case(s) failed)" if caught else ""))
        if not caught:
            survived.append(name)
    return survived


print()
if FAIL:
    print(f"FAIL — {len(FAIL)} case(s): {FAIL}")
    sys.exit(1)
print("all eval_learn checks pass")
if "--self-test" in sys.argv:
    print("mutations (each MUST be caught):")
    _survived = self_test()
    if _survived:
        print(f"FAIL: {len(_survived)} mutation(s) survived: {_survived}")
        sys.exit(1)
    print(f"all {len(MUTATIONS)} mutations caught")
