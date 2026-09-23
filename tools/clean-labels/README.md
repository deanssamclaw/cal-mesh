# clean-labels — labels built without a model in the loop

**Why this exists.** The first label set for the routing work partly adopted the cloud service's
answers: where it and the regex ladder agreed, the agreement was accepted. That is fine for
*measuring* and disqualifying for *training* — the service's terms forbid using its output to
train a model, and a model trained on another's answers cannot exceed it anyway.

[`RUBRIC.md`](RUBRIC.md) is the rule set, read off this repo's own capability code: what each
capability is for, in the ladder's own order, with the collision rules and the exclusions that the
code already enforces. A label is the capability that **should** answer, plus two flags: `refuse`
(the capability should decline — a forecast or a past-tense weather ask) and `unsure` (the rules do
not settle it, so it is an operator call and is never guessed).

## The flow

```bash
python3 label_prep.py        # mechanical evidence per message: which rules fire, on what
                             # -> label_evidence.json  (NOT a label: the rules decide, a person applies them)
```

Then label each message against `RUBRIC.md`, recording **the rule that fired** with every label. A
label with no rule is an opinion.

## Where the labels live

**Not here.** The label file is keyed by `sha256(text)[:16]` and lives in the operator's private
workspace (`cal-workspace/labels/`), because a label set that carries message text carries other
people's messages. The hash makes the set portable; the text stays where the inbox already is.

## The first pass, and its honest limits

319 unique non-reaction messages: 41 `sigreport`, 38 `greeting`, 12 `weather`, 10 `calc`,
2 `caps`, 216 `conversation`; **23 `unsure`** and 4 `refuse`.

* It differs from the old model-derived labels on **37 of 319** messages, but on only **one**
  addressed message — so the published routing numbers survive the relabelling.
* The labeller had already seen the cloud service's answers. That cannot be undone, so it is
  stated: every label was decided by applying the rules, each records its rule, and what the rules
  did not settle is marked `unsure` rather than filled in from memory. **A second pass by an
  unexposed labeller is the right thing to do before any training run that matters.**
* 319 messages, most of them channel chatter, is a small set. Fine-tuning would want contrastive
  pairs — two near-identical messages where one changed word flips the answer — and those have to
  be authored, not collected.
