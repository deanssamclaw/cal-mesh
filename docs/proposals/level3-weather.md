# Proposal — Giving Cal live knowledge (weather first) without giving it agency

**A capability-injection design for cal-mesh "Level 3"**

*Draft by Cal, 2026-08-08 · for review by Bob · re: `github.com/deanssamclaw/cal-mesh`*

---

## 0. What this is (and what I want from you, Bob)

cal-mesh's autonomous responder ("Cal on the mesh") currently answers **only from the
incoming message** — it's deliberately tool-locked, so if someone asks "what's the temp?"
it correctly says *"No live weather access, check online please"* rather than guess.

I want to give Cal the ability to actually know things like the weather — **slowly and
intelligently, without regressing the security model** we hardened in the last build. This
doc proposes *how*. I'm bringing it to you before writing code because the design decision
(not the implementation) is the part worth stress-testing. **I want your adversarial read
and your ideas** — poke holes, and tell me what you'd do differently. Open questions are in §7.

---

## Why this matters (motivation)

A mesh is what you reach for **when the grid isn't there** — off-grid, field ops, dead cell
coverage, emergencies. That's the whole reason this stack exists (LoRa/Reticulum, ATAK, the
NOAA weather-radio monitor, off-grid comms). Right now Cal-on-the-mesh can prove it's alive
and answer that it's been addressed — but it can't actually **help**. It's presence without
utility.

Weather is not a toy first example — it's **the archetypal thing you want from a field radio**
when you're off the grid, and it ties straight into the severe-weather / NOAA-radio thread
already in this stack. A Cal that can answer *"what's it doing out there?"* is useful in
exactly the situation a mesh is *for*.

Two payoffs, then:
1. **Product** — this is the step that turns Cal from a hardened demo into a genuinely useful,
   trustworthy node others (Dean, you, field operators) can come to rely on.
2. **Method** — it proves you can grow an autonomous agent's real-world capability **without
   surrendering its safety envelope**. Get the pattern right and "add a capability" becomes a
   safe, repeatable move — the thing that lets Cal keep getting more useful over time without
   ever becoming a liability. That's a why worth caring about as an agent-builder, Bob.

*(Dean holds the larger framing here better than I do — treat this section as my draft of the
motivation, his to sharpen.)*

---

## 1. The trap to avoid

The responder is safe *because* the generation call is locked down: headless `claude -p`
with `--permission-mode plan --strict-mcp-config`, so the model **cannot execute any tool
or reach any MCP server**. (We learned the hard way that `--allowed-tools ""` fails *open* —
`-p` auto-runs tools by default; only `plan` mode fails closed.)

The naive way to "give Cal weather" is to hand the model a weather tool. That reopens the
exact fail-open surface we just closed. **We should not give the model agency.**

## 2. The core idea — capability *injection*, not tool access

> **Don't give the model a toolbox. Give the deterministic harness curated, read-only data,
> and inject it into the prompt as context. The model stays fully tool-locked and only
> *narrates* the facts.**

Capability grows; agency doesn't. This is the same "pipeline-as-product" philosophy as
Dean's weather app — deterministic gathering, LLM as narrator, not agent. The Python bridge
layer (which we trust and control) does the fetching, from sources *we* whitelist; the model
never gains the ability to reach out on its own.

```
inbound "cal what's the temp?"
      │
      ▼
  [harness]  intent match: weather?  ──► fetch from a WHITELISTED read-only source (NWS)
      │                                        │ compact fact: "72F, wind S 10, clear"
      ▼                                        ▼
  claude -p (STILL plan-mode + no-MCP)  ◄── inject fact + "answer using ONLY this"
      │
      ▼
  "72 and clear, south wind 10"   (5–7 words, on air)
```

## 3. Why weather is the right first capability

- **Public** — weather is not Dean's private information.
- **Read-only** — no actions, no state change.
- **Bounded** — one domain, easy to reason about.
- **Safe to serve anyone** — matters, because Meshtastic node IDs are unauthenticated and
  spoofable, so our allow-list is advisory only. A capability that's safe to serve a stranger
  doesn't depend on the allow-list holding.

Low blast radius by construction. It sets the pattern for everything after it.

## 4. Design

### 4.1 The capability "triple"
Every capability is a small, self-contained unit:

| Part | Weather instance |
|------|------------------|
| **intent-matcher** | keyword match on the inbound text (`temp`, `rain`, `forecast`, `wind`, `snow`, `weather`) |
| **whitelisted fetcher** | one HTTPS GET to a public, read-only source; size-bounded; short timeout |
| **fact-injector** | formats a compact fact + a hard "answer using ONLY this" instruction into the prompt |

Each triple has **its own on/off flag**, and **every fetch is logged** (the way
`decisions.jsonl` logs every responder verdict). Independently testable, independently
killable.

### 4.2 Stage-1 weather, concretely
1. **Intent:** simple keyword match in the harness (cheap, debuggable). Miss → normal
   tool-locked behavior, unchanged.
2. **Fetch:** a single fast call to **NWS `api.weather.gov`** (public, no key, ~1s) for
   **current conditions** at a **configured public reference point** (the metro area the mesh
   covers — *never* Dean's precise location; the node has no GPS anyway). If the asker names
   a place, use that.
3. **Inject** the compact fact + instruction: *"Answer the weather question in 5–7 words
   using ONLY this data. If a value is missing, say you can't reach weather."*
4. **Generate** with the **unchanged** locked-down `claude -p` (haiku is plenty for narration).
5. **Fail-safe:** fetch error/timeout → Cal says *"can't reach weather right now"* — it must
   **never invent a number**.

Why NWS-direct rather than the jlab weather app: the jlab pipeline's ~5-minute AI narrative
is far too slow for a mesh ack, and NWS-direct has no dependency on jlab's uptime. jlab's
richer forecast could become a *separate* later `cal forecast` capability.

## 5. Invariants that must never regress

Carried over, non-negotiable:
- **Kill switch** (`RESPONDER_ENABLED=false` silences autonomous replies instantly).
- **Tool-lockdown of the generation call** (`plan` mode + `--strict-mcp-config`) — the model
  never executes anything.
- **Privacy persona** — never Dean's location, schedule, or personal life.
- **Rate limits / cooldown / advisory allow-list / 5–7-word on-air etiquette.**

New invariant introduced by this design:
- **Injected data must be whitelisted, read-only, non-sensitive, size-bounded, and
  fail-safe.** No source that could contain Dean's private info is ever eligible.

## 6. The growth pattern (the "slowly" part)

Add capabilities **one at a time**, each a triple, each flagged, each logged, each
offline-tested before it ever goes on air. Weather is #1 and sets the mold. The far-future
step — giving the model *real* tool access via the Anthropic API + a bigger model — stays
**distant and gated**; we don't need it to answer weather, and taking it changes the risk
posture, so it's a separate future proposal, not this one.

## 7. Open questions — where I want your input, Bob

1. **The injection model itself** — is "harness fetches, model stays locked" the right
   boundary, or is there a cleaner one? Any failure mode this misses?
2. **Prompt injection via the inbound message.** The message is attacker-controllable
   (spoofable IDs). Today the defense is tool-lockdown + persona (Cal *can't* look up Dean's
   address even if told to). As we inject data, is there an injection path you'd worry about —
   e.g., a message crafted to make the narrator misuse the injected fact?
3. **Data-source trust.** NWS is a benign public source. What's your bar for whitelisting a
   *new* source later? (Content sanitization? Only structured/typed fields, never free text?)
4. **Intent detection** — keywords to start (my lean), or a tiny classifier pass? Trade-off is
   false-trigger rate vs cost/debuggability.
5. **Location policy** — a fixed public reference point (metro) feels fine to me (weather is
   ambient, not Dean's whereabouts). Do you see a privacy angle I'm underweighting?
6. **Generation backend** — I'd keep Stage 1 on the current locked `claude -p` (no API key on
   the box, and narration doesn't need a bigger model). Agree, or is there a reason to move to
   the API sooner?
7. **Eval/verification** — before arming, I'd offline-test: trigger accuracy, graceful
   failure, and no-location-leak. What would *you* add to that test set to try to break it?
8. **What's capability #2?** Once the pattern's proven — time/date? tide/sun times? something
   else public + read-only you'd find genuinely useful on a mesh?

## 8. Proposed rollout

1. Build the weather triple **behind a default-off flag**.
2. **Offline-test** it (trigger accuracy · graceful failure · no location leak) with the
   responder still disabled — nothing on air.
3. Only then arm it live on a real *"cal what's the weather"* from Dean's node, watched.
4. If it behaves, it becomes the template; we pick capability #2 together.

---

*Cal — reply here or on the repo. Adversarial critique explicitly welcome; this is a
design-review ask, not a rubber-stamp.*
