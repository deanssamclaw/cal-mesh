#!/usr/bin/env python3
"""Post-arm watch for the COMPUTE doer. Read-only; run it any time.

The three things the round-3 adversarial review said to watch after arming:

  1. MIS-PARSE SIGNATURE — a number in the reply that is a FRAGMENT of a number in the question.
     That is the shape every wrong answer this doer produced took: ".5 mi" read as "5 mi",
     "10^-3 w" read as "3 w", "2e3" read as "3". A computed answer legitimately contains numbers
     that are not in the question (32.8 cm from 915 MHz), so novelty alone is not the signal —
     truncation is.
  2. FIRE RATIO — calc fires as a share of inbound. A jump means a false-fire shape we have not
     seen has been found by real traffic.
  Known limit: this catches the LEADING-DECIMAL family (".5" -> "5"), which is what the live
  defect was. It cannot see the caret/exponent family ("10^-3 w" -> "3 w"), because there the
  fragment genuinely appears in the question as its own digit run.

  3. DM-PATH CALC — the DM path is where the trigger strip is loosest and where the reply is
     published, so those exchanges are listed individually rather than counted.

Usage:  python3 watch-calc.py [--days N]
Exit 1 if any mis-parse candidate is found, so it can be wired to a check later.
"""
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
DECISIONS = os.path.join(BASE, "decisions.jsonl")
OUR_ID = None
try:
    OUR_ID = (json.load(open(os.path.join(BASE, "status.json"))).get("node") or {}).get("id")
except Exception:
    pass

# MUST match a LEADING-DOT decimal. Without the `\.\d+` alternative, ".5" extracts as "5"
# and the truncation this whole check exists to find becomes invisible — verified by a
# negative control that failed to fire on ".5 mi" -> "5 mi".
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?|\.\d+")
days = 7
if "--days" in sys.argv:
    days = float(sys.argv[sys.argv.index("--days") + 1])
cutoff = time.time() - days * 86400


def nums(s):
    return {m.group(0).replace(",", "") for m in NUM.finditer(s or "")}


def ts_of(rec):
    try:
        from datetime import datetime
        return datetime.fromisoformat(rec["ts"]).timestamp()
    except Exception:
        return 0.0


def fragment_of(small, big):
    """Is `small` a truncation of `big` rather than a different number? '5' of '0.5', '3' of
    '10^-3'. Equal values are not fragments; a genuinely computed value is not either."""
    if small == big:
        return False
    return (big.endswith(small) or big.startswith(small)) and len(small) < len(big)


rows = []
for line in open(DECISIONS) if os.path.exists(DECISIONS) else []:
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    if ts_of(r) >= cutoff:
        rows.append(r)

inbound = [r for r in rows if r.get("text")]
calc = [r for r in inbound if r.get("capability") == "calc" and r.get("reply")]
is_dm = lambda r: OUR_ID is not None and r.get("to") == OUR_ID

print(f"window: last {days:g} day(s)   inbound: {len(inbound)}   calc replies: {len(calc)}")

# ---- 2. fire ratio, split by channel ----------------------------------------------------------
bc_in = [r for r in inbound if not is_dm(r)]
dm_in = [r for r in inbound if is_dm(r)]
bc_calc = [r for r in calc if not is_dm(r)]
dm_calc = [r for r in calc if is_dm(r)]
def pct(a, b):
    return f"{100.0 * len(a) / len(b):.1f}%" if b else "n/a"
print(f"  broadcast: {len(bc_calc)}/{len(bc_in)} ({pct(bc_calc, bc_in)})"
      f"   direct: {len(dm_calc)}/{len(dm_in)} ({pct(dm_calc, dm_in)})")

# ---- 1. mis-parse candidates -------------------------------------------------------------------
suspects = []
for r in calc:
    q, a = nums(r["text"]), nums(r["reply"])
    for x in a:
        if any(fragment_of(x, y) for y in q):
            suspects.append((r, x))
            break

print()
if suspects:
    print(f"!! {len(suspects)} MIS-PARSE CANDIDATE(S) — a reply number is a truncation of a question number")
    for r, x in suspects:
        print(f"   {r.get('ts', '')[:19]}  {r.get('text')!r}")
        print(f"       -> {r.get('reply')!r}   (suspect fragment: {x})")
else:
    print("no mis-parse candidates (no reply number is a fragment of a question number)")

# ---- 3. DM-path calc, listed individually -------------------------------------------------------
print()
if dm_calc:
    print(f"DM-path calc replies ({len(dm_calc)}) — published, and the loosest trigger path:")
    for r in dm_calc:
        h = ((r.get("calc") or {}).get("handler")) or "?"
        print(f"   {r.get('ts', '')[:19]}  [{h}]  {r.get('text')!r}")
        print(f"       -> {r.get('reply')!r}")
else:
    print("DM-path calc replies: none")

sys.exit(1 if suspects else 0)
