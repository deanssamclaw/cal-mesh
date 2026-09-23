# Proposal — not everything earns a reply

**Measured: how much more Cal could say on the open channel, and why almost none of it is worth
saying**

*Cal · v1 2026-09-23 · re: `github.com/deanssamclaw/cal-mesh` · follows
[`local-system-one.md`](local-system-one.md) · **nothing built by this document***

---

## 0. The question that started it

The routing work ended with an option left open: widen the second opinion from messages addressed
to Cal to the whole channel. On the routing corpus that widening looked large — 44 of 48 fixes
were messages nobody had addressed to Cal. So: **which classes of message should Cal start
answering, and what evidence should earn that?**

The plan was a per-class gate: a class earns a door when enough graded simulated replies in that
class come back good, and a post-reply reviewer can close the door again. The measurement below
is why the door half of that plan is shelved.

## 1. What is actually behind each door

Every simulated reply on record — **548 drafts, 377 unique messages, 45 days** — scored by the
local System One scorer (`system-one` on jlab), then restricted to the messages Cal is **currently
silent on**, with the same floor and guards the router uses.

| Door | Messages Cal is silent on | Would newly answer | Per week |
|---|---|---|---|
| Signal report | 10 | **1** | 0.2 |
| Weather | 6 | 1 | 0.2 |
| Greeting | 11 | 2 | 0.3 |
| Capability menu | 2 | 0 | 0 |
| **Model prose** | 273 | 91 | **14.2** |

**The armed capabilities already catch what they can.** Cal answered 74 distinct messages in that
window and was silent on 302, and of those 302 about **four a month** are ones a doer could answer
well. The widening is worth roughly one message a month per door.

**The only large door is the one the evidence says to keep shut.** Model prose on un-addressed
chatter is 14 messages a week, in a class that graded `good` **once in eighteen**.

**And the weather door opens onto a mistake.** Its single candidate is *"Storm is brewing"* — a
statement, not a question. The clean label set calls it conversation. The first thing through that
door is a wrong answer.

## 2. Why "should Cal speak?" is not a question for a model

Asked directly, on 26 graded drafts, the scorer answered **0.49–0.51 for every verdict class
alike** — `doer`, `good`, `wrong`, `harmful`. That is not a weak model. It is the honest shape of
the evidence: the `harmful` verdicts were harmful because of **when** Cal spoke — 20 seconds after
somebody else's exchange, into a run of reactions, 78 seconds late, in reply to an automated
alert. None of that is in the message text.

**The division of labour this forces:** the model says **what kind of message this is** (a typed
choice, well calibrated — 0.90 on a link ask, 0.97 on a capability question). The machinery says
**whether to speak**: the channel context, the cooldowns, the budgets, and the operator's arming.
This is not a workaround. It is the correct assignment of a decision to the component that holds
the evidence for it.

## 3. What to build instead

Silence is already this node's default, and the measurement says that default is right. The work
worth doing is not a wider door; it is proof that the few replies Cal does send are honest and
well-timed.

**A post-reply reviewer, mostly code.** Volume makes it affordable: **102 real sends, ever.**

1. **Deterministic invariants, no model.** Every number in a signal report equals the packet's own
   measurement; a weather reply's numbers equal the observation that was fetched; nothing goes out
   within N seconds of another exchange, into a run of reactions, or in reply to a packet whose
   shape is automated (the four real cases — severe-weather WATCH/WARNING, a position share, a
   weather-service relay — are fixed shapes a pattern matches exactly). Invariants are
   mutation-testable; a reviewer that cannot fail is decoration.
2. **The scorer only where it is calibrated:** which capability should have answered, does the
   message name another station.
3. **A model for prose, never the model that wrote the reply.**
4. **The operator on a sample.** All 44 grades on record read `by: cal-review`. For a system that
   licenses airtime on a shared channel, some verdicts should be Dean's.

**Asymmetric authority, which is the safety property:** the reviewer may **close** a capability by
itself — one harmful send disarms that class — and may never **open** one. The downside is bounded
automatically; the upside needs a person.

## 4. Shelved, with the numbers attached

**The broadcast widening is not built and should not be**, on this evidence. Re-open it if the
traffic changes shape: if the silent population grows a class that a doer could answer more than
a handful of times a month, or if a second measurement finds the `good` rate on model prose is
materially better than 1 in 18.

## 5. Two corrections to earlier claims in this repo's records

* **`faithful` is not a truthfulness signal.** `drafts.faithful_draft` answers "is this draft a
  faithful account of what Cal WOULD have said" and returns false whenever Cal actually replied.
  "203 of 548 drafts unfaithful" therefore says nothing about reply quality, and it was cited that
  way while scoping this work.
* **The automated-packet question is narrower than first reported.** It is decisive at the top
  (1.00 on a severe-weather WARNING and on a position share, 0.99 on a weather-service relay) and
  unreliable below ~0.97, where it flags humans ("Got you in Olathe. 7 hops.", "Cal test"). The
  first report of it rested on a single example. Its four real catches have fixed shapes, so the
  rule that acts on them should be a pattern, not a probability.
