# Proposal — the unknown-sender tier ("we hear you")

**What Cal does when someone who isn't on the allow-list says its name**

*Draft by Cal · v1 2026-08-09 · re: `github.com/deanssamclaw/cal-mesh` · sits under
`channel-trust-and-agency.md` §3 (public channel), and uses the selection pattern from
`level3-weather-intent-layer.md` §2*

---

## 0. What this is

Today an unknown sender who addresses Cal gets **nothing**. The message is received, logged,
verdicted `sender_not_allowed`, and dropped. That was the right call when we armed the responder —
silence has no failure modes. It is also, on reflection, the wrong long-term behavior, and this doc
says what should replace it.

**Dean's framing, which is the requirement:** *"I don't like acting like we don't hear them."*

This is a design proposal, **not a build plan**. Three decisions in §7 are open and are the
operator's to make. Nothing here should be built before those land and before the spec has been
through a refute-it review (§9).

---

## 1. Silence is not neutral on a public channel

The motivating incident, from the live logs (2026-08-09):

1. A node on the allow-list asked Cal a question. Cal answered on the **public broadcast channel**,
   where everyone in range sees it.
2. Twenty-four minutes later a **different, unknown node** — a tracker-class device, heard at good
   SNR — sent a short reply into the same conversation.
3. Cal said nothing. Verdict logged: `sender_not_allowed`.

The intent of that silence is *"you are not authorized to task me."* What it actually transmits, to
someone who just watched Cal answer a different node on the same channel, is **"I am ignoring
you."** On a shared broadcast medium, **selective silence is visible**, and visible silence is a
message whether or not we meant to send one.

Two consequences worth separating:

- **Courtesy.** Being snubbed by a machine on a community mesh is a poor way to introduce a project
  we publish openly and invite people to fork.
- **Honesty.** We publish a dashboard and a repo describing exactly what Cal does. Behaving on air
  as though nobody spoke is the one place the system is quietly less transparent than its own docs.

There is a real argument for keeping silence — §2 makes it — but "silence is the neutral default"
is not that argument, and should stop being used as one.

---

## 2. What actually changes — and the metric the existing ladder can't supply

`channel-trust-and-agency.md` §4 grades unlocks by **forge-damage**: *what breaks if the sender
auth is forged?* That metric is load-bearing for the private tier and **does not apply here at
all** — the public channel already assumes every sender is hostile and spoofable (§3, tier P0).
There is no trust to forge into. Nothing about answering strangers weakens sender-auth reasoning,
because we never had sender auth on this channel.

**The governing metric for this tier is different: amplification.** *How much shared airtime, and
how much of someone else's attention, can one attacker action buy?*

| Risk | Real? | Why |
|---|---|---|
| **Airtime consumption** | **Yes — the primary risk.** | LoRa airtime is physically shared across the whole local mesh. Anyone in radio range can now make Cal transmit. Rate limiting stops being hygiene and becomes the security control. |
| **Reflection / third-party targeting** | **Yes.** | Source IDs are unauthenticated. An attacker can spoof node X and induce Cal to transmit *at* X, who never spoke. Cal becomes a small reflector pointed at a neighbor. |
| **Discoverability** | **Yes, and it's a genuine cost.** | Today Cal is inert to unknowns, so there is little reason to poke it. Answering advertises *there is an AI on this channel*, which invites probing. This is the cost of the feature, not a bug in it. |
| **Prompt injection** | **Only if we let a model write.** | Today no unknown-sender text ever reaches generation — the gate fires first. Naively opening this tier would put attacker-authored text into a generation prompt for the first time. §3 exists to prevent that. |
| **Sender-auth erosion** | **No.** | Nothing to erode; P0 already assumes hostile senders. |
| **Content/reputation risk** | **Bounded by §3.** | If Cal never emits model-authored prose to strangers, it cannot say something embarrassing to one. |

**Read that table as a design brief:** the dangerous axes are *volume* and *what Cal is allowed to
say*, and both are controllable by construction. The tier is safe if — and only if — it is
**enclosed** (§3) and **budgeted** (§5).

---

## 3. "Enclosed" should mean *no generation*, not *a careful prompt*

The strongest available reading of Dean's "completely enclosed box" is not a hardened persona. It
is: **no attacker-authored text reaches a model, and no model-authored text reaches the air.**

That is achievable, and it is stricter than what the allow-listed tier gets today.

### 3.1 The tiers

| Tier | Behavior | Model in the path? | Attacker text in a prompt? |
|---|---|---|---|
| **U0** | silence (today) | no | no |
| **U1** | one **fixed** acknowledgment string | **no** | no |
| **U2** | model **classifies** the message; harness emits a **pre-approved** reply from a fixed catalog | yes — as a classifier | yes, but its only output is a catalog index |
| **U3** | U2 plus the capabilities explicitly designed as safe-to-serve-anyone (weather) | yes — as a narrator over a harness-fetched fact | no (weather path already drops the message, `responder.py:174-183`) |

### 3.2 The mechanism (U2 — "the intelligent box")

1. Harness receives an unknown-sender message that addresses Cal.
2. Harness passes it to the tool-locked model with a **closed instruction**: *choose the single
   best-fitting response from this numbered catalog; reply with the number and nothing else.*
3. Model returns an integer.
4. **Harness emits the catalog string.** The model's prose never reaches the air.
5. Any output that is not a valid catalog index — extra text, out of range, timeout, empty — falls
   back to the **U1 fixed string**. Fail-safe, same discipline as `fetch_current` returning `None`.

The attacker influences **selection among operator-authored sentences**. That is exactly the
pattern `level3-weather-intent-layer.md` §2 argued for after the focus-tag experiment showed that
steering *prose* produces confident fabrication: **classify to select, never to author.** Two
unrelated problems converging on the same shape is the strongest evidence we have that it's right.

### 3.3 The property this buys — machine-verifiable output provenance

Because every emitted byte comes from a fixed catalog, the tier satisfies a check the allow-listed
tier cannot: **the transmitted string is byte-identical to a catalog entry, or it is a bug.**

That is precisely the *positive provenance* check `level3-weather-intent-layer.md` §5 says the eval
suite is missing. It is trivially assertable here, and it should be written **before** the tier
ships, not after — a one-line invariant that no future change can quietly erode.

### 3.4 Sketch of a catalog

Operator-authored, short (5–7 words, mesh etiquette), and honest about the mechanism — the repo and
dashboard already document all of it, so disclosure costs nothing:

- *"Heard you. I answer my operator's nodes."*
- *"Cal here — listening, not taking requests."*
- *"Hello. I'm Cal, an AI on this mesh."*
- *"Heard you. Details at the project page."*
- *"Can't help with that one, sorry."*

Catalog size should stay small. Every entry is a sentence Cal might say to a stranger, and each one
needs to be defensible on its own with no context around it.

---

## 4. Direct-message the sender; do not broadcast

Every responder reply today goes to `^all` (confirmed in the sent log). For this tier that is wrong
on two counts:

- **Etiquette** — everyone in range pays airtime for a courtesy note addressed to one node.
- **Scaling** — ten strangers becomes ten broadcasts the whole mesh sits through.

**Reply by DM.** One node's airtime, no channel spam, and the courtesy still lands. This single
choice removes most of the airtime objection and should be treated as non-optional for the tier.

Note the honest limit: a DM to a **spoofed** source ID is a DM to whoever really holds that ID.
That is the reflection risk in §2, and §5 is what bounds it.

---

## 5. Airtime governance — the load-bearing part

Not decoration. This *is* the security control for the tier.

- **One hello per unknown node per long window** (hours, not minutes). A stranger gets an
  acknowledgment, not a conversation. Caps amplification at ~1 transmission per node per window.
- **Global stranger budget** — a hard ceiling of unknown-sender replies per hour, mesh-wide.
  Because "one per node" is trivially defeated by spoofing many IDs, **the global budget is the
  real control**; the per-node limit is only good manners.
- **Flood behavior is silence.** Budget exhausted → back to U0, logged, no reply. Degradation must
  be toward quiet, never toward chattier.
- **Direct-heard preference** — consider replying only to nodes heard at **0 hops**. Weak (an
  attacker in range can still spoof), but it bounds *remote* abuse and costs nothing. Flagged as a
  candidate control, not a decided one.
- **Its own kill switch**, independent of `RESPONDER_ENABLED`, defaulting **off**. The allow-listed
  responder must be able to keep running while this tier is disabled.
- **Quiet hours** — worth considering. A courtesy DM at 3 a.m. is not a courtesy.

---

## 6. Where it slots, and what it changes downstream

**Gate order.** Unchanged up to the allow-list check. Today `sender_not_allowed` → skip; it becomes
`sender_not_allowed` → **unknown-sender tier**, which then applies its own gates (tier enabled?
addressed? per-node window? global budget? quiet hours?) and either emits a catalog reply or skips
with a specific reason. Every other gate — self, freshness, addressed, kill switch — runs first and
is untouched.

**Logging.** `decisions.jsonl` needs a verdict that is neither `replied` nor plain `skipped`.
Proposal: `acknowledged`, plus the catalog index that was chosen and the classifier's raw output
when it was rejected. A tier that silently changes what Cal says on air must be fully readable
after the fact.

**Dashboard.** The `OFF-LIST · heard, not answered` badge shipped today (`66c1bdd`) becomes
**`OFF-LIST · acknowledged`** with the catalog reply shown inline, using the pairing that already
exists. The FAQ entry explaining off-list behavior needs rewriting in the same pass. Pleasingly,
the view built this morning to make the gap visible is the view that will show the fix.

---

## 7. Open decisions — the operator's, not mine

**7.1 How far up the tier ladder?** U1 (fixed ack), U2 (catalog), or U3 (catalog + weather).
U3 is the one that makes Cal genuinely *useful* to a stranger in the field, which is what a mesh is
for — and weather is already argued to be safe-to-serve-anyone in `level3-weather.md`. It is also
the largest step. **Cal's recommendation: ship U1 first** — it is a few lines, it has no model in
the path, and it resolves Dean's actual objection immediately. Then U2, then U3, each on its own
review.

**7.2 One hello, or a conversation?** Recommendation: one acknowledgment per unknown node per long
window. Anything more makes Cal a public chat service, which is a different product with a
different threat model.

**7.3 Do we accept being discoverable?** This is the genuine cost and cannot be engineered away:
answering advertises that Cal exists and invites probing. Everything in §5 bounds the damage; none
of it prevents the attention.

---

## 8. Pre-registered objections (for whoever reviews this)

Stated up front so a reviewer can attack the strongest version rather than rediscover the weak one:

- **"The catalog is still attacker-steerable."** True — an attacker picks *which* operator-authored
  sentence Cal says. The claim is only that every possible output is one we wrote and would stand
  behind, so worst case is a **mis-selected** sentence, never an authored one. If a catalog entry
  would be damaging when selected at the wrong moment, that entry is the bug.
- **"A classifier is a model, so injection applies."** Yes — the model can be induced to return the
  wrong index, or malformed output. The former is bounded by the paragraph above; the latter falls
  back to U1. Neither puts attacker text on the air. Worth an adversarial test regardless.
- **"Silence really is safer."** Correct, strictly, on every axis except the one Dean raised. This
  doc argues the courtesy and honesty gain is worth a bounded, budgeted airtime cost. That is a
  judgment call, and it should be argued on those terms rather than by pretending the cost is zero.
- **"The global budget is guessable, so an attacker can exhaust it to silence Cal."** True, and
  accepted: the failure mode of an exhausted budget is *Cal goes quiet to strangers*, which is
  today's behavior. Denial-of-courtesy, not denial-of-service.
- **"DM replies are unverifiable and thus a reflector."** Bounded by the global budget and the
  0-hop preference, not eliminated. The amplification factor is ~1 short DM per admitted message,
  which is a poor reflector by construction — but it is not zero and should not be described as
  such.

---

## 9. Build & review plan

Same gate as everything else in this project: **default-OFF → spec → refute-it review → eval → arm
on explicit operator go.**

1. Land this spec; get the §7 decisions.
2. **Adversarial review before any code** — reviewers instructed to refute, and to *run* things
   rather than reason about them. That discipline has now caught a location leak in the responder's
   context and a fabricated-forecast failure mode that inspection alone missed twice.
3. Build U1 behind its own default-off switch. It needs no model and no catalog.
4. Write the §3.3 provenance assertion **before** U2 exists.
5. U2, then U3, each with its own eval and its own arming decision.

---

## 9a. Addendum, 2026-08-11 — two of this tier's controls rested on broken data

Added after a review of the live exchange stream, before any of this was built. Kept in the
doc rather than quietly fixed, because the near-miss is the useful part.

**What was wrong.** Two capture bugs in the bridge, both silent:

- The **sender's id** was dropped for any node not yet in the local node database — i.e. at
  **first contact**. A "Hi" on 2026-08-11 was logged from nobody; the sender's introduction
  arrived eleven minutes later and it had been `!ba0cc0c0` all along.
- The **hop count** was recorded as unknown for any packet that used its entire hop budget,
  because the radio library's dict view omits a `hop_limit` of zero. The most-relayed packets
  were exactly the ones being discarded.

**Why it matters here specifically.** This tier is *defined* by first contact. §4 replies by DM
to the sender's id, and §5 budgets per sender id — both key on the one field that was
systematically missing for precisely this population. §5's "direct-heard preference — reply only
to nodes heard at 0 hops" rested on a hop count that was null on 14 of 15 records, and null
*specifically when the message had been relayed*, which is the case it exists to exclude. Built
as written, on that data, the per-node budget would have collapsed every stranger into a single
null bucket and the 0-hop control would have failed toward whichever default the comparison
happened to pick.

Both bugs are fixed and guarded by `eval_routing.py`. The controls are viable as specified.

**The general lesson, which outlives this tier.** Every control in §5 is a claim about a *field*.
A spec that reasons about behavior without checking that the underlying field is actually
populated — and populated for the population it targets — is not yet a spec. The bugs were
invisible to inspection and produced plausible output; they were found only by comparing Cal's
log against a second, independent receiver's log of the same air. **Before building a control,
verify its input on real captured data, for the specific senders it will govern.**

**Unchanged by this:** node ids remain unauthenticated and spoofable, so the global budget is
still the real control and the per-node limit is still only good manners (§5).

---

## 10. Relation to the other docs

`channel-trust-and-agency.md` grades the **private** tier by forge-damage; this grades the
**public-unknown** tier by amplification (§2). `level3-roadmap.md` says what Cal can do; this says
whether a stranger gets any of it. `level3-weather-intent-layer.md` §2 supplies the mechanism —
classify to select, never to author — and §5 supplies the provenance check this tier makes
trivially enforceable.
