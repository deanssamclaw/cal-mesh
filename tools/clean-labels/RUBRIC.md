# Labelling rubric — which capability should answer this message?

**One label per unique inbound message.** The label is the capability that *should* handle it, read
off this repo's own code and docstrings. **No model's answer is an input to a label** — that is the
whole point: a label set derived from a service's output cannot be used to train anything (the
cloud service's terms forbid it) and cannot exceed that service's quality if it could.

Labels: `weather` · `sunmoon` · `calc` · `sigreport` · `caps` · `greeting` · `conversation`
Flags: `refuse` (the capability should decline rather than answer) · `unsure` (the rule does not
decide — an operator call, never a guess)

## The rules, in the ladder's own order

Read them top to bottom and stop at the first that fires. That order is not a preference: it is
the order the code runs, and the reason a menu never outranks a real capability.

1. **`sigreport`** — the message is about **reception between the sender and Cal**:
   * a range/signal test: short, ends in "test" or "check" (`sigreport._TAIL_RE`), or a whole-phrase
     test (`_WHOLE_RE`);
   * a contact report: a receipt verb + a second-person reference + **no other node named**
     (`_CONTACT_RECEIPT`, `_CONTACT_SECOND`, `_CONTACT_OTHER_NODE`);
   * a question about how well Cal hears them, or how the link is holding up.
   **Not** `sigreport`, even when it mentions a test: someone *talking about* a test ("got the
   test", `_REFERENTIAL`), a test belonging to someone else ("Cal's test"), a question about
   **another** station/repeater/node, or a qualifier another capability owns ("weather check",
   `_OTHER_DOER`). A message naming any other node, short id or callsign is never `sigreport`.
2. **`calc`** — the message contains a **successful bounded parse**: arithmetic, a fraction or
   percentage, a unit conversion, bolt torque, concrete volume, acreage, a radio or electrical
   formula (wavelength, antenna length, dBm, ohms, path loss), or a navigation calculation
   (Maidenhead grid, distance/bearing). `calc.HANDLERS` is the list. A number in the text is not
   enough: if it does not parse to an exact answer, it is not `calc`.
   * **Collision rule:** a message that carries both an unambiguous calculation and a weather word
     is `calc` ("whats temp 12*12?"). Python keeps the digits.
3. **`sunmoon`** — sunrise, sunset, dawn, dusk, twilight or moon **phase**. Moonrise and moonset
   are `sunmoon` + `refuse`: the module recognises them **in order to decline** them.
   * **Collision rule:** if both a sun/moon and a weather subject appear, the label follows
     whichever the message is *about* — the first mentioned, or the one the time-ask governs
     (`sunmoon.governed_by_time_ask`).
4. **`weather`** — a question asking for **current local conditions**: rain, wind, temperature,
   snow, humidity, storms, how hot or cold it is.
   * A **forecast** ask ("will it rain tomorrow") is `weather` + `refuse` — current observations
     only, and saying so is the capability working.
   * A **past** ask ("what was the high yesterday", "how much rain overnight") is `weather` +
     `refuse` for the same reason. *(The armed regex path currently answers these with the current
     reading; that is a known defect on the watch list, not a labelling question.)*
   * A **statement** about weather, or a relayed alert, is not a question: label `conversation`.
5. **`caps`** — asking what this station can do, what topics it knows, its purpose, or for a list
   of commands. Last of the real capabilities, deliberately: a menu must never take a message a
   capability would answer.
6. **`greeting`** — the **whole** message is a hello or a wave, and it asks nothing
   (`responder._GREET_RE`, `is_bare_greeting`). A greeting wearing a question mark is not a bare
   greeting. A farewell ("night", "take care") is **not** `greeting`; label `conversation`.
7. **`conversation`** — everything else, including chat, opinions, general knowledge, statements,
   radio talk that asks for none of the above, and anything addressed to another station.

## How to apply it

* **Judge the message alone**, as the responder sees it after sanitising: no sender, no history.
* **The rule decides, not the outcome.** If the rule says `sigreport` and the radio happens to hold
  no measurement, the label is still `sigreport`; whether a reply goes out is a gate, not a label.
* **`unsure` is a real answer.** If two rules both fire and no collision rule settles it, or the
  message is ambiguous English, mark `unsure` and let the operator decide. A guess recorded as a
  label is worse than a gap: it trains the next model to guess the same way.
* **Record the rule that fired** alongside the label. A label with no rule is an opinion.

## Provenance, stated plainly

The labeller for this pass (Cal) has previously seen the cloud service's answers for these
messages, so "never saw them" is not a claim that can be made. What is claimed: every label here
was decided by applying the rules above to the message text, each one records the rule that fired,
and anything the rules do not settle is marked `unsure` for the operator rather than filled in from
memory. A second pass by a labeller with no such exposure would be stronger, and is the right thing
to do before any training run that matters.
