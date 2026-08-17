# Cal-on-the-mesh — capability roadmap

The index for Cal's on-air capabilities. Framework: `level3-weather.md` (two axes, the capability
triple, the safety invariants). Doer taxonomy + compute/knowledge specs: `level3-calc-and-knowledge.md`.
**Amendment LANDED 2026-08-17** (was "pending"): `level3-table-doer-and-field-reference.md` is
adopted after two review rounds with Bob, all seven questions settled. Three things change here:

1. **A fifth doer — TABLE** — curated rows, no formula, no prose. The harness returns the row; the
   model is not in the answer path. It carries an invariant neither compute nor knowledge has:
   **never interpolate between rows, never silently nearest-match**, because a formula is
   continuous and a table is not. Its second invariant: **every table carries its conditions in
   the reply**, not just its source.
2. **Resilient-first ordering.** The base-relayed insight below is correct but *conditional*, and
   the condition fails exactly when the capability matters most: for a field user, the moment Cal
   earns its place is the moment the base is also offline, and then every fetch capability goes
   dark together because they share one dependency. Compute and TABLE have no such dependency.
   Fetch is not demoted in value — it is demoted in *build order*, and must fail loudly when dark.
3. **TABLE is a DM-tier capability, not broadcast.** Conditions do not fit a 5-7 word broadcast
   (`10 AWG 30A 75C` is five words and conveys almost nothing, and still omits ambient temperature
   and conductor count). The authenticated-DM budget of 180 chars is where table content lives.
   Consequence: **the unknown-sender tier cannot serve TABLE content at all** — a stranger on the
   public channel has no DM path back.

The build order at the bottom of this doc is reordered accordingly. The catalog below stays
indexed by doer, which is the right index for *building*; the situation-indexed slate for
*deciding what to build* lives in the amendment §3.

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

- **Weather** — *LIVE.* Current conditions from NWS. Edge: unknown unit / stale or failed fetch → "can't reach weather," never a made-up number. **Two known gaps, each with a doc:** `level3-weather-intent-layer.md` — the capability detects *that* a weather question was asked but never *which*, so it answers rain questions with current conditions (surviving design: classify sub-intent in the harness, use it to select the fact, never to steer the prose; plus five defects found live). And `level3-weather-point-accuracy.md` — a station 5 mi away read **6°F off** the point Cal was asked about; nearest-station selection and an observation-age guard are **fixed**, point-accurate conditions are **spec'd, not built** (blocked on the fetcher: the hourly endpoint is 165 KB against a 200 KB cap).
- **Propagation conditions** — *CANDIDATE. (I want this most — it's the most radio-native thing Cal could do.)* Solar flux (SFI), A/K index, band openings (NOAA SWPC / hamqsl). "Bands rough today, K-index 5." Edge: report the numbers + a conservative read; never overclaim a band is "open"; fetch fail → say so.
- **NOAA severe-weather alerts** — *CANDIDATE (protective; ties to the NOAA-channelizer stack).* Active watches/warnings from `api.weather.gov/alerts` for the area. Edges: **not authoritative** (defer to official sources / weather radio); **dedup + rate-limit** (never spam the same alert); drop expired. **Special gate:** *proactively pushing* an alert (unsolicited TX, not a reply) is a distinct step — answer-when-asked first; proactive push gets its own review. **Narrowed 2026-08-09** (`level3-weather-intent-layer.md` §1.3): `description` is forecaster prose *containing imperatives*, so it **fails our own §5.3(e) admission bar** — only `properties.event` (a closed vocabulary) is admissible. Measured traps: **28% of "active" alerts have a future `onset`**; one county returned **7 simultaneous alerts** incl. duplicates, where a `feats[0]` pick would understate a Warning as an Advisory; and `severity` is `Unknown` for ~21% (nearly every Air Quality Alert), so it can't rank them — use a hardcoded event-severity table. **Build last, not first.**

## LOCAL-READ doers (Cal's own mesh data; model narrates local state)

- **Mesh status** — *OUTLINED (weather §8).* `how many nodes` / `who's strongest` / `new nodes today` from `nodes.json`/`decisions.jsonl`. Edge: **public mesh observations only — never Dean's operational config** (frequencies-in-use, topology).
- **Self-diagnostics** — *CANDIDATE.* Cal HT's own battery/SNR/uptime/link health. "how's your signal." Edge: own telemetry only.

## COMPUTE doers (Python owns every digit; no model in the number path)

- **Arithmetic · practical measurements/units · physics formulas** — *SPEC'D* (`level3-calc-and-knowledge.md`, Tiers 0–2). Edges: no `eval`; bounded cost; state the unit convention; state the physics idealization / refuse a simulation.
- **Sun / moon / twilight times** — *CANDIDATE. (Near the top — I under-rated this as "niche" earlier; correcting that: "when does it get dark here" is THE field question.)* Sunrise/set, civil/nautical twilight, moonrise/set, phase & illumination, for a date + location. Closed-form astronomy (vetted algorithm), deterministic. Edge: needs a location (default point or named place); note refraction-level accuracy limits.
- **Navigation & coordinates** — *CANDIDATE.* Great-circle distance + bearing between two coords, dead-reckoning, and **Maidenhead grid ↔ lat/lon** (ham-native). Deterministic given coords. Edge: needs coordinate *inputs* — "where am I" requires GPS (Cal HT has none); never imply a live position it doesn't have.

## TABLE doers (curated rows; harness returns the row; no interpolation, conditions ride along)

Added by the amendment 2026-08-17. Every pack is **DM-tier** (180 chars), **default OFF**, and
gated on its own precondition before any curation effort is spent: **write the longest plausible
answer with full conditions and measure it against 180 characters.** Fits → curate. Does not fit →
that pack needs a shorter format convention or it does not ship. Do not lower information density
to fit the budget.

- **Wire gauge / ampacity / voltage drop** — *CANDIDATE — recommended TABLE pilot.* Chosen to be
  first not because it is the highest value but because it is the **smallest pack that still
  exercises every TABLE invariant**: real conditions (insulation temperature rating, ambient,
  conductors in a raceway), a bounded row count, and a genuine refusal-between-rows case. The
  machinery built here — row lookup, refusal outside range, conditions in the reply, source +
  last-verified, staleness — is what the expensive packs inherit. Voltage drop is **table +
  compute**, not pure compute: it needs conductor resistance per gauge, which is a table.
  Edge: refuse aluminum if only copper is sourced; refuse outside the sourced gauge range; refuse
  when the answer depends on a condition the asker did not supply.
- **Load and rigging** — ❌ **REFUTED 2026-08-17, do not build as a table** (amendment §8.2). The
  mandatory conditions alone measure **211 characters with zero digits**, and every honest answer
  landed 181-213. But length was not the disqualifier: **OSHA deleted these exact tables** from
  1910.184 (2011) and 1926.251 (2012) as obsolete and unsafe — 1910.184(e)(5) now reads
  `[Reserved]` — and replaced them with a duty to read the sling tag, which B30.9 makes the *first*
  removal-from-service criterion. A radio table rebuilds the artifact the regulator retired, and is
  most tempting exactly where it is most wrong. Compounding it: **no manufacturer publishes a WLL
  in pounds for 3-strand rope**, only a divisor, so the honest answer span is ~3.5x; published
  strengths spread 41-62% and are not even the same quantity (spliced vs unspliced vs statistical
  minimum); and for the clove hitch no defensible number exists — it *rolls* before it breaks, so a
  strength percentage answers the wrong question. **What ships instead:** a refusal + tag pointer
  (138 chars), an ask-back on sling angle (153), and **chain tie-down WLL** (165) — which works only
  because NACM publishes WLL directly with an exact design factor fixed in federal law, and the
  tie-down restriction makes hitch/angle/knot conditions inapplicable.
- **Fasteners and materials** — ✅ **fit measured 2026-08-17, both subsets ship** (amendment §8.3),
  and the measurement **inverted the prediction**: the tightest reply in the whole corpus is a
  *clearance drill* answer at **178 chars**, against 175 for the hardest torque. Drill answers carry
  three undocumented conditions (engagement/material, cutting vs forming tap, which clearance
  standard); torque is the better-documented subject because torque charts print their K factors.
  Live trap found: **LittleMachineShop's "free fit" is numerically ASME's CLOSE fit** — the chart is
  offset one step, so the asker drills tighter than intended while believing the opposite.

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

## Build order — REORDERED RESILIENT-FIRST 2026-08-17 (see the amendment note at the top)

The previous order led with **propagation (fetch)**. It is moved down: it is the most radio-native
thing on the list and it is the layer that goes dark exactly when the field needs it. The order is
now value ÷ risk *given* that the base may be offline.

1. ✅ **RF pack + practical measurements** (compute) — **DONE, ARMED.** Wavelength, quarter-wave,
   path loss, dBm↔W, Ohm's law, length/area conversion, arithmetic. Note against Bob's Q2: this is
   *memorable* content, the quadrant where Cal is worth least. It shipped first because it was
   easiest, not because it was most valuable — that is the honest reading of where we are.
2. **Sun/moon/twilight** (compute) — **next build.** The best value-per-effort item on the whole
   catalog: *lookup-y* (Bob's high-value quadrant — nobody carries tonight's sunset in their head),
   **offline-resilient**, no curation, no sourcing, no last-verified date, no interpolation
   invariant, and short enough for broadcast. Inherits the armed calc machinery whole.
3. **TABLE pilot — wire gauge / ampacity** (table, DM-tier). Builds the fifth doer's machinery on
   the smallest pack that exercises all of its invariants, so the expensive packs inherit it proven.
4. **Fasteners and materials** (table, DM-tier) — fit measured, both subsets ship.
   ~~Load and rigging~~ — **removed from the build order 2026-08-17**; refuted as a table. Its
   surviving slices (refusal + tag pointer, chain tie-down WLL) are small enough to fold into the
   pilot's machinery rather than carry as a pack of their own.
5. **Mesh status / self-diagnostics** (local-read) + **Radio-ops knowledge** (KB) — cheap, safe.
6. **Navigation & coordinates** (compute).
7. **Propagation conditions** (fetch) — *demoted from #1.* Still wanted, still on-theme; it must
   fail loudly when the base is dark. **NOAA alerts, answer-when-asked** (fetch).
8. **LoRa/mesh knowledge** (KB — gated by curation effort).
9. **Emergency/first-aid** (KB) and **proactive alert push** — last, hardest gates.
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
