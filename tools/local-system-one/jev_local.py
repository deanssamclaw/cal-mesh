#!/usr/bin/env python3
"""Local Jev-style readout on jlab's Ollama: option-letter probabilities from the first token.
Thermal-gated. Usage: jev_local.py MODEL [limit]"""
import json, math, sys, time, urllib.request
MODEL = sys.argv[1]; LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else None
RAW = "--raw" in sys.argv          # plain completion: no chat template, no thinking channel
D = json.load(open("eval_data.json")); CRIT = D["criteria"]; GUARDS = D["guards"]
ROUTES = list(CRIT); LET = "ABCDEFG"[:len(ROUTES)]
SYS_ROUTE = ("You route messages sent to Cal, a station on a LoRa mesh radio network. Decide which service "
             "should answer the message.\nOptions:\n" +
             "\n".join(f"{L}) {r}: {CRIT[r]}" for L, r in zip(LET, ROUTES)) +
             "\nAnswer with the single letter of the best option and nothing else.")
def sys_guard(q): return ("You judge messages sent to Cal, a station on a LoRa mesh radio network.\n"
                          f"Question: {q.replace('`mesh_message`', 'the message')}\nAnswer Yes or No and nothing else.")
START_MAX = 70000   # begin a call only at or below 70 C; jlab spikes to ~100 C during any prefill
def temp():
    try: return int(open("/sys/class/thermal/thermal_zone2/temp").read())
    except Exception: return 0
pauses = [0]
def gate():
    t0 = time.time()
    while temp() > START_MAX: time.sleep(5)
    pauses[0] += time.time() - t0
def call(system, prompt):
    gate()
    if RAW:
        body = {"model": MODEL, "prompt": system + "\n\n" + prompt, "raw": True, "stream": False,
                "keep_alive": "30m", "logprobs": True, "top_logprobs": 20,
                "options": {"num_predict": 1, "temperature": 0, "num_ctx": 4096}}
    else:
        body = {"model": MODEL, "system": system, "prompt": prompt, "stream": False, "think": False,
                "keep_alive": "30m", "logprobs": True, "top_logprobs": 20,
                "options": {"num_predict": 1, "temperature": 0, "num_ctx": 4096}}
    t0 = time.time()
    r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/generate",
        json.dumps(body).encode(), {"Content-Type": "application/json"}), timeout=600))
    wall = time.time() - t0
    top = {}
    for e in (r.get("logprobs") or [{}])[0].get("top_logprobs", []):
        k = e["token"].strip().strip(")").strip(".")
        top[k] = max(top.get(k, -1e9), e["logprob"])
    return top, {"wall_s": round(wall, 3), "prompt_tokens": r.get("prompt_eval_count"),
                 "prefill_s": round(r.get("prompt_eval_duration", 0) / 1e9, 3), "first": r.get("response")}
def dist(top, keys):
    p = {k: math.exp(top[k]) if k in top else 0.0 for k in keys}
    s = sum(p.values())
    return {k: v / s for k, v in p.items()} if s > 0 else None, s
out = []; t_start = time.time()
rows = sorted(D["rows"], key=lambda r: not (r["dm"] or r["calch"] or r["kw"]))   # addressed first
rows = rows[:LIMIT] if LIMIT else rows
OUT = f"results_{MODEL.replace(':','_')}{'_raw' if RAW else ''}.json"
def save(done=False):
    json.dump({"model": MODEL, "done": done, "elapsed_s": round(time.time() - t_start, 1),
               "thermal_pause_s": round(pauses[0], 1), "results": out}, open(OUT, "w"))
for i, row in enumerate(rows):
    msg = (f"Message: {row['clean']}\nThe single letter of the service that should answer is:"
           if RAW else f"Message: {row['clean']}\nAnswer:")
    top, tm = call(SYS_ROUTE, msg)
    d, mass = dist(top, LET)
    rec = {"text": row["text"], "timing": tm, "letter_mass": round(mass, 4)}
    if d:
        pl = {ROUTES[LET.index(k)]: v for k, v in d.items()}
        best = max(pl, key=pl.get); n = len(pl)
        rec.update({"route": best, "probs": {k: round(v, 4) for k, v in pl.items()},
                    "conf": round(max(0.0, (n * pl[best] - 1) / (n - 1)), 4)})
    else:
        rec.update({"route": None, "probs": None, "conf": None})
    cr = row
    for gname, q in GUARDS.items():            # guards inline, only where a rescue could act
        want = "weather" if gname == "weather_now" else "sigreport"
        if rec.get("route") == want and cr["current"] == "conversation":
            gmsg = (f"Message: {cr['clean']}\nThe answer (Yes or No) is:" if RAW
                    else f"Message: {cr['clean']}\nAnswer:")
            gtop, gtm = call(sys_guard(q), gmsg)
            gd, _ = dist(gtop, ["Yes", "No"])
            rec[gname] = round(gd["Yes"], 4) if gd else None
            rec[gname + "_timing"] = gtm
    out.append(rec); save()
    if (i + 1) % 10 == 0:
        print(f"{i+1}/{len(rows)} {time.time()-t_start:.0f}s temp={temp()/1000:.0f}C pauses={pauses[0]:.0f}s", flush=True)
save(done=True)
print("DONE", round(time.time() - t_start, 1), "s; thermal pauses", round(pauses[0], 1), "s", flush=True)
