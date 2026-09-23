import json, math, sys, statistics
D = {r["text"]: r for r in json.load(open("eval_data.json"))["rows"]}
CRIT = list(json.load(open("eval_data.json"))["criteria"])
IDX = {x["id"]: x for x in json.load(open("semif_index.json"))}
rows = {}
for l in open(sys.argv[1]):
    d = json.loads(l)
    lg = d.get("option_logits") or []
    if not lg: continue
    rows[d["id"]] = lg
print(f"scored {len(rows)} rows")
def soft(lg, T):
    m = max(lg); e = [math.exp((x - m) / T) for x in lg]; s = sum(e)
    return [x / s for x in e]
def nll(ids, T):
    tot = 0.0
    for i in ids:
        p = soft(rows[i], T); t = D[IDX[i]["text"]]["truth"]
        tot -= math.log(max(1e-12, p[CRIT.index(t)] if t in CRIT else 1e-12))
    return tot / len(ids)
fit_ids = [i for i in rows if not IDX[i]["addressed"]]
ev_ids = [i for i in rows if IDX[i]["addressed"]]
best = min((nll(fit_ids, T / 100), T / 100) for T in range(20, 1200, 5)) if fit_ids else (0, 1.0)
T = best[1]
print(f"fitted T = {T} on {len(fit_ids)} un-addressed rows (NLL {best[0]:.3f}); evaluating on {len(ev_ids)} addressed")
def report(ids, T, label):
    acc = conf = 0; pts = []
    for i in ids:
        p = soft(rows[i], T); k = max(range(len(p)), key=lambda j: p[j]); route = CRIT[k]
        n = len(p); c = max(0.0, (n * p[k] - 1) / (n - 1))
        ok = route == D[IDX[i]["text"]]["truth"]; acc += ok; pts.append((p[k], ok, c, route, i))
    ece = 0.0; bins = [[] for _ in range(10)]
    for p0, ok, *_ in pts: bins[min(9, int(p0 * 10))].append((p0, ok))
    for b in bins:
        if b: ece += len(b) / len(pts) * abs(statistics.mean(x for x, _ in b) - statistics.mean(y for _, y in b))
    print(f"  {label}: route-alone {acc}/{len(ids)} | ECE {ece:.3f} | mean top-p {statistics.mean(x[0] for x in pts):.2f} | conf>=0.8 on {sum(1 for x in pts if x[2]>=0.8)} rows")
    return pts
report(ev_ids, 1.0, "addressed, uncalibrated (T=1)")
pts = report(ev_ids, T, f"addressed, calibrated (T={T})")
# which fallthrough rows would now clear the floor, and into what
cand = [(IDX[i]["text"], route, round(c, 3)) for p0, ok, c, route, i in pts
        if c >= 0.8 and D[IDX[i]["text"]]["current"] == "conversation" and route in ("weather", "caps", "sigreport")]
print(f"  rescue candidates at conf>=0.8 (guards still to apply): {len(cand)}")
for t, r, c in cand: print(f"     {t[:52]!r:56} -> {r} ({c})  truth={D[t]['truth']}")
json.dump({"T": T, "candidates": [{"text": t, "route": r, "conf": c} for t, r, c in cand]},
          open("semif_calibrated.json", "w"))
