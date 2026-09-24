# Build a second-opinion router for a mesh bot

*Runbook for `s1route`, as running on Cal since 2026-09-24. Written so a person or their agent
can go from a Meshtastic radio and a spare Linux box to an armed router with a cloud backup, and
know at every step whether it is safe. The design record behind each decision is in
[`proposals/jev-routing.md`](proposals/jev-routing.md) and
[`proposals/local-system-one.md`](proposals/local-system-one.md).*

---

## The idea in one screen

Cal answers from **capabilities**: small deterministic programs (weather, signal report,
capability list, calc…) that each claim a message with a word rule and answer from real data.
A message no rule claims falls through to a **language model**, the one part of the system that
can make things up. Once, "Cal, hows the link holding up?" matched no rule, and the model answered
"Link's solid and steady over here" while the radio's measured signal numbers sat unused on disk.

The router changes one thing: **on that fallthrough only**, before the model runs, it asks a
**decision model** one typed question: *which of these capabilities should answer this?* The
decision model returns a choice and a probability. It writes nothing and computes nothing. If it
is confident and two checks agree, the message goes to that capability, which still applies every
refusal it has. Otherwise nothing changes.

```
 radio ─▶ bridge ─▶ inbox ─▶ responder: word rules (the ladder)
                                  │ a rule claimed it ─▶ that capability answers (router never asked)
                                  │ nothing claimed it, addressed to Cal
                                  ▼
                       s1route: "which capability should answer this?"
                         ├─ local scorer on your own box (frozen open model, CPU)  ← asked first
                         └─ Jev, TypeSafe's cloud service                          ← backup, public only
                                  │ confident (≥0.8), guards agree, text passes the walls
                                  ├─▶ weather / caps / sigreport answers, with its own checks
                                  └─ otherwise ─▶ the language model, exactly as before
```

**The rules that make this safe.** Each one exists because breaking it broke something.

1. **The model says what kind of message this is; code decides whether to speak.** Asked "should
   Cal answer this at all?", the scorer returned 0.49–0.51 for everything. Whether to speak
   depends on timing and context, which aren't in the text. Cooldowns, budgets and channel
   checks stay in code.
2. **Asked only on the fallthrough.** A word rule that works is never second-guessed.
3. **Rescues only into a capability that can still refuse.** Weather still refuses forecasts,
   and a signal report still needs measurements, a quiet channel and a budget.
4. **Fail open to today.** Any error, timeout or odd answer means the message takes the path it
   would have taken if the router didn't exist.
5. **Walls in code behind every model judgement.** The "is this about another station?" guard
   barely separates on the local scorer (see step 6). So a routed signal report also needs
   **every word of the message to be link-question vocabulary**. A name, place, pronoun or
   station word refuses it. A list of what a third party looks like can never be complete; a
   list of what the safe case is made of can.
6. **Private traffic stays home.** DMs and the bot's own channel are excluded unless you
   separately allow them, and they never go to the cloud backup.
7. **Every decision is on the page.** Each message's public trace names the scorer, where it ran,
   and whether it left the house.

---

## Step 0 — What you need

| Piece | Reference install | Notes |
|---|---|---|
| Meshtastic radio with the TCP API | LilyGO T-Deck, BaseUI (non-TFT) firmware, port 4403 | the touchscreen build does not serve 4403 |
| A host for cal-mesh | a Mac (launchd); Linux works (systemd examples provided) | Python 3.12+ |
| The `claude` CLI | for the model fallthrough | the router works without it; the fallthrough does not |
| A box for the local scorer (optional) | 2019 MacBook Pro running Linux, 16 threads, CPU only | ~3 GB model, ~11 GB total install; ~13 s a decision |
| A private network between them | Tailscale | the scorer has no auth; the tailnet is the boundary |
| A TypeSafe API key (optional) | for the cloud backend or backup | ~$0.00003 a decision (step 8) |

You can skip the scorer box and use Jev alone (`S1_BACKEND=typesafe`), at the cost of sending
addressed message text to a third party. Or use the local scorer alone and never set a key.

## Step 1 — Get cal-mesh running, unarmed

Follow the README's *Run it* and start the services from [`deploy/`](../deploy). Leave
`RESPONDER_ENABLED=false` for a week and read `decisions.jsonl`: what your mesh actually asks, and
what Cal would have said.

**Check:** `./run-evals.sh` ends `GREEN — every suite ran and passed.` A fresh clone is **amber**
(SKIPs) until the bridge has captured traffic and `drafts.py` has run once. Amber there is
expected; FAIL is not. Three suites need `node`.

## Step 2 — Capabilities first

The router can only hand a message to a capability that already exists: `RESCUABLE = ("weather",
"caps", "sigreport")` in `s1route.py`. Arm the ones you want (`WEATHER_ENABLED`, `CAPS_ENABLED`,
`SIGREPORT_ENABLED`) and let them run on their word rules first. The router adds reach to working
capabilities. It can't stand in for a missing one.

The option descriptions the scorer chooses between are `CRITERIA` in `s1route.py`. **Describe each
option from the capability's actual code scope, not from memory.** When the calc description
left out torque, the scorer sent torque questions to "conversation". It reads option text
literally.

## Step 3 — Choose a backend

| | `local` (your box) | `typesafe` (Jev, cloud) |
|---|---|---|
| Latency | ~13 s cold, ~18 s under sustained heat | ~0.25 s |
| Message text | never leaves your network | sent to api.typesafe.ai |
| Cost | electricity; the box runs 95–100 °C per call | ~$0.00003 a decision |
| Raw routing accuracy (our 56 addressed msgs, clean labels) | 43 / 56 | 53 / 56 |
| Where it acts (≥0.8 confidence) | the same rescues as Jev on our traffic | — |
| Failure mode | hot, busy or down → no opinion → today's path | outage, rate limit, credit → today's path |

The reference install runs **local first, Jev as backup** (steps 4–8). The local scorer is less
accurate overall, but on the few messages where it is confident enough to act, it has matched Jev
on our traffic. The evidence is thin (step 6 says how thin).

## Step 4 — Build the local scorer

Follow [`tools/system-one/README.md`](../tools/system-one/README.md). It has the pinned sources
with a sha256, the systemd unit, the thermal guard and the wire format. The two things people get
wrong: **bind the box's tailnet address** (never 0.0.0.0, and never point the client at a MagicDNS
name, which resolves to the public relay when Funnel is on), and **set `S1_ZONE`** to your own
CPU sensor.

**Check:** its README's `curl` checks pass from the cal-mesh host.

## Step 5 — Get a Jev key (for the backup, or as the only backend)

Create a key at `console.typesafe.ai/keys`. Put the key alone on one line in a file, `chmod 600`,
and point `S1_KEY_FILE` at it (default `~/.credentials/typesafe`). The key is read at call time
and never written to a log, trace or page. A call is ~760 input tokens at $0.042 per million
(output is free), about $0.00003. Our early-access account came with a starting credit (it
appeared to be $5, which is about 150,000 calls). The docs don't say what happens when it runs
out; the router treats any rejection as "no opinion".

## Step 6 — Prove it is safe before you arm it

1. **`./run-evals.sh` is GREEN.** `eval_s1route.py` covers these, and turns red on every one of
   its planted faults:
   - scorer failures (timeouts, errors, bad JSON, odd numbers)
   - private traffic never leaving
   - the guards and walls
   - backoff
   - the backup's rules
2. **Measure the guards on your own scorer.** `tools/local-system-one/guard_probe.py
   http://<tailnet-ip>:8799/v1/systemone 1.45` scores 24 hand-labelled probes (it waits for the box
   to cool between calls). Ours:

   | Guard | should say yes | should say no | bar |
   |---|---|---|---|
   | `weather_now` | 0.80 – 0.99 | 0.04 – 0.19 | 0.8 |
   | `other_station` | 0.57 – 0.95 | 0.19 – 0.53 | 0.5 |

   If yours separate worse than this, don't rely on the model guard. The code walls are already
   in place for that reason.
3. **Measure on your own traffic if you can.** The offline runners are in
   [`tools/local-system-one/`](../tools/local-system-one). Some of their input files are built by
   hand, as that README states. Build your labels **without** a model's answers in view
   ([`tools/clean-labels/`](../tools/clean-labels)). Our first label set leaned toward Jev, and
   that inflated "local matches Jev".
4. **Know what the evidence is.** On our 319 messages, the default setting (addressed, public)
   rests on 7 messages that fall through and 2 fixes. That points in a direction; it isn't a
   rate. The first week live is the real test.

## Step 7 — Arm it

The router sits **behind the responder's gates**. It is only asked for a message the responder
is already willing to answer: `RESPONDER_ENABLED=true`, a sender in `ALLOW_FROM`, and the message
addressed to Cal. If the responder is off, arming the router does nothing.

Back up `config` first (`cp config config.bak-pre-s1-arm-$(date +%F)`), then set:

```ini
RESPONDER_ENABLED=true          # if you have not already armed replies
S1_BACKEND=local
S1_LOCAL_URL=http://<scorer-tailnet-ip>:8799/v1/systemone
S1_ROUTE_ENABLED=true
# S1_PRIVATE_OK stays false unless you decide DMs may be scored.
```

Config is re-read on every loop pass, so no restart is needed. (Restart the responder after any
**code** change.) **Check**, on the next addressed message from an allowed sender that no rule
claims:
- `decisions.jsonl` has an `s1_route` field for it (`asked`, `backend`, `route`, `conf`, `acted`),
  and the scorer's own log shows one `scored …` line.
- On the dashboard, that message's trace draws **routed on our own hardware**, or **second
  opinion … not acted on**.
- The capabilities page lists `second-opinion routing` as armed.

## Step 8 — Add Jev as the backup

```ini
S1_FALLBACK=typesafe
```

When the local scorer can't answer — too hot, already busy, down, timed out, gave a malformed
answer, or is sitting out a backoff — a **public** message is asked of Jev instead. It goes
through the same confidence floor, guards and walls. A DM or the bot's own channel never goes to
the backup, whatever `S1_PRIVATE_OK` says. The two scorers keep separate failure state, so an
outage of one never silences the other.

**Every use is recorded:**
- One line in `s1-fallback.jsonl`: when, why the house couldn't answer, and what Jev chose. No
  message text, no sender.
- An `S1 FALLBACK` line in `responder.log`.
- A count on the capabilities page: uses in 7 days, total, and the last reason.
- A sentence in that message's trace saying it left the house and why.

**Know this before you turn it on:** if the scorer box is slow rather than down, Jev ends up
routing most messages. After one timeout the house sits out 300 s, and every public ask in that
window goes to the cloud. If the key or credit fails, the backup fails closed and the message
takes today's path.

## Step 9 — Watch it, and turn it off

| To… | Do |
|---|---|
| Turn the whole router off | `S1_ROUTE_ENABLED=false` (live; takes effect after the batch in hand) |
| Stop anything reaching the cloud | `S1_FALLBACK=none` |
| Use only Jev | `S1_BACKEND=typesafe` |
| See why a message went where it did | its trace on the dashboard (scorer, confidence, guards, walls) |
| See how often the backup is used | capabilities page, or `s1-fallback.jsonl` |
| See the scorer box's health | `curl http://<ip>:8799/health` (load state, CPU temperature) |

Look at the first week by hand. The floor (0.8) and both guards were measured on a few dozen
messages, and live traffic isn't the corpus.

---

## For an agent following this

Stop at the first failed **Check**. Arm nothing (step 7 or 8) without your operator's explicit
go-ahead; arming the backup is a separate decision because it sends public message text to a
third party. Report the eval result, the guard ranges from step 6 and what is armed, each with
the time you checked.
