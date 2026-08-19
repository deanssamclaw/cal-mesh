# cal-mesh — working conventions

## Commit messages

Measured 2026-08-19 across the first 87 commits: 149,506 characters of commit prose
against 20,022 lines of code churn. Median body went 505 -> 1,551 -> 1,968 -> 1,960
chars by quartile while median diff size stayed near 150 lines. The driver was
adversarial-review commits enumerating every finding as its own paragraph
(median 1,960 chars with review words present, 1,008 without; longest 6,400).

Rules, not preferences:

- Subject: <= 72 chars. What changed, and where.
- Body: **<= 1,000 characters, hard.** Enforced by `.git/hooks/commit-msg`.
- The body answers three things and stops:
  1. what changed
  2. why it is safe to ship — eval and mutation counts as bare numbers
  3. what is ARMED or DISARMED as a result
- Review findings get a **count and severity only**: "9 findings, 4 HIGH, all fixed,
  eval_routes 57 -> 127 checks / 23 mutations". Never the enumeration.
- No narrative: not what I tried first, not what the bug taught me, not the reasoning
  chain. A commit says what changed and why it is safe.
- Over 1,000 chars means the commit is too big. Split it.

The enumerated findings, the lessons, and the reasoning go in
`~/.claude/projects/-Users-systems/memory/work-log.md`. Not here. A reader of this repo
wants to know what a change did; they should not have to scroll a session's thinking
to find out.

## Attribution

Commit as `Cal`. **No Claude trailer** on this repo.

## Hook install (fresh clone)

    ln -sf ../../hooks/commit-msg .git/hooks/commit-msg

## Verifying claims

A factual claim inside a **code comment** gets checked before it is written, at the same bar
as a claim in an answer. Measured 2026-08-19: three of four misses in one session were
assertions typed as documentation -- "U+FE0F is a format character" (it is Mn, not Cf), "this
eval writes to the production log" (log() prints to stdout; launchd owns the redirect), and a
fix referencing a module constant that does not exist.

The tell is a **causal** comment: "because X is...", "the library omits...", "this never
arrives here". If the comment explains WHY, it is asserting a fact, and the check is nearly
always one command. Comments are worse than answers to get wrong -- nobody re-reads them, and
the next session quotes them as ground truth.

Firmware claims cite `file:line` from `~/src/meshtastic-firmware` (tag matching the device),
never memory.
