# system-one — the local scorer behind `s1route`

`system_one_server.py` answers one question for cal-mesh: **which capability should answer this
message?** It is the local backend for `s1route.py` (`S1_BACKEND=local`), and it exists so that
message text does not leave the house. The measurement that licenses it, and its limits, are in
[`docs/proposals/local-system-one.md`](../../docs/proposals/local-system-one.md).

* **Model:** a frozen Qwen3.5-4B (Q4_K_M GGUF), scored by SemIf's own direct scorer on llama.cpp
  ([repo](https://github.com/TheoLeeCJ/SemIf-OpenJev), formerly `TheoLeeCJ/SemIf`), with one softmax temperature applied to the option logits.
* **It is not TypeSafe's Jev.** It accepts the same request *shape* so the client needs one config
  line, not a second parser. The `model` field always names what actually ran.
* **One request at a time, CPU only, no auth.** The tailnet is the boundary.

## 1. Prerequisites

| | |
|---|---|
| OS | Linux with systemd. Tested only on x86-64. |
| CPU | No GPU needed. Measured on a 2019 MacBook Pro running Linux (i9, 16 threads) with 8 threads given to the scorer: **~13 s cold, ~18 s median under sustained thermal throttling** for one `s1route` request (a route plus two guards). |
| RAM | Enough for a ~3 GB model plus the Python stack; the reference box has 31 GB. |
| Disk | **~3 GB** for the GGUF, **~11 GB total** with the venv (torch, transformers) and the SemIf checkout. |
| Network | A [Tailscale](https://tailscale.com) tailnet shared with the machine running cal-mesh. |
| Build | `python3.13`, `python3.13-venv`, `git`, a C/C++ compiler (`build-essential` or equivalent). `llama-cpp-python` compiles llama.cpp at install time. |

## 2. Build it from zero

The layout below is the one the server's defaults assume (`S1_GGUF` defaults to
`~/jev-eval/gguf/...`; the directory name is historical). Any other layout works if you set
`S1_GGUF` and the paths in the unit.

```bash
# the server
mkdir -p ~/system-one ~/jev-eval/gguf
cp tools/system-one/system_one_server.py ~/system-one/        # from a cal-mesh checkout

# the venv
python3.13 -m venv ~/jev-eval/.venv
~/jev-eval/.venv/bin/pip install torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu
~/jev-eval/.venv/bin/pip install cmake ninja

# SemIf (MIT), pinned
git clone https://github.com/TheoLeeCJ/SemIf-OpenJev ~/jev-eval/SemIf
git -C ~/jev-eval/SemIf checkout 1f2dea3e25379f9dfc98cb83c324f00ab5deda37
~/jev-eval/.venv/bin/pip install -e ~/jev-eval/SemIf'[llamacpp]'
```

SemIf at that commit pins its own dependencies exactly (`llama-cpp-python==0.3.35` via the
`llamacpp` extra, `torch==2.10.0`, `transformers==5.17.0`, `numpy==2.2.6`,
`huggingface-hub==1.31.0`), so the install above lands on the **known working versions**:
Python 3.13.5, llama_cpp_python 0.3.35, torch 2.10.0, transformers 5.17.0, numpy 2.2.6,
huggingface_hub 1.31.0. Installing `torch==2.10.0` from the CPU index first keeps pip from pulling
the multi-gigabyte CUDA build; pin it, or SemIf's `torch==2.10.0` will replace a newer CPU wheel
with the default one from PyPI.

### The checkpoint — pinned and checked

```bash
~/jev-eval/.venv/bin/hf download bartowski/Qwen_Qwen3.5-4B-GGUF Qwen_Qwen3.5-4B-Q4_K_M.gguf \
    --revision 4168f45a16a1290d65a4ec0fa312ae917a4c15d6 --local-dir ~/jev-eval/gguf
# (older huggingface_hub: `huggingface-cli download` with the same arguments)

cd ~/jev-eval/gguf
echo "13c16f426047e2de38cd075bdade4a7bcbc8c774384876f677740cda65f8a983  Qwen_Qwen3.5-4B-Q4_K_M.gguf" | sha256sum -c
stat -c %s Qwen_Qwen3.5-4B-Q4_K_M.gguf       # 3013027808
```

### The reference tokenizer — pre-fetch it

SemIf scores with the GGUF but checks every prompt against the **reference tokenizer**, loaded
with `transformers` from `S1_SOURCE` at `S1_REVISION` — by default `Qwen/Qwen3.5-4B` at
`851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a` (the server's defaults). It refuses to start if the
GGUF vocabulary disagrees with it. Fetch it once, as the user the service runs as, because the
unit mounts home read-only:

```bash
~/jev-eval/.venv/bin/hf download Qwen/Qwen3.5-4B \
    tokenizer.json tokenizer_config.json vocab.json merges.txt chat_template.jinja config.json \
    --revision 851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a
```

(Only the tokenizer files; the model weights in that repo are not needed.)

### Smoke test before the unit

```bash
cd ~/system-one && ~/jev-eval/.venv/bin/python system_one_server.py
# loading Qwen_Qwen3.5-4B-Q4_K_M.gguf threads=8 T=1.45
# backend metadata: {...}
# loaded in ...s; serving on 127.0.0.1:8799
```

Start-up hashes the whole 3 GB GGUF (SemIf records its sha256), so the load takes a while.

## 3. Install the unit

```bash
sudo cp tools/system-one/system-one.service.example /etc/systemd/system/system-one.service
sudoedit /etc/systemd/system/system-one.service   # replace <you> and the tailnet address
sudo systemctl daemon-reload && sudo systemctl enable --now system-one
journalctl -u system-one -f
```

**Bind to the tailnet address (`tailscale ip -4`), and only that.** There is **no authentication**
on this port; the tailnet is the boundary, so the address decides who can reach it: this
operator's own devices, not the LAN and not the internet.

* **Never `0.0.0.0`.** That opens an unauthenticated CPU-burner to every network the box is on.
* **Never point the client at a MagicDNS name.** On a host with Tailscale Funnel enabled the name
  resolves to the public relay address, not the `100.x` one, and the router quietly leaves the
  tailnet. Use the numeric `100.x` address in `S1_LOCAL_URL`.
* If cal-mesh runs on the same box, leave `S1_HOST` at its default, `127.0.0.1`.

The unit keeps the hardening: `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=full`,
`ProtectHome=read-only` (writable only under `~/system-one`), and `Nice=10` so interactive work
wins.

## 4. Environment

Every variable the server reads:

| Variable | Default | Meaning |
|---|---|---|
| `S1_GGUF` | `~/jev-eval/gguf/Qwen_Qwen3.5-4B-Q4_K_M.gguf` | the checkpoint |
| `S1_SOURCE` | `Qwen/Qwen3.5-4B` | reference tokenizer repo (or a local directory) |
| `S1_REVISION` | `851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a` | its pinned revision; SemIf requires 40 hex chars for a remote source |
| `S1_THREADS` | `8` | llama.cpp threads |
| `S1_TEMPERATURE` | `1.45` | softmax temperature on the option logits (section 8) |
| `S1_PORT` | `8799` | port |
| `S1_HOST` | `127.0.0.1` | bind address; the unit sets the tailnet address |
| `S1_BUSY_TEMP_C` | `95` | at or above this, refuse to START a request (503) |
| `S1_ABORT_TEMP_C` | `99` | at or above this, abort IN FLIGHT between questions (503) |
| `S1_ZONE` | `/sys/class/thermal/thermal_zone2/temp` | the temperature sensor file (millidegrees C) |
| `S1_MAX_QUESTIONS` | `4` | questions per request |
| `S1_MAX_STATE_CHARS` | `4000` | characters of `state` |
| `S1_MAX_INSTR_CHARS` | `4000` | characters of each question's `instructions` |
| `S1_READ_TIMEOUT_S` | `10` | socket read timeout (slowloris guard) |

`HF_HUB_OFFLINE=1` is also honoured, by SemIf rather than the server: it loads the tokenizer from
the cache only.

## 5. Wire shape

`POST /v1/systemone` (alias `/systemone`), `Content-Type: application/json`. This is exactly what
`s1route.py`'s local path sends for *"Cal, hows the link holding up?"* — the state as **plain
text**, `LOCAL_INSTRUCTIONS`, `CRITERIA`, and the two `LOCAL_GUARDS` as `noul` questions. No
`model`, no key:

```json
{
  "state": "Cal, hows the link holding up?",
  "questions": {
    "route": {
      "type": "choice",
      "instructions": "Which service should answer this message, sent to a station named Cal on a LoRa mesh radio network?",
      "criteria": {
        "weather": "A question asking for current or forecast local weather information: rain, wind, temperature, snow, humidity, storms, whether it is hot or cold out. Not a statement, report or relayed alert about the weather",
        "sunmoon": "Asking for sunrise, sunset, dawn, dusk, twilight, moonrise, moonset or moon phase",
        "calc": "Asking for a computed answer: arithmetic, fractions, percentages, a unit conversion, bolt torque, concrete volume, acreage, a radio or electrical formula (wavelength, antenna length, dBm, ohms, path loss), or a navigation calculation such as a Maidenhead grid square or the distance and bearing between two places",
        "sigreport": "A radio range test, signal test, radio check or mic check, a contact report, or asking how well their signal is being received (SNR, RSSI, 'how do you hear me', 'copy?')",
        "caps": "Asking what this station can do, what topics it knows, or for a list of its commands or help",
        "greeting": "The whole message is only a hello or a wave (hi, hello, hey, good morning, good evening, a wave emoji) and asks nothing. Not a goodbye, not cheering, and not an emoji other than a wave",
        "conversation": "Anything else: chat, questions about the station or its operator, opinions, general knowledge, statements, or radio talk that is not asking for one of the specific services above"
      }
    },
    "weather_now": {
      "type": "noul",
      "instructions": "Is this message asking about the weather conditions right now? Not about past weather and not a forecast."
    },
    "other_station": {
      "type": "noul",
      "instructions": "Does this message ask about the signal, link or reception of a specific other station, repeater, router or node, rather than the link between the sender and Cal?"
    }
  }
}
```

The response. The keys and types are exact; the route and confidence are the ones measured for
this message on 2026-09-24 (`sigreport`, 0.899); **the other numbers are illustrative**:

```json
{
  "model": "semif-qwen3.5-4b-q4km",
  "answers": {
    "route": {"type": "choice", "choice": "sigreport",
              "probabilities": {"weather": 0.0081, "sunmoon": 0.0012, "calc": 0.0021, "sigreport": 0.9134,
                                "caps": 0.0065, "greeting": 0.0044, "conversation": 0.0643},
              "confidence": 0.899},
    "weather_now": {"type": "noul", "noul": 0.0312},
    "other_station": {"type": "noul", "noul": 0.2215}
  },
  "usage": {"input_tokens": 0, "output_tokens": 0},
  "ms": 13042
}
```

* `choice`: the arg-max option; `probabilities` are the softmax at `S1_TEMPERATURE`;
  `confidence = (n·p_max − 1)/(n − 1)` (0 at uniform, 1 at certainty). `s1route` compares this
  against `S1_MIN_CONF`.
* `noul`: the probability of "yes".
* `score` questions (a `criteria` list) are also accepted: `score` is the expected index, with
  `legend`, `probabilities` and `confidence`.
* `usage` is always zero; nothing is metered.

**Status codes:** `200`; `404` wrong path; `413` missing, zero or >256 KiB body; `422` missing
`state`/`questions`, too many questions, state or instructions too long, unknown question type,
fewer than two options, unparseable JSON, or input SemIf refuses (its GGUF and reference
tokenizations disagree, e.g. a lone `❤️`); `503` too hot or busy; `500` anything else, with only
the exception class name. `s1route` treats a 4xx as one rejected request (no backoff until
`S1_REJECTS_MAX` in a row) and a 503 as busy (`S1_BUSY_BACKOFF_S`, 2 min).

`GET /health`:

```json
{"ok": true, "model": "semif-qwen3.5-4b-q4km", "cpu_c": 52, "busy_above_c": 95, "temperature": 1.45}
```

## 6. Guards on the service itself

* **Thermal, two thresholds.** `S1_BUSY_TEMP_C` is the door: at or above it a request is refused
  with `503 {"error": "too hot"}` before any work. `S1_ABORT_TEMP_C` is checked **between
  questions** and aborts with `503 {"error": "busy", "detail": "cpu ...C mid-request"}`. It sits at
  the hardware's limit, not beside the door value: on the reference laptop one question takes the
  package from 40 °C to ~98 °C, and an abort at 95 failed nearly every real request.
* **The sensor fails closed.** An unreadable `S1_ZONE` reads as 999 °C, so the service refuses
  work rather than running unguarded.
* **`S1_ZONE` is machine-specific.** `thermal_zone2` is right for the reference laptop and
  probably wrong for yours. List the zones and pick the CPU package (on Intel usually
  `x86_pkg_temp`). Any file holding millidegrees works, so on AMD, if no zone carries the package,
  point `S1_ZONE` at the `temp1_input` of the `k10temp` device under `/sys/class/hwmon`:

  ```bash
  for z in /sys/class/thermal/thermal_zone*; do echo "$z $(cat $z/type) $(( $(cat $z/temp)/1000 ))C"; done
  ```

  Then check `GET /health` reports a sane `cpu_c`. To test the guard without heating anything,
  point `S1_ZONE` at a file you write, e.g. `echo 96000 > /tmp/t`.
* **Single flight.** One model, one lock. A second concurrent request gets
  `503 {"error": "busy", "detail": "already scoring"}` **at once** rather than queueing past the
  client's 30 s deadline.
* **Bounded work.** At most 4 questions, 4000 characters of state and of each instruction, a
  256 KiB body, a 10 s read timeout. SemIf itself refuses any row over 4096 tokens.
* **Privacy.** No message text is logged: the journal carries question ids, timings, the CPU
  temperature and the chosen option only, and the default per-request access log is switched off.

## 7. Verify, then point cal-mesh at it

From the cal-mesh machine, over the tailnet:

```bash
S1=http://<tailnet-ip>:8799
curl -s $S1/health
curl -s $S1/v1/systemone -H 'Content-Type: application/json' -d '{"state":"cal whats the weather",
  "questions":{"route":{"type":"choice","instructions":"Which service should answer this message?",
  "criteria":{"weather":"A weather question","conversation":"Anything else"}}}}'
curl -s -o /dev/null -w '%{http_code}\n' $S1/v1/systemone -H 'Content-Type: application/json' -d '{}'   # 422
```

Then in cal-mesh's `config`:

```
S1_BACKEND=local
S1_LOCAL_URL=http://<tailnet-ip>:8799/v1/systemone
S1_ROUTE_ENABLED=true
```

Every gate, guard, floor and fail-open path in `s1route.py` is the same code on both backends; the
local path reads no key and sends none.

## 8. The temperature

The model's option logits go through a softmax divided by one number, `S1_TEMPERATURE`. At 1.45
the probabilities are flatter than the raw ones. It was fitted, by minimising log loss, on the
operator's **own** traffic: the 263 messages in the corpus that were **not** addressed to Cal,
then applied to the 56 that were, so it was never fitted on what it was scored against
(`tools/local-system-one/semif_analyze.py`). Your traffic is not that traffic.

**Refitting is optional, not load-bearing.** A review on 2026-09-24 re-ran the saved logits at
T = 1.0 and found the same decisions at the `S1_MIN_CONF` floor: the same three rescues, nothing
broken. The lever was SemIf's option scoring, not the calibration. Leave 1.45, or set 1.0; refit
only if you have your own labelled traffic and a reason.
