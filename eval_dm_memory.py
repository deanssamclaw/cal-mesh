#!/usr/bin/env python3
"""eval_dm_memory — the offline gate eval. Most of it is the security boundary.

This is a LIVE path: it stores private words and replays them into a prompt. So the cases that
matter most prove memory stores/recalls ONLY the pinned identity, and — after the session-128
adversarial review — that it models the RIGHT adversary. The first version of this eval only ever
flipped the fingerprint to a WRONG value; it never modelled the realistic forger, who supplies
Dean's *public* fingerprint (broadcast in NodeInfo, so trivially known) with a forged `pki` flag.
That forger is the actual threat, and the honest truth is that this layer cannot distinguish him
from Dean — the fingerprint is not a possession proof (see dm_memory's HONEST LIMIT). What this
layer CAN guarantee is pinned-node + pinned-fp binding done in-module, a fully double-gated enable,
and a bounded store. Those are what the cases below pin.

Runs against a throwaway store (dm_memory.STORE is repointed) so it never touches the real one.
"""
import os, sys, tempfile
import dm_memory

FAIL = []
PIN  = "a1b2c3d4e5f60718"          # synthetic fingerprint = sha256(pubkey)[:16]; PUBLIC, not secret
NODE = "!5e9de701"                 # synthetic pinned node id
ATTACKER = "!aa000001"             # a different node the forger actually controls


def check(name, got, want=True):
    ok = (got == want)
    print(f"  {'ok ' if ok else 'FAIL'}  {name}" + ("" if ok else f"   got {got!r} want {want!r}"))
    if not ok:
        FAIL.append(name)


def fresh_store():
    fd, path = tempfile.mkstemp(prefix="dmmem-", suffix=".json")
    os.close(fd)
    os.unlink(path)                # start empty; _load tolerates missing
    dm_memory.STORE = path
    return path


def cfg(**over):
    c = {"DM_MEMORY_ENABLED": "true", "DM_UNLOCK_ENABLED": "true",
         "DM_UNLOCK_PUBKEY_FP": PIN, "DM_UNLOCK_NODE": NODE,
         "DM_MEMORY_MAX_TURNS": "8", "DM_MEMORY_MAX_CHARS": "1200"}
    c.update(over)
    return c


def rec(fp=PIN, pki=True, node=NODE):
    return {"from": node, "to": "!ca100001", "pki": pki, "pubkey_fp": fp}


# ---------------------------------------------------------------- gating (fails closed, double)
print("GATING — inert unless the whole unlock is configured (review #5)")
fresh_store()
check("disabled: no store", dm_memory.remember(cfg(DM_MEMORY_ENABLED="false"), rec(), "q", "a"), False)
check("disabled: no recall", dm_memory.recall(cfg(DM_MEMORY_ENABLED="false"), rec()), None)
check("unlock switch off: not enabled", dm_memory.enabled(cfg(DM_UNLOCK_ENABLED="false")), False)
check("unlock switch off: no store", dm_memory.remember(cfg(DM_UNLOCK_ENABLED="false"), rec(), "q", "a"), False)
check("no pinned fp: not enabled", dm_memory.enabled(cfg(DM_UNLOCK_PUBKEY_FP="")), False)
check("no pinned node: not enabled", dm_memory.enabled(cfg(DM_UNLOCK_NODE="")), False)
check("fully configured: enabled", dm_memory.enabled(cfg()), True)

# ---------------------------------------------------------------- identity binding
print("\nIDENTITY — pinned node AND fingerprint, bound in-module (review #2)")
fresh_store()

# The FIX: an attacker on his OWN node, presenting Dean's (public) fingerprint and a forged pki
# flag, is rejected — the node id must also match the pin, and that check now lives inside
# _identity, not only at the dm_unlock call site.
check("attacker node + Dean's fp: no store", dm_memory.remember(cfg(), rec(node=ATTACKER), "poison", "x"), False)
check("attacker node + Dean's fp: no recall", dm_memory.recall(cfg(), rec(node=ATTACKER)), None)

# wrong / absent fingerprint, non-pki: all rejected (these were always covered)
check("wrong fp: no store", dm_memory.remember(cfg(), rec(fp="deadbeefdeadbeef"), "s", "r"), False)
check("absent fp: no store", dm_memory.remember(cfg(), rec(fp=""), "s", "r"), False)
check("non-pki packet: no store", dm_memory.remember(cfg(), rec(pki=False), "q", "a"), False)
check("non-pki packet: no recall", dm_memory.recall(cfg(), rec(pki=False)), None)

# HONEST LIMIT (review #1): a forger who supplies the pinned NODE + the pinned (public) FP + a
# forged pki:True IS accepted here — this layer cannot tell him from Dean. We assert it so the
# residual exposure is documented in the corpus, not hidden. The reply still goes only to Dean's
# node and the stored text is sanitized, so the exposure is bounded factual poison, not exfil.
fresh_store()
check("KNOWN LIMIT: pinned-node+fp+forged-pki is accepted (not a possession proof)",
      dm_memory.remember(cfg(), rec(), "legit-or-forged", "reply"), True)

# CROSS-IDENTITY: a different fingerprint (even from the pinned node) cannot read the history
fresh_store()
dm_memory.remember(cfg(), rec(), "which box are you on", "rflab")
check("pinned id can read its own history", dm_memory.recall(cfg(), rec()) is not None)
check("different fp cannot read pinned history", dm_memory.recall(cfg(), rec(fp="0000111122223333")), None)
check("store holds exactly one identity", list(dm_memory._load().keys()) == [PIN])
check("fingerprint match is case-insensitive", dm_memory.recall(cfg(), rec(fp=PIN.upper())) is not None)

# ---------------------------------------------------------------- functional
print("\nFUNCTIONAL — remembers, bounds, combines")
fresh_store()
c = cfg()
dm_memory.remember(c, rec(), "which box are you running on?", "I'm on rflab.")
dm_memory.remember(c, rec(), "how much disk?", "67 GB free.")
block = dm_memory.recall(c, rec())
check("recall contains earlier Q", "which box" in block)
check("recall contains earlier A", "67 GB free" in block)
check("recall is oldest-first (box before disk)", block.index("which box") < block.index("how much disk"))

# max_turns: only the last N pairs survive
fresh_store()
c = cfg(DM_MEMORY_MAX_TURNS="3")
for i in range(6):
    dm_memory.remember(c, rec(), f"question number {i}", f"answer {i}")
block = dm_memory.recall(c, rec())
check("oldest turn (0) fell off", "question number 0" not in block)
check("newest turn (5) retained", "question number 5" in block)
check("store trimmed to max_turns", len(dm_memory._load()[PIN]) == 3)

# max_chars: injected block is hard-capped, header preserved, recent kept
fresh_store()
c = cfg(DM_MEMORY_MAX_CHARS="200", DM_MEMORY_MAX_TURNS="50")
for i in range(40):
    dm_memory.remember(c, rec(), f"q{i} " + "x" * 20, f"a{i} " + "y" * 20)
block = dm_memory.recall(c, rec())
check("block respects char cap", len(block) <= 200)
check("cap keeps the header", block.startswith("Earlier in this conversation"))
check("cap keeps a recent turn over an old one", ("q39" in block) and ("q0" not in block))

# review #4: a cap SMALLER than the header must still hold, not blow out to the whole body
fresh_store()
c = cfg(DM_MEMORY_MAX_CHARS="30", DM_MEMORY_MAX_TURNS="50")
for i in range(40):
    dm_memory.remember(c, rec(), f"q{i} " + "x" * 20, f"a{i} " + "y" * 20)
block = dm_memory.recall(c, rec())
check("degenerate cap (< header) still respected", len(block) <= 30)

# empty-history and empty-turn handling
fresh_store()
check("empty history recalls None", dm_memory.recall(cfg(), rec()), None)
check("empty q and a not stored", dm_memory.remember(cfg(), rec(), "", ""), False)
check("still empty after empty store attempt", dm_memory.recall(cfg(), rec()), None)

# combine(): operator context + memory, either may be None
print("\ncombine() — operator frame first, live thread second")
check("both None -> None", dm_memory.combine(None, None), None)
check("op only", dm_memory.combine("OPCTX", None), "OPCTX")
check("mem only", dm_memory.combine(None, "MEM"), "MEM")
merged = dm_memory.combine("OPCTX", "MEM")
check("both: op before mem", merged.index("OPCTX") < merged.index("MEM"))
check("both: blank-string treated as empty", dm_memory.combine("   ", "MEM"), "MEM")

print()
if FAIL:
    print(f"FAIL — {len(FAIL)} case(s): {FAIL}")
    sys.exit(1)
print("all eval_dm_memory checks pass")
