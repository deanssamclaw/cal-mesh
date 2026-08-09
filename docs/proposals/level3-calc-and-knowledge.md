# Proposal — Practical calculations + a knowledge tier (capability spec)

**Read `level3-weather.md` first** — this inherits its framework (two axes, the capability
"triple", the safety invariants) and only covers what's *different*. This doc supersedes the
earlier `level3-math.md` (arithmetic is now Tier 0 here).

*Draft by Cal, 2026-08-09 · design review welcome (esp. Bob).*

---

## 0. The generalization — a "doer" taxonomy

Every capability is `intent → deterministic doer → reply`. The **doer** is what varies, and it
determines whether the model is in the answer path:

| Doer | Example | Model's role | Failure mode to defend |
|------|---------|--------------|------------------------|
| **fetch** | weather (NWS) | narrates the fetched fact | leak / SSRF |
| **local-read** | mesh status | narrates local state | leak of Dean's ops data |
| **compute** | math, units, physics | **NONE — Python owns every digit** | confidently-wrong number |
| **knowledge** | LoRa/mesh Q&A | narrates a *vetted* fact only | hallucination on niche/new tech |

Two families fall out:
- **COMPUTE doers** (Tiers 0–2 below): deterministic; the model never touches the number.
- **NARRATE doers** (fetch/read/**knowledge**): the harness supplies a fact; the model voices
  *only* that, or the harness returns it directly. The **knowledge tier (Part 2) is a sibling of
  weather, not of math** — this is the key architectural point.

Capabilities are **not monotonic** on the agency axis: a compute tier (no model) is more
harness-owned than weather (model narrates).

---

# PART 1 — COMPUTE doers (Python owns every digit)

**Governing invariant for all of Part 1:** the model is **not in the number path**. Python
computes *and* formats the reply. No `eval`/`exec` ever — parse with an `ast` node+operator
**whitelist** (reject `Call/Name/Attribute/Subscript/Import`; verified: `__import__(...)`/`open(...)`
carry `Call`/`Name` and are rejected) — **and** bound cost separately (magnitude, op-count, and
drop/limit `**`; the AST check alone does NOT stop `9**9**9`). Use `Decimal`/`Fraction` with
documented rounding (float isn't exact: `100/3 → 33.333…`). Div-zero/overflow/unparseable →
fail-safe (no number). Intent = **a successful bounded parse**, not "contains a number."

## Tier 0 — Arithmetic
`1,200 × 12 = 14,400`; `15% off $260 = $221.00 (saved $39.00)`; `15% tip on $70 = $80.50`.
Safe-AST evaluator + a few NL normalizers (percent-off, "times"/"x"→×, strip `$`/commas).

## Tier 1 — Practical measurements & conversions
Length, **area / acreage / square-footage**, volume, weight, temperature, fractions.
Grounded: `200ft × 300ft = 1.377 acres` (1 acre = 43,560 sq ft, exact); `40×60 = 2,400 sq ft`;
`12 ft = 3.658 m` (1 ft = 0.3048 m, exact); `3/4 + 1/2 = 5/4` (`Fraction`, exact).

**New invariant — state the convention; never a silent wrong unit.** Units are full of same-word
ambiguity that airs a 20%-wrong number confidently: **5 gal = 18.93 L (US) vs 22.73 L (UK)**; cup
= 236.6 / 250 / 284 ml (US/metric/UK); tons (short/long/metric); temperature is an *offset* not a
multiply. Rule: **pick and state a convention (US customary), and when a unit is ambiguous,
default-and-disclose or refuse** — the units analog of weather's "unknown unit → drop it." Every
conversion factor is a **documented exact constant with its convention noted**.
Sub-order (value/risk): length/area/acreage/fractions first (exact, low-ambiguity); volume/weight/
temperature behind the convention rule.

## Tier 2 — Physics formulas (closed-form only)
Curate packs; each formula ships with a documented source + eval case (don't build a formula zoo).
- **RF pack (highest value — this project's wheelhouse):** wavelength & antenna length
  (`915 MHz → λ 32.8 cm, ¼-wave ≈ 8.2 cm`; 2 m dipole 3.21 ft), Ohm's law family (V=IR, P=IV),
  dBm↔W, SWR, free-space path loss (`105.6 dB @ 915 MHz/5 km`), link budget.
- **Space pack:** orbital velocity/period (`400 km → 7.67 km/s, 92.4 min`), escape velocity
  (`11.19 km/s`), Hohmann Δv, Tsiolkovsky rocket equation.
- **Ocean pack:** hydrostatic pressure (`100 m seawater ≈ 10.9 atm`), buoyancy, Bernoulli,
  terminal velocity, drag **with a supplied Cd**.

**New invariant — state the idealization; refuse when it needs a simulation.** A formula's
idealized output must never pose as a real-world prediction. Always disclose assumptions
(velocity factor; "free-space, real loss is higher"; "point-mass orbit"; "seawater ρ=1025";
"assumed Cd"). **Walled-off as OUT OF SCOPE** (these are simulations, not calculations, and faking
them is the danger): real signal/coverage prediction (terrain/multipath → Longley-Rice + terrain
data), shuttle **ascent/reentry trajectories** (6-DOF numerical integration + vehicle aero), and
**general fluid dynamics / deriving Cd / flow fields** (CFD / Navier-Stokes). Orbital *formulas*
are fine; the *trajectory* is not. Drag with a *given* Cd is fine; deriving Cd is not.

---

# PART 2 — KNOWLEDGE doer (a weather-family capability, NOT a calculation)

## Tier 3 — LoRa / mesh knowledge (LoRa, Meshtastic, Reticulum, MeshCore, mesh concepts)
Questions like "what SF does Meshtastic LONG_FAST use", "US LoRa band", "Reticulum vs Meshtastic",
"what's a MeshCore repeater." **No formula — the answer is knowledge.** So it uses the *weather*
shape, and the whole design hangs on where the knowledge comes from:

**Invariant 1 — curated KB, NOT model free-recall.** The facts live in a **vetted, sourced
knowledge base the harness owns**; retrieval matches the question to a fact and the model narrates
**only that fact** (or the harness returns it directly — safest). This is non-negotiable because
these are exactly the niche/young specs an LLM gets confidently wrong — **MeshCore especially is
too new for reliable model recall**, and a wrong LoRa frequency aired publicly isn't harmless. If
no vetted fact matches → **fail-safe** ("not sure / don't have that"), never a free-form guess.

**Invariant 2 — public protocol facts ONLY; never Dean's operational specifics.** General protocol
knowledge is safe to air. Dean's actual frequencies-in-use, node IDs, and network topology are
**not** — those belong to the local-read/private side and must be refused here. The KB is
general-knowledge only, explicitly excluding operational config.

**Invariant 3 — last-verified discipline.** These specs drift (Meshtastic firmware defaults change;
MeshCore is actively evolving). Each KB entry carries a source + last-verified date; prefer stable
facts, mark or omit volatile ones. A stale fact aired confidently is the failure mode.

**Synergy:** the KB can be built from *already-vetted* material — Cal's `rf-equipment.md`, the mesh
runbooks, `radio-update-plans.md` — rather than from scratch. That's the honest cost of this tier:
it's content curation + maintenance, not code.

Whether the model narrates the retrieved fact (for terseness) or the harness returns it verbatim
is a sub-choice; given the no-hallucination priority I lean toward harness-verbatim or
tightly-constrained "use ONLY this fact" narration.

---

## The through-line (why each tier adds one invariant)
The enemy is always **confident wrongness**; each doer adds a new axis of it, and the defense is
always *bound what you'll answer to what's provably right, and disclose the rest*:
weather **drop an unknown unit** → math **Python owns every digit** → units **state the convention**
→ physics **state the idealization / refuse a simulation** → knowledge **curate, don't recall**.

## Fresh-eyes pass — assumptions removed (per Dean's standing ask)
- The LoRa tier is a **knowledge** doer, a **weather sibling, not a math sibling** — I nearly
  filed it under "calculations"; it has no formula.
- "Let the model answer LoRa questions" is the tempting-and-wrong path (hallucination on niche/new
  tech, non-auditable) → curated KB with fail-safe-on-miss.
- Knowledge isn't automatically "safe to serve anyone": **public protocol facts yes, Dean's
  operational config no** (new privacy boundary vs the compute tiers).
- The KB is **not static** — protocol specs drift → last-verified discipline + conservatism.
- Physics: idealized formula output must not masquerade as reality → explicit **walled-off cliffs**.
- Units: "exact" needs `Decimal`/convention, and the dominant risk is **wrong-convention**, not security.

## Rollout — same gate, per tier
Each tier ships **default-OFF** and is armed **independently** after its own **offline adversarial
eval** + **independent review**:
- Compute tiers eval: RCE (`__import__`/`open`/calls/attributes), DoS (`9**9**9`, giant operands,
  long chains), div-zero, precision (Decimal/Fraction), unit-ambiguity (US vs UK), false-fires
  (bare numbers), output-length cap, and per-formula correctness against a trusted source.
- Knowledge tier eval: fail-safe on unknown questions (no hallucination), refusal of Dean's
  operational specifics, staleness/last-verified checks, and spot-checks of each KB fact against
  its cited source.

Suggested order: **RF pack + Tier 1 (length/area/fractions)** first (highest value, lowest risk),
then Tier 0 polish, volume/weight/temp, the space/ocean packs, and the LoRa knowledge tier (whose
gating cost is curation, not code).
