# cal-mesh — Cal on the Meshtastic mesh

Cal's presence on LoRa radio, via node **Cal HT** (`!xxxxxxxx`, LilyGO T-Deck, US/LONG_FAST).

**What it is for.** A radio you can ask things — and, increasingly, a field reference that answers
when nothing else can: *a Pocket Ref over the air*. Sun and moon times, RF and unit maths, current
conditions, and eventually curated lookup tables (wire gauge first). The ordering is deliberate and
is called **resilient-first**: capabilities that need no network rank above ones that do, because
the moment this earns its place is the moment the base is offline too, and every fetch capability
goes dark together. Direction and specs live in `docs/proposals/`.

**What it will not do is as much of the design as what it will.** Every capability keeps an
explicit edge where it says *"I can't verify that"* rather than guessing, and each one ships
switched off until an offline eval and an independent adversarial review say otherwise.

Three independent layers, each its own always-on launchd agent:

```
   RADIO (Cal HT, USB/WiFi)
        │  packets
        ▼
┌──────────────────┐   inbox.jsonl    ┌──────────────────┐   outbox/    ┌──────────────────┐
│  bridge.py       │ ───────────────► │  responder.py    │ ───────────► │  bridge.py       │
│  owns the radio  │                  │  cognition/gate  │  (reply)     │  transmits       │
│  capture + send  │ ◄─────────────── │  + headless Cal  │              │                  │
└──────────────────┘   outbox/        └──────────────────┘              └──────────────────┘
        │  status.json / sent.jsonl / nodes.json / decisions.jsonl
        ▼
   dashboard.py  →  http://localhost:8787  ·  https://<your-funnel-host>/cal-mesh
                    the current page is at "/"; retired versions keep a
                    permanent /old-N address (old-1 = the first retired)
```

**Design principle:** the bridge is the *only* owner of the radio. The responder never
touches hardware — it reads `inbox.jsonl` and writes `outbox/`, so its cognition can
crash/restart without ever dropping packet capture. The dashboard only observes.

## Layers / launchd agents
| Agent | File | Role |
|-------|------|------|
| `com.cal.mesh-bridge`    | `bridge.py`    | Owns Cal HT (serial or TCP). Capture → `inbox.jsonl`; send ← `outbox/`. Emits `status.json`, `sent.jsonl`, `nodes.json`. |
| `com.cal.mesh-responder` | `responder.py` | Autonomous Cal. Gates inbound → generates a terse reply via headless `claude` → `outbox/`. Logs every verdict to `decisions.jsonl`. |
| `com.cal.mesh-dashboard` | `dashboard.py` | Web view of every lever, plus one write: `POST /api/grade`. No deps (stdlib). Funnel-exposed. |
| `com.cal.mesh-learn`     | `learn.py`     | Daily 06:15. Distils `decisions.jsonl` into a ranked ledger of what Cal could not answer, and audits that ledger against its own classifier. Reads only; proposes, never arms. |
| `com.cal.mesh-drafts`    | `drafts.py`    | Daily 06:30. Runs Cal's own ladder over every message that was not his, stores what he would have said, and transmits nothing. |

Restart any: `launchctl kickstart -k gui/$(id -u)/com.cal.mesh-<name>`

## Addressing: how a message becomes Cal's

Three routes, and the decision trace names which one fired (`via`: `dm` / `trigger` / `channel`).

| route | how | why it is trustworthy |
|---|---|---|
| DM | `to == our node` | addressed at the protocol level |
| trigger word | `\bcal\b` in the text | the only thing that works on a shared channel |
| own channel | arrives on `CAL_CHANNEL` | see below |

On the public channel the trigger word has to stay: it is the only thing separating a question
*for* Cal from a sentence *about* him, on a channel every node in range can hear. Dropping it
there was tried two other ways and both failed — a reply-to pointer costs more taps than typing
four characters, and a conversation window makes Cal answer messages meant for other people
(it also ate a live clarify in testing, turning a deterministic torque figure into a model guess).

A secondary PSK'd channel carries that meaning in the transport instead. `p->channel` is assigned
only after the payload decrypts and parses to a valid `Data` with a known portnum
(`Router.cpp:482-515`), so the index is a cryptographic assertion that the sender holds a key you
chose — unlike `from`, which is cleartext, or a reply id, forgeable by anyone holding the public
channel's well-known PSK. It is stateless: no window, no ring of remembered ids, no TTL, no clock.

**To set it up on your own mesh:**

1. On one radio: `meshtastic --host <radio> --ch-add cal` — adds a secondary at index 1, leaves
   the primary untouched. Name is capped at **11 characters** (`char name[12]`, channel.pb.h:80)
   and is part of the channel hash along with the PSK (`Channels.cpp:39-52`), so both radios need
   the *identical* name and key or nothing matches and the failure is silent.
2. Key size **256-bit**. The "default/8-bit" option is not a key: the firmware expands it into the
   publicly known default PSK with the last byte bumped by your index (`Channels.cpp:228-244`), so
   any node could transmit on it — which defeats the only reason to use a channel at all.
3. Share it: `meshtastic --host <radio> --qr-all`, scan, choose **Add**. Do NOT use `--ch-set-url`
   / `--seturl`; that replaces every channel *and* the LoRa config.
4. Prove a message crosses on the new index, THEN set `CAL_CHANNEL=<index>` in `config`.
   Until the transport is proven, "not received" and "not addressed" look identical.

**`CAL_CHANNEL` is `-1` when disarmed, never `0`.** Zero is the public channel, so any spelling
that falls open — empty string, typo, missing key, a bare `except` returning 0 — would drop the
trigger requirement for every node on the mesh at once, silently. `eval_channel.py` mutates three
different routes to landing on 0.

**Your key never enters this repo.** The mechanism here is meant to be copied; the key is yours.
`config` is gitignored, and `scrub-staged.sh` blocks a channel URL or a `psk` assignment at
`git add` time — that guard went in before the first channel was created, because a guard that
arrives after the paste is not a guard.

## Files
- `config` — all knobs (transport + responder). Read live every loop.
- `inbox.jsonl` — received text. `sent.jsonl` — sent text + metadata (`source`: manual/responder).
- `decisions.jsonl` — every inbound the responder evaluated: matched? reason? reply?
  Carries the inbound **packet id**, which is what binds a decision to its message.
- `status.json` / `nodes.json` / `responder-state.json` — live state. `nodes.json` carries a
  `grid` per neighbour when `POSITION_GRID=true`: a **coarse Maidenhead locator**, not a point.
  See below.
- `gap-ledger.json` / `gap-ledger.md` — the distiller's bank and its rendered view. `learn-state.json`
  holds the watermark and run counter, `learn-history.jsonl` is one line per run, `triage.json` holds
  the oracle verdicts. All gitignored: they carry message text and third-party node ids.
- `mesh` — CLI: `mesh send "…"` · `mesh read [N]` · `mesh watch` · `mesh nodes` · `mesh status` · `mesh log`
- `bridge.log` / `responder.log` / `dashboard.log`

## Send manually
`~/cal-mesh/mesh send "text"` (broadcast ch0) — **keep it 5–7 words** (LoRa airtime is shared).
DM/JSON: `mesh send -j '{"text":"hi","dest":"!aaaaaaaa","channel":0}'`

## Switch transport (USB ↔ WiFi)
Edit `config` → `TRANSPORT=serial` (USB) or `tcp` (WiFi, `Meshtastic.local:4403`),
then kickstart the bridge. **WiFi/TCP is the active transport** — the node runs untethered
on the LAN and the bridge reaches it over WiFi; USB is now only power (or a fallback link).
Unplugging USB while the node stays powered doesn't drop the link (no reboot → the TCP
session carries through).

> **Gotcha — the node must run a firmware build that serves the TCP API.** Meshtastic's
> heavy touchscreen-UI build (LVGL "MUI", e.g. the `*-tft` variant on ESP32-S3) is compiled
> with the webserver/API **excluded** (`MESHTASTIC_EXCLUDE_WEBSERVER=1`) to save flash — so
> WiFi associates (ping + mDNS work) but **port 4403 is refused** and `TRANSPORT=tcp` can't
> connect. Flash the plain **BaseUI** (non-`tft`) build for that device, which includes the
> API, or point the bridge at another node that serves 4403.

## Config hot-reload
The **responder** re-reads `config` every loop (~1s): `RESPONDER_ENABLED`, `ALLOW_FROM`,
`TRIGGER_WORD`, and the rate limits all take effect **live — no restart** (e.g. flip
`RESPONDER_ENABLED=false` to silence Cal instantly). The **bridge** re-reads `config` on each
(re)connect, so **transport** changes (`TRANSPORT`/`PORT`/`HOST`) apply on the next reconnect —
kickstart the bridge to apply immediately. Reconnects use exponential backoff (8→60s) when a link flaps.

## Responder — training wheels (current)
Conservative by design; widen deliberately as trust grows.
- `RESPONDER_ENABLED` — **master kill switch.** `false` = capture/log only, never transmits.
- `ALLOW_FROM` — only these node IDs can trigger Cal (placeholders in `config.example`;
  set to your own node IDs in your local `config`).
- `TRIGGER_WORD=cal` — replies only to a DM or a message containing this word.
- `RATE_MAX`/`RATE_WINDOW_S`/`COOLDOWN_S` — anti-spam / anti-loop (Sam-and-Bob lesson).
- `MAX_AGE_S` — ignore stale backlog after downtime. Backlog on first start is skipped.
- Never replies to its own node. Persona forbids leaking Dean's private info; 5–7 words.
- Model: `RESPONDER_MODEL` (haiku 4.5 — fast/cheap, adequate for terse acks).

**Disarm instantly:** set `RESPONDER_ENABLED=false` (takes effect within ~1s, no restart).

## Capabilities (doers)
Each capability is `intent → deterministic doer → reply`. What varies is whether the **model is in
the answer path** — that is the whole safety story. Taxonomy and specs in `docs/proposals/`.

| Capability | Doer | Model in the number path? | State |
|---|---|---|---|
| Weather (current conditions, NWS) | fetch | narrates the fetched fact only | **ARMED** |
| Arithmetic / units / RF pack | compute | **no — Python owns every digit** | **ARMED** |
| Sun / moon / twilight | compute | **no — Python formats the whole reply** | **ARMED** — works offline |
| Signal report (range/signal test) | measured | **no — every number is the radio's own** | **ARMED** — runs *first*, see below |
| Capability list ("what can you do") | flags | no — composed from the live config flags | **ARMED** |
| Greeting ack (off-list senders) | fixed table | no | **ARMED** |
| Wire gauge, fasteners (TABLE) | table | no — the harness returns the row | measured, not built |
| Load and rigging | — | — | **refuted, will not ship** (see below) |
| Proactive welcome (new node's first message) | — | — | **refuted, will not ship** (see below) |

**Which capability owns a message is decided by position, not vocabulary.** Whichever one's
subject appears *first* is the one being asked about; anything later is context or a time adjunct.
"when does it get dark, storm coming" opens on dark; "will it rain at sunset" opens on rain. A time
interrogative directly governing a sun/moon word overrides that, so "rain later, when is sunset" is
still a sunset question, and ties go to weather.

That rule replaced three earlier ones, each of which decided by *which words appear* and each of
which failed: widening weather claimed 210 of 210 synthetic non-weather messages; yielding on any
weather word dropped 86% of a test grid to no capability at all; arbitrating by prepositions was
wrong in both directions simultaneously. Calc is separate and wins outright — `cal sunset 12*12`
answers `144` — because a calculation embedded in a message another capability would claim is
still a calculation.

**The governing discipline is refusal.** Every capability keeps an explicit edge where it says
"I can't verify that" instead of guessing:
- weather refuses forecast-shaped asks (it holds observations only) — including daily highs/lows
  and time-of-day qualifiers like "at dusk", which are future states;
- calc refuses ambiguous units, prose containing an expression, and anything past its cost bounds;
- sun/moon refuses moonrise/moonset (not implemented — it is *recognised* so it can be refused,
  because an unrecognised ask falls through to the model, which would invent a time), dates
  outside 1901–2099, asks about another day ("sunset saturday" — it computes for *now*, not for a
  date), and any event that does not occur — reporting *which* one is missing rather than a
  generic "no sunrise", since midnight sun and polar night are opposite conditions.

Config failures **fail closed**, never open: an unset observer point or an unparseable timezone
refuses rather than substituting a default that would put a confident wrong answer on air.

### Why "load and rigging" is not here
It was the highest-value pack on the field-reference slate and it is **not going to ship**. The
mandatory safety conditions alone measure 211 characters with zero digits, against a 180-character
budget — but the disqualifier is not length. OSHA *deleted* these tables from 1910.184 (2011) and
1926.251 (2012) as obsolete and unsafe, replacing them with a duty to read the sling tag. Serving
one over radio rebuilds the artifact the regulator retired, and it is most tempting exactly where
it is most wrong. Full measurement in `docs/proposals/level3-table-doer-and-field-reference.md` §8.2.

### Why the proactive welcome is not here
The idea was good: a node Cal has never heard sends its first public message and gets one short
public hello, on the theory that a visible welcome draws people into the mesh. It was built
twice — once bare, once carrying the measurement — and **refuted both times, for different
reasons.** Kept here because the idea is attractive enough to be proposed again.

A **bare** hello is a claim, not an observation. "Good to hear you" reads identically whether the
newcomer arrived direct and strong or scraped in at seven hops, so it cannot show the one thing a
newcomer actually wants to know. This repo already paid for that lesson: sigreport exists because
the model once answered "Link's solid and steady over here" with no access to a number at all.

Attaching the **measurement** fixes that and creates something worse. sigreport's trilateration
residual is priced on the attacker having to ASK — they range test, and the answer goes to them.
A welcome broadcasts signal and hop count to `^all`, unsolicited, to someone who asked nothing.
Replayed over 29 days of real log: of 45 welcomes, **23 went to nodes that broadcast their own
GPS**, and the node DB says half this mesh does. That is a passive harvest of
`(known coordinate, RSSI into Cal, hop count)` on a schedule. Cal broadcasts no position
precisely to avoid being locatable; this hands the same thing out a side door.

And the trigger cannot be made to mean what it says. **"New to Cal's node database" is not "new
to the mesh"** — a node's first TEXT packet can arrive before its NodeInfo, so seeding from the
node DB still fired 13 times in replay. Cal is one node on a 313-node metro mesh and has no
standing to welcome a 7-hop stranger to it. On the real log the armed feature stepped on a
severe-thunderstorm broadcast to welcome the station issuing it, welcomed an automated weather
bot monthly, and welcomed mid-conversation replies ("No but I'm on a plane").

Measured by adversarial review over ~34,500 inputs, 2026-09-06. The implementation is on the
`welcome-measured` branch, disarmed, if the shape is ever worth revisiting.

## Signal report: the doer that runs first
Every other capability answers a question somebody chose to ask Cal. A range test is addressed to
*whoever can hear it*, so this one sits **ahead of the whole ladder** and answers senders who are
not on the allow list. That position is what makes its failure mode asymmetric: **a false fire is
a message no other capability will ever see.** A miss costs a reply; a false fire silently eats
somebody else's question.

So the shape rules are about who is being addressed, not about which words mean "test":

- **A greeting may precede the trigger.** `Hey Cal, this is a test` addresses Cal exactly as much
  as `Cal, this is a test`. Anchoring the strip to position 0 meant it did not, and the log has
  the miss: 2026-08-08, answered by the model while measured SNR sat on disk.
- **Talking about a test is not running one.** A determiner or possessive immediately before
  test/check makes the phrase a *reference* — `got the test`, `Cal's test`, `Cal aced the test`.
  Deliberately not a vocabulary of test words: that design already died on `tange test`. This is
  the short, closed list of words that turn any noun into a reference.
- **Another capability's word makes the message that capability's.** `weather check` is a weather
  question, and answering it from here means weather never sees it.

Being *named* is not being *asked* — the referential rule applies whether or not Cal is addressed,
because `Cal aced the test` is a sentence about Cal. That exemption was in the first draft and an
adversarial review refuted it in one line.

### Contact reports (2026-09-12)

A neighbour saying "Got you in Olathe" is running the same experiment a range test runs, in the
other direction: they are telling Cal they heard him. The reciprocal -- how Cal heard *them* --
is the one fact Cal holds and they do not.

Before this, the model answered those by feel. Read over the whole log: **27 replies asserted
link quality, Cal held the measured SNR and RSSI on the packet for all 27**, and four called a
link "loud and clear" at -15 to -19 dB SNR, at or past the usable floor. That is the failure this
module was built to prevent, happening in a shape the module did not recognise.

**Why this is not the proactive welcome**, which was refuted for broadcasting exactly this data.
The welcome fired on a node's *first message whatever it said*, so signal and hop count went to
someone who had raised no such topic. Here the sender opens the subject of reception with Cal
themselves -- the same consent a range test carries. That is the whole distinction, and it is the
load-bearing one.

Three conditions, all required, because this doer pre-empts every other capability:

1. **A receipt verb**, so the message is about reception at all.
2. **A second-person reference**, so the report is about hearing *Cal*. This is what refuses
   "I heard about that show. Never saw it" and "Pretty good! Just got into town."
3. **No other node named.** `871c I hear you in Lee's Summit` and `Hello 63d8 and 6404, both 3
   hops` are addressed to a third party, and Cal answering would barge into an exchange he is not
   part of. On the live corpus this is the condition doing the real work.

Measured on the full inbox at the time of writing: **fires on 8 of 386 messages**, all genuine.
A doer-word guard runs too, and here it must be *inflected* -- `Got you, is it raining?` fired in
testing because the closed list holds "rain" and the sender typed "raining".

The eval allows contact reports as a **class**, via a skeleton written by hand in the eval rather
than a call into `_is_contact_report`: a fixture built from the thing under test could only ever
pass. Breaking either condition, or the rule entirely, fails the suite.

## How a capability ships
Nothing goes on air because it looked right. The gate is the same for every tier:

**default OFF → offline eval → independent adversarial review → arm.**

- **The eval runs with no radio and no network.** Current corpus: calc 273 checks, sun/moon 873,
  greeting 91, DM 71 + 45, render 74, routing 21, plus a page parser. Numbers only mean something
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

## Direct messages
A DM is a different room, not a louder one.

- **Length.** Broadcast replies are 5–7 words because every one costs shared airtime in the whole
  radio's range. An authenticated DM lands on one screen, so the budget relaxes to ~180 characters.
  Airtime is still shared — this is not a chat window.
- **Authentication is real but is not a security boundary.** Meshtastic PKC gives a `pki_encrypted`
  flag and a public-key fingerprint, and both are captured and pinned. Node IDs remain spoofable,
  so the DM tier is **forge-tolerant by construction**: the worst case if sender auth is forged is
  that a forger reads a reply meant for Dean — never that tools unlock.
- **Content only.** A private-context unlock exists, is disabled, and changes exactly one argument
  to the model: the system prompt. Tool lockdown is byte-identical to the public channel, asserted
  by eval.
- **DMs are published on the dashboard, deliberately.** The DM path is a test bench so experiments
  do not spend open-channel airtime, and showing it is the point. The consequence is stated plainly:
  whatever goes in the DM context file becomes public the first time Cal references it.

## The decision trace
The dashboard is not a log viewer. For **every** reply it publishes *why that reply exists*: the
gate ladder and which gate failed, what the wording matched and why that selected a capability,
what was fetched and how old it was, which single fact crossed to the model, the model and
latency — and, for compute answers, that **no model ran at all**.

It shows machinery, never introspection. Generation is `--output-format text`; there is no
reasoning to display, and inventing one would publish narrative as if it were mechanism.

Two things this has already caught that testing did not: a diagram that read to a stranger as
*"your message failed to send"* when the message had arrived fine and was what caused the reply,
and a trace asserting one cause for a blank that had several. If the page cannot explain a reply
honestly, that is a defect in the reply.

**The page has two surfaces and they mean different things.** The ground is a warm mid neutral
(v6, 2026-09-05); the trace panel — and only the trace panel — is a dark well. The page is a
status board you scan, the trace is an instrument you read one record on, and dropping it well
below the ground gives the boxes, wires and dots a plane of their own. The ground moved off white
because the page had been at both ends and neither was it, and the middle is not the midpoint
between them: that is a mid grey no text sits on comfortably from either direction. Contrast is
measured rather than eyeballed, in both directions — a raised plane must be lighter than its
ground and a well darker.

**Every trace is bound to its message by packet id**, because a trace shown under the wrong
exchange is worse than no trace at all: it reads as evidence. The responder stamps the radio's
own id on each decision record and the dashboard joins on `(sender, id)`. Sender, text and
timestamp are all heuristics — `Cal test` sent twice from one node is genuinely indistinguishable
under them — and the fallback that still has to serve pre-id records now pairs *globally*
nearest, closest pair first across the whole set. Before that it walked record by record, and a
message whose own decision had been trimmed away could take a later duplicate's and render a
gate ladder it never earned, while the message that did earn it showed `no trace recorded`.
`eval_correlate.py` holds both ends.

## Neighbour position: a bucket, never a point

About half the neighbours broadcast their own position over the air. With `POSITION_GRID=true`
the bridge reduces that to a **Maidenhead locator** and stores only the locator — roughly
**3 × 4.5 miles** at 6 characters, or 70 × 110 miles at 4. `POSITION_GRID_CHARS` sets which.

Three properties make it safe to publish, and each is load-bearing:

- **Bucketed at capture.** `coarse_grid()` runs in `bridge.py` and the exact latitude and
  longitude are discarded in the same expression. Rounding at render time would leave the precise
  point sitting in a file that feeds a public API — one bug away from being served.
- **Cal's own node is excluded structurally**, not because it happens to advertise nothing today.
  Cal HT sits at a fixed private address, and a firmware setting must not be able to start
  publishing it as a side effect. `eval_hops.py` mutates that exclusion away and requires the
  suite to fail.
- **A node that broadcasts nothing simply has no grid.** It is never inferred from neighbours,
  hop count or signal.

What it still gives away, stated plainly: one bucket says little, but buckets across enough
neighbours narrow where the receiver hearing all of them must be. That is a deliberate trade,
made with the knowledge that these are positions their operators already transmit in the clear
to everyone in range. Set `POSITION_GRID=false` to turn the column off entirely.

The encoder is `calc.latlon_to_grid`, pinned against IARU's published worked examples — the
same one the `calc` doer uses, so there is one implementation rather than two that can drift.

## Hops: named when known, counted when not

A traceroute reply carries each relay's **full node number**, so hops are named from the node
database — name, hardware, hops from Cal, last heard. Nothing there is inferred.

The **relay byte** the firmware reports with an ordinary message is different: one byte, and
genuinely a fragment. Measured over 292 known nodes, a two-character fragment matches exactly
one node only **28%** of the time and one value is shared by **nine**. So a relay is named only
on a unique match and otherwise reports how many candidates it has. Guessing the likeliest is
the mistake `clean_name` already exists for.

## Simulated replies, and grading them

Cal hears far more than he answers. Nothing recorded what he *would* have said, so whether the
silence was right was unknowable. `drafts.py` runs his real ladder over every message that was
not his own — sigreport, then the doer plan, then the off-list greeting ack, and a model prompt
only when nothing else claims it — and writes the result to its own tab. It cannot transmit, and
that is tested at the filesystem rather than asserted: the eval runs the module under a wrapper
that fails on any write resolving inside `outbox/`.

It did not always work that way. Until 2026-09-08 it called the model for every message and
separately *labelled* which doer would have answered, so the tab published Cal declining a
capability he has — "Can't check live weather try online" beside a note that the weather doer
matched. 13 of 143 stored rows were wrong that way and were re-drafted in place. (This read "40" until
2026-09-12. 40 is `DRAFTS_MAX_PER_RUN` — the per-run cap that appears in `drafts.log` as
"drafted 40 new", and the number of rows carrying the `bf3fef6` stamp. A run cap had been written
down as a defect count; the measured figure is 13, in `18644b3` and in `drafts.py`'s docstring.)

Each row says which arm answered. `sigreport` or `weather` means no model ran at all.
`model+weather` means the harness fetched a real observation and the model only phrased it —
neither a doer nor free prose, so it is named for what it is.

**One honest limit.** `ALLOW_FROM` stops an off-list sender from ever reaching generated prose
in production. Drafts consult it only to place the greeting arm; they do not obey it, because
obeying it would draft silence for 103 of 143 rows and blank the tab where it is worth reading.
So an off-list row with no doer match shows what Cal *could* have said, not what he would have
sent — he would have said nothing. `why_silent` carries `sender_not_allowed` on those rows.

### What else was on the channel

A message alone is often unjudgeable and roughly a third of the log is exactly that, so each row
carries the traffic within **±3 minutes**, oldest first, Cal's own sends included. Measured on
real rows, it changes the reading: "Aye" reads as a hail until you see it arrived 20 s after a
*different* node's "Heard from 159th and I-35!"; a 👍 turns out to land 78 s after Cal's own
signal readback, making it a thank-you rather than noise; and "Right on 33c4, sounds like great
news" had **nothing** before it, which is what proves the model invented the news. A row with no
neighbours says so — "no context" is itself evidence.

Window and cap are measured, not chosen: median 1 neighbour, p90 of 5, busiest minute on record
10, and 36% of messages have none at all. The cap keeps the **nearest** neighbours rather than
the first by time, or a busy minute would show only its oldest corner.

**The drafter sees it too, and that is a reversal made the same day.** This section first said
the window was evidence for the reader and *never* input to the draft, on the grounds that a
conversation window had already been built for the responder and refused. That refutation is
real but it is about the AIR: the window made Cal answer messages meant for other people, and it
ate a live clarify, turning a deterministic torque figure into a model guess. Drafts transmit
nothing — the eval proves that at the filesystem — so those consequences do not transfer, while
blindness has a measured cost of its own. Re-drafted with context, `"Aye, loud and clear here."`
became `"Copy that, thanks for checking in"`: the fabricated signal claim simply disappears.

Two rules bound it.

**Context never touches a capability prompt.** `build_prompt`'s weather path deliberately does
not echo the sender's message at all — the model sees the harness-fetched fact and nothing else,
so no attacker-controlled text sits beside a number it is told to repeat verbatim. Prepending a
dozen strangers' lines there would reopen, wider, exactly what that path was shaped to close. It
is added only where the model is already writing free prose, and the eval drives a weather ask
through and reads the prompt to prove it.

**Every line is sanitized**, through the same `sanitize_inbound` the live path uses. There are
44 distinct off-list senders in this corpus; skipping it would recreate the hole that was closed
when raw stranger text was found reaching the user turn.

**And a context-built draft is not a counterfactual.** The responder sees one message; a draft
that saw eight is what Cal *could* say if context were armed, not what he *would* have said. The
row carries `ctx_n` and such rows are marked **unfaithful**, the same way a live weather fact
marks one — so the tab never claims a proposal is a record.

If those drafts read better than the blind ones over time, that is the argument for arming
context on the responder. It is not that argument yet.

### Grading is public and ungated

`POST /api/grade` takes a verdict from anyone who can open the page. That is deliberate: the
page is on the open internet by design, and a grade is an opinion about a sentence Cal already
published there. What is bounded is the *write*, which is a different question from the writer —
a closed verdict set, capped note and body, and a `draft_id` that must already exist, so the
keyspace is the set of drafts Cal actually made rather than anything a client can name.

Four verdicts, each naming its own consequence, because a bare thumb says a draft was bad and
nothing about why — so it cannot route anywhere:

| verdict | means | what happens |
|---|---|---|
| 👍 `good` | this is what Cal should have said | counts toward a baseline |
| 🔧 `doer` | should have been deterministic, not prose | enters the triage queue that arms doers |
| ✏️ `wrong` | carries the reply that would have been better | the only feedback with training signal |
| ⚠️ `harmful` | should not have been said at all | queued as work |

**Three writers, and the record says which one.** The public page writes `by: "page"` and
cannot be told otherwise -- it is ungated, so a value it accepted from the client would let a
passer-by sign as the operator. The local CLI writes whoever `--by` names, defaulting to the
operator, because running it means having the machine. Programmatic review writes
`by: "cal-review"`. Until 2026-09-12 all three pooled into `"page"`, which made a machine's
pattern match indistinguishable from a person's judgement; the page now shows the author beside
the verdict.

    python3 drafts.py --grade <draft_id> --verdict doer --better "…" --by dean

The CLI's verdict set had also drifted: it offered `good/wrong/harmful` and omitted **`doer`**,
the only verdict `grade_queue()` routes into triage. So the operator could not record the one
judgement with leverage while an anonymous visitor could. The two sets are now asserted equal in
`eval_drafts.py` rather than kept in step by hand.

`learn.grade_queue()` is the join that makes this a loop rather than a log. It keys each verdict
with the distiller's own `normalize()`, so a graded ask and a distilled gap land in one namespace
instead of two spellings of the same thing, and an item clears when its ask is **triaged** — not
when it is graded again. The join happens at read time and nowhere else: a grade is an opinion,
`gap-ledger.json` is a measurement of what happened on the radio, and appending one to the other
would leave nothing downstream able to tell them apart.

### Auditing the bank

`drafts.jsonl` is cumulative and `run()` skips anything already drafted, so a capability armed
later corrects every *future* row and cannot reach one already banked. That is the same shape
the gap ledger had in session 150: a store with a watermark, and nothing comparing it to the
code that fills it. On 2026-09-12 it was 8 banked rows a doer would now claim, and 40 of 44
graded defects still showing the old Cal.

    python3 drafts.py --audit      # read-only; 0 nothing to do, 1 drift found
    python3 drafts.py --redraft    # re-run the drifted rows under today's code

The audit decides which arm *would* answer without generating anything, so it costs no model
calls -- every arm above the model is deterministic, and `cal_reply(dry=True)` stops there. It
reuses the real ladder rather than restating the order, because a second copy is how the two
drift apart. The eval proves the no-model property with a spy rather than a raising stub: a
raise is swallowed by `audit()`'s own per-row `except`, and that version of the check survived
the mutation that turned dry mode off.

`--redraft` rewrites only rows whose arm changed, and re-stamps `armed`/`commit` **only on a row
it actually regenerated** -- those fields say which Cal produced the text, so stamping a row it
did not touch would assert something false. A row whose new arm is the model needs a model call
to get text and is skipped unless `--with-model`.

Two counts are reported apart because they mean different things: **drift** (a different arm
would answer now) is the alerting signal, while a row with **no recorded arm** predates arm
stamping and is not drift -- nothing is known to have changed about it.

## The learning loop, and whether it is running

The distiller is a scheduled job that reports its own numbers, which is a shape that can be
wrong invisibly — and was. Three situations produce an identical "nothing new": a healthy loop
over a quiet mesh, a responder that has stopped writing, and a bank that no longer agrees with
the code that classifies it. The third one shipped. Adding `sigreport` to `DOER_CAPS` fixed the
classifier, but the aggregate is cumulative and the watermark classifies each record exactly
once, so the fix corrected every future record and could not reach the three already counted.
Three answered range tests stayed published as unanswered gaps for six days, while every figure
on the page was internally consistent.

So the page publishes a verdict from a closed set, with the evidence under it:

| state | means |
|---|---|
| `FRESH` | ran on schedule, input flowing, bank agrees with the classifier |
| `LATE` | no run inside the threshold — everything below it is stale |
| `STALLED` | the loop is running, but nothing has been received |
| `DRIFT` | banked records would classify differently under the code running now |
| `UNKNOWN` | an artefact could not be read — never a cheerful default |

Two rules make it worth trusting. **Liveness is derived from artefacts carrying their own
timestamps** — the newest run in `learn-history.jsonl`, the newest record in `decisions.jsonl` —
never from a heartbeat the monitored job writes, because that cannot report that the job has
stopped; it keeps saying whatever it last said. And **the drift check runs on every page read,
not once per run**, because the failure it catches is a classifier edited *between* runs, so
banking the number at run time would rebuild the same blind spot a day wide.

Flags are collected rather than short-circuited, so two faults at once cannot hide each other;
precedence only chooses which chip is shown, and all four facts render regardless. There is no
green light on the page. A light is a claim, and that claim would have read true throughout the
six days — what is shown instead is the evidence it would have been claiming from.

Thresholds are measured, not chosen. Runs land 24.00 h apart across eight consecutive days, so
`LATE` is 26 h. The largest natural silence between two inbound records is 39.3 h (median
0.47 h, p90 11.5 h), so `STALLED` is 48 h — below about 40 h it fires on a genuinely quiet mesh,
and a health signal that cries wolf is one nobody reads. Re-derive both if traffic changes shape.

Both checks are read-only and exit with a code, so they compose with a monitor:

    learn.py --check     # 0 fresh · 1 unhealthy · 2 unknown
    learn.py --audit     # re-classify the bank against the current code; per-bucket delta

`eval_health.py` — 62 checks. Every state is driven by a fixture that forces it; the drift check
is exercised by putting `DOER_CAPS` back to its pre-fix shape and demanding the audit finds all
three records; and both browser renderers are executed against live and adversarial payloads.
Three self-test mutations, including "health always returns FRESH", each of which must fail the
suite.

## How to grow from here
1. Widen `ALLOW_FROM` / trigger policy to serve other operators.
2. Give autonomous Cal tools/context (move generation to the Anthropic API + a bigger model,
   inject relevant state) — but keep the tool-less, privacy-first persona as the floor.
3. Private PSK channel for Cal↔Dean if desired (currently public ch0 by Dean's choice).
4. Add auth to the dashboard for parity with rflab (currently open, read-only).

## Facts
- Node: Cal HT `!xxxxxxxx` · fw 2.7.26.54e0d8d (**BaseUI / non-tft build — serves the WiFi API**) · US / LONG_FAST · ch0 public.
- Serial: `/dev/cu.usbmodemXXXXXXXXXXXX` (MAC-derived, stable). WiFi: `Meshtastic.local` (<your-LAN-IP> as of 2026-08-08).
- Generation: subscription `claude -p` (no API key on this box), `--system-prompt` override + `--permission-mode plan --strict-mcp-config` → no bootstrap, tools cannot execute, no MCP servers load. (NOTE: `--allowed-tools ""` does NOT disable tools — fails open. Verified 2026-08-08.)
- ALLOW_FROM is advisory only (node IDs are spoofable) — real controls are the kill switch + tool lockdown, not the allow-list. See the runbook §12.
