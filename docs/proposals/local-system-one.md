# Proposal — a System One model that runs in the house

**Can the routing second opinion run on jlab instead of TypeSafe, and is it worth arming at all?**

*Cal · v1 2026-09-23 · re: `github.com/deanssamclaw/cal-mesh` · sits under
[`jev-routing.md`](jev-routing.md), which built the router · runners in
[`tools/local-system-one/`](../../tools/local-system-one) · **nothing armed; no production code
changed by this work***

---

## 0. The question

`jevroute` is merged and OFF. Arming it sends the sanitized text of an addressed fallthrough
message to TypeSafe. That privacy cost is the only reason it is not on, so: can an open model on
our own hardware answer the same question, and how much does the answer cost in quality?

Within 48 hours of Jev's release, 20-30 open reproductions existed. Seven are relevant; five were
measured here.

## 1. Method

The same 319 unique non-reaction inbox messages, the same adjudicated labels, the same scoring as
the built router: act only on a fallthrough, only into weather / caps / sigreport, only at
confidence >= `JEV_MIN_CONF` (0.8), with the same `weather_now` / `other_station` guards and the
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

* **New production code:** a second classifier behind the same flag (`jevroute.classify` grows a
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
* **`JEV_BACKEND=local`** in cal-mesh points `jevroute` at it. No key is read or sent; every gate,
  guard, threshold and fail-open path is the same code as the cloud path.
* **The local prompt is not the cloud prompt.** The measurement was taken with the state as plain
  text and a question that names no state field. Sending the cloud shape to the local scorer moved
  "Cal, hows the link holding up?" from **0.90 to 0.79** — across the floor. `LOCAL_INSTRUCTIONS`
  and `LOCAL_GUARDS` are the measured wording, and the eval asserts they name no state field.
  Guard wording is load-bearing too: rephrasing `other_station` moved one message from 0.33 to
  0.52, across its bar.
* **Measured end to end through the service** (jlab, same messages): the two addressed public
  rescues score 0.90 and 0.88 with their guards at 0.32 and 0.33; the third-party control
  ("signal from the Olathe repeater") is refused by the guard at 0.88; a past-tense weather ask
  stays at 0.54, under the floor. ~13 s for a route plus both guards.

## 4. Decision

**`jevroute` stays OFF, and if it is ever armed it is armed LOCAL-FIRST** — the local backend is
built and measured (§3a) so that arming is one config line rather than a project.

The reasoning is the size of the prize, not the quality of the options. On 44 days of real traffic
the rescue is worth **three messages**, all of them link/signal asks. Cloud and local now buy the
same three. Cloud costs $0.0004 a month and sends message text to a third party; local costs a day
of work, 7 seconds a decision and a dependency on a hot laptop. Neither is worth doing for three
messages **today** — but the moment it is worth doing, the local one has no privacy cost to weigh,
so there is no reason to prefer the cloud path.

Re-open this decision when any of these is true:

* **The invented-signal failure happens again.** One occurrence is recorded in `sigreport.py`; a
  second means the ladder's gap is live, not historical.
* **Addressed traffic grows.** Three fixes in 44 days is 25 addressed public messages. At ten times
  the traffic the same rate is a fix a week.
* **The broadcast widening is wanted.** There Jev is measurably better than the local model, and
  the privacy question changes shape: it is every public message, not a handful of addressed ones.
* **The local runner gets cheap.** Prompt-cache reuse works on jlab for MoE models (7.5 s → 0.39 s
  prefill); it does not for the dense 4B. A cache-friendly scorer would remove the 7 s.

## 5. Not done, and deliberately

* **No fine-tuning.** Our labels partly adopt Jev's answers, and TypeSafe's agreement (§2.3(b))
  forbids using its output to train a model. A local model trained for this job needs a label set
  built without Jev in it.
* **No benchmark publication.** The figures here compare systems on our own traffic for our own
  decision; they are not a public benchmark of Jev.
* **The measurement is small.** 25 addressed public messages, one adjudicator, agreements between
  ladder and model were not audited, and at least one label pair is inconsistent
  ("Testing 35 and paola exit" vs "Testt 151st and 35"). Treat the +2 as a direction, not a rate.
