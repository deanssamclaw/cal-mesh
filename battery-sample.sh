#!/bin/bash
# Samples Cal HT's battery/voltage from the bridge status file once a minute.
# The T-Deck's MAX17048 is in ADC-fallback, so a plugged-in reading is the
# charge rail (battery pinned to the 101 sentinel), NOT the cell. Real cell
# readings only appear while OFF external power. Kill: pkill -f battery-sample
cd "$(dirname "$0")" || exit 1
while true; do
  python3 - <<'PY' >> battery-history.jsonl 2>/dev/null
import json, datetime
try:
    d = json.load(open("status.json"))
except Exception:
    raise SystemExit
m = d.get("metrics")
if not m:
    raise SystemExit
print(json.dumps({
    "ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "connected": d.get("connected"),
    "battery": m.get("battery"),
    "voltage": m.get("voltage"),
    "uptime": m.get("uptime"),
}))
PY
  sleep 60
done
