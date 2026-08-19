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

print()
if FAIL:
    print(f"FAIL — {len(FAIL)} case(s): {FAIL}")
    sys.exit(1)
print("all eval_learn checks pass")
