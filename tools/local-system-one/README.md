# local-system-one — the runners behind `docs/proposals/local-system-one.md`

Offline comparison of open "System One" models against the cloud router, on this node's own
traffic. **Nothing here runs in production**; `responder.py` never imports it. Every script is
thermally gated for jlab: a call starts only at or below 70 °C.

## The data file these expect

`eval_data.json` is built from the live log and is **not committed** — it carries message text and
third-party node ids, which the staged-set scrub rightly blocks. Build it where the log lives:

```json
{"criteria": {...jevroute.CRITERIA...}, "guards": {...jevroute.GUARDS...},
 "rows": [{"text": "...", "clean": "...", "current": "weather", "truth": "weather",
           "names_other": false, "dm": false, "calch": false, "kw": true,
           "jev_route": "weather", "jev_conf": 0.99, "jev_act": null}]}
```

`current` is what the ladder does today (`plan_response` + `sigreport.match` +
`is_bare_greeting`), `truth` is the adjudicated label, `jev_*` is the cloud router for comparison,
and `dm` / `calch` / `kw` are the addressing flags that decide which population a row belongs to.

## Runners

| Script | What it does |
|---|---|
| `jev_local.py MODEL [N] [--raw]` | Ollama readout: options in the prompt, first-token `logprobs`, softmax over the option letters. `--raw` skips the chat template (needed for models that open a thinking channel; it also costs letter mass — see the proposal). |
| `small_local.py decider\|laya [N]` | The purpose-trained small models. Both accept TypeSafe-shaped typed questions, so jevroute's own criteria and guards go in verbatim. |
| `semif_chunks.sh` | SemIf's own scorer over the whole file in 20-row chunks, cooling between chunks. Needs `semif-score` and a GGUF checkpoint. |
| `semif_analyze.py FILE` | Fits ONE temperature on the un-addressed rows, applies it to the addressed ones, reports accuracy/ECE and the rescue candidates. Fitting and evaluation never share rows. |
| `score_local.py FILE` | Scores any runner's output exactly as the built router would act: fallthrough only, `RESCUABLE` only, the floor, both guards, the node-name backstop. |

## Reproducing the headline

```bash
bash semif_chunks.sh                  # ~1 h on 8 threads, thermally gated
python3 semif_analyze.py semif_results.jsonl
python3 score_local.py results_semif.json
```

## Notes that cost time to learn

* **Prompt-cache reuse works for MoE models on Ollama** (7.5 s → 0.39 s prefill) and not for the
  dense 4B, which re-prefills every call.
* **`pkill -f "jev_local.py ..."` also matches the tmux server** that launched it, and kills every
  session on the box. Kill the session, not the pattern.
* **Laya's `Router` picks the general English checkpoint**, which its own table scores at 0.362 on
  typed decisions against 0.766 for `typed-decisions`. Load the checkpoint deliberately.
