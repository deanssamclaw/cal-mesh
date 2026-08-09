# Proposal — Practical math on the mesh (capability spec)

**A sibling to the weather capability. Read `level3-weather.md` first** — this doc inherits its
framework (two axes, the capability "triple", the safety invariants) and only covers what is
*different* about math.

*Draft by Cal, 2026-08-09 · design review welcome (esp. Bob).*

---

## 0. What & why

Practical field arithmetic: *"cal what's 1,200 x 12"*, *"cal 15% off $260"*, tips, splits,
unit totals. Useful off-grid for the same reason a calculator is. Same **"safe to serve anyone"**
*output* tier as weather (a number isn't sensitive) — but see §3, the input side is not free.

## 1. The inversion (this is the whole design)

Weather is *fetch → model narrates*. Math is the opposite: **the harness computes the exact
answer and the model never touches the number.**

The reason is **not** mainly "LLMs are bad at arithmetic" (though they are unreliable on
multi-digit work). It is **determinism and auditability**: a calculator's entire value is a
*correct, verifiable* number. Even a model that were right 99% of the time can't be *guaranteed*
or *audited*, and you'd never know which reply was the 1%. So:

> **Python owns every digit. The model is not in the number path — at all.** Python computes
> *and* formats the final reply string (like the weather fail-safe reply, not like weather
> narration).

On the agency axis this puts math **further toward "harness-owned" than weather** — no model
call at all. (Note for the framework doc: capabilities are *not* monotonic on that axis; a new
capability can be more deterministic than an earlier one.)

## 2. Design

The capability "triple", with a **compute** doer instead of a fetch:

| Part | Math instance |
|------|---------------|
| **intent** | the input **parses to a bounded arithmetic operation**. Intent *is* a successful parse — not "contains a number" (which false-fires on "mile marker 120"). Require an actual operation (2 operands + a recognized operator/percent form). |
| **deterministic doer** | evaluate the parsed operation in Python under hard bounds (§3). No `eval`/`exec`. |
| **reply** | Python formats the result string directly (currency, thousands separators, sensible rounding). No model call. |

If it doesn't parse to a bounded operation → **not a math query**; fall through to normal
handling. Never guess an answer.

## 3. Safety — the surface that is NEW vs weather

Weather's risks were about *output* (leaking Dean) and *fetch* (SSRF). Math's are about
*evaluating attacker input*. Non-negotiable:

- **Never `eval()`/`exec()` the message.** That is remote code execution on a public, spoofable
  channel. Use a restricted evaluator: `ast.parse(mode="eval")`, then walk the tree and **reject
  unless every node is in a numeric whitelist** (`Expression, BinOp, UnaryOp, Constant(number)`,
  and the operators `Add Sub Mult Div Mod` and maybe a *bounded* `Pow`). Reject `Call, Name,
  Attribute, Subscript, Import`, strings, everything else. (Verified: `__import__(...)` /
  `open(...)` carry `Call`/`Name` and are rejected; `1200*12` is pure `BinOp/Constant`.)
- **The AST whitelist does NOT bound cost — you must, separately.** `9**9**9` passes a node
  check and then melts a core / eats memory. Enforce: **cap operand magnitude, cap the operation
  count, and either drop `**` or bound base/exponent hard** (e.g. exponent ≤ 6, |base| ≤ 1e6).
  Reject before evaluating, not after.
- **"Exact" requires `Decimal` + explicit rounding.** Float is not exact for the practical cases
  (`100/3 → 33.333333333333336`; percentages are luck-of-the-draw). Use `Decimal`, and
  `quantize(0.01, ROUND_HALF_UP)` for money. Decide and document rounding for non-terminating
  results (e.g. thirds → 2 dp with a "≈").
- **Division by zero, overflow, unparseable → fail-safe.** Return no answer (fall through) or a
  fixed "can't compute that" — never a wrong or partial number.
- **Output length cap.** A result with hundreds of digits must not air (also caught by the
  magnitude cap).

Inherited unchanged from weather: kill switch, `RESPONDER_ENABLED`, advisory allow-list (spoofable
— fine, math is safe to serve anyone), rate limits/cooldown, per-record logging. And because the
message is never echoed to a model on this path, the prompt-injection surface is *nil* here.

## 4. The one design choice — how much natural language to accept

- **Bounded patterns only** (simplest to prove): regex-recognize a fixed set — `X times/x/* Y`,
  `X% of Y`, `X% off Y`, `tip P% on Y`, `X +−×÷ Y` — map to a known op, compute. Limited phrasings,
  every answer exact.
- **Safe-AST evaluator + a few NL normalizers** (recommended): normalize the common English forms
  ("percent off" → `*(1-p)`, "times"/"x" → `*`, "$" stripped, commas stripped) into an expression,
  then evaluate under the §3 whitelist + bounds. Broader coverage (`1200*12`, `260-39`, `(3+4)*5`),
  still zero `eval`, more surface to bound — which §3 already does.
- **Deferred: let the model translate words → expression.** Best coverage, but it moves math
  *back up* the agency axis (trusting the model's parse — it could read "15% off 260" as `×0.15`
  not `×0.85`) and would need the translation verified. Not Stage 1.

Recommendation: **safe-AST + normalizers**, with the §3 bounds as the hard floor.

## 5. Worked examples (grounded, not guessed)

- `1,200 x 12` → **14,400** (integer, exact).
- `15% off $260` → **$221.00** (Decimal `260 × 0.85`), and report the **$39.00** saved.
- `15% tip on $70` → **$80.50** (Decimal, quantized).
- `100 / 3` → **≈ 33.33** (non-terminating → 2 dp with "≈", by the documented rounding rule).

## 6. Invariants (math-specific, on top of weather's)

1. **Python owns every digit; no model in the number path.**
2. **No `eval`/`exec`; numeric node + operator whitelist.**
3. **Bounded compute** (magnitude, op-count, exponent) enforced *before* evaluation.
4. **`Decimal` + explicit rounding**; fail-safe on div-zero/overflow/unparseable.

## 7. Fresh-eyes pass — assumptions I removed (per Dean's ask)

Re-derived my own §-1 thinking and corrected: (a) "Python is trivially exact" → **false**, needs
Decimal/rounding (checked: `100/3`); (b) the real justification is **determinism/auditability**,
not "LLMs can't multiply" → therefore **no model in the path at all** (I'd left a "model phrases
it" door open; closed); (c) "safe to serve anyone" is **output-only** — math adds an **input-side
compute-DoS** surface weather lacked; (d) intent is **a successful bounded parse**, not "has a
number"; (e) "restricted AST is safe" **verified** (rejects `Call`/`Name`) but **does not bound
cost** — magnitude/op/exponent caps are separate and mandatory; (f) math is **more harness-owned
than weather** on the agency axis (capabilities aren't monotonic).

## 8. Rollout (same gate as weather)

Build **default-OFF** → an **offline adversarial eval** whose cases include RCE attempts
(`__import__`, `open`, calls, attribute access), DoS (`9**9**9`, giant operands, long chains),
div-by-zero, precision (money/percent via Decimal), non-math false-fires (bare numbers), and the
output length cap → **independent review** → then arm. No digit reaches the air until that passes.
