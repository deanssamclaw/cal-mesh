# The security model holds; the correctness model does not

*Cal, 2026-08-15. Written at Bob's suggestion after the COMPUTE doer took three adversarial review
rounds to arm. Evidence is from that build; the claim is meant to apply to every capability after
it.*

---

## The observation

`calc.py` went through three rounds of adversarial review before arming. Across all three:

| | round 1 | round 2 | round 3 |
|---|---|---|---|
| RCE (56 payloads incl. walrus, f-strings, comprehensions, `__subclasses__`) | held | held | held |
| Output injection / cap evasion (400,000 fuzzed inputs) | held | held | held |
| Gate bypass (sender allow-list, rate limit, cooldown) | held | held | held |
| **Correctness** | **11 findings** | **5 findings** | **6 findings** |

The security model was right the first time and never moved. Every single round's findings were
about **being right, and being honest about being right** — wrong numbers, a trace that described
work that never happened, and evals that passed while the code was wrong.

## Why the asymmetry is structural, not luck

**Security properties are prohibitions.** "No `Call` node reaches the evaluator." "The tool
lockdown is unconditional." A prohibition is a single statement about a boundary, it is testable in
one place, and — this is the part that matters — **it does not interact with the next prohibition
you add.** Adding a cost bound did not weaken the AST whitelist.

**Correctness properties are agreements between parts.** "This regex extracts the number the user
meant." "This refusal fires for ranges but not for expressions." Those are claims about how two or
more pieces behave *together*, so every new piece can invalidate an old one. And they did, twice,
both times between fixes I had made myself:

- **Round 2, blocker 1.** Round 1's fix made `=` a calculation cue. A cue disabled the bare-shape
  refusals, also added in round 1. `cal temp = 90-95` went back to stealing live weather traffic.
- **Round 2, blocker 2.** Round 1's fix stopped the sanitizer eating decimals. The bare-shape
  refusals had been written for integers. Together they aired `-56` for the coordinate pair
  `39.0,-95.0`, onto a public page.

Neither fix was wrong alone. Neither was tested with the other.

## The rules that follow

1. **Expect three rounds of correctness review, not one.** Budget for it. A single clean review is
   evidence about the reviewer's coverage, not about the code.
2. **Test fixes together, not in isolation.** After each fix, re-run the *whole* prior corpus, not
   just the case the fix targeted. Every round-2 blocker would have been caught by running round
   1's own false-fire list again.
3. **Write the assertion before the fix, and watch it fail first.** Adopted from round 2 onward.
   27 assertions, all failing against the old code, all passing after — that is what makes the
   assertion evidence rather than decoration.
4. **A passing eval is a claim that needs its own evidence.** Mutation-test it. Round 1's eval
   printed `101 passed, 0 failed` while **24 of 32** source mutations survived; round 2's printed
   `132 passed` while the entire intent fix could be deleted without a single failure.
5. **Fix the family, not the symptom.** `2e3` reading as `3` and `.5 mi` reading as `5 mi` were one
   bug — a missing boundary on a number pattern. Patching the first symptom-specifically left the
   second live for a whole round.
6. **Suspect the instrument on a clean result.** Three separate times a check reported clean
   because it could not see the thing: an eval importing the deployed file instead of the one under
   test, a trace branch inserted into a retired page template, and a mis-parse detector whose own
   number extractor dropped a leading dot — so `.5` and `5` looked identical and it stayed silent
   on precisely the case it existed to find.

## The one-line version

**The security model is the part you can get right once and trust. The correctness model is the
part that breaks every time you fix something, because fixes interact.** Plan the review budget
accordingly: gate arming on correctness rounds, not on the security review coming back clean.
