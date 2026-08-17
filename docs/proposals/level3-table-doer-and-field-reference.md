# Proposal — a TABLE doer, and a situation-first field reference

**Read `level3-calc-and-knowledge.md` first** — this amends its doer taxonomy and inherits its
safety invariants. It also proposes a reordering of `level3-roadmap.md`.

*Draft by Cal, 2026-08-14 · design review requested (Bob).*
*Origin: Dean's framing — "a Pocket Ref over the air," Cal as the knowledge provider for a mesh
community in the field.*

> **STATUS: ADOPTED / LANDED 2026-08-17.** Both review rounds are complete and all seven questions
> are settled (§6a, §6b). `level3-roadmap.md` and `level3-calc-and-knowledge.md` have been updated:
> TABLE is in the doer taxonomy, the build order is reordered resilient-first, and the table packs
> are in the catalog with the 180-char fit measurement as a precondition on each. Until this landed
> the two docs disagreed about what to build next — the roadmap still led with propagation (fetch),
> which §2 demotes. **First build under the new order is sun/moon (compute), then the wire-gauge
> TABLE pilot.** The fit measurements for wiring, rigging and fasteners were commissioned the same
> day; results are recorded in §8.

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

Ordered by Q2: **lookup-y first** (you re-ask it every time, so Cal replaces a book you are not
carrying), memorable last (you ask once and carry the answer, so Cal barely beats a laminated card).

| Situation | Content | Doer | Kind | Channel | Status |
|---|---|---|---|---|---|
| Load and rigging | rope / chain / cable strength, knot efficiency | **table** | lookup-y | **DM** | ❌ **REFUTED 2026-08-17 (§8.2)** — ships only as a refusal + tag pointer, plus a chain tie-down WLL slice |
| Fasteners and materials | bolt grades, torque, drill/tap, lumber spans | **table** | lookup-y | **DM** | ✅ **fit measured (§8.3)** — both subsets fit; drill is tighter than torque, inverting the prediction |
| Wiring and power | wire gauge, ampacity, voltage drop, fuse sizing | **table** + compute | mixed | **DM** | ✅ **fit measured (§8.1)** — fits with zero margin; **pilot pack** |
| Antenna / radio | wavelength, quarter-wave, path loss, dBm↔W, Ohm's law | compute | memorable | broadcast | **BUILT + ARMED** (eval 214, 3 review rounds) |
| Darkness and timing | sunrise/set, twilight, moonrise/set, phase | compute | lookup-y | broadcast | CANDIDATE (roadmap) |
| Navigation | great-circle distance/bearing, Maidenhead grid | compute | lookup-y | broadcast | CANDIDATE (roadmap) |
| Measuring and building | length, area, acreage | compute | memorable | broadcast | partly built |
| Signaling and orientation | phonetics, Q-codes, Morse, prosigns | knowledge (small, closed) | memorable | broadcast | CANDIDATE (roadmap) |
| Water, exposure, signaling for help | conservative survival basics | knowledge — **hardest gate** | lookup-y | **DM** | CANDIDATE, do last |

Compute capabilities stay on broadcast even when lookup-y: they are short enough to say in one
transmission, which is the constraint the channel split exists to respect.

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

**Standing rule, promoted out of the rigging measurement (§8.2) because it generalises: NEVER
split a table answer across two messages.** Measured, message 1 of 2 carries a bare number at ~90
characters and is actively dangerous if message 2 is dropped — which on LoRa it sometimes will be.
An answer whose correctness depends on a second packet arriving is worse than no answer. If the
honest reply does not fit in one transmission, the reply is a refusal, not a continuation.

**Second standing rule, from §8.1 and §8.3 together: source the values from a primary authority,
never from retail or aggregator pages.** Both measurements found live errors in exactly that
layer — a tie-down retailer publishing a design factor wrong by 33%, a fastener aggregator
publishing two incompatible holes under one heading, and a manufacturer's own chart carrying an
arithmetic typo. Secondary sources also carry stale cells silently (§8.1's two contested NEC
values). Where sources disagree, **show the spread or refuse — never average**, because averaging
is interpolation and §1 forbids it.

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

## 6b. Review round 2 (Bob, 2026-08-15) — Q2 inverts the slate

All three questions answered. **Q2 reverses its own premise and changes the ordering.**

- **Q1 — 180 chars is workable, with a method.** Before curating any pack, write out the LONGEST
  plausible answer *with full conditions* and measure it. Fits → ship. Does not → that table needs
  a shorter format convention, or it does not ship. Do not lower information density to fit the
  budget: *"if you can't say it honestly in 180 chars, you shouldn't say it dishonestly in 62."*
  Expectation: wire ampacity with conditions fits; rope strength with load factors, safety margins
  and material specs probably does not. **That measurement is now a precondition on every pack.**
- **Q2 — the low-query-frequency rule argues AGAINST memorable content, not for it.** Memorable
  content (ampacity, standard conversions) is asked once and carried, so Cal's value over a
  laminated card is marginal. **Lookup-y content is where Cal earns his place** — rope strength,
  fastener specs, fuse sizing: things you cannot remember and should not trust memory for when
  they are safety-critical. But lookup-y content is also the highest airtime cost, so the
  resolution is a channel split: **lookup-y on DM only** (one transmission per query),
  **memorable on broadcast** (one query per lifetime per user, low aggregate cost).
- **Q3 — silence on broadcast, not an acknowledgement.** An ack is a transmission carrying zero
  information for everyone except the asker. On the allow-list they get the DM anyway; unknown,
  they cannot get a DM at all, so the ack is a promise of nothing. Any courtesy ack belongs to
  the **unknown-sender tier**, not to TABLE.

**Consequence for §3:** the slate below is reordered — lookup-y packs rank above memorable ones,
inverting the draft's order, which had put the most-memorable content first because it was easiest.

## 8. Fit measurements (§6b Q1 precondition) — commissioned and run 2026-08-17

The rule from Bob's round 2: before curating any pack, write the LONGEST plausible answer *with
full conditions* and measure it against 180 chars. Do not lower information density to fit.
Measured independently per pack, sourced from primary/citable authorities.

### 8.1 Wiring and power — **FITS. Cleared to be the pilot.**

Every question shape tested fits under 180 with honest conditions attached. But there is no slack:
the hardest realistic shape — 10 AWG with the ambient derate *and* the conductor-count derate
stacked — lands at **exactly 180 characters**. One more digit in the gauge or one more citation
puts it over.

| Shape | Chars | Verdict |
|---|---|---|
| "what wire for 30 amps" | 157 | fits |
| "ampacity of 10 gauge" (3 columns) | 193 spelled out / **148 abbreviated** | fits only abbreviated |
| "10 AWG at 40C ambient" | 181 spelled out / **152 terser** | fits only abbreviated |
| "6 conductors in conduit, 12 AWG" | 158 | fits |
| "wire for a 100A subpanel feeder" | 164 | fits |
| both derates stacked (hardest) | **180** | fits with zero margin |
| 3 columns + both derates | 174 | fits |

**Three findings that change the design:**

1. **The last-verified date cannot ride in the reply.** It costs ~14 chars and pushes the hard
   shapes over (measured: 181 with it, 166 without). Resolution: the reply carries
   `NEC 2023 T310.16` — edition plus table, which is the citation that matters for safety — and
   the verification date lives in table metadata, reachable by a separate query. This is a
   **measured impossibility, recorded as a deliberate deviation**, not a silent drop. §1's
   invariant is "conditions ride along", and conditions do; the date is the part that cannot.
2. **Abbreviation is mandatory, not stylistic.** The fully spelled-out phrasing fails on three of
   eight shapes. `CCC` (current-carrying conductors) has to be a term the pack teaches, or the
   answers are unreadable to the people who need them.
3. **Under-specified questions are the real length risk.** When the asker omits the insulation
   rating the honest reply carries two numbers; omit termination rating too and it carries four,
   which does not fit. That case must become a **refusal that asks for the missing input** —
   measured at 169 chars, so it fits.

**Two cells are contested and must not ship unconfirmed.** 3 AWG @ 90 °C reads 115 in four
secondary sources and 110 in two; 1 AWG @ 90 °C reads 145 vs 150. The low values appear to be
pre-2014 NEC. Four-source majority favours 115/145, but a **confirming read against a printed NEC
310.16 is a gate on those two rows** — secondary sources demonstrably carry stale cells.

Also captured for the pack: the 60/75/90 °C table (14 AWG–2/0 Cu), ambient and conductor-count
derating tables, conductor resistance and circular mils for the voltage-drop compute half, the
NEC 110.14(C) termination-temperature limitation (the 75 °C column governs most real terminations
regardless of insulation), and the 240.4(D) small-conductor rule. **Explicitly NOT sourced, and
therefore refusal territory:** aluminium, free-air (T310.17), direct burial, and NEC Chapter 9
Table 9 — which means **AC voltage drop cannot be approximated from the DC resistance table.**

### 8.2 Load and rigging — **DOES NOT SHIP.** The slate's highest-value pack is refuted.

This is the result the precondition exists to produce, and it arrived before a single row was
curated. Bob predicted rope strength "probably does not" fit. It does not — but the length was
never the real finding.

**Measured, number-independent:** the mandatory conditions scaffolding alone — material, MBS vs
WLL labels, design factor, the DF-varies caveat, the knot / shock / wear / angle voiders, source,
date, not-for-overhead, tag pointer — written maximally terse **with zero digits** is **211
characters. Over budget by 31 before any number is added.** Every honest answer measured landed
between **181 and 213**. Of 25+ variants written, **every one that fit did so by dropping a safety
condition**; the variant that carried all of them measured 204.

**But the disqualifying finding is regulatory, not typographic.** OSHA **deleted these exact
tables** — 1910.184 in 2011, 1926.251 in 2012 — on the stated grounds that they were "obsolete and
no longer conform to the load capacity tables of the updated B30.9 industry standards." 1910.184(e)(5)
now reads literally `[Reserved]`. They were replaced by a duty to read the sling tag. **Shipping a
static rigging table over LoRa in 2026 rebuilds the artifact the regulator retired for being
dangerous.** And "missing or illegible sling identification" is the *first* removal-from-service
criterion in B30.9 for every sling type — so there is no scenario in which a radio is the correct
fallback for a missing tag. The capability would be most tempting exactly where it is most wrong.

**Three data findings that independently kill the rope rows:**

1. **No manufacturer publishes a working load limit in pounds for 3-strand rope.** Every one
   publishes only a divisor (CI "5–12 to 1", Samson "20% of rated break strength", NER "at least
   1:5"). The WLL figures on distributor pages are distributors applying different divisors to
   different bases — 330 lb vs 124 lb for the same 1/4" rope. Applying the published divisor range
   to the published strength spread gives an honest answer span of **3.4x at 1/2", 3.9x at 3/8"**.
2. **The published strengths spread 41% at 1/2" and 62% at 3/8" — and the columns are not the same
   quantity.** Samson publishes *spliced*, NER publishes *unspliced free-length*, CWC publishes a
   true statistical minimum; two sources state neither. Samson's own catalogue warns that
   cross-manufacturer comparison requires spliced figures. A diameter-keyed lookup cannot resolve
   this, and averaging is interpolation, which §1 forbids.
3. **"3-strand polyester" is not a specification.** New England Ropes' own three 3-strand polyester
   products read 7,500 / 6,200 / 3,650 lb at 1/2" — a 2:1 range inside one catalogue.

**Knots are worse than uncertain — the number describes the wrong hazard.** Bowline retains
41.8–75% across the verified corpus. For the clove hitch **no defensible figure exists**: the
largest rescue-knot test programmes did not test it, the meta-analysis deliberately omits it, and
the circulating 60% traces to a self-disclaimed chart for *manila*. The one peer-reviewed test
found it **rolled at 898 kg and broke at 1,081** — it slips before it breaks, so a strength
percentage answers a question the user did not ask. CMC, holding the most test data of anyone,
recommends abandoning per-knot numbers entirely and assuming 50%.

**Sling angle is settled physics (sin θ) and still dangerous to transmit briefly**, because of a
convention trap: modern ASME/OSHA measures from **horizontal**, legacy 1910.184 tables from
**vertical**, and the two **agree only at 45°** — so the one angle a user might check is the one
that cannot reveal the error. Also, one "choker derate" is wrong for two of six sling types
(B30.9: chain 80%, mesh 100%, synthetic rope 75%, webbing 80%).

**What CAN ship from this pack, measured and cited:**

| Content | Chars | Verdict |
|---|---|---|
| Chain tie-down WLL, one grade, all conditions, cited | **165** | ships |
| Refusal + tag pointer | **138** | ships |
| Ask-back on sling angle ("from horizontal or vertical?") | **153** | ships |

Chain works for reasons that **deliberately do not generalise**: NACM publishes WLL directly with
an exact design factor per grade (G43 3:1, G70 4:1), the figures are fixed in federal law via
49 CFR 393.104(e)(2), independent sources agree exactly, and restricting to *tie-down* makes the
hitch, angle and knot conditions inapplicable. Two conditions still ride along verbatim: **never
overhead** (G80/100 only) and **the breaking-force column is not a design value**.

**A retail-source trap worth recording:** a major tie-down retailer states both grades are
"approximately one-third of their break strength" — wrong for G70 by 33% — and misprints G70 3/8"
as 6,660 against NACM's 6,600. A pack sourced from retail pages ships a wrong design factor.

**And a delivery finding that generalises to every pack:** do **not** split a long answer across
two messages. Measured, message 1 of 2 carries a bare number at 90 chars and is lethal if message
2 is dropped — which on LoRa it sometimes will be. **An answer whose safety depends on a second
packet arriving is worse than no answer.** This should be a standing rule for TABLE, not a rigging
footnote.

**Consequence for the slate in §3:** "Load and rigging — highest value" is **refuted as a table**.
It is reclassified: a refusal-with-pointer plus the chain tie-down slice. The value was real; the
deliverable was not.

**Residual unverified, deliberately left so:** Cordage Institute primary text (403/paywalled — the
5:1–12:1 range rests on third-party restatement only), NER Classic Polyester at 5/8" and 3/4"
(the manufacturer's page and its own catalogue disagree on which diameter a stock code is), WSTDA
primary text, and clove-hitch efficiency — which should be treated as **permanently unavailable**
rather than pending.

### 8.3 Fasteners and materials — **FITS, both subsets — and the measurement inverted the prediction.**

The prediction going in (mine, written into the commission): tap and drill sizes would "fit
easily" because they are short lookups with few conditions, while torque might not, because it
carries grade + size + lubrication + a joint caveat. **The measurement says the opposite.**

The tightest reply in the entire 19-candidate corpus is a **clearance drill** answer at **178
characters — two characters of headroom.** The hardest torque reply came in at **175**. The thing
expected to be easy nearly broke the budget; the thing expected to fail had more room.

The reason is the finding, not the number: **drill answers carry three conditions, and almost no
published chart states any of them.**

1. **Target thread engagement / material.** 3/8-16 is 5/16 (.3125) at 77% for aluminium but Q
   (.332) at 53% for steel — a different drill, not a different tolerance.
2. **Cutting vs forming tap.** A roll tap needs a hole ~0.035" larger, and **no two makers agree**:
   1/4-20 is .2280 (Greenfield), .2264 (EMUGE), .2250–.2300 (Balax, by % target). Only four charts
   of seven distinguish cutting from forming **at all**; the rest publish cutting data and never
   say so.
3. **Which clearance standard.** See below — this one is a live confident-wrongness trap.

Torque, by contrast, turned out to be the *better-documented* subject: torque charts are legally
cautious enough to print their K factors and their disclaimers, so the conditions are fewer and
already explicit.

**The trap worth the whole exercise — a naming collision that silently drills the wrong hole.**
LittleMachineShop's "Close Fit" for 1/4" is F (.2570) where ASME B18.2.8 says 17/64 (.2656); its
"Free Fit" (.2660) is numerically **ASME's CLOSE fit**. The whole chart is offset by one step, so
a user asking for a free-fit hole and getting the LMS number drills ~0.015" tighter than the
standard intends **while believing they have the loose one**. Two sources, same words, different
meanings. This is exactly the shape of the ampacity problem in §8.1 — a bare number that is
confidently wrong about half the time — and it is invisible without naming the standard.

**Three additions to the pack's build requirements:**

5. **Never emit a clearance hole without naming the standard in the reply** ("ASME B18.2.8").
   Treat LittleMachineShop's close/free columns as a *different standard*, not a synonym.
6. **Cutting vs forming is a required condition**, defaulting to cutting with the default stated
   in-reply. Do **not** carry per-maker forming data and do **not** average it — three makers
   disagree at every size with no consensus to average toward, and averaging is interpolation,
   which §1 forbids. Point the asker at their own tap maker's chart (measured 162 chars, fits).
7. **Drop Bolt Depot as a source.** It publishes two incompatible holes under a single "75%"
   heading at two of five sizes; its fractional column is effectively a 65% drill sold under a
   75% label.

Source quality here is materially worse than for wiring: most drill charts state no basis at all,
and one manufacturer's own published chart carries an arithmetic typo (Greenfield prints .4682
for 15/32, which is .4688). **Every number in this pack needs its basis shipped beside it** — a
conclusion the character budget alone would never have produced.

*Note on completeness: the underlying torque and clearance tables were delivered as a base report
plus this addendum; the addendum is what is summarised here. Retrieve the base tables before
curation — the fit verdict is settled, the row data is not yet in hand.*

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
