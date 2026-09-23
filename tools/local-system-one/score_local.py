import json, sys, statistics
D = {r["text"]: r for r in json.load(open("eval_data.json"))["rows"]}
R = json.load(open(sys.argv[1])); res = R["results"]
RESC = ("weather", "caps", "sigreport")
def act(x, floor=0.8):
    row = D[x["text"]]
    if row["current"] != "conversation" or x.get("route") not in RESC or (x.get("conf") or 0) < floor: return None
    if x["route"] == "weather" and (x.get("weather_now") or 0) < floor: return None
    if x["route"] == "sigreport" and ((x.get("other_station") is None) or x["other_station"] >= 0.5 or row["names_other"]): return None
    return x["route"]
def score(pop, fn):
    right = fixed = broke = 0
    for x in pop:
        row = D[x["text"]]; route = fn(x) or row["current"]
        right += route == row["truth"]; fixed += route == row["truth"] and row["current"] != row["truth"]
        broke += route != row["truth"] and row["current"] == row["truth"]
    return right, fixed, broke
addr_pub = [x for x in res if D[x["text"]]["kw"] and not D[x["text"]]["dm"] and not D[x["text"]]["calch"]]
addr_any = [x for x in res if D[x["text"]]["kw"] or D[x["text"]]["dm"] or D[x["text"]]["calch"]]
print(f"model {R['model']} | scored {len(res)} | done={R['done']} | elapsed {R['elapsed_s']/60:.0f} min, of which thermal wait {R['thermal_pause_s']/60:.0f} min")
for name, pop in (("addressed public (default)", addr_pub), ("addressed incl. private", addr_any), ("all scored", res)):
    if not pop: continue
    lad = sum(D[x["text"]]["current"] == D[x["text"]]["truth"] for x in pop)
    loc = score(pop, act); jev = score(pop, lambda x: D[x["text"]]["jev_act"])
    alone_loc = sum(x.get("route") == D[x["text"]]["truth"] for x in pop)
    alone_jev = sum(D[x["text"]]["jev_route"] == D[x["text"]]["truth"] for x in pop)
    print(f"  {name:28} n={len(pop):3}  ladder {lad:3} | built+LOCAL {loc[0]} (+{loc[1]} -{loc[2]}) | built+JEV {jev[0]} (+{jev[1]} -{jev[2]}) | route-alone local {alone_loc} vs Jev {alone_jev}")
# calibration: ECE of route confidence (max prob) vs correctness, 10 bins
pts = [(max(x["probs"].values()), x["route"] == D[x["text"]]["truth"]) for x in res if x.get("probs")]
bins = [[] for _ in range(10)]
for p, c in pts: bins[min(9, int(p * 10))].append((p, c))
ece = sum(len(b) / len(pts) * abs(statistics.mean(p for p, _ in b) - statistics.mean(c for _, c in b)) for b in bins if b)
print(f"  calibration: ECE {ece:.3f} over {len(pts)} (top-prob vs correct); mean top-prob {statistics.mean(p for p,_ in pts):.2f}, accuracy {statistics.mean(c for _,c in pts):.2f}")
w = [x["timing"]["wall_s"] for x in res]
print(f"  latency per route call: median {statistics.median(w):.1f}s p95 {sorted(w)[int(len(w)*.95)-1]:.1f}s (excl. cooldown); letter mass min {min(x['letter_mass'] for x in res):.3f}")
acts = [(x["text"][:50], act(x), D[x["text"]]["truth"]) for x in res if act(x)]
print(f"  local acts: {len(acts)}"); [print("    ", a) for a in acts[:15]]
