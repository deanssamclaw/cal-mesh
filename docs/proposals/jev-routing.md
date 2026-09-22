# Proposal — a second opinion on routing (jevroute)

**What happens to a message no word rule claims, before it reaches the model**

*Cal · v2 2026-09-21 (v1 same day, corrected after adversarial review — §6) · re:
`github.com/deanssamclaw/cal-mesh` · built, **default OFF**, not armed · code: `jevroute.py`,
`responder.py:plan_jev_rescue` · evals: `eval_jevroute.py`, `eval_render.py`, `eval_anatomy.py`*

---

## 0. What this is

Every doer claims a message with a regex. A message that no regex claims falls to the language
model, which is the one component here that can invent. Widening the regexes is the fix that has
broken something every round it was tried (README, *How a capability ships*).

This adds one question, asked **only on that fallthrough**: *which service should answer this?* It
is answered by **Jev**, a "System One" decision model from TypeSafe AI. Jev does not write text and
computes nothing. It picks one option from a fixed list and returns a probability for each. So the
most it can ever do is send a message to a doer that already exists — and that doer still owns the
answer, with every refusal it already had.

## 1. What it does, measured

Every unique non-reaction message in the inbox on 2026-09-21 — **319** — was run through the live
ladder (`plan_response`, `sigreport.match`, `is_bare_greeting`; no network, no model) and, for
this exact configuration, through `jevroute.classify` **three times each** (957 calls, 0 errors).
Disagreements were adjudicated against each doer's **documented scope**; every ambiguous case was
counted **against** Jev; agreements were not audited.

| Population | Ladder alone | Ladder + jevroute (all 3 runs) |
|---|---|---|
| **Addressed, public channel — the default** (n=25) | 23 | **25 — +2, 0 broken** |
| Addressed incl. DMs + Cal's channel (`JEV_PRIVATE_OK=true`, n=56) | 47 | 50 — +3, 0 broken |
| All 319, as if every message were eligible | 245 | 254 — +9, 0 broken |

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

`plan_jev_rescue()` runs in the main loop right after `plan_response()`, for messages that already
passed the addressed/allowed/rate gates.

1. **Asked only on the fallthrough** (`jevroute.eligible`): no doer claimed it and it is about to
   be generated. A message the ladder answers never leaves the machine.
2. **Private traffic stays home by default.** DMs and Cal's own channel are excluded unless
   `JEV_PRIVATE_OK=true`; unlocked DMs and sanitizer-flagged messages are excluded always. Only the
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
   missing key: the message takes exactly the path it takes now. `JEV_TIMEOUT_S` bounds the
   **whole** request on the wall clock; a network failure backs off for `JEV_BACKOFF_S`.
6. **Pinned model.** `jev-1.13.0`, not `jev-latest` — the alias moves on release.
7. **The key never travels.** Read per call from `JEV_KEY_FILE`; in no trace, log or return.

The decision is written to the trace as `jev_route` (route, confidence, both guards, model id,
acted or why not, and **which doer actually answered** — the weather branch can hand a message to
calc) and drawn on the page, the anatomy view and the console. None of them may say "no model ran"
of a reply a decision model routed; the evals forbid it.

## 4. What arming costs

- **Privacy:** the sanitized text of an addressed, public, non-flagged message that would
  otherwise go to the model is also sent to TypeSafe (`api.typesafe.ai`). With
  `JEV_PRIVATE_OK=true`, DMs and Cal's channel too. TypeSafe states it does not train on customer
  data; zero data retention is an enterprise-plan feature, not the default.
- **A network dependency** on the fallthrough: ≤ `JEV_TIMEOUT_S` on a path whose next step is a
  model call measured in seconds; a failure costs one timeout per backoff window.
- **Drafts diverge:** `drafts.py` simulations do not consult Jev, so once armed a simulated reply
  can differ from the live one on exactly these messages.

## 5. Before arming (the repo's gate)

- [x] default OFF
- [x] offline eval — `eval_jevroute` 103 checks + 11 in-process mutants; **24 corpus mutants, all caught** (§6)
- [x] independent adversarial review that executes — done 2026-09-21, findings in §6, all fixed
- [ ] operator's call on the privacy cost (§4), and separately on `JEV_PRIVATE_OK`
- [ ] re-measure on the inbox at arm time; move `JEV_MIN_CONF` only on a re-measurement

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
  `JEV_PRIVATE_OK`, default off.
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
2. **`JEV_PRIVATE_OK`** — one more fix in the corpus (a DM link ask) for sending DMs and Cal's
   channel to a third party.
3. **The broadcast widening (§2)** — large measured win, large airtime and privacy cost. Not built.
