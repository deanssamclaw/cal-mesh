# local-system-one — the runners behind `docs/proposals/local-system-one.md`

Offline comparison of open "System One" models against the cloud router, on this node's own
traffic. **Nothing here runs in production**; `responder.py` never imports it. Every script is
thermally gated for jlab: a call starts only at or below 70 °C.

## The data file these expect

`eval_data.json` is built from the live log and is **not committed** — it carries message text and
third-party node ids, which the staged-set scrub rightly blocks. Build it where the log lives:

```json
{"criteria": {...s1route.CRITERIA...}, "guards": {...s1route.GUARDS...},
 "rows": [{"text": "...", "clean": "...", "current": "weather", "truth": "weather",
           "names_other": false, "dm": false, "calch": false, "kw": true,
           "s1_route": "weather", "jev_conf": 0.99, "jev_act": null}]}
```

`current` is what the ladder does today (`plan_response` + `sigreport.match` +
`is_bare_greeting`), `truth` is the adjudicated label, `jev_*` is the cloud router for comparison,
and `dm` / `calch` / `kw` are the addressing flags that decide which population a row belongs to.

## Runners

| Script | What it does |
|---|---|
| `jev_local.py MODEL [N] [--raw]` | Ollama readout: options in the prompt, first-token `logprobs`, softmax over the option letters. `--raw` skips the chat template (needed for models that open a thinking channel; it also costs letter mass — see the proposal). |
| `small_local.py decider\|laya [N]` | The purpose-trained small models. Both accept TypeSafe-shaped typed questions, so s1route's own criteria and guards go in verbatim. |
| `semif_chunks.sh` | SemIf's own scorer over the whole file in 20-row chunks, cooling between chunks. Needs `semif-score` and a GGUF checkpoint. |
| `semif_analyze.py FILE` | Fits ONE temperature on the un-addressed rows, applies it to the addressed ones, reports accuracy/ECE and the rescue candidates. Fitting and evaluation never share rows. |
| `score_local.py FILE` | Scores any runner's output exactly as the built router would act: fallthrough only, `RESCUABLE` only, the floor, both guards, the node-name backstop. |

## Reproducing the headline — what each step needs, and what is missing

The SemIf result is three steps. **Only the middle one runs from public inputs plus the
scorer's own output; the chain does not close from this repo alone.** Every script reads
`eval_data.json` from the working directory.

| Step | Reads | Writes | Input made by public code? |
|---|---|---|---|
| `bash semif_chunks.sh` | `~/jev-eval/semif_decisions.jsonl` | `~/jev-eval/semif_results.jsonl` | **No.** Nothing here writes `semif_decisions.jsonl`. |
| `python3 semif_analyze.py semif_results.jsonl` | that file, `eval_data.json`, `semif_index.json` | `semif_calibrated.json` | **No** for `eval_data.json` and `semif_index.json`. |
| `python3 score_local.py FILE` | `FILE`, `eval_data.json` | stdout only | **No** for a SemIf `FILE`: nothing here writes one. |

What each missing input has to look like, read off the scripts:

* **`semif_decisions.jsonl`** — SemIf input, one row per line:
  `{"id": ..., "state": "<message text>", "question": "...", "options": [{"id": "weather", "description": "..."}, ...]}`.
  `semif_analyze.py` maps option **position** to `list(criteria)`, so the options must be the
  route criteria in that order. `tools/system-one/system_one_server.py` (`_rows`) builds exactly
  this row from `s1route`'s local request; whether the offline file used the same wording is not
  recorded here.
* **`semif_index.json`** — a list of `{"id": <row id>, "text": <eval_data row "text">, "addressed": bool}`.
  `addressed` splits the rows: `false` rows fit the temperature, `true` rows are scored.
* **`semif_results.jsonl`** — written by `semif-score`; the scripts use only `id` and `option_logits`.
* **`FILE` for `score_local.py`** — the shape `jev_local.py` and `small_local.py` write:
  `{"model", "done", "elapsed_s", "thermal_pause_s", "results": [{"text", "route", "conf", "probs": {route: p}, "weather_now", "other_station", "timing": {"wall_s"}, "letter_mass"}]}`.
  Every result needs `timing.wall_s` and `letter_mass`; `eval_data.json` rows also need
  `jev_route` and `jev_act`.

**The gap.** `semif_analyze.py` writes `{"T": ..., "candidates": [{"text", "route", "conf"}]}` —
the rescue candidates at conf >= 0.8 **before the guards** — not a `score_local.py` input. The
SemIf run's two guard questions, and a SemIf `results_semif.json` for `score_local.py`, were
produced outside this repo. So from public code you can
reproduce the fitted temperature and the pre-guard candidates; the final "+2 / +3, nothing
broken" for SemIf also needs a `results_semif.json` in the shape above, with `weather_now` and
`other_station` scored per row. The Ollama and small-model runners (`jev_local.py`,
`small_local.py`) do write `score_local.py` input directly.

`semif_chunks.sh` also hard-codes `~/jev-eval`, the GGUF path and `thermal_zone2`; the scorer
setup it assumes is in [`tools/system-one/README.md`](../system-one/README.md).

## Notes that cost time to learn

* **Prompt-cache reuse works for MoE models on Ollama** (7.5 s → 0.39 s prefill) and not for the
  dense 4B, which re-prefills every call.
* **`pkill -f "jev_local.py ..."` also matches the tmux server** that launched it, and kills every
  session on the box. Kill the session, not the pattern.
* **Laya's `Router` picks the general English checkpoint**, which its own table scores at 0.362 on
  typed decisions against 0.766 for `typed-decisions`. Load the checkpoint deliberately.
