# Running cal-mesh

*The processes, files, transport and config behaviour, and the standing facts. Moved out of the README on 2026-09-24, unchanged apart from link paths.*


## Architecture (the README intro as of 2026-09-24)

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

Three independent layers, each its own always-on service (the reference install runs them as
`systemd --user` units on an always-on Linux box with the radio on its USB; macOS launchd works too —
see [`deploy/`](../deploy)):

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

## Layers / services
| Service (systemd unit · launchd label) | File | Role |
|-------|------|------|
| `cal-mesh-bridge` · `com.cal.mesh-bridge`       | `bridge.py`    | Owns Cal HT (serial or TCP). Capture → `inbox.jsonl`; send ← `outbox/`. Emits `status.json`, `sent.jsonl`, `nodes.json`. |
| `cal-mesh-responder` · `com.cal.mesh-responder` | `responder.py` | Autonomous Cal. Gates inbound → generates a terse reply via headless `claude` → `outbox/`. Logs every verdict to `decisions.jsonl`. |
| `cal-mesh-dashboard` · `com.cal.mesh-dashboard` | `dashboard.py` | Web view of every lever, plus one write: `POST /api/grade`. No deps (stdlib). Funnel-exposed. |
| `cal-mesh-learn` · `com.cal.mesh-learn`         | `learn.py`     | Daily 06:15. Distils `decisions.jsonl` into a ranked ledger of what Cal could not answer, and audits that ledger against its own classifier. Reads only; proposes, never arms. |
| `cal-mesh-drafts` · `com.cal.mesh-drafts`       | `drafts.py`    | Daily 06:30. Runs Cal's own ladder over every message that was not his, stores what he would have said, and transmits nothing. |
| `cal-mesh-tracer` · `com.cal.mesh-tracer`       | `tracer.py`    | Every 2 h. Queues traceroutes for the bridge. |
| `cal-mesh-presence` · `com.cal.mesh-presence`   | `presence.py`  | Every 30 min; sends at most one presence line a day. |

Restart any: `systemctl --user restart cal-mesh-<name>` (Linux) · `launchctl kickstart -k gui/$(id -u)/com.cal.mesh-<name>` (macOS)

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
- `tools/local-system-one/` — offline runners that compared five open System One models against the
  cloud router on this node's own traffic. Never imported by the responder. See the proposal above.
- `s1route.py` — the second-opinion router (**armed 2026-09-24**, local scorer, Jev as backup). One
  Choice question and two guards, the option text in one block; two backends plus a fallback.
  Key read from `S1_KEY_FILE`, never logged. Build-your-own runbook: [`docs/router.md`](router.md).
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

## How to grow from here
1. Widen `ALLOW_FROM` / trigger policy to serve other operators.
2. Give autonomous Cal tools/context (move generation to the Anthropic API + a bigger model,
   inject relevant state) — but keep the tool-less, privacy-first persona as the floor.
3. Private PSK channel for Cal↔Dean if desired (currently public ch0 by Dean's choice).
4. Add auth to the dashboard for parity with rflab (currently open, read-only).

## Facts
- Node: Cal HT `!xxxxxxxx` · fw 2.7.26.54e0d8d (**BaseUI / non-tft build — serves the WiFi API**) · US / LONG_FAST · ch0 public.
- Serial: `/dev/serial/by-id/usb-Espressif_…-if00` on Linux, `/dev/cu.usbmodemXXXXXXXXXXXX` on macOS (both stable). A configured port that is absent makes the bridge wait for it; it does not auto-detect another radio. WiFi: `Meshtastic.local` (<your-LAN-IP> as of 2026-08-08).
- Generation: subscription `claude -p` (no API key on this box), `--system-prompt` override + `--permission-mode plan --strict-mcp-config` → no bootstrap, tools cannot execute, no MCP servers load. (NOTE: `--allowed-tools ""` does NOT disable tools — fails open. Verified 2026-08-08.)
- ALLOW_FROM is advisory only (node IDs are spoofable) — real controls are the kill switch + tool lockdown, not the allow-list.
