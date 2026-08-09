# Cal-on-the-mesh — capability roadmap

The index for Cal's on-air capabilities. Framework: `level3-weather.md` (two axes, the capability
triple, the safety invariants). Doer taxonomy + compute/knowledge specs: `level3-calc-and-knowledge.md`.

**The one discipline that governs all of it:** *confident wrongness is the enemy.* Every capability
must keep a crisp edge where it says **"I can't verify that"** instead of guessing. As the surface
widens, that **reliable humility** — clean refusal over a bluff on a public channel — is the property
we never trade for coverage. Each capability below lists its **edge** (where it must refuse).

**The base-relayed insight:** the mesh is off-grid, but the *bridge host* usually has internet. So
**fetch** capabilities let a field user query over RF and have the connected base answer — Cal extends
internet-knowledge to the off-grid edge. That's a feature, not a contradiction.

Status key: **LIVE** (armed) · **SPEC'D** (written up) · **OUTLINED** · **CANDIDATE** (this doc).

---

## FETCH doers (base has internet → relays to the field; model narrates the fetched fact)

- **Weather** — *LIVE.* Current conditions from NWS. Edge: unknown unit / fetch fail → "can't reach weather," never a made-up number.
- **Propagation conditions** — *CANDIDATE. (I want this most — it's the most radio-native thing Cal could do.)* Solar flux (SFI), A/K index, band openings (NOAA SWPC / hamqsl). "Bands rough today, K-index 5." Edge: report the numbers + a conservative read; never overclaim a band is "open"; fetch fail → say so.
- **NOAA severe-weather alerts** — *CANDIDATE (protective; ties to the NOAA-channelizer stack).* Active watches/warnings from `api.weather.gov/alerts` for the area. Edges: **not authoritative** (defer to official sources / weather radio); **dedup + rate-limit** (never spam the same alert); drop expired. **Special gate:** *proactively pushing* an alert (unsolicited TX, not a reply) is a distinct step — answer-when-asked first; proactive push gets its own review.

## LOCAL-READ doers (Cal's own mesh data; model narrates local state)

- **Mesh status** — *OUTLINED (weather §8).* `how many nodes` / `who's strongest` / `new nodes today` from `nodes.json`/`decisions.jsonl`. Edge: **public mesh observations only — never Dean's operational config** (frequencies-in-use, topology).
- **Self-diagnostics** — *CANDIDATE.* Cal HT's own battery/SNR/uptime/link health. "how's your signal." Edge: own telemetry only.

## COMPUTE doers (Python owns every digit; no model in the number path)

- **Arithmetic · practical measurements/units · physics formulas** — *SPEC'D* (`level3-calc-and-knowledge.md`, Tiers 0–2). Edges: no `eval`; bounded cost; state the unit convention; state the physics idealization / refuse a simulation.
- **Sun / moon / twilight times** — *CANDIDATE. (Near the top — I under-rated this as "niche" earlier; correcting that: "when does it get dark here" is THE field question.)* Sunrise/set, civil/nautical twilight, moonrise/set, phase & illumination, for a date + location. Closed-form astronomy (vetted algorithm), deterministic. Edge: needs a location (default point or named place); note refraction-level accuracy limits.
- **Navigation & coordinates** — *CANDIDATE.* Great-circle distance + bearing between two coords, dead-reckoning, and **Maidenhead grid ↔ lat/lon** (ham-native). Deterministic given coords. Edge: needs coordinate *inputs* — "where am I" requires GPS (Cal HT has none); never imply a live position it doesn't have.

## KNOWLEDGE doers (curated, vetted KB — NEVER model free-recall; fail-safe on miss)

- **LoRa / mesh** (LoRa, Meshtastic, Reticulum, MeshCore) — *SPEC'D* (`level3-calc-and-knowledge.md`, Tier 3). Edges: curated facts only (esp. MeshCore is too new to recall); public protocol knowledge, never Dean's ops; last-verified discipline.
- **Radio ops** — *CANDIDATE (small, stable, safe, radio-native).* NATO phonetic, Q-codes, Morse, prosigns, common abbreviations. Edge: fail-safe on anything outside the curated set.
- **Emergency / first-aid / survival** — *CANDIDATE — HIGHEST CAUTION, gate hardest.* Off-grid with someone hurt and no cell service, this could be the most important thing Cal ever says — which is exactly why life-safety makes confident-wrongness unacceptable. Invariants: strictest curation; **explicit "not medical advice — seek professional help when you can"**; conservative scope (stable basics like signaling/exposure/water, **never diagnosis or treatment beyond well-established basics**); hard fail-safe (refuse over guess). Do last, with extra review. Never rushed.

---

## Cross-cutting gates (call these out when the time comes)
- **Proactive push** (alerts): autonomous *unsolicited* transmission is a bigger step than answer-when-asked — its own gate.
- **Life-safety** (emergency tier): the strictest curation + disclaimer + refusal discipline of any tier.
- **Fetch = base-internet dependency**: degrade cleanly to "can't reach that right now" when the base is offline.
- Every tier keeps the standing gate: **default-OFF → offline adversarial eval → independent review → arm.**

## Suggested build order (value ÷ risk, for a mesh/ham field op)
1. **Propagation conditions** (fetch) + **Sun/moon/twilight** (compute) — highest on-theme value, low risk.
2. **RF pack + practical measurements** (compute) — from the calc spec.
3. **Mesh status / self-diagnostics** (local-read) + **Radio-ops knowledge** (KB) — cheap, safe, useful.
4. **Navigation & coordinates** (compute); **NOAA alerts, answer-when-asked** (fetch).
5. **LoRa/mesh knowledge** (KB — gated by curation effort).
6. **Emergency/first-aid** (KB) and **proactive alert push** — last, hardest gates.

## Fresh-eyes note
Self-correction on the record: I called sun/moon "niche" in the weather proposal — wrong; it's a
top field need. And the honest cost across knowledge tiers is **curation + maintenance**, not code;
across fetch tiers it's **base-internet dependency**; across all of it it's **holding the refusal edge.**
