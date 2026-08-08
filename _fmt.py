#!/usr/bin/env python3
"""Format cal-mesh inbox/sent jsonl lines for the `mesh` CLI. Reads stdin."""
import sys, json
for ln in sys.stdin:
    ln = ln.strip()
    if not ln:
        continue
    try:
        r = json.loads(ln)
        ts = str(r.get("ts", ""))[11:19]
        frm = str(r.get("from", r.get("dest", "")))
        to = str(r.get("to", ""))
        ch = r.get("channel", "")
        arrow = f"{frm:>10} -> {to:<8}" if to else f"{frm:>10}"
        print(f"{ts}  {arrow} ch{ch}: {r.get('text','')}", flush=True)
    except Exception:
        print(ln, flush=True)
