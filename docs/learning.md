# Simulated replies, grading and the learning loop

*How Cal's unanswered and mis-answered messages are found, graded and turned into fixes. Moved out of the README on 2026-09-24, unchanged apart from link paths.*

## Simulated replies, and grading them

Cal hears far more than he answers. Nothing recorded what he *would* have said, so whether the
silence was right was unknowable. `drafts.py` runs his real ladder over every message that was
not his own — sigreport, then the doer plan, then the off-list greeting ack, and a model prompt
only when nothing else claims it — and writes the result to its own tab. It cannot transmit, and
that is tested at the filesystem rather than asserted: the eval runs the module under a wrapper
that fails on any write resolving inside `outbox/`.

It did not always work that way. Until 2026-09-08 it called the model for every message and
separately *labelled* which doer would have answered, so the tab published Cal declining a
capability he has — "Can't check live weather try online" beside a note that the weather doer
matched. 13 of 143 stored rows were wrong that way and were re-drafted in place. (This read "40" until
2026-09-12. 40 is `DRAFTS_MAX_PER_RUN` — the per-run cap that appears in `drafts.log` as
"drafted 40 new", and the number of rows carrying the `bf3fef6` stamp. A run cap had been written
down as a defect count; the measured figure is 13, in `18644b3` and in `drafts.py`'s docstring.)

Each row says which arm answered. `sigreport` or `weather` means no model ran at all.
`model+weather` means the harness fetched a real observation and the model only phrased it —
neither a doer nor free prose, so it is named for what it is.

**One honest limit.** `ALLOW_FROM` stops an off-list sender from ever reaching generated prose
in production. Drafts consult it only to place the greeting arm; they do not obey it, because
obeying it would draft silence for 103 of 143 rows and blank the tab where it is worth reading.
So an off-list row with no doer match shows what Cal *could* have said, not what he would have
sent — he would have said nothing. `why_silent` carries `sender_not_allowed` on those rows.

### What else was on the channel

A message alone is often unjudgeable and roughly a third of the log is exactly that, so each row
carries the traffic within **±3 minutes**, oldest first, Cal's own sends included. Measured on
real rows, it changes the reading: "Aye" reads as a hail until you see it arrived 20 s after a
*different* node's "Heard from 159th and I-35!"; a 👍 turns out to land 78 s after Cal's own
signal readback, making it a thank-you rather than noise; and "Right on 33c4, sounds like great
news" had **nothing** before it, which is what proves the model invented the news. A row with no
neighbours says so — "no context" is itself evidence.

Window and cap are measured, not chosen: median 1 neighbour, p90 of 5, busiest minute on record
10, and 36% of messages have none at all. The cap keeps the **nearest** neighbours rather than
the first by time, or a busy minute would show only its oldest corner.

**The drafter sees it too, and that is a reversal made the same day.** This section first said
the window was evidence for the reader and *never* input to the draft, on the grounds that a
conversation window had already been built for the responder and refused. That refutation is
real but it is about the AIR: the window made Cal answer messages meant for other people, and it
ate a live clarify, turning a deterministic torque figure into a model guess. Drafts transmit
nothing — the eval proves that at the filesystem — so those consequences do not transfer, while
blindness has a measured cost of its own. Re-drafted with context, `"Aye, loud and clear here."`
became `"Copy that, thanks for checking in"`: the fabricated signal claim simply disappears.

Two rules bound it.

**Context never touches a capability prompt.** `build_prompt`'s weather path deliberately does
not echo the sender's message at all — the model sees the harness-fetched fact and nothing else,
so no attacker-controlled text sits beside a number it is told to repeat verbatim. Prepending a
dozen strangers' lines there would reopen, wider, exactly what that path was shaped to close. It
is added only where the model is already writing free prose, and the eval drives a weather ask
through and reads the prompt to prove it.

**Every line is sanitized**, through the same `sanitize_inbound` the live path uses. There are
44 distinct off-list senders in this corpus; skipping it would recreate the hole that was closed
when raw stranger text was found reaching the user turn.

**And a context-built draft is not a counterfactual.** The responder sees one message; a draft
that saw eight is what Cal *could* say if context were armed, not what he *would* have said. The
row carries `ctx_n` and such rows are marked **unfaithful**, the same way a live weather fact
marks one — so the tab never claims a proposal is a record.

If those drafts read better than the blind ones over time, that is the argument for arming
context on the responder. It is not that argument yet.

### Grading is public and ungated

`POST /api/grade` takes a verdict from anyone who can open the page. That is deliberate: the
page is on the open internet by design, and a grade is an opinion about a sentence Cal already
published there. What is bounded is the *write*, which is a different question from the writer —
a closed verdict set, capped note and body, and a `draft_id` that must already exist, so the
keyspace is the set of drafts Cal actually made rather than anything a client can name.

Four verdicts, each naming its own consequence, because a bare thumb says a draft was bad and
nothing about why — so it cannot route anywhere:

| verdict | means | what happens |
|---|---|---|
| 👍 `good` | this is what Cal should have said | counts toward a baseline |
| 🔧 `doer` | should have been deterministic, not prose | enters the triage queue that arms doers |
| ✏️ `wrong` | carries the reply that would have been better | the only feedback with training signal |
| ⚠️ `harmful` | should not have been said at all | queued as work |

**Three writers, and the record says which one.** The public page writes `by: "page"` and
cannot be told otherwise -- it is ungated, so a value it accepted from the client would let a
passer-by sign as the operator. The local CLI writes whoever `--by` names, defaulting to the
operator, because running it means having the machine. Programmatic review writes
`by: "cal-review"`. Until 2026-09-12 all three pooled into `"page"`, which made a machine's
pattern match indistinguishable from a person's judgement; the page now shows the author beside
the verdict.

    python3 drafts.py --grade <draft_id> --verdict doer --better "…" --by dean

The CLI's verdict set had also drifted: it offered `good/wrong/harmful` and omitted **`doer`**,
the only verdict `grade_queue()` routes into triage. So the operator could not record the one
judgement with leverage while an anonymous visitor could. The two sets are now asserted equal in
`eval_drafts.py` rather than kept in step by hand.

`learn.grade_queue()` is the join that makes this a loop rather than a log. It keys each verdict
with the distiller's own `normalize()`, so a graded ask and a distilled gap land in one namespace
instead of two spellings of the same thing, and an item clears when its ask is **triaged** — not
when it is graded again. The join happens at read time and nowhere else: a grade is an opinion,
`gap-ledger.json` is a measurement of what happened on the radio, and appending one to the other
would leave nothing downstream able to tell them apart.

### Auditing the bank

`drafts.jsonl` is cumulative and `run()` skips anything already drafted, so a capability armed
later corrects every *future* row and cannot reach one already banked. That is the same shape
the gap ledger had in session 150: a store with a watermark, and nothing comparing it to the
code that fills it. On 2026-09-12 it was 8 banked rows a doer would now claim, and 40 of 44
graded defects still showing the old Cal.

    python3 drafts.py --audit      # read-only; 0 nothing to do, 1 drift found
    python3 drafts.py --redraft    # re-run the drifted rows under today's code

The audit decides which arm *would* answer without generating anything, so it costs no model
calls -- every arm above the model is deterministic, and `cal_reply(dry=True)` stops there. It
reuses the real ladder rather than restating the order, because a second copy is how the two
drift apart. The eval proves the no-model property with a spy rather than a raising stub: a
raise is swallowed by `audit()`'s own per-row `except`, and that version of the check survived
the mutation that turned dry mode off.

`--redraft` rewrites only rows whose arm changed, and re-stamps `armed`/`commit` **only on a row
it actually regenerated** -- those fields say which Cal produced the text, so stamping a row it
did not touch would assert something false. A row whose new arm is the model needs a model call
to get text and is skipped unless `--with-model`.

Two counts are reported apart because they mean different things: **drift** (a different arm
would answer now) is the alerting signal, while a row with **no recorded arm** predates arm
stamping and is not drift -- nothing is known to have changed about it.

## The learning loop, and whether it is running

The distiller is a scheduled job that reports its own numbers, which is a shape that can be
wrong invisibly — and was. Three situations produce an identical "nothing new": a healthy loop
over a quiet mesh, a responder that has stopped writing, and a bank that no longer agrees with
the code that classifies it. The third one shipped. Adding `sigreport` to `DOER_CAPS` fixed the
classifier, but the aggregate is cumulative and the watermark classifies each record exactly
once, so the fix corrected every future record and could not reach the three already counted.
Three answered range tests stayed published as unanswered gaps for six days, while every figure
on the page was internally consistent.

So the page publishes a verdict from a closed set, with the evidence under it:

| state | means |
|---|---|
| `FRESH` | ran on schedule, input flowing, bank agrees with the classifier |
| `LATE` | no run inside the threshold — everything below it is stale |
| `STALLED` | the loop is running, but nothing has been received |
| `FAILING` | the loop is fine, but the last 3 model replies all failed — "nothing new" may be replies that never happened |
| `DRIFT` | banked records would classify differently under the code running now |
| `UNKNOWN` | an artefact could not be read — never a cheerful default |

Two rules make it worth trusting. **Liveness is derived from artefacts carrying their own
timestamps** — the newest run in `learn-history.jsonl`, the newest record in `decisions.jsonl` —
never from a heartbeat the monitored job writes, because that cannot report that the job has
stopped; it keeps saying whatever it last said. And **the drift check runs on every page read,
not once per run**, because the failure it catches is a classifier edited *between* runs, so
banking the number at run time would rebuild the same blind spot a day wide.

Flags are collected rather than short-circuited, so two faults at once cannot hide each other;
precedence only chooses which chip is shown, and all four facts render regardless. There is no
green light on the page. A light is a claim, and that claim would have read true throughout the
six days — what is shown instead is the evidence it would have been claiming from.

`FAILING` was added 2026-09-21 after five model replies failed in a six-minute burst
(`gen_rc1`, empty stderr) while the strip read `FRESH` for a day: the distiller was healthy, and
a responder whose model calls all fail is indistinguishable from a quiet mesh at that layer. It
is derived from `decisions.jsonl` like everything else — the trailing run of failed reply
attempts — and three in a row has never happened in healthy operation (40 attempts on record).
The failing call's stdout and stderr now go to `responder.log`, never to the public trace.

Thresholds are measured, not chosen. Runs land 24.00 h apart across eight consecutive days, so
`LATE` is 26 h. The largest natural silence between two inbound records is 39.3 h (median
0.47 h, p90 11.5 h), so `STALLED` is 48 h — below about 40 h it fires on a genuinely quiet mesh,
and a health signal that cries wolf is one nobody reads. Re-derive both if traffic changes shape.

Both checks are read-only and exit with a code, so they compose with a monitor:

    learn.py --check     # 0 fresh · 1 unhealthy · 2 unknown
    learn.py --audit     # re-classify the bank against the current code; per-bucket delta

`eval_health.py` — 62 checks. Every state is driven by a fixture that forces it; the drift check
is exercised by putting `DOER_CAPS` back to its pre-fix shape and demanding the audit finds all
three records; and both browser renderers are executed against live and adversarial payloads.
Three self-test mutations, including "health always returns FRESH", each of which must fail the
suite.
