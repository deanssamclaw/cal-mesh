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
| `com.cal.mesh-dashboard` | `dashboard.py` | Read-only web view of every lever. No deps (stdlib). Funnel-exposed. |

Restart any: `launchctl kickstart -k gui/$(id -u)/com.cal.mesh-<name>`

## Files
- `config` — all knobs (transport + responder). Read live every loop.
- `inbox.jsonl` — received text. `sent.jsonl` — sent text + metadata (`source`: manual/responder).
- `decisions.jsonl` — every inbound the responder evaluated: matched? reason? reply?
- `status.json` / `nodes.json` / `responder-state.json` — live state.
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
| Greeting ack (off-list senders) | fixed table | no | **ARMED** |
| Wire gauge, fasteners (TABLE) | table | no — the harness returns the row | measured, not built |
| Load and rigging | — | — | **refuted, will not ship** (see below) |

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
