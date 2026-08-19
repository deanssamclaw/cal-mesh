#!/usr/bin/env python3
"""dm_memory — per-identity conversation memory on the AUTHENTICATED DM path (default OFF).

WHAT IT BUYS. Today every DM is stateless: "which box are you running on?" and a follow-up
"and how much disk?" arrive as two strangers. This lets Cal carry a short thread with ONE
identity — Dean — so an authenticated exchange builds on the last one instead of starting cold.

WHY IT RIDES THE STRONG TIER, NOT dm_longer. Memory stores private words and replays them into
a later prompt. That is exactly the disclosure the weak `dm_longer` path refuses by design (its
docstring: "no context is injected on this path"). So memory sits ONLY on the `dm_unlock` tier,
which requires pinned node + pinned fingerprint + PKI, and `_identity()` re-checks BOTH the pinned
node id AND the pinned public-key fingerprint itself — the node binding lives inside this module,
not only at the call site, so a second caller or a refactor cannot open a cross-identity read.

HONEST LIMIT — read this before arming (adversarial review, session 128). The fingerprint is NOT
a possession proof. `pubkey_fp` is `sha256(publicKey)[:16]` (bridge.py) and the public key is
broadcast in NodeInfo, so anyone who heard Dean's node can reproduce his fingerprint. And `pki`
is `packet.get("pkiEncrypted") is True` — the firmware's flag, copied verbatim, NOT verified by
any crypto in cal-mesh. So the real trust anchor is that one flag, which Meshtastic's documented
downgrade attack can forge. This is the SAME anchor `dm_unlock` already rides. What memory ADDS
over dm_unlock is PERSISTENCE: dm_unlock's worst forged case is "a forger reads one reply meant
for Dean"; memory's is "an active on-air forger writes poison that replays into Dean's later
prompts." That poison is heavily bounded — the stored question is the sanitized `plan["clean"]`
(first sentence, 120 chars, injection/exfil tokens redacted), so it is factual/context poisoning,
never tool execution or key exfil — and reads still go only to Dean's node (the reply is a DM
encrypted to Dean). But it is a real, persistent delta over the tier's stated forge-tolerance,
which is why ARMING IS A DELIBERATE DECISION recorded in status, not a consequence of the flags.
Do not arm without either accepting that bounded-poison exposure explicitly or adding an
out-of-band factor (a per-session challenge Dean's node answers).

WHAT STILL HOLDS from the rest of the harness, unchanged:
  * Recalled text is DATA, not instructions. Both sides were sanitized before they were stored
    (the question is `plan["clean"]`, the answer is a `clean_reply()` output), and on recall it is
    injected into a plan-mode, no-MCP, no-CLAUDE.md generation exactly like the operator context —
    the model cannot act on it or reach past it.
  * PERSONA_PRIVATE still forbids emitting keys/passwords/PSKs even if the remembered text names one.

BOUNDED. Last N turns, and the injected block is hard-capped in characters — airtime is shared and
a prompt is not a chat window. Oldest turns fall off first.

PRIVACY / STORAGE. `dm-memory.json` is a new place Dean's private words live in plaintext on the
Mac. It is gitignored alongside the other runtime data; this repo is public. Nothing here is a
secret store — it is conversation text — but it is Dean's, so it never leaves the box in git.

GATING. Double-gated and fails closed: `DM_MEMORY_ENABLED` must be true AND the unlock must be
configured (a pinned fingerprint must exist). With the unlock off/unconfigured this module is inert,
which is the state it ships in.
"""
import os, json, re
from datetime import datetime, timezone

BASE  = os.path.expanduser("~/cal-mesh")
STORE = os.path.join(BASE, "dm-memory.json")

# Caps. Kept here, not only in DEFAULTS, so the module is correct even if called with a bare cfg.
_DEF_MAX_TURNS = 8       # how many recent (q,a) pairs to retain per identity
_DEF_MAX_CHARS = 1200    # hard cap on the injected memory block (bytes of prompt, not a window)
_DEF_STORE_Q   = 240     # per-turn stored question length
_DEF_STORE_A   = 240     # per-turn stored answer length


def _int(cfg, key, default):
    try:
        return int(str((cfg or {}).get(key, default)).strip())
    except Exception:
        return default


def enabled(cfg):
    """True only if the feature is on AND the whole unlock is configured. Fails closed and is
    fully double-gated: memory rides dm_unlock, so it stays inert unless the unlock SWITCH is on
    (not just its pins) and BOTH the pinned node id and fingerprint are set. Checking the pins
    without the switch was a defense-in-depth gap (review #5)."""
    cfg = cfg or {}
    if str(cfg.get("DM_MEMORY_ENABLED", "false")).lower() != "true":
        return False
    if str(cfg.get("DM_UNLOCK_ENABLED", "false")).lower() != "true":
        return False
    return bool((cfg.get("DM_UNLOCK_PUBKEY_FP") or "").strip()) \
        and bool((cfg.get("DM_UNLOCK_NODE") or "").strip())


def _identity(cfg, rec):
    """The store key for this record, or None. A key is returned ONLY when the record is a
    PKC-encrypted DM whose pinned NODE ID and public-key FINGERPRINT both match the configured
    pins. Binding the node id here — not only at the dm_unlock call site — is what makes the
    single-identity property hold BY CONSTRUCTION (review #2): recall/remember cannot read or
    write another node's content even if a future caller forgets the from-check. The returned
    value is the fingerprint (the stable store key), never a node id.

    What this does NOT prove: possession of Dean's private key. Both fields are firmware-supplied
    (see the HONEST LIMIT in the module docstring); the fingerprint is derived from a broadcast
    public key and `pki` is an unverified flag. This gate confines the store to the pinned
    identity as strongly as dm_unlock does — no stronger."""
    cfg, rec = cfg or {}, rec or {}
    pin = (cfg.get("DM_UNLOCK_PUBKEY_FP") or "").strip().lower()
    node = (cfg.get("DM_UNLOCK_NODE") or "").strip()
    if not pin or not node:
        return None
    if rec.get("pki") is not True:                       # proto3 drops false; require explicit True
        return None
    if (rec.get("from") or "") != node:                  # pinned node id must match (in-module bind)
        return None
    if (rec.get("pubkey_fp") or "").strip().lower() != pin:
        return None
    return pin


def _load():
    try:
        d = json.load(open(STORE))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(store):
    tmp = STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, ensure_ascii=False)
    os.replace(tmp, STORE)


def recall(cfg, rec):
    """The memory block to inject for this record, or None. None on: feature off, no pinned
    identity, or an empty history. The block is a compact transcript, oldest first, capped."""
    if not enabled(cfg):
        return None
    key = _identity(cfg, rec)
    if not key:
        return None
    turns = _load().get(key, [])
    if not turns:
        return None
    max_turns = _int(cfg, "DM_MEMORY_MAX_TURNS", _DEF_MAX_TURNS)
    cap = _int(cfg, "DM_MEMORY_MAX_CHARS", _DEF_MAX_CHARS)
    lines = ["Earlier in this conversation with Dean (most recent last):"]
    for t in turns[-max_turns:]:
        q = (t.get("q") or "").strip()
        a = (t.get("a") or "").strip()
        if q:
            lines.append(f"- Dean: {q}")
        if a:
            lines.append(f"  You: {a}")
    block = "\n".join(lines)
    if len(block) <= len(lines[0]) + 1:          # header only, nothing real accumulated
        return None
    if len(block) > cap:
        # Trim from the FRONT (oldest), keeping the header and the most recent exchanges, which
        # are the ones a follow-up refers to. Guard the degenerate cap: when the cap is smaller
        # than the header, `cap - len(head) - 1` goes negative and `body[-neg:]` returns almost
        # the WHOLE body (review #4: cap=30 aired 2609 chars). Clamp both branches.
        head = lines[0]
        if cap <= len(head):
            block = head[:cap]
        else:
            body = block[len(head) + 1:]
            keep = cap - len(head) - 1               # guaranteed > 0 here
            block = head + "\n" + body[-keep:].lstrip("\n")
    return block


def remember(cfg, rec, question, answer):
    """Append one (question, answer) turn for the pinned identity. No-op unless enabled and the
    record is the pinned, key-verified identity, so nothing but Dean's key-verified exchanges can
    ever enter the store. Bounded per turn and per identity; oldest turns fall off."""
    if not enabled(cfg):
        return False
    key = _identity(cfg, rec)
    if not key:
        return False
    q = (question or "").strip()[:_DEF_STORE_Q]
    a = (answer or "").strip()[:_DEF_STORE_A]
    if not q and not a:
        return False
    store = _load()
    turns = store.setdefault(key, [])
    turns.append({"ts": datetime.now(timezone.utc).isoformat(), "q": q, "a": a})
    max_turns = _int(cfg, "DM_MEMORY_MAX_TURNS", _DEF_MAX_TURNS)
    if len(turns) > max_turns:
        del turns[:-max_turns]
    _save(store)
    return True


def combine(operator_ctx, memory_block):
    """Merge the operator-curated static context (load_dm_context) with the recalled memory block.
    Either may be None. Operator context first (it is the stable frame), memory second (the live
    thread). Returns None if both are empty, so the caller's `if dm_context` stays truthy-correct."""
    parts = [p for p in (operator_ctx, memory_block) if p and p.strip()]
    return "\n\n".join(parts) if parts else None
