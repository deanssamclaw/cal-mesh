# Second-opinion routing — the record

*How s1route was decided, measured and armed. To build one, follow [router.md](router.md); this is the history behind it. Moved out of the README on 2026-09-24, unchanged apart from link paths.*

## Second-opinion routing (s1route, ARMED 2026-09-24)

> **To build this yourself, start at [`docs/router.md`](router.md)** — the runbook, from a
> radio and a spare Linux box to an armed router with a cloud backup. This section is the record
> of how it was decided; the runbook is how to do it.

*Named for the model class, not a vendor: what runs locally is a frozen Qwen3.5-4B scored by
SemIf. It was `jevroute`/`JEV_*` until 2026-09-23, after the cloud service it was first built
against.*

Every doer claims a message with a regex; what no regex claims falls to the model. `s1route.py`
asks one more question on **that fallthrough only** — *which service should answer this?* — of
**Jev** (`jev-1.13.0`, TypeSafe AI), a decision model that writes no text and computes nothing. It
can only send a message to a doer that already exists, and that doer keeps every refusal it had.

- **Asked only** when an addressed message reached the fallthrough. **Private traffic stays home
  by default**: DMs and Cal's own channel are excluded unless `S1_PRIVATE_OK=true`; unlocked DMs
  and sanitizer-flagged messages are excluded always. A message the ladder answers never leaves.
- **Acts only** into weather, caps or sigreport, at `S1_MIN_CONF` (0.8) or above, and only when
  a same-call guard agrees: weather must be about **now** (past-tense asks were getting the current
  reading), sigreport must be about **this** link and name no other node. Never calc, never
  greeting. Any failure = today's behaviour; the timeout bounds the whole call, with a backoff.
- **Measured** on all 319 unique inbox messages, this configuration, three calls each: default
  build **23 → 25 of 25** addressed public messages, **0 broken**; with DMs and Cal's channel,
  47 → 50 of 56; across all 319 as if eligible, 245 → 254. Small on purpose — every fix is a
  link/signal ask the model used to answer with no number behind it. A wider hybrid (greetings,
  un-addressed chatter) scored far higher and is **not built**; that is the operator's call. The
  first write-up of this quoted the wider hybrid's numbers for this build; an adversarial review
  caught it. Full detail: [`docs/proposals/jev-routing.md`](proposals/jev-routing.md).
- **A local model now buys the same three fixes.** SemIf's scorer on a frozen Qwen3.5-4B on jlab,
  with one fitted temperature, makes the same rescues as Jev at the floor (25/25 addressed public,
  +2, 0 broken; the live end-to-end replay gave 48 of 56 incl. private vs Jev's 49) at ~13 s a decision cold (~18 s under sustained throttling) with nothing leaving the house. Of the five local setups tried it
  was the only one with no break at the floor; the difference was SemIf's option scoring, not the
  temperature and not size (corrected 2026-09-24 — and "matches Jev" was measured on labels that
  side with Jev on every disputed message; see the review section of the proposal). **ARMED 2026-09-24 on the local backend** (it stayed off until the local path removed the privacy cost) — with the triggers that would re-open that in
  [`docs/proposals/local-system-one.md`](proposals/local-system-one.md). Runners:
  [`tools/local-system-one/`](../tools/local-system-one).
- **Two backends, one decision.** `S1_BACKEND=local` asks `system_one_server.py` on jlab and
  **no message text leaves the house** — no key is sent, and the local path uses its own measured
  prompt wording (the cloud shape costs it 0.11 of confidence, which crosses the floor).
  `S1_BACKEND=typesafe` asks the cloud service (Jev) instead. **Armed 2026-09-24 on the local
  backend**; `S1_PRIVATE_OK` stays false, so DMs and Cal's own channel are still excluded.
- **Jev as the backup (`S1_FALLBACK=typesafe`, built 2026-09-24, OFF by default).** When the local
  scorer cannot answer — hot, busy, down, timed out, malformed, or sitting out a backoff — a
  PUBLIC message is asked of Jev instead, through the same floor, guards and walls, and the page
  says it left the house and why. A DM or Cal's channel never goes to the backup, whatever
  `S1_PRIVATE_OK` says; the two scorers keep separate failure state. If jlab is chronically
  slow rather than down, Jev effectively becomes the router: after one timeout the house sits out
  `S1_BACKOFF_S` (300 s) and every public ask in that window goes to the cloud, with one 30 s
  probe of the house per window. Config: `S1_ROUTE_ENABLED`,
  `S1_BACKEND`, `S1_FALLBACK`, `S1_LOCAL_URL`, `S1_LOCAL_TIMEOUT_S`, `S1_BUSY_BACKOFF_S`,
  `S1_MIN_CONF`, `S1_TIMEOUT_S`, `S1_BACKOFF_S`, `S1_REJECTS_MAX`, `S1_PRIVATE_OK`, `S1_KEY_FILE`.
- The decision is on the page: the trace draws a **routed on our own hardware** stage (or
  **routed by Jev** if the cloud backend is ever used), names the model that chose, and a routed
  weather reply no longer claims "plain word matching, no model involved".
- **The eval runs with no radio and no network.** Current corpus: calc 273 checks, sun/moon 873,
  greeting 91, DM 71 + 45, render 74, routing 21, s1route 103 + 11 mutants, plus a page parser. Numbers only mean something
  where they are pinned to an outside source — sun/moon is measured against **43 U.S. Naval
  Observatory times, worst error 43 seconds**; the RF pack against published worked values.
- **Mutation decides whether a check is real.** Break the code deliberately and the eval must go
  red. A check that survives its own bug is decoration, and several here were: an invariant that
  claimed to cover "every handler at once" and was instantiated only on the ones already correct;
  an assertion sitting after a `continue` that skipped the only case it could fail on; one that was
  constant-true by operator precedence. All found by mutation, none by reading.
  Two things make the mutation check itself decoration, and both were live here: a mutant that
  **crashes** — the child died on an import before reaching a single check and the non-zero exit
  was read as a catch — and a mutant that **is not a defect**, where a rule guarded in two places
  was mutated in one and behaved identically. So require the suite to have run and reported its
  own failures, and mutate every copy of the rule.
- **The review is adversarial and must execute.** A reviewer told to *refute* and to *run it* finds
  what a reviewer told to *check* does not. Five rounds on the sun/moon tier found real defects
  every time — including three live bugs in already-armed capabilities that had nothing to do with
  the new one.
- **Refusals are shipped deliberately**, and the eval asserts them as hard as it asserts answers.

Reviews are recorded in commit messages rather than summarised away, including the ones that
refuted the design being reviewed.
