#!/usr/bin/env python3
"""dm_memory — per-identity conversation memory on the AUTHENTICATED DM path (default OFF).

WHAT IT BUYS. Today every DM is stateless: "which box are you running on?" and a follow-up
"and how much disk?" arrive as two strangers. This lets Cal carry a short thread with ONE
identity — Dean — so an authenticated exchange builds on the last one instead of starting cold.

WHY IT RIDES THE STRONG TIER, NOT dm_longer. Memory stores private words and replays them into
a later prompt. That is exactly the disclosure the weak `dm_longer` path refuses by design (its
docstring: "no context is injected on this path"), because Meshtastic node ids are spoofable and
PKC has a documented downgrade attack. So memory keys on the ONE thing a spoofer cannot supply —
the sender's pinned public-key fingerprint — and only ever on the `dm_unlock` path, which already
requires pinned node + pinned fingerprint + PKI. The store is therefore SINGLE-IDENTITY BY
CONSTRUCTION: `_identity()` returns a key only when the record's fingerprint equals the configured
pin, so there is no code path that writes or reads another node's content even if a caller is
buggy. A forger who presents Dean's node id but not his key gets nothing — not a read, not a write.

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
    """True only if the feature is on AND the unlock is configured. Fails closed: no pin, no
    memory, because without a pinned fingerprint there is no unspoofable key to file under."""
    cfg = cfg or {}
    if str(cfg.get("DM_MEMORY_ENABLED", "false")).lower() != "true":
        return False
    return bool((cfg.get("DM_UNLOCK_PUBKEY_FP") or "").strip())


def _identity(cfg, rec):
    """The store key for this record, or None. A key is returned ONLY when the record is a
    PKC-encrypted DM whose public-key fingerprint equals the configured pin. This is the entire
    security boundary: the returned value is always the pinned fingerprint, never a node id, so
    the store cannot hold anyone but the pinned identity."""
    cfg, rec = cfg or {}, rec or {}
    pin = (cfg.get("DM_UNLOCK_PUBKEY_FP") or "").strip().lower()
    if not pin:
        return None
    if rec.get("pki") is not True:                       # proto3 drops false; require explicit True
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
        # are the ones a follow-up refers to.
        head = lines[0]
        body = block[len(head) + 1:]
        body = body[-(cap - len(head) - 1):]
        block = head + "\n" + body.lstrip("\n")
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
