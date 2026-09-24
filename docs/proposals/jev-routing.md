# Proposal — a second opinion on routing (s1route)

> **Renamed 2026-09-23.** The module was `jevroute.py` with `JEV_*` config keys, after the
> cloud service it was first built against. What actually runs is a frozen Qwen3.5-4B scored by
> SemIf on our own hardware, so it is now `s1route.py` with `S1_*` keys — "System One" being the
> model class, not a vendor. The cloud backend still exists as `S1_BACKEND=typesafe`.
>
> **Status 2026-09-24: ARMED** on the local scorer ([`local-system-one.md`](local-system-one.md)),
> and Jev is switched on as the **backup** (`S1_FALLBACK=typesafe`): asked only when the local
> scorer cannot answer, and only for public-channel messages. Build it yourself:
> [`docs/router.md`](../router.md). What follows is the record as of 2026-09-21.

**What happens to a message no word rule claims, before it reaches the model**

*Cal · v2 2026-09-21 (v1 same day, corrected after adversarial review — §6) · re:
`github.com/deanssamclaw/cal-mesh` · built, **default OFF**, not armed · code: `s1route.py`,
`responder.py:plan_s1_rescue` · evals: `eval_s1route.py`, `eval_render.py`, `eval_anatomy.py`*

---

## 0. What this is

Every doer claims a message with a regex. A message that no regex claims falls to the language
model, which is the one component here that can invent. Widening the regexes is the fix that has
broken something every round it was tried.

This adds one question, asked **only on that fallthrough**: *which service should answer this?* It
is answered by **Jev**, a "System One" decision model from TypeSafe AI. Jev does not write text and
computes nothing. It picks one option from a fixed list and returns a probability for each. So the
most it can ever do is send a message to a doer that already exists — and that doer still owns the
answer, with every refusal it already had.

## 1. What it does, measured

Every unique non-reaction message in the inbox on 2026-09-21 — **319** — was run through the live
ladder (`plan_response`, `sigreport.match`, `is_bare_greeting`; no network, no model) and, for
this exact configuration, through `s1route.classify` **three times each** (957 calls, 0 errors).
Disagreements were adjudicated against each doer's **documented scope**; every ambiguous case was
counted **against** Jev; agreements were not audited.

| Population | Ladder alone | Ladder + s1route (all 3 runs) |
|---|---|---|
| **Addressed, public channel — the default** (n=25) | 23 | **25 — +2, 0 broken** |
| Addressed incl. DMs + Cal's channel (`S1_PRIVATE_OK=true`, n=56) | 47 | 50 — +3, 0 broken |
| All 319, as if every message were eligible | 245 | 254 — +9, 0 broken |

**Review 2026-09-24 — read these as smaller than they look.** The labels were adjudicated after
seeing Jev's answers, and on every addressed message where the ladder and Jev disagree they side
with Jev; relabelling alone turns the all-319 row from +9/0 broken into **+6/−3**. The default row
rests on **7 messages that actually fall through**, 2 of them fixes (one sender, 08-08 and 08-09);
0 broken of 5 is consistent with a false-act rate up to ~50%. The 0.8 floor and the option wording
were chosen on the same messages. The ladder misroutes 9 of the 56 addressed messages (10 on the
clean labels), not 4 as one summary said. The first week of live traffic is the held-out test.

*Addressed* = a DM, the trigger word, or Cal's own channel — the responder's `addressed` gate.
(Counting only DMs and the trigger word gives 44; the difference is Cal's channel.)

No message flipped between act and no-act across the three calls. Latency median ~240 ms; the
whole 957-call measurement cost about $0.03.

**Every fix is a link/signal ask** — "Cal, hows the link holding up?" and its kin — the exact ask
`sigreport.py` records the model answering "Link's solid" with no number behind it. That is the
whole measured value of the default build: small, and on the failure class this repo exists to
prevent.

## 2. What was deliberately not built

An earlier hybrid that also routed **greetings** and **un-addressed channel chatter** scored far
higher on the same data (287–293 / 319). 44 of its 48 fixes were messages nobody addressed to Cal —
"Good morning from ___" and other stations' "Test ___" range tests. Answering those is a different
decision: airtime on a shared channel, and sending **every** public message to a third party to
find out. It is the operator's to make and is not made here.

Also measured, and worth saying plainly: the ten weather phrasings that motivated this ("is it
raining", "how cold is it") appear **zero** times in the real inbox. They are a known gap, not an
observed one.

## 3. How it is built

`plan_s1_rescue()` runs in the main loop right after `plan_response()`, for messages that already
passed the addressed/allowed/rate gates.

1. **Asked only on the fallthrough** (`s1route.eligible`): no doer claimed it and it is about to
   be generated. A message the ladder answers never leaves the machine.
2. **Private traffic stays home by default.** DMs and Cal's own channel are excluded unless
   `S1_PRIVATE_OK=true`; unlocked DMs and sanitizer-flagged messages are excluded always. Only the
   sanitized text is sent.
3. **Rescues only into a doer that can still refuse** (`RESCUABLE` = weather, caps, sigreport).
   Weather re-runs its own branch (forecast still refused, "Can't reach weather" still the
   fail-safe); caps is still composed from the flags; sigreport still needs measurements, a quiet
   channel, the per-sender cooldown and the daily budget. **Not** calc (a failed parse means
   nothing to compute), **not** greeting (a policy change).
4. **Two guards, asked in the same call** (they cost input tokens, not latency):
   `weather_now` — the weather rescue needs it ≥ the floor (past and future asks score ~0.02);
   `other_station` — the sigreport rescue needs it < 0.5 (third-party asks 0.93–0.97, self asks
   ≤ 0.23), **and** `sigreport.names_other_node` must find no node id, short name or callsign in
   the raw text, as a literal backstop.
5. **Fail open to today.** Timeout, HTTP error, malformed or wrongly-typed body, unknown route,
   missing key: the message takes exactly the path it takes now. `S1_TIMEOUT_S` bounds the
   **whole** request on the wall clock; a network failure backs off for `S1_BACKOFF_S`.
6. **Pinned model.** `jev-1.13.0`, not `jev-latest` — the alias moves on release.
7. **The key never travels.** Read per call from `S1_KEY_FILE`; in no trace, log or return.

The decision is written to the trace as `s1_route` (route, confidence, both guards, model id,
acted or why not, and **which doer actually answered** — the weather branch can hand a message to
calc) and drawn on the page, the anatomy view and the console. None of them may say "no model ran"
of a reply a decision model routed; the evals forbid it.

## 4. What arming costs

- **Privacy:** the sanitized text of an addressed, public, non-flagged message that would
  otherwise go to the model is also sent to TypeSafe (`api.typesafe.ai`). With
  `S1_PRIVATE_OK=true`, DMs and Cal's channel too. TypeSafe states it does not train on customer
  data; zero data retention is an enterprise-plan feature, not the default.
- **A network dependency** on the fallthrough: ≤ `S1_TIMEOUT_S` on a path whose next step is a
  model call measured in seconds; a failure costs one timeout per backoff window.
- **Drafts diverge:** `drafts.py` simulations do not consult Jev, so once armed a simulated reply
  can differ from the live one on exactly these messages.

## 5. Before arming (the repo's gate)

- [x] default OFF
- [x] offline eval — `eval_s1route` 103 checks + 11 in-process mutants; **24 corpus mutants, all caught** (§6)
- [x] independent adversarial review that executes — done 2026-09-21, findings in §6, all fixed
- [x] operator's call on the privacy cost (§4) — resolved 2026-09-24 by arming on the LOCAL
  scorer (no text leaves the house); Jev became the public-only backup the same day.
  `S1_PRIVATE_OK` stays false.
- [x] re-measure at arm time — the end-to-end replay through the running local service
  (local-system-one.md §2); `S1_MIN_CONF` unchanged at 0.8

## 6. The review, and what it changed

An adversarial reviewer, told to refute and to run it, found two blockers and seven should-fixes
in v1. All are fixed; the ones that changed the design:

- **A forced sigreport answered questions about other stations with the sender's own numbers**
  ("how is the signal from the Olathe repeater" → a real reading of the wrong link). → the
  `other_station` guard and the node-name backstop.
- **The pages still described a mechanism that did not run** — shape-matched, "no model ran" —
  for Jev-routed replies. → every flow panel, the anatomy view and the console tile corrected;
  the reviewer's strings are now forbidden by `eval_render`.
- **Past-tense weather asks were routed to weather** and would have been answered with the
  current reading. → the `weather_now` guard.
- **The 2 s timeout bounded nothing** — a trickling server held a call for 40 s. → wall-clock
  deadline + backoff.
- **Malformed answers could crash the loop and drop the message.** → all validation inside the
  failure path; booleans, strings and non-finite numbers rejected.
- **The privacy claim was broader than the code** (locked DMs and Cal's channel were sent). →
  `S1_PRIVATE_OK`, default off.
- **v1's numbers described the wider hybrid, not the build.** → §1 re-measured on the build.
- **Evals missed most of the new code** (11 of 17 mutants survived). → the main-loop send was
  extracted and driven; 24 mutants — the reviewer's 17 ported plus 7 for the new guards — were
  re-run against the whole corpus and all 24 are caught (one, an unescaped route on the
  "declined" line, survived the first sweep and got its own render case).

Pre-existing, found by the reviewer, not changed here: `sigreport._CONTACT_OTHER_NODE`'s callsign
branch runs on lower-cased text and can never match (`names_other_node` works on the raw text for
this reason); and the regex weather path shares the past-tense gap.

## 7. Open, and the operator's

1. **Arming**, and the privacy cost in §4.
2. **`S1_PRIVATE_OK`** — one more fix in the corpus (a DM link ask) for sending DMs and Cal's
   channel to a third party.
3. **The broadcast widening (§2)** — large measured win, large airtime and privacy cost. Not built.
