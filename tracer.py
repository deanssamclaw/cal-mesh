#!/usr/bin/env python3
"""TRACER — decide which node to traceroute next, and keep score of who answers.

WHY THIS IS ITS OWN LOOP (Dean, 2026-08-23). The first proposal was to hang a traceroute off
the range-test doer: a multi-hop test arrives, fire a probe at the sender. That is a worse
design and he said so. A traceroute answers a question nobody asked at that moment, on someone
else's airtime, and it couples two capabilities whose failure modes have nothing in common. It
also only ever learns about nodes that happen to send tests.

The path question deserves its own program: probe deliberately, record what came back, and
build a picture of the mesh over time.

WHAT THE LOOP IS ACTUALLY FOR, and it is not "collect paths". It is to answer a question that
has been open since 2026-08-19 and was mis-stated in the record until tonight:

    Of 8 probes fired, 2 came back. BOTH responders were nodes the operator owns.

The record called this "direct 2/2, third-party 0/7" and blamed distance. Tonight that was
checked against the data and distance does not explain it: one silent node sat at hops 0
while a node at hops 1 answered fine. The instrument is not the problem either -- the bridge
logs `PROBE REPLY` when one arrives, and did, for the two that worked. And the port census
shows `TRACEROUTE_APP=1` in window after window: the only traceroute traffic on this mesh is
Cal's own, so there is nothing to harvest passively.

So the real question is WHICH NODES ANSWER AT ALL, and the only way to find out is to ask a
lot of them, slowly, and write down what happened. That is the loop. The paths are a
by-product; the responder map is the product.

    (Caveat carried in the open: `hops` for a node Cal has never had NodeInfo from may be a
    default rather than a measurement. The silent hops-0 node above renders with a name
    derived from its own hex, which is what an un-introduced node looks like. Do not read
    hops 0 on such a node as "direct".)

PROPOSE-ONLY. This module never transmits. It writes ONE queue entry per run and the bridge's
own politeness gate decides whether that ever leaves the antenna -- channel utilization under
the firmware's polite threshold, a measurement-window gap, unknown state failing closed. If
the gate says no, the entry simply waits. Airtime policy lives in exactly one place and it is
not here.
"""
import json
import os
import re
import time

# A probe is worth roughly 5.7 s of channel occupancy for a 3-hop round trip, so the budgets
# below are the whole safety story. They are deliberately small: this loop has weeks.
DEFAULTS = {
    "TRACER_ENABLED": "false",
    "TRACER_MAX_PER_DAY": "12",
    # Do not re-probe a node that just answered; its path is fresh.
    "TRACER_REFRESH_S": str(6 * 3600),
    # A node that has ignored us this many times in a row is not running traceroute, or does
    # not want to answer. Stop asking -- but not forever, see TRACER_RETRY_SILENT_S: a node
    # that was simply switched off must be able to rejoin the population.
    "TRACER_SILENT_AFTER": "3",
    "TRACER_RETRY_SILENT_S": str(7 * 86400),
    # Only ask nodes we have actually heard from recently. Probing a node last heard days ago
    # measures its absence, which the node list already told us.
    "TRACER_HEARD_WITHIN_S": str(2 * 3600),
    # A reply has to get home too. Probes go out at the configured hop limit; a node further
    # away than that cannot answer even if it wants to, and counting that as a refusal would
    # poison the responder map with a routing fact.
    "TRACER_MAX_HOPS": "3",
}

ID_RE = re.compile(r"^![0-9a-f]{8}$")


def _int(cfg, key):
    try:
        return int(str(cfg.get(key, DEFAULTS[key])).strip())
    except (TypeError, ValueError):
        return int(DEFAULTS[key])


def load_jsonl(path, limit=4000):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue          # one bad line costs that line, never the run
    except OSError:
        return []
    return out[-limit:]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def responders(routes, ours):
    """{node: last_response_ts} from routes.jsonl.

    Counts only a RESPONSE to a probe WE made. An overheard third-party response says that
    node answers somebody, which is interesting but is not evidence it answers us, and the
    responder map exists to predict our own probes.
    """
    out = {}
    for r in routes:
        if r.get("kind") != "response" or r.get("requester") != ours:
            continue
        node = r.get("traced")
        if not isinstance(node, str) or not ID_RE.match(node):
            continue
        ts = _epoch(r.get("ts"))
        if ts is not None:
            out[node] = max(out.get(node, 0), ts)
    return out


def _epoch(ts):
    """ISO string -> epoch seconds. Returns None rather than guessing."""
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    if not isinstance(ts, str):
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def probe_history(state):
    """{node: [ts, ...]} from the bridge's own probe record, oldest first."""
    out = {}
    for p in (state.get("probes") or []):
        d, ts = p.get("dest"), p.get("ts")
        if isinstance(d, str) and ID_RE.match(d) and isinstance(ts, (int, float)):
            out.setdefault(d, []).append(float(ts))
    for v in out.values():
        v.sort()
    return out


def score(nodes, state, routes, ours, cfg, now):
    """Rank every known node as a probe candidate. Pure: no I/O, no transmission.

    Returns (ranked, skipped) where ranked is a list of dicts, best first, and skipped explains
    every exclusion — because a loop that silently narrows its own population is how a probe
    campaign ends up measuring six nodes and calling it a mesh.
    """
    hist = probe_history(state)
    resp = responders(routes, ours)
    heard_within = _int(cfg, "TRACER_HEARD_WITHIN_S")
    refresh = _int(cfg, "TRACER_REFRESH_S")
    silent_after = _int(cfg, "TRACER_SILENT_AFTER")
    retry_silent = _int(cfg, "TRACER_RETRY_SILENT_S")
    max_hops = _int(cfg, "TRACER_MAX_HOPS")

    ranked, skipped = [], []

    def skip(nid, why):
        skipped.append({"node": nid, "why": why})

    for n in nodes:
        nid = (n or {}).get("id")
        if not isinstance(nid, str) or not ID_RE.match(nid):
            continue
        if nid == ours:
            continue
        heard = n.get("lastHeard")
        if not isinstance(heard, (int, float)) or isinstance(heard, bool):
            skip(nid, "never_heard")
            continue
        age = now - float(heard)
        if age > heard_within:
            skip(nid, "stale")
            continue
        hops = n.get("hops")
        # A node beyond the probe's reach cannot answer. Excluded rather than counted silent:
        # calling that a refusal would write a routing fact into the responder map.
        if isinstance(hops, (int, float)) and not isinstance(hops, bool) and hops > max_hops:
            skip(nid, "too_far")
            continue
        probes = hist.get(nid, [])
        last_probe = probes[-1] if probes else None
        answered = resp.get(nid)
        # Consecutive silences = probes fired since the last answer we got.
        silences = len([t for t in probes if answered is None or t > answered])
        if answered is not None and now - answered < refresh:
            skip(nid, "path_fresh")
            continue
        if silences >= silent_after:
            if last_probe is not None and now - last_probe < retry_silent:
                skip(nid, f"silent_x{silences}")
                continue
        # PRIORITY. Never-probed first, because discovery is what this loop is for and an
        # unasked node is the only kind that can still surprise us. Then known responders whose
        # path has aged out. Then everyone else, oldest probe first, so the campaign rotates
        # instead of grinding on one node.
        if last_probe is None and answered is not None:
            # It answered us once, but the bridge's probe list has since been trimmed. Not a
            # discovery candidate: calling a known responder "never probed" would put it at
            # the front of a queue whose whole purpose is asking nodes we have never asked.
            tier, why = 1, "responder, probe record trimmed"
        elif last_probe is None:
            tier, why = 0, "never probed"
        elif answered is not None:
            tier, why = 1, "responder, path stale"
        else:
            tier, why = 2, f"silent x{silences}, retrying"
        ranked.append({"node": nid, "short": n.get("short"), "hops": hops, "tier": tier,
                       "why": why, "last_probe": last_probe, "answered": answered,
                       "silences": silences, "heard_age_s": int(age)})
    ranked.sort(key=lambda r: (r["tier"], r["last_probe"] if r["last_probe"] else 0,
                               r["heard_age_s"]))
    return ranked, skipped


def spent_today(state, now):
    """Probes fired since UTC midnight, read from the bridge's own record.

    Deliberately derived from the bridge's probe list rather than a counter this module keeps:
    two counters for one quantity is how a budget gets spent twice.
    """
    day_start = now - (now % 86400)
    return len([p for p in (state.get("probes") or [])
                if isinstance(p.get("ts"), (int, float)) and p["ts"] >= day_start])


def plan(nodes, state, routes, ours, cfg, now=None):
    """(target|None, reason, ranked, skipped). Never transmits, never writes."""
    now = time.time() if now is None else now
    if str(cfg.get("TRACER_ENABLED", "false")).lower() != "true":
        return None, "tracer_disabled", [], []
    used = spent_today(state, now)
    cap = _int(cfg, "TRACER_MAX_PER_DAY")
    if used >= cap:
        return None, f"budget_spent_{used}_of_{cap}", [], []
    ranked, skipped = score(nodes, state, routes, ours, cfg, now)
    if not ranked:
        return None, "no_candidates", ranked, skipped
    return ranked[0], "probe", ranked, skipped


def enqueue(queue_dir, target, now=None):
    """Write ONE queue entry. The bridge decides if and when it is ever sent."""
    now = time.time() if now is None else now
    os.makedirs(queue_dir, exist_ok=True)
    path = os.path.join(queue_dir, f"{int(now * 1000)}-{target['node'].lstrip('!')}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"dest": target["node"], "why": target["why"]}, f)
    os.replace(tmp, path)             # atomic: the bridge globs this directory continuously
    return path


def _ledger(ranked, skipped, resp, hist, target, reason):
    """A short human-readable account of the campaign so far. Score is the product."""
    asked = sorted(hist)
    answered = sorted(resp)
    lines = ["# cal-mesh route ledger", ""]
    lines.append(f"asked **{len(asked)}** node(s), **{len(answered)}** answered · "
                 f"{len(ranked)} live candidate(s), {len(skipped)} excluded")
    if asked:
        rate = 100.0 * len(answered) / len(asked)
        lines.append(f"response rate **{rate:.0f}%** — the number this loop exists to move")
    lines += ["", "## Answered", ""]
    lines += [f"- `{n}`" for n in answered] or ["_(none)_"]
    lines += ["", "## Asked, never answered", ""]
    lines += [f"- `{n}` ({len(hist[n])}x)" for n in asked if n not in resp] or ["_(none)_"]
    lines += ["", f"## Next: {target['node'] if target else '—'} ({reason})", ""]
    for r in ranked[:10]:
        lines.append(f"- `{r['node']}` {r['short'] or ''} tier {r['tier']} — {r['why']}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    """CLI: `python3 tracer.py [--enqueue]`. Without --enqueue it only reports."""
    import sys
    argv = sys.argv[1:] if argv is None else argv
    base = os.path.dirname(os.path.abspath(__file__))
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(base, "config"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except OSError:
        pass
    nodes = (load_json(os.path.join(base, "nodes.json"), {}) or {}).get("nodes") or []
    state = load_json(os.path.join(base, "traceroute-state.json"), {}) or {}
    routes = load_jsonl(os.path.join(base, "routes.jsonl"))
    ours = ((load_json(os.path.join(base, "status.json"), {}) or {}).get("node") or {}).get("id")
    if not ours:
        print("no node id in status.json — refusing to plan against an unknown self")
        return 1
    target, reason, ranked, skipped = plan(nodes, state, routes, ours, cfg)
    now = time.time()
    text = _ledger(ranked, skipped, responders(routes, ours), probe_history(state),
                   target, reason)
    with open(os.path.join(base, "route-ledger.md"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    if target and "--enqueue" in argv:
        path = enqueue(os.path.join(base, "traceroute"), target, now=now)
        print(f"queued {target['node']} -> {os.path.basename(path)} "
              f"(the bridge decides if it is polite to send)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
