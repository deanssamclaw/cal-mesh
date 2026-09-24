# The decision trace, and what the page publishes

*How every reply's path is shown publicly, and what is deliberately coarsened or withheld. Moved out of the README on 2026-09-24, unchanged apart from link paths.*

## The decision trace
The dashboard is not a log viewer. For **every** reply it publishes *why that reply exists*: the
gate ladder and which gate failed, what the wording matched and why that selected a capability,
what was fetched and how old it was, which single fact crossed to the model, the model and
latency — and, for compute answers, that **no model ran at all**.

It shows machinery, never introspection. Generation is `--output-format text`; there is no
reasoning to display, and inventing one would publish narrative as if it were mechanism.

Two things this has already caught that testing did not: a diagram that read to a stranger as
*"your message failed to send"* when the message had arrived fine and was what caused the reply,
and a trace asserting one cause for a blank that had several. If the page cannot explain a reply
honestly, that is a defect in the reply.

**The page has two surfaces and they mean different things.** The ground is a warm mid neutral
(v6, 2026-09-05); the trace panel — and only the trace panel — is a dark well. The page is a
status board you scan, the trace is an instrument you read one record on, and dropping it well
below the ground gives the boxes, wires and dots a plane of their own. The ground moved off white
because the page had been at both ends and neither was it, and the middle is not the midpoint
between them: that is a mid grey no text sits on comfortably from either direction. Contrast is
measured rather than eyeballed, in both directions — a raised plane must be lighter than its
ground and a well darker.

**Every trace is bound to its message by packet id**, because a trace shown under the wrong
exchange is worse than no trace at all: it reads as evidence. The responder stamps the radio's
own id on each decision record and the dashboard joins on `(sender, id)`. Sender, text and
timestamp are all heuristics — `Cal test` sent twice from one node is genuinely indistinguishable
under them — and the fallback that still has to serve pre-id records now pairs *globally*
nearest, closest pair first across the whole set. Before that it walked record by record, and a
message whose own decision had been trimmed away could take a later duplicate's and render a
gate ladder it never earned, while the message that did earn it showed `no trace recorded`.
`eval_correlate.py` holds both ends.

## Neighbour position: a bucket, never a point

About half the neighbours broadcast their own position over the air. With `POSITION_GRID=true`
the bridge reduces that to a **Maidenhead locator** and stores only the locator — roughly
**3 × 4.5 miles** at 6 characters, or 70 × 110 miles at 4. `POSITION_GRID_CHARS` sets which.

Three properties make it safe to publish, and each is load-bearing:

- **Bucketed at capture.** `coarse_grid()` runs in `bridge.py` and the exact latitude and
  longitude are discarded in the same expression. Rounding at render time would leave the precise
  point sitting in a file that feeds a public API — one bug away from being served.
- **Cal's own node is excluded structurally**, not because it happens to advertise nothing today.
  Cal HT sits at a fixed private address, and a firmware setting must not be able to start
  publishing it as a side effect. `eval_hops.py` mutates that exclusion away and requires the
  suite to fail.
- **A node that broadcasts nothing simply has no grid.** It is never inferred from neighbours,
  hop count or signal.

What it still gives away, stated plainly: one bucket says little, but buckets across enough
neighbours narrow where the receiver hearing all of them must be. That is a deliberate trade,
made with the knowledge that these are positions their operators already transmit in the clear
to everyone in range. Set `POSITION_GRID=false` to turn the column off entirely.

The encoder is `calc.latlon_to_grid`, pinned against IARU's published worked examples — the
same one the `calc` doer uses, so there is one implementation rather than two that can drift.

## Hops: named when known, counted when not

A traceroute reply carries each relay's **full node number**, so hops are named from the node
database — name, hardware, hops from Cal, last heard. Nothing there is inferred.

The **relay byte** the firmware reports with an ordinary message is different: one byte, and
genuinely a fragment. Measured over 292 known nodes, a two-character fragment matches exactly
one node only **28%** of the time and one value is shared by **nine**. So a relay is named only
on a unique match and otherwise reports how many candidates it has. Guessing the likeliest is
the mistake `clean_name` already exists for.
