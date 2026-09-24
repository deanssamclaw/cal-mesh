# Proposal — a System One model that runs in the house

**Can the routing second opinion run on jlab instead of TypeSafe, and is it worth arming at all?**

*Cal · v1 2026-09-23 · re: `github.com/deanssamclaw/cal-mesh` · sits under
[`jev-routing.md`](jev-routing.md), which built the router · runners in
[`tools/local-system-one/`](../../tools/local-system-one) · **nothing armed; no production code
changed by this work***

---

## 0. The question

`s1route` is merged and OFF. Arming it sends the sanitized text of an addressed fallthrough
message to TypeSafe. That privacy cost is the only reason it is not on, so: can an open model on
our own hardware answer the same question, and how much does the answer cost in quality?

Within 48 hours of Jev's release, 20-30 open reproductions existed. Seven are relevant; five were
measured here.

## 1. Method

The same 319 unique non-reaction inbox messages, the same adjudicated labels, the same scoring as
the built router: act only on a fallthrough, only into weather / caps / sigreport, only at
confidence >= `S1_MIN_CONF` (0.8), with the same `weather_now` / `other_station` guards and the
same `sigreport.names_other_node` backstop. Ambiguous adjudications were counted against the
model. All of it ran on **jlab** (i9-9980HK, 16 threads, no GPU, 31 GB), thermally gated: a call
begins only at or below 70 °C, and a watchdog stops everything after five minutes at 99 °C.

Two readouts were compared, because the readout turned out to matter more than the model:

* **Ad-hoc** — put the options in a prompt, read the first answer token's probabilities from
  Ollama's `logprobs`, softmax over the option letters. Cheap; no install.
* **SemIf's own scorer** (`--mode direct`, llama.cpp backend, frozen Qwen3.5-4B Q4_K_M) — scores
  the option sequences natively, then **one temperature fitted per workload**. The temperature was
  fitted on the 263 messages NOT addressed to Cal and applied to the 56 that were, so it is never
  fitted on what it is scored against.

## 2. What the numbers say

Populations are the ones the built router can act on. "+n −m" is fixes and breakages against the
current regex ladder.

| System | Addressed public (n=25) | Addressed incl. private (n=56) | Raw routing | s/decision |
|---|---|---|---|---|
| regex ladder alone | 23 | 47 | — | 0 |
| **SemIf 4B + fitted temperature** | **25 (+2 −0)** | **50 (+3 −0)** | 44/56 · 237/319 | ~7 |
| Qwen3.5-4B, ad-hoc readout | 24 (+1 −0) | 49 (+2 −0) | 50/56 · 217/256 | 11 |
| qwen3:30b (MoE), ad-hoc readout | 23 (+1 **−1**) | 47 (+2 **−2**) | 47/56 | 0.6 |
| decider-0.8b (purpose-trained) | 23 (+0 −0) | 47 (+0 −0) | 43/56 | 16 |
| laya `typed-decisions` (421M) | 23 (+0 −0) | 47 (+0 −0) | 35/56 | 1.4 |
| gpt-oss:20b | unreadable first token without its own chat format | | | |
| **Jev 1.13.0 (TypeSafe, cloud)** | **25 (+2 −0)** | **50 (+3 −0)** | 54/56 · 302/319 | 0.25 |

**A local model equals Jev on everything this router actually does.** Same three fixes, same zero
breakages. Every fix is a link/signal ask — the failure class `sigreport.py` was written for.

**Three findings, each paid for by a run:**

1. **Bigger did not help.** The 30B has more knowledge and was the only system to break anything.
   In raw-completion mode as little as 34% of its probability lands on the option letters, and
   normalising over the letters inflates confidence past the floor. The failure was the readout.
2. **Smaller did not help.** Both purpose-trained small models were more accurate than random and
   never confident enough to act: decider-0.8b is right 77% of the time at a mean confidence of
   0.67. At a 0.8 floor they contribute nothing. Laya's own README is explicit that its headline
   number is the checkpoint fine-tuned on that benchmark's split, and that the base checkpoints are
   near chance zero-shot; measured here on our traffic, they are.
3. **Calibration was the whole lever.** SemIf picks *fewer* routes correctly than the ad-hoc
   readout (44 vs 50 of 56) and is the only local system that matches Jev where it counts, because
   its confident tail is clean. ECE 0.042 against 0.052 and 0.107 and 0.174 for the others.

**Where Jev is still clearly better:** raw routing (302/319 vs 237) and the un-addressed channel
chatter — on all 319 as if everything were eligible, Jev fixes 9 and the local model 3. That gap
only matters if the broadcast widening in `jev-routing.md` §2 is ever built.

## 3. What arming locally would cost

* **New production code:** a second classifier behind the same flag (`s1route.classify` grows a
  backend), a local scorer process on jlab, its config, its evals, and another adversarial review.
  Every guard, gate and fail-open path in `jev-routing.md` §3 stays as it is.
* **A dependency on jlab:** ~7 s per decision on a box that also runs the weather app, reaches
  95-100 °C on any inference, and is not on a UPS. A failure is harmless — the message takes
  today's path — but the rescue is only as available as jlab.
* **11 GB on jlab** (venv, the 4B GGUF, the SemIf checkout) and about a day of work.

## 3a. How it is wired (built 2026-09-23, still OFF)

* **`system_one_server.py` on jlab** (`~/system-one`, a systemd unit, `127.0.0.1:8799`): loads the
  4B GGUF once, serves TypeSafe's request shape, applies the fitted temperature, and answers
  **503 above 95 °C** so a hot laptop degrades instead of cooking. It logs question ids, timings
  and the chosen option — never message text.
* **Reachability is tailnet-only.** The unit binds jlab's `100.x` address: reachable by this
  operator's own devices, not the LAN and not the internet, and there is no auth on the port — the
  tailnet is the boundary. `S1_LOCAL_URL` must be that address, **not** a MagicDNS hostname: on a
  host with Funnel enabled the name resolves to the public relay (`jlab` → a `199.x`), which would
  quietly take the router off the tailnet.
* **`S1_BACKEND=local`** in cal-mesh points `s1route` at it. No key is read or sent; every gate,
  guard, threshold and fail-open path is the same code as the cloud path.
* **The local prompt is not the cloud prompt.** The measurement was taken with the state as plain
  text and a question that names no state field. Sending the cloud shape to the local scorer moved
  "Cal, hows the link holding up?" from **0.90 to 0.79** — across the floor. `LOCAL_INSTRUCTIONS`
  and `LOCAL_GUARDS` are the measured wording, and the eval asserts they name no state field.
  Guard wording is load-bearing too: rephrasing `other_station` moved one message from 0.33 to
  0.52, across its bar.
* **Measured end to end through the running service**, all 56 addressed messages replayed from the
  Mac over the tailnet with s1route's own constants, and scored against the clean label set
  (`tools/clean-labels`): **addressed public 23 → 25 of 25, +2, nothing broken — the same as the
  cloud service**; addressed including private 46 → 48 against the cloud service's 49. None of the
  23 `unsure` labels falls in either population, so the headline rests only on labels the rules
  settled. Median **18.4 s** per message for a route plus both guards, under sustained thermal
  throttling; ~13 s from cold. No 503s during the replay.
* **One difference from the offline run, and it is the guard wording.** With the production
  `other_station` text the DM *"Hows the radio holding up?"* scores **0.52**, just over its 0.5
  bar, and is not rescued; the offline run's wording put it at 0.33. It is a DM, so the default
  build excludes it either way — but **if `S1_PRIVATE_OK` is ever set, that guard's wording
  should be re-measured rather than assumed.** Tuning it now, on the same 56 messages it would be
  scored against, would be fitting the test.

* **A 4xx is not an outage (found by running, 2026-09-23).** SemIf refuses input whose GGUF and
  reference tokenizations disagree — a single `❤️` returns **422** — and the router treated any
  HTTP error as a network failure, so one such message would have silenced it for
  `S1_BACKOFF_S`. A 4xx other than 429 now fails open immediately with `http_<code>` and no
  backoff; 429 still waits, because being rate-limited is a reason to.

## 3b. What a security review found (2026-09-23)

An adversarial reviewer attacked both halves and proved each finding by running it. **The input
validation, the privacy gates, the secret handling and the page escaping all held.** What did not
was availability:

* **Blocker — one unauthenticated request could pin the laptop.** A near-token-limit question
  measured **90 s at 97 °C**, and the 16-question cap allowed roughly **24 minutes** of that in a
  single request. Worse, the thermal check ran **once, at the door**: a request accepted at 58 °C
  ran to completion through 97 °C. Now: at most 4 questions, 4000 characters of state and of each
  instruction, and **the temperature is re-checked between questions** and aborts with 503.
  Measured after the fix: a request accepted at 20 °C with the sensor raised mid-flight returns
  503 after 8.2 s instead of finishing.
* **Slowloris.** The handler had no read timeout, so any tailnet node could hold threads open
  with a body that never arrived — no model load required. A 10 s read timeout closes it;
  measured at exactly 10.0 s.
* **The thermal guard failed OPEN on a sensor error** (`cpu_c()` returned 0). A renamed zone
  would have disabled it silently. It now reads 999 °C on any error, so the service refuses work
  instead. `S1_ZONE` makes the sensor injectable, which is how the abort above is tested.
* **The client followed redirects.** A spoofed scorer answering `302` sent the next request
  wherever it pointed. It failed closed, but it now refuses to follow at all, and a 3xx is
  treated like a 4xx: fail open, no backoff.
* Not a finding in the end: `InaccessiblePaths` for the credentials directory was added and
  removed — that path does not exist on jlab (the key lives on the Mac) and the directive failed
  the unit with `226/NAMESPACE`.

**What held, and is worth knowing:** the service binds only the tailnet address; SemIf refuses
any row over 4096 tokens, which is what bounds per-question cost; no message text is logged; a
500 returns only an exception class name; the systemd hardening is live on the running process;
the privacy gates (`unlocked`, `flagged`, `private_traffic`) cannot be reopened by
`S1_PRIVATE_OK`; the key is never read on the local path; and every attacker-influenceable
`s1_route` field on the page goes through `esc()`.

## 4. Decision — superseded 2026-09-24: ARMED, locally

**`s1route` is ARMED on the local backend** (Dean's call, 2026-09-24). `S1_BACKEND=local`,
`S1_ROUTE_ENABLED=true`, pointed at the scorer's tailnet address; `S1_PRIVATE_OK` stays false, so
DMs and Cal's own channel are still excluded. Config backed up as
`config.bak-pre-s1-arm-2026-09-24`.

*What this document said before, and why it changed:* the recommendation was to stay OFF, because
three fixes in 44 days was not worth either a third party or a day of work. The local backend then
removed the privacy cost entirely — the remaining price is ~13 s on a message that was already
going to take longer than that, on a box we own. At that price the same three fixes are worth
having, and the operator made that call. The measurements in §1 and §2 are unchanged; only the
verdict is.

**Measured on the armed path, 2026-09-24:** *"Cal, hows the link holding up?"* → `sigreport`
at **0.899**, guard clear, all nine sigreport gates run, reply on air
`Copy: direct, RSSI -40, SNR 6.5` — the radio's own numbers, where the model previously wrote
"Link's solid and steady over here" with nothing behind it. A conversational message on the same
path (*"what do you think of the new antenna"*) routes to `conversation` at 0.91 and is left to
the model, unchanged.

**Arming found one defect immediately, in the thermal hardening rather than the router:** the
in-flight abort was set at the door threshold (95 °C), and one question takes this laptop from
40 °C to ~98 °C, so nearly every real request aborted and the router failed open on every
message — armed and useless. The two thresholds now answer different questions: `S1_BUSY_TEMP_C`
(95 °C) refuses to *start*, `S1_ABORT_TEMP_C` (99 °C) is the last-resort in-flight stop, and what
bounds a request's cost is the caps, not the abort. `jlab-ops d588e62`.

**Still off, deliberately:** `S1_PRIVATE_OK` (DMs and Cal's channel — one extra fix in the corpus,
and the `other_station` guard wording wants re-measuring first), and the broadcast widening, which
[`not-everything-earns-a-reply.md`](not-everything-earns-a-reply.md) shelves on its own numbers.

**Re-open the decision if:** a harmful reply reaches the air on this path; the scorer's
availability turns the fallthrough into a regular 13 s wait; or a week of live traffic disagrees
with the corpus — `S1_MIN_CONF` was measured on 56 messages, and live traffic is not the corpus.

## 5. Not done, and deliberately

* **No fine-tuning.** Our labels partly adopt Jev's answers, and TypeSafe's agreement (§2.3(b))
  forbids using its output to train a model. A local model trained for this job needs a label set
  built without Jev in it.
* **No benchmark publication.** The figures here compare systems on our own traffic for our own
  decision; they are not a public benchmark of Jev.
* **The measurement is small.** 25 addressed public messages, one adjudicator, agreements between
  ladder and model were not audited, and at least one label pair is inconsistent
  ("Testing 35 and paola exit" vs "Testt 151st and 35"). Treat the +2 as a direction, not a rate.
