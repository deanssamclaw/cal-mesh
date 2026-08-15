# Proposal — a TABLE doer, and a situation-first field reference

**Read `level3-calc-and-knowledge.md` first** — this amends its doer taxonomy and inherits its
safety invariants. It also proposes a reordering of `level3-roadmap.md`.

*Draft by Cal, 2026-08-14 · design review requested (Bob).*
*Origin: Dean's framing — "a Pocket Ref over the air," Cal as the knowledge provider for a mesh
community in the field.*

---

## 0. The amendment in one line

The taxonomy has four doers — **fetch · local-read · compute · knowledge**. There is a fifth, and
most of a field reference lives in it: **TABLE** — a curated lookup with no formula and no prose.

## 1. Why TABLE is not just "knowledge with numbers"

Wire ampacity for a gauge and run length. Rope and chain breaking strength. Drill and tap sizes.
Lumber spans. None of these is a calculation, and none is prose. They are *rows*.

Placing them matters because the two axes disagree:

| Axis | TABLE behaves like | Consequence |
|------|--------------------|-------------|
| **Safety** | COMPUTE — deterministic, harness-returned, **model not in the number path** | inherits the strongest safety story we have |
| **Content** | KNOWLEDGE — curated, needs sourcing and a last-verified date | inherits the curation cost |

| Doer | Model's role | Failure mode defended |
|------|--------------|----------------------|
| fetch | narrates the fetched fact | leak / SSRF |
| local-read | narrates local state | leak of Dean's ops data |
| compute | **none — Python owns every digit** | confidently-wrong number |
| **table** | **none — the harness returns the row** | **wrong or stale row; silent nearest-match** |
| knowledge | narrates a *vetted* fact only | hallucination on niche/new tech |

**The practical payoff:** a large slice of a field reference can ship at **compute-tier risk, not
knowledge-tier risk**. It does not have to queue behind the hardest curation gates. That is a much
shorter path to a usable field reference than the current roadmap implies.

**The new invariant TABLE adds — never interpolate silently, never nearest-match.** A formula is
continuous; a table is not. If the question falls between rows, or outside the table's stated
range, the answer is a refusal or an explicit "nearest tabulated value is X" — never a quiet
interpolation presented as a tabulated fact. This is the table analog of "state the convention"
(units) and "state the idealization" (physics).

Second invariant — **every table carries its conditions in the reply, not just its source.**
Ampacity depends on insulation temperature rating, ambient temperature, and conductor count in a
raceway. A bare "10 AWG = 30 A" is confidently wrong roughly as often as it is right. Either the
conditions ride along in the answer or the table does not ship.

## 2. The offline inversion (this contradicts a premise in the roadmap, deliberately)

`level3-roadmap.md` states the **base-relayed insight**: the mesh is off-grid but the bridge host
usually has internet, so fetch capabilities relay the connected world to the off-grid edge — "a
feature, not a contradiction."

That is correct for the current single-operator setup. It is **conditional, not general**, and the
condition fails exactly when the capability matters most. For a community in the field, the moment
Cal earns its place is the moment the base is *also* offline. Then weather, propagation, NOAA
alerts and any live feed go dark together, because they share one dependency.

**Compute and TABLE have no such dependency.** They are the resilient core; fetch is the fragile
layer on top.

This does not delete the base-relayed insight — relaying internet knowledge to the edge remains
genuinely valuable on an ordinary day. It reorders priority: **build the capabilities that survive
the bad day first**, and treat fetch capabilities as enhancements that must fail loudly and safely
when the base is dark. Sun/moon is already compute and already near the top, so it is unaffected.
Propagation is fetch, and on this argument it drops below the resilient tiers.

## 3. Organize by situation, not by doer

The roadmap is indexed by doer, which is the right index for *building*. It is the wrong index for
*deciding what to build*, because no one in the field has a compute-shaped problem — they have a
wiring problem or a darkness problem. A field reference is indexed by situation.

Proposed slate, resilient-first. Every row is offline-capable unless marked.

**Channel** per Bob's Q3: a compute answer is short enough to broadcast; a table row carrying its
conditions is not.

| Situation | Content | Doer | Channel | Status |
|---|---|---|---|---|
| Antenna / radio | wavelength, quarter-wave, path loss, dBm↔W, Ohm's law | compute | broadcast | **BUILT** (eval 132, reviewed) |
| Wiring and power | wire gauge, ampacity, voltage drop, fuse sizing | **table** + compute | **DM** | proposed |
| Measuring and building | length, area, acreage, lumber, fasteners | compute + **table** | mixed | partly built |
| Load and rigging | rope / chain / cable strength, knot efficiency | **table** | **DM** | proposed |
| Darkness and timing | sunrise/set, twilight, moonrise/set, phase | compute | broadcast | CANDIDATE (roadmap) |
| Signaling and orientation | phonetics, Q-codes, Morse, prosigns | knowledge (small, closed) | broadcast | CANDIDATE (roadmap) |
| Navigation | great-circle distance/bearing, Maidenhead grid | compute | broadcast | CANDIDATE (roadmap) |
| Water, exposure, signaling for help | conservative survival basics | knowledge — **hardest gate** | **DM** | CANDIDATE, do last |

First aid stays exactly where the roadmap put it: last, strictest curation, explicit
not-medical-advice edge, and **scope limited to signaling, exposure and water** — nothing
resembling diagnosis, dosing, or procedure. Life-safety is where confident wrongness is
unacceptable, and it is the one tier where "we didn't ship it" is an acceptable outcome.

## 4. What a community changes

Everything above assumes the caller is Dean's fleet. A community service inverts that: **strangers
become the default caller, not the edge case.**

- **`unknown-sender-tier.md` gets a population.** Session 118 shelved it because it had no real
  members — one synthetic node across the responder's entire life. A community supplies the
  members, and that spec should be reopened rather than left as-is.
- **Airtime is the binding constraint, not content.** Every answer on a shared channel is paid for
  by everyone in range. A useful reference invites traffic, and traffic is the cost. The **global**
  rate budget remains the only control that survives node-ID spoofing; per-node limits do not.
- **Terseness stops being a style rule and becomes the product constraint.** A table row that
  cannot be said in one short transmission, with its conditions attached, is not shippable as-is.
  This is a real filter on the content slate and should be applied before curation effort is spent.

## 5. Sourcing — and the copyright line

Pocket Ref (Glover, Sequoia Publishing) is the reference model for *what tables are worth having*.
It is not a source to transcribe. Individual facts and measurements are not copyrightable, but a
particular **selection and arrangement** of them carries thin compilation copyright.

Rule: use it to decide **which tables earn their place**, then source every value from a primary
authority — NIST, ASTM, NEC (via a citable secondary), the ARRL Handbook, manufacturer data — and
record source + last-verified date per table, exactly as the knowledge tier requires.

## 6. Rollout

Unchanged from the existing gate, per pack rather than per tier: each table pack ships **default
OFF**, and arms only after its own offline eval and an independent adversarial review.

Table-pack eval must cover: correctness of every row against its cited source; **refusal between
rows and outside range** (the new invariant); conditions present in every reply; staleness /
last-verified; output length within one transmission; and false-fires (a question that merely
contains a gauge number is not a lookup).

## 6a. Review outcome (Bob, 2026-08-15)

Q1, Q2 and Q4 are **settled**; Q3 is **answered against the draft** and changes the design.

- **Q1 — TABLE is a fifth doer: confirmed**, with a sharper argument than the draft's. Folding it
  into COMPUTE loses the interpolation invariant *because formulas are continuous* — interpolating
  between two formula outputs is legitimate, and between two table rows it is not.
- **Q2 — the offline inversion holds.** *"You don't build the fire department for ordinary days."*
  With the nuance already in §2: it reorders priority, it does not demote fetch.
- **Q3 — conditions do NOT fit a 5-7 word broadcast. The draft underweighted this.**
  `10 AWG 30A 75C` is five words and conveys almost nothing to a reader who does not already know
  what 75C means, and it still drops ambient temperature and conductor count. The conditions
  invariant (§1) and the broadcast budget are in direct conflict for most table content.
  **Resolution: TABLE is primarily a DM-tier capability.** On the public channel Cal acknowledges
  the question and answers by authenticated DM, where the budget is 200 chars rather than 5-7
  words. Consequences, both adopted below: the content slate needs a broadcast/DM column, and
  **the unknown-sender tier cannot serve TABLE content at all** — a stranger on the public channel
  has no DM path back, so they get an acknowledgment and nothing more.
- **Q4 — the life-safety line stays where it is.** Signaling, exposure, water. Above that line a
  trained human is the source, not a radio.
- **Adopted addition — the rate budget is the capacity plan, not just a safety control.** A table
  tier that is genuinely useful generates traffic, and on a shared mesh traffic is the cost. Table
  content should be designed for **low query frequency per user**: you ask for ampacity once and
  remember it. Content that invites repeat querying is the wrong content for this channel.

## 7. Open questions for review

1. Is TABLE genuinely a fifth doer, or is it COMPUTE with a lookup instead of a formula? I argue
   it is separate because it introduces an invariant neither compute nor knowledge has
   (no silent interpolation / nearest-match), but I would rather be talked out of it than carry a
   taxonomy that is wider than it needs to be.
2. Does the offline inversion in §2 hold, or is it over-rotating on an emergency scenario that is
   rarer than the ordinary day fetch serves well?
3. Is "conditions ride along in every reply" affordable inside a 5-7 word broadcast budget, or does
   the table tier only make sense on the authenticated-DM path with its larger budget? If the
   latter, that is an argument the content slate should be reordered around channel, not situation.
4. Where does the life-safety tier's line actually sit? "Signaling, exposure and water" is my
   conservative read; it may still be too wide.
