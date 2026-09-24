#!/usr/bin/env python3
"""decider / laya on the addressed set. Both take TypeSafe-shaped typed questions, so the
criteria and guards are s1route's own, verbatim. Usage: small_local.py decider|laya [N]"""
import json, sys, time
BACKEND = sys.argv[1]; LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 56
D = json.load(open("eval_data.json")); CRIT = D["criteria"]; GUARDS = D["guards"]
rows = sorted(D["rows"], key=lambda r: not (r["dm"] or r["calch"] or r["kw"]))[:LIMIT]
Q = {"route": {"type": "choice", "instructions":
     "Which service should answer the message in `mesh_message`, sent to a station named Cal on a "
     "LoRa mesh radio network?", "criteria": CRIT}}
Q.update({k: {"type": "noul", "instructions": v} for k, v in GUARDS.items()})
START_MAX = 70000
def temp():
    try: return int(open("/sys/class/thermal/thermal_zone2/temp").read())
    except Exception: return 0
pauses = [0.0]
def gate():
    t0 = time.time()
    while temp() > START_MAX: time.sleep(5)
    pauses[0] += time.time() - t0
t_load = time.time()
if BACKEND == "decider":
    from decider.infer import Decider
    m = Decider("Mapika/decider-0.8b")
    run = lambda state: m.system_one(state, Q)
else:
    from laya import Router
    r = Router(preload=True)
    # the typed-decisions checkpoint, not the general English one the router picks by default:
    # their own table puts it at 0.766 on typed decisions against 0.362 for `laya`. Raise the
    # option/state budgets too -- our 7 criteria are ~380 tokens and the default head is 256.
    try:
        import laya as _l
        ag = _l.load("convaiinnovations/laya", subfolder="typed-decisions")
        ag.cfg["head_max_len"] = 512; ag.cfg["max_len"] = 1024
        run = lambda state: ag.predict(state, Q)
    except Exception as e:
        print("typed-decisions load failed, falling back to Router:", repr(e), flush=True)
        run = lambda state: r.predict(state, Q, model="typed-decisions")
load_s = round(time.time() - t_load, 1)
out = []
t0 = time.time()
for i, row in enumerate(rows):
    gate()
    t = time.time()
    a = run({"mesh_message": row["clean"]})["answers"]
    wall = round(time.time() - t, 3)
    rt = a["route"]
    out.append({"text": row["text"], "route": rt.get("choice"),
                "conf": round(float(rt.get("confidence", 0)), 4),
                "probs": {k: round(float(v), 4) for k, v in (rt.get("probabilities") or {}).items()},
                "letter_mass": 1.0,
                "weather_now": round(float(a["weather_now"]["noul"]), 4) if "weather_now" in a else None,
                "other_station": round(float(a["other_station"]["noul"]), 4) if "other_station" in a else None,
                "timing": {"wall_s": wall, "prompt_tokens": None, "prefill_s": None, "first": None}})
    if (i + 1) % 10 == 0:
        print(f"{i+1}/{len(rows)} {time.time()-t0:.0f}s temp={temp()/1000:.0f}C", flush=True)
json.dump({"model": BACKEND, "done": True, "load_s": load_s, "elapsed_s": round(time.time() - t0, 1),
           "thermal_pause_s": round(pauses[0], 1), "results": out}, open(f"results_{BACKEND}.json", "w"))
print("DONE", BACKEND, "load", load_s, "s; run", round(time.time() - t0, 1), "s; pauses", round(pauses[0], 1), flush=True)
