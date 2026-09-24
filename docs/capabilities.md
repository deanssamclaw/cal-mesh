# Capabilities

*Each doer: what it answers, what triggers it, and what it will not do. The live list, with its limits, is on the dashboard's capabilities page. Moved out of the README on 2026-09-24, unchanged apart from link paths.*

## Capabilities (doers)
Each capability is `intent → deterministic doer → reply`. What varies is whether the **model is in
the answer path** — that is the whole safety story. Taxonomy and specs in `docs/proposals/`.

| Capability | Doer | Model in the number path? | State |
|---|---|---|---|
| Weather (current conditions, NWS) | fetch | narrates the fetched fact only | **ARMED** |
| Arithmetic / units / RF pack | compute | **no — Python owns every digit** | **ARMED** |
| Sun / moon / twilight | compute | **no — Python formats the whole reply** | **ARMED** — works offline |
| Signal report (range/signal test) | measured | **no — every number is the radio's own** | **ARMED** — runs *first*, see below |
| Capability list ("what can you do") | flags | no — composed from the live config flags | **ARMED** |
| Greeting ack (off-list senders) | fixed table | no | **ARMED** |
| Second-opinion routing (s1route) | picks a doer on the fallthrough | **no — it writes nothing; the doer answers** | **ARMED** 2026-09-24 (local scorer; Jev as backup) — see below |
| Wire gauge, fasteners (TABLE) | table | no — the harness returns the row | measured, not built |
| Load and rigging | — | — | **refuted, will not ship** (see below) |
| Proactive welcome (new node's first message) | — | — | **refuted, will not ship** (see below) |

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

### Why the proactive welcome is not here
The idea was good: a node Cal has never heard sends its first public message and gets one short
public hello, on the theory that a visible welcome draws people into the mesh. It was built
twice — once bare, once carrying the measurement — and **refuted both times, for different
reasons.** Kept here because the idea is attractive enough to be proposed again.

A **bare** hello is a claim, not an observation. "Good to hear you" reads identically whether the
newcomer arrived direct and strong or scraped in at seven hops, so it cannot show the one thing a
newcomer actually wants to know. This repo already paid for that lesson: sigreport exists because
the model once answered "Link's solid and steady over here" with no access to a number at all.

Attaching the **measurement** fixes that and creates something worse. sigreport's trilateration
residual is priced on the attacker having to ASK — they range test, and the answer goes to them.
A welcome broadcasts signal and hop count to `^all`, unsolicited, to someone who asked nothing.
Replayed over 29 days of real log: of 45 welcomes, **23 went to nodes that broadcast their own
GPS**, and the node DB says half this mesh does. That is a passive harvest of
`(known coordinate, RSSI into Cal, hop count)` on a schedule. Cal broadcasts no position
precisely to avoid being locatable; this hands the same thing out a side door.

And the trigger cannot be made to mean what it says. **"New to Cal's node database" is not "new
to the mesh"** — a node's first TEXT packet can arrive before its NodeInfo, so seeding from the
node DB still fired 13 times in replay. Cal is one node on a 313-node metro mesh and has no
standing to welcome a 7-hop stranger to it. On the real log the armed feature stepped on a
severe-thunderstorm broadcast to welcome the station issuing it, welcomed an automated weather
bot monthly, and welcomed mid-conversation replies ("No but I'm on a plane").

Measured by adversarial review over ~34,500 inputs, 2026-09-06. The implementation is on the
`welcome-measured` branch, disarmed, if the shape is ever worth revisiting.

## Signal report: the doer that runs first
Every other capability answers a question somebody chose to ask Cal. A range test is addressed to
*whoever can hear it*, so this one sits **ahead of the whole ladder** and answers senders who are
not on the allow list. That position is what makes its failure mode asymmetric: **a false fire is
a message no other capability will ever see.** A miss costs a reply; a false fire silently eats
somebody else's question.

So the shape rules are about who is being addressed, not about which words mean "test":

- **A greeting may precede the trigger.** `Hey Cal, this is a test` addresses Cal exactly as much
  as `Cal, this is a test`. Anchoring the strip to position 0 meant it did not, and the log has
  the miss: 2026-08-08, answered by the model while measured SNR sat on disk.
- **Talking about a test is not running one.** A determiner or possessive immediately before
  test/check makes the phrase a *reference* — `got the test`, `Cal's test`, `Cal aced the test`.
  Deliberately not a vocabulary of test words: that design already died on `tange test`. This is
  the short, closed list of words that turn any noun into a reference.
- **Another capability's word makes the message that capability's.** `weather check` is a weather
  question, and answering it from here means weather never sees it.

Being *named* is not being *asked* — the referential rule applies whether or not Cal is addressed,
because `Cal aced the test` is a sentence about Cal. That exemption was in the first draft and an
adversarial review refuted it in one line.

### Contact reports (2026-09-12)

A neighbour saying "Got you in Olathe" is running the same experiment a range test runs, in the
other direction: they are telling Cal they heard him. The reciprocal -- how Cal heard *them* --
is the one fact Cal holds and they do not.

Before this, the model answered those by feel. Read over the whole log: **27 replies asserted
link quality, Cal held the measured SNR and RSSI on the packet for all 27**, and four called a
link "loud and clear" at -15 to -19 dB SNR, at or past the usable floor. That is the failure this
module was built to prevent, happening in a shape the module did not recognise.

**Why this is not the proactive welcome**, which was refuted for broadcasting exactly this data.
The welcome fired on a node's *first message whatever it said*, so signal and hop count went to
someone who had raised no such topic. Here the sender opens the subject of reception with Cal
themselves -- the same consent a range test carries. That is the whole distinction, and it is the
load-bearing one.

Three conditions, all required, because this doer pre-empts every other capability:

1. **A receipt verb**, so the message is about reception at all.
2. **A second-person reference**, so the report is about hearing *Cal*. This is what refuses
   "I heard about that show. Never saw it" and "Pretty good! Just got into town."
3. **No other node named.** `871c I hear you in Lee's Summit` and `Hello 63d8 and 6404, both 3
   hops` are addressed to a third party, and Cal answering would barge into an exchange he is not
   part of. On the live corpus this is the condition doing the real work.

Measured on the full inbox at the time of writing: **fires on 8 of 386 messages**, all genuine.
A doer-word guard runs too, and here it must be *inflected* -- `Got you, is it raining?` fired in
testing because the closed list holds "rain" and the sender typed "raining".

The eval allows contact reports as a **class**, via a skeleton written by hand in the eval rather
than a call into `_is_contact_report`: a fixture built from the thing under test could only ever
pass. Breaking either condition, or the rule entirely, fails the suite.
