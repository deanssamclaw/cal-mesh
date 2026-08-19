#!/usr/bin/env python3
"""eval_dm_memory — the offline gate eval. Most of it is the security boundary.

This is a LIVE path: it stores private words and replays them into a prompt. So the cases that
matter most are the ones that prove memory NEVER stores or recalls anything but the pinned,
key-verified identity — a node-id spoofer, a non-PKI packet, a wrong fingerprint, the feature
off, the unlock unconfigured all get NOTHING. The functional cases (it remembers, it bounds,
it combines) come after, because a memory that leaks is worse than one that forgets.

Runs against a throwaway store (dm_memory.STORE is repointed) so it never touches the real one.
"""
import os, sys, tempfile
import dm_memory

FAIL = []
PIN = "a1b2c3d4e5f60718"          # synthetic 16-hex fingerprint; never a real node's identifier


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
    c = {"DM_MEMORY_ENABLED": "true", "DM_UNLOCK_PUBKEY_FP": PIN,
         "DM_MEMORY_MAX_TURNS": "8", "DM_MEMORY_MAX_CHARS": "1200"}
    c.update(over)
    return c


def rec(fp=PIN, pki=True, node="!5e9de701"):          # synthetic sender id; memory keys on fp, not node
    return {"from": node, "to": "!ca100001", "pki": pki, "pubkey_fp": fp}


# ---------------------------------------------------------------- security boundary
print("SECURITY — memory stores/recalls the pinned identity and NOTHING else")
fresh_store()

# feature off -> inert
check("disabled: no store", dm_memory.remember(cfg(DM_MEMORY_ENABLED="false"), rec(), "q", "a"), False)
check("disabled: no recall", dm_memory.recall(cfg(DM_MEMORY_ENABLED="false"), rec()), None)

# unlock unconfigured (no pinned fp) -> fails closed even if feature on
check("no pin: not enabled", dm_memory.enabled(cfg(DM_UNLOCK_PUBKEY_FP="")), False)
check("no pin: no store", dm_memory.remember(cfg(DM_UNLOCK_PUBKEY_FP=""), rec(), "q", "a"), False)

# forger: right node id, WRONG fingerprint -> nothing
fresh_store()
check("wrong fp: no store", dm_memory.remember(cfg(), rec(fp="deadbeefdeadbeef"), "secret", "reply"), False)
check("wrong fp: no recall", dm_memory.recall(cfg(), rec(fp="deadbeefdeadbeef")), None)

# forger: right node id, NO fingerprint field -> nothing
check("absent fp: no store", dm_memory.remember(cfg(), rec(fp=""), "secret", "reply"), False)

# non-PKI packet (spoofable plaintext) -> nothing even with a matching fp string
check("non-pki: no store", dm_memory.remember(cfg(), rec(pki=False), "q", "a"), False)
check("non-pki: no recall", dm_memory.recall(cfg(), rec(pki=False)), None)

# CROSS-IDENTITY: after storing Dean's turn, a different fp must never read it back
fresh_store()
dm_memory.remember(cfg(), rec(), "which box are you on", "rflab")
check("stored for pinned id", dm_memory.recall(cfg(), rec()) is not None)
check("other fp cannot read pinned history", dm_memory.recall(cfg(), rec(fp="0000111122223333")), None)
check("store holds exactly one identity", list(dm_memory._load().keys()) == [PIN])
# case-insensitive fingerprint match (fp may arrive upper/lower)
check("fp match is case-insensitive", dm_memory.recall(cfg(), rec(fp=PIN.upper())) is not None)

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
