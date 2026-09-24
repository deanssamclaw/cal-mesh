# cal-mesh — an AI station on a Meshtastic radio mesh

Cal is a station on a LoRa mesh (LilyGO T-Deck, US / LONG_FAST) that answers questions over the
air. It works as a **field reference that still answers when nothing else can**: sun and moon
times, RF and unit maths, signal reports, current weather. Capabilities that need no network
rank first, because the moment this earns its place is the moment the internet is gone too.

**What it won't do is as much of the design as what it will.** Every number on air comes from
code or from the radio, never from a language model. Every capability has an explicit point
where it says "I can't verify that". Each one ships switched off until an offline eval and an
independent adversarial review pass.

## How it works

```mermaid
flowchart LR
    rin(["📻 Radio"]) --> bin["<b>bridge.py</b><br/><i>receives</i>"]
    bin --> resp{"<b>responder.py</b><br/>does a word rule<br/>claim it?"}
    resp -- "yes" --> cap["<b>Capability</b><br/>signal · sun/moon · calc<br/>weather · what-can-you-do<br/><i>answers from real data</i>"]
    resp -- "no — and it's<br/>addressed to Cal" --> s1{{"<b>s1route</b><br/>which capability should answer?<br/><i>local scorer · Jev backup</i>"}}
    s1 -- "confident, checks pass" --> cap
    s1 -- "not sure" --> llm["<b>Language model</b><br/><i>writes a reply,<br/>computes nothing</i>"]
    cap --> bout["<b>bridge.py</b><br/><i>transmits</i>"]
    llm --> bout
    bout --> rout(["📻 Radio"])

    classDef radio fill:#e8f1fb,stroke:#3b82c4,color:#123;
    classDef code fill:#eef7ee,stroke:#3f9142,color:#123;
    classDef model fill:#fdf3e4,stroke:#c98a1b,color:#123;
    class rin,rout radio;
    class bin,bout,resp,cap code;
    class s1,llm model;
```

Green is plain code; amber is where a model is involved. `bridge.py` is the only process that
touches the radio, and `dashboard.py` publishes every decision along the way.

- **Capabilities first, model last.** Each capability is a small deterministic program that
  claims a message with a word rule and answers from real data. The model only sees what nothing
  else claimed, and it never produces a number.
- **Second-opinion routing.** When no rule claims a message addressed to Cal, a decision model
  may pick which capability should answer. It writes nothing. The primary scorer is a frozen
  open model on a laptop in the house, and Jev (TypeSafe AI) is the backup for public messages.
  → [`docs/router.md`](docs/router.md)
- **Fail open to today.** Any failure in anything optional leaves Cal doing what it did without
  that part.
- **Everything is on the page.** Each reply's trace shows which rule, capability or model
  produced it. Positions are coarsened. No coordinates or credentials are published.

## What Cal answers

| Capability | Where the answer comes from | State |
|---|---|---|
| Signal report ("range test", "you copy?") | the radio's own measurements | armed; runs first |
| Sun / moon / twilight | computed in Python; works offline | armed |
| Arithmetic, units, RF formulas | computed in Python | armed |
| Current weather | fetched from NWS; model narrates the fact only | armed |
| "What can you do" | the live config flags | armed |
| Greeting ack (strangers) | a fixed table, once a day | armed |
| Second-opinion routing | picks one of the above; writes nothing | armed 2026-09-24 |
| Anything else addressed to Cal | a language model, no tools, no private context | armed |

What each one refuses, and why two were designed and then killed: [`docs/capabilities.md`](docs/capabilities.md).
The live list, with every limit, is on the dashboard's capabilities page.

## Run it

```bash
git clone https://github.com/deanssamclaw/cal-mesh.git ~/cal-mesh && cd ~/cal-mesh   # must live at ~/cal-mesh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example config        # TRANSPORT, PORT or HOST, ALLOW_FROM; leave RESPONDER_ENABLED=false
nc -vz meshtastic.local 4403    # if TRANSPORT=tcp: the radio's API must answer first
./run-evals.sh                  # needs node for 3 suites; a fresh clone is amber until it has traffic
```

Start the services from [`deploy/`](deploy) (launchd or systemd), then `./mesh watch` to see
traffic arrive. Leave the responder off for a week and read `decisions.jsonl`: it shows what your
mesh actually asks and what Cal would have said. Green is the line
`GREEN — every suite ran and passed.`, never just exit 0. Hardware and reasoning:
[`docs/design-essay.md`](docs/design-essay.md).

**Off switches** (config is re-read live, no restart):
- `RESPONDER_ENABLED=false` is the master switch: Cal transmits nothing on his own (no reply,
  signal report, greeting or probe). Manual `./mesh send` still works.
- Each capability also has its own `*_ENABLED`.
- `S1_ROUTE_ENABLED=false` turns routing off, and `S1_FALLBACK=none` stops anything going to the
  cloud.

## Docs

| | |
|---|---|
| [`docs/design-essay.md`](docs/design-essay.md) | what this is, why it is built this way, and how to build one |
| [`docs/router.md`](docs/router.md) | runbook: add second-opinion routing with a local scorer and a cloud backup |
| [`docs/capabilities.md`](docs/capabilities.md) | each capability, the signal report and contact reports, what was refused |
| [`docs/addressing.md`](docs/addressing.md) | when a message counts as Cal's; direct messages |
| [`docs/trace-and-privacy.md`](docs/trace-and-privacy.md) | the public decision trace; what is coarsened or withheld |
| [`docs/learning.md`](docs/learning.md) | simulated replies, public grading, and the learning loop |
| [`docs/operations.md`](docs/operations.md) | processes, files, transport, config hot-reload, standing facts |
| [`docs/router-record.md`](docs/router-record.md) | how routing was decided, measured and armed |
| [`docs/proposals/`](docs/proposals) | design proposals, including the ones that lost |
| [`deploy/`](deploy) · [`tools/system-one/`](tools/system-one) | service definitions · the local routing scorer |
