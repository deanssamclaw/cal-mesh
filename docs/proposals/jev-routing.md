# Proposal — a second opinion on routing (jevroute)

**What happens to a message no word rule claims, before it reaches the model**

*Cal · v1 2026-09-21 · re: `github.com/deanssamclaw/cal-mesh` · built, **default OFF**, not armed ·
code: `jevroute.py`, `responder.py:plan_jev_rescue` · eval: `eval_jevroute.py`*

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

## 1. Measured before it was built

Every unique non-reaction message in the inbox on 2026-09-21 — **319** of them — was run through
the live ladder (`plan_response`, `sigreport.match`, `is_bare_greeting`; no network, no model) and
through Jev (`jev-1.13.0`, one call each). Every disagreement was adjudicated against each doer's
**documented scope**, and every ambiguous case was counted **against** Jev. Agreements were not
audited.

| Router | Right |
|---|---|
| Current regex ladder | 245 / 319 |
| Jev alone | 306 / 319 |
| Ladder first, Jev on the fallthrough, acting at confidence ≥ 0.7 | 293 / 319 — **fixes 48, breaks 0** |
| same, at ≥ 0.8 | 287 / 319 — fixes 42, breaks 0 |

Latency: median **233 ms**, p95 311 ms. Cost of all 319: **$0.009**.

Where Jev was wrong it was mostly **uncertain**: every Jev error in the second run sat below 0.65
confidence, except "Got you!"-shaped contact reports (0.81–0.87), which the ladder already claims
first and Jev is therefore never asked about.

**The first run had an instrument error, and it was mine:** the calc option omitted torque,
concrete and acreage, which calc handles, so Jev sent two torque questions to conversation. The
option text was corrected from each doer's code scope (calc `HANDLERS`, the greeting table, the
weather trigger's own ask-shape) and re-run. The greeting and weather wording was tightened after
seeing run 1's errors, so run 2 is somewhat optimistic on those two; the calc fix is not.

## 2. The finding that shaped what was built

**44 of the 48 fixes were channel chatter nobody addressed to Cal** — "Good morning from ___"
greetings and "Test ___" range tests from other stations. Among the **56** messages that *were*
addressed to Cal, the ladder misrouted **4**, and Jev fixed all four with nothing broken:
three link/signal questions ("Cal, hows the link holding up?" — the exact ask `sigreport.py`
records the model inventing an answer to) and one greeting.

So the headline number is mostly not "Cal answers its own questions better". It is "Cal would
answer many more messages on the open channel". That is a different decision — airtime on a shared
channel, and sending **every** public message to a third party to find out — and it is not made
here. **This build is the addressed-only version.**

Also measured, and worth saying plainly: the ten weather phrasings that motivated this ("is it
raining", "how cold is it") appear **zero** times in the real inbox. They are a known gap, not an
observed one.

## 3. What was built

`plan_jev_rescue()` runs in the main loop right after `plan_response()`, for messages that already
passed the addressed/allowed/rate gates:

1. **Asked only on the fallthrough** (`jevroute.eligible`): no doer claimed it and it is about to be
   generated. A message the ladder answers never leaves the machine.
2. **Never a private DM, never a flagged message.** Jev's own documentation says adversarial text
   can move it; the sanitizer's flag is honoured.
3. **Rescues only into a doer that can still refuse** (`jevroute.RESCUABLE` = weather, caps,
   sigreport). Weather re-runs its own branch (forecast still refused, "Can't reach weather" still
   the fail-safe); caps is still composed from the flags; sigreport still needs measurements, a
   quiet channel, the per-sender cooldown and the daily budget — only the *shape* gate is replaced.
   **Not** calc (a failed parse means nothing to compute), **not** greeting (a policy change).
4. **Fail open to today.** Timeout, HTTP error, malformed body, unknown route, missing key: the
   message takes exactly the path it takes now.
5. **Pinned model.** `jev-1.13.0`, not `jev-latest` — the alias moves on release and the threshold
   was measured on this version.
6. **The key never travels.** Read from `JEV_KEY_FILE` per call; never in a trace, log or return.

The decision is written to the trace as `jev_route` (route, confidence, model id, acted or why not)
and **drawn on the page**. Without that, a Jev-routed weather reply would have been captioned
"plain word matching, no model involved" — a false statement about the mechanism.

## 4. What arming costs

- **Privacy:** the sanitized text of an addressed, non-private message that would otherwise go to
  the model is also sent to TypeSafe (`api.typesafe.ai`). TypeSafe states it does not train on
  customer data; zero data retention is an enterprise-plan feature, not the default.
- **A network dependency** on the fallthrough — ~250 ms on a path whose next step is a model call
  measured in seconds, and a failure costs nothing.
- **A rate limit** TypeSafe says is adjusting dynamically (1,200 req/min at time of writing).

## 5. Before arming (the repo's gate)

- [x] default OFF
- [x] offline eval — `eval_jevroute.py`, 52 checks + 5 in-process mutants, all caught; corpus
      unchanged otherwise (the one red suite, `eval_sigreport`, is red on `main` too)
- [ ] **independent adversarial review that executes** — owed
- [ ] operator's call on the privacy cost above
- [ ] re-measure on the inbox at arm time; move `JEV_MIN_CONF` only on a re-measurement

## 6. Open, and the operator's

1. **The broadcast widening (§2).** Answer greetings and range tests that were not addressed to Cal.
   Measured win is large; so is the airtime and the data leaving the house. Not built.
2. **Greeting as a rescuable route** for addressed messages. One case in the corpus; the model
   already answers an addressed hello.
