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

**The agency layer:** this catalog is the *breadth* axis (what Cal can do). How much of any capability
Cal actually exposes — hardened on the public channel vs. an unbounded "full Cal" on an authenticated
private one — lives in `channel-trust-and-agency.md`. A capability's real behavior is the intersection
of its own spec and that channel policy.

Status key: **LIVE** (armed) · **SPEC'D** (written up) · **OUTLINED** · **CANDIDATE** (this doc).

---

## FETCH doers (base has internet → relays to the field; model narrates the fetched fact)

- **Weather** — *LIVE.* Current conditions from NWS. Edge: unknown unit / fetch fail → "can't reach weather," never a made-up number. **Known gap + next step: `level3-weather-intent-layer.md`** — the capability detects *that* a weather question was asked but never *which*, so it answers rain questions with current conditions. That doc has the surviving design (classify sub-intent in the harness, use it to select the fact, never to steer the prose), the two proposals it refuted with live measurements, and **five defects live in `main` today** (incl. `forecast` firing as a strong trigger word for a capability with no forecast).
- **Propagation conditions** — *CANDIDATE. (I want this most — it's the most radio-native thing Cal could do.)* Solar flux (SFI), A/K index, band openings (NOAA SWPC / hamqsl). "Bands rough today, K-index 5." Edge: report the numbers + a conservative read; never overclaim a band is "open"; fetch fail → say so.
- **NOAA severe-weather alerts** — *CANDIDATE (protective; ties to the NOAA-channelizer stack).* Active watches/warnings from `api.weather.gov/alerts` for the area. Edges: **not authoritative** (defer to official sources / weather radio); **dedup + rate-limit** (never spam the same alert); drop expired. **Special gate:** *proactively pushing* an alert (unsolicited TX, not a reply) is a distinct step — answer-when-asked first; proactive push gets its own review. **Narrowed 2026-08-09** (`level3-weather-intent-layer.md` §1.3): `description` is forecaster prose *containing imperatives*, so it **fails our own §5.3(e) admission bar** — only `properties.event` (a closed vocabulary) is admissible. Measured traps: **28% of "active" alerts have a future `onset`**; one county returned **7 simultaneous alerts** incl. duplicates, where a `feats[0]` pick would understate a Warning as an Advisory; and `severity` is `Unknown` for ~21% (nearly every Air Quality Alert), so it can't rank them — use a hardcoded event-severity table. **Build last, not first.**

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
- **Geology** — *CANDIDATE.* Curated regional/practical geology (rock types, the region's/Kansas geology, aquifers, faults). Two companions: a **fetch** side for live **USGS earthquakes** (recent quakes near a point) and a **compute** side for magnitude/depth/wave-travel math. Edges: curated general facts with cited sources; live quake data fails safe when the base is offline; **never state a fault's behavior as a prediction** (that's the seismology "cliff," same as the physics-simulation cliffs).
- **Emergency / first-aid / survival** — *CANDIDATE — HIGHEST CAUTION, gate hardest.* Off-grid with someone hurt and no cell service, this could be the most important thing Cal ever says — which is exactly why life-safety makes confident-wrongness unacceptable. Invariants: strictest curation; **explicit "not medical advice — seek professional help when you can"**; conservative scope (stable basics like signaling/exposure/water, **never diagnosis or treatment beyond well-established basics**); hard fail-safe (refuse over guess). Do last, with extra review. Never rushed.

## LOCAL INTEREST — Kansas City Chiefs (Dean-requested; spans doers — the static/live split IS the design)

Value here is **local culture/interest, not field-survival** — a different but legitimate category.
The governing distinction: **static facts are clean curated-KB; live data is fetch-and-freshness-gated,
and faking live data from stale curation is the confident-wrongness trap** (a cut player named active,
a flexed kickoff time). Placed by that split:

- **Arrowhead Stadium history & facts** — *CANDIDATE · KNOWLEDGE (static).* Opened 1972, capacity,
  the Guinness loudest-stadium record, notable moments. Stable, curated, safe.
- **Team general & history facts** — *CANDIDATE · KNOWLEDGE (mostly static).* Franchise history,
  championships, retired numbers, colors. Curated; mark the few semi-volatile bits (coaching, etc.).
- **Player roster** — *CANDIDATE · FETCH (LIVE — highest staleness risk).* Rosters churn weekly
  (trades/injuries/cuts/practice-squad). MUST come from a live source with an **as-of date**; a roster
  is never "final." Curate it statically and Cal will confidently name someone who was cut. Edge:
  fetch fail → "can't pull the current roster," never a remembered one.
- **Schedule / next game / score** — *CANDIDATE · FETCH.* The season schedule is mostly static once
  released but **flexes** (flex scheduling, postponements); "next game" and live scores are live.
  Edge: give kickoff times as subject-to-change; fetch fail → say so.
- **Tailgating at Arrowhead** — *CANDIDATE · FETCH + KNOWLEDGE (event-based, hardest to source).*
  General policies (lot open times, rules) are semi-static and curatable; specific per-game events are
  live and poorly structured to source. Edge: give general policy from curated facts; for specific
  events, **point to the official source rather than air a stale/wrong event**.

**Sourcing (open question that gates the live pieces):** roster/schedule/scores are fetchable via
sports APIs (ESPN's unofficial JSON, or a paid feed — reliability/ToS TBD); tailgate events have no
clean structured feed → likely curated-general + defer-to-official. No live sports data is trusted
until a source is chosen and its freshness proven.

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
- *Alongside (local-interest, Dean-requested):* **geology** and the **KC Chiefs** cluster. Their
  *static* parts (regional geology, stadium/team history) are easy curated-KB wins; their *live* parts
  (USGS quakes, roster/schedule/scores/tailgate) are gated on choosing a data source and proving its
  freshness. Sequence the static facts early, the live feeds when a source is settled.

## Fresh-eyes note
Self-corrections on the record: I called sun/moon "niche" in the weather proposal — wrong; it's a
top field need. The KC Chiefs request surfaced the sharpest lesson yet — the **static-vs-live split**:
*history* (stadium, franchise) is clean curated-KB, but *roster/schedule/tailgate* are live data that
go confidently-wrong if curated statically, so they need a fetch source + an **as-of/freshness**
discipline (same family as MeshCore-too-new and NWS-alert-staleness). And **local-interest is a
legitimate value category distinct from field-survival** — worth building, just not mission-critical.
The honest costs stay constant: knowledge tiers → **curation + maintenance**; fetch tiers →
**base-internet dependency + freshness**; all of it → **holding the refusal edge.**
