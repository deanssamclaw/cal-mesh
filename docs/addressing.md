# Addressing and direct messages

*How a message becomes Cal's to answer, and how DMs are handled. Moved out of the README on 2026-09-24, unchanged apart from link paths.*

## Addressing: how a message becomes Cal's

Three routes, and the decision trace names which one fired (`via`: `dm` / `trigger` / `channel`).

| route | how | why it is trustworthy |
|---|---|---|
| DM | `to == our node` | addressed at the protocol level |
| trigger word | `\bcal\b` in the text | the only thing that works on a shared channel |
| own channel | arrives on `CAL_CHANNEL` | see below |

On the public channel the trigger word has to stay: it is the only thing separating a question
*for* Cal from a sentence *about* him, on a channel every node in range can hear. Dropping it
there was tried two other ways and both failed — a reply-to pointer costs more taps than typing
four characters, and a conversation window makes Cal answer messages meant for other people
(it also ate a live clarify in testing, turning a deterministic torque figure into a model guess).

A secondary PSK'd channel carries that meaning in the transport instead. `p->channel` is assigned
only after the payload decrypts and parses to a valid `Data` with a known portnum
(`Router.cpp:482-515`), so the index is a cryptographic assertion that the sender holds a key you
chose — unlike `from`, which is cleartext, or a reply id, forgeable by anyone holding the public
channel's well-known PSK. It is stateless: no window, no ring of remembered ids, no TTL, no clock.

**To set it up on your own mesh:**

1. On one radio: `meshtastic --host <radio> --ch-add cal` — adds a secondary at index 1, leaves
   the primary untouched. Name is capped at **11 characters** (`char name[12]`, channel.pb.h:80)
   and is part of the channel hash along with the PSK (`Channels.cpp:39-52`), so both radios need
   the *identical* name and key or nothing matches and the failure is silent.
2. Key size **256-bit**. The "default/8-bit" option is not a key: the firmware expands it into the
   publicly known default PSK with the last byte bumped by your index (`Channels.cpp:228-244`), so
   any node could transmit on it — which defeats the only reason to use a channel at all.
3. Share it: `meshtastic --host <radio> --qr-all`, scan, choose **Add**. Do NOT use `--ch-set-url`
   / `--seturl`; that replaces every channel *and* the LoRa config.
4. Prove a message crosses on the new index, THEN set `CAL_CHANNEL=<index>` in `config`.
   Until the transport is proven, "not received" and "not addressed" look identical.

**`CAL_CHANNEL` is `-1` when disarmed, never `0`.** Zero is the public channel, so any spelling
that falls open — empty string, typo, missing key, a bare `except` returning 0 — would drop the
trigger requirement for every node on the mesh at once, silently. `eval_channel.py` mutates three
different routes to landing on 0.

**Your key never enters this repo.** The mechanism here is meant to be copied; the key is yours.
`config` is gitignored, and `scrub-staged.sh` blocks a channel URL or a `psk` assignment at
`git add` time — that guard went in before the first channel was created, because a guard that
arrives after the paste is not a guard.

## Direct messages
A DM is a different room, not a louder one.

- **Length.** Broadcast replies are 5–7 words because every one costs shared airtime in the whole
  radio's range. An authenticated DM lands on one screen, so the budget relaxes to ~180 characters.
  Airtime is still shared — this is not a chat window.
- **Authentication is real but is not a security boundary.** Meshtastic PKC gives a `pki_encrypted`
  flag and a public-key fingerprint, and both are captured and pinned. Node IDs remain spoofable,
  so the DM tier is **forge-tolerant by construction**: the worst case if sender auth is forged is
  that a forger reads a reply meant for Dean — never that tools unlock.
- **Content only.** A private-context unlock exists, is disabled, and changes exactly one argument
  to the model: the system prompt. Tool lockdown is byte-identical to the public channel, asserted
  by eval.
- **DMs are published on the dashboard, deliberately.** The DM path is a test bench so experiments
  do not spend open-channel airtime, and showing it is the point. The consequence is stated plainly:
  whatever goes in the DM context file becomes public the first time Cal references it.
