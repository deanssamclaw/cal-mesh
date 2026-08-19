#!/usr/bin/env python3
"""learn.py — the mesh distiller. Turns every logged exchange into a build queue.

WHY THIS EXISTS. Every capability this node has ever shipped was built against sentences
Cal invented while imagining the mesh. That is exactly the trap this codebase keeps falling
into — an invariant instantiated only where the code was already right, a review run against
a distribution that does not match the air. The real distribution is sitting in
`decisions.jsonl`: every inbound, its verdict, and what Cal actually said back. This reads
that log and answers one question — *what did a real person ask that Cal could not answer as
Cal?* — and writes the answer down as a ranked ledger.

WHAT IT IS NOT. It NEVER touches the live responder. It proposes; it does not arm. A gap it
surfaces still walks the house gate (default off -> offline eval -> adversarial review ->
arm) before any capability answers it. A loop that silently retuned triggers from traffic is
the unsupervised optimizer we distrust, and it would bypass the gate that has kept security
stable while correctness churned. So this is a reader, not a writer, of everything that flies.

CLASSIFICATION. Each decision record lands in exactly one bucket, by priority:

    FILTERED   matched=false. Dropped upstream (off-list sender, not addressed). Not a gap.
    REFUSED    a designed fixed non-answer (forecast refusal). Working as built.
    GREETING   a fixed greeting ack. Working as built.
    HIT        a real doer answered from a fact (weather / calc / sunmoon / nav).
    GAP        matched, and none of the above caught it — it reached the model, which
               answered as itself. THIS is the build queue. (A matched message reaches the
               model UNLESS a doer, a fixed refusal, or a greeting claimed it first, so the
               rule is defined by exclusion rather than by prompt_kind — which older records
               predating that field do not carry.)

    OTHER exists only as a tripwire: nothing should land here. If it does, the record has a
    shape the rules above did not anticipate and the classifier needs a look.

The ledger ACCUMULATES. `decisions.jsonl` is trimmed to its last 5000 lines, so a run-time
watermark (max ts seen) advances across runs and the aggregate in `gap-ledger.json` survives
rotation. Re-running is idempotent from the watermark: only records newer than last run are
folded in. That makes it safe to put on a loop.

DM WEIGHT. A DM to Cal (to == our node id) is authenticated, private, and high-intent — it is
where the real questions land. Gaps seen over DM are weighted and flagged, because a stranger's
broadcast "what's up" and Dean's DM "list what you know" are not the same signal.

PRIVACY. Output carries inbound text and Cal's replies, including DM content. This repo is
public; `gap-ledger.json`, `gap-ledger.md` and `learn-state.json` are gitignored alongside
`decisions.jsonl` for the same reason. `learn.py` itself is source and is tracked.
"""
import os, sys, re, json, argparse
from datetime import datetime, timezone

BASE       = os.path.expanduser("~/cal-mesh")
DECISIONS  = os.path.join(BASE, "decisions.jsonl")
STATUS     = os.path.join(BASE, "status.json")
STATE      = os.path.join(BASE, "learn-state.json")     # watermark + run counter
LEDGER_JSON = os.path.join(BASE, "gap-ledger.json")     # accumulated aggregate
LEDGER_MD  = os.path.join(BASE, "gap-ledger.md")        # rendered human view

OUR_ID_FALLBACK = "!xxxxxxxx"   # Cal HT (real id lives in gitignored status.json; read via our_id())
DOER_CAPS = {"weather", "calc", "sunmoon", "nav", "navigation"}

# The tell that the model answered as a product instead of as this node. A GAP reply matching
# any of these is the worst kind — it invents a self-description a stranger cannot check. These
# are surfaced FIRST in the ledger. Kept narrow so it flags boilerplate, not every long reply.
_GENERIC_SMELL = re.compile(
    r"\bI can help (?:you )?with\b|\blanguage model\b|\bas an AI\b|\bAI assistant\b|"
    r"\bhappy to help\b|\bfeel free\b|\bhow can I (?:help|assist)\b|\bI'?m Claude\b|"
    r"\bcoding\b|\bgeneral knowledge\b", re.I)


def our_id():
    try:
        return (json.load(open(STATUS)).get("node") or {}).get("id") or OUR_ID_FALLBACK
    except Exception:
        return OUR_ID_FALLBACK


def normalize(text):
    """Cluster key: lowercase, drop the trigger word, strip punctuation, collapse whitespace.
    Mirrors capabilities._normalize so 'Cal, what do you know?' and 'what do you know'
    collapse to one gap, not two."""
    t = (text or "").lower()
    t = re.sub(r"\bcal\b", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def classify(rec, our):
    """One bucket per record, by priority. Returns a bucket name from the docstring set."""
    if not rec.get("matched"):
        return "FILTERED"
    gen = rec.get("gen_status") or ""
    if gen == "fixed_forecast_refused":
        return "REFUSED"
    if gen == "fixed_greeting_ack" or rec.get("capability") == "greeting":
        return "GREETING"
    pk = rec.get("prompt_kind")
    cap = rec.get("capability")
    if cap in DOER_CAPS and pk != "general":
        return "HIT"
    # Matched, but no doer / refusal / greeting claimed it: it reached the model. That is a
    # gap whether the record stamps prompt_kind='general' (current schema) or nothing at all
    # (records before the field existed). OTHER is left only as a tripwire below.
    if rec.get("matched"):
        return "GAP"
    return "OTHER"


def is_dm(rec, our):
    return rec.get("to") == our


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def iter_decisions():
    """Yield parsed records in file order. A malformed line is skipped, not fatal."""
    if not os.path.exists(DECISIONS):
        return
    with open(DECISIONS) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except Exception:
                continue


def fold(reset=False):
    """Read decisions newer than the watermark, fold them into the aggregate, advance the
    watermark. Returns (aggregate, run_summary)."""
    our = our_id()
    state = {"last_ts": "", "runs": 0} if reset else load_json(STATE, {"last_ts": "", "runs": 0})
    agg = {"totals": {}, "clusters": {}} if reset else load_json(
        LEDGER_JSON, {"totals": {}, "clusters": {}})
    totals = agg.setdefault("totals", {})
    clusters = agg.setdefault("clusters", {})
    watermark = state.get("last_ts", "")
    new_watermark = watermark

    run = {"processed": 0, "new_gaps": 0, "buckets": {}}
    for rec in iter_decisions():
        ts = rec.get("ts", "")
        if watermark and ts <= watermark:
            continue          # already folded on a prior run
        if ts > new_watermark:
            new_watermark = ts
        bucket = classify(rec, our)
        dm = is_dm(rec, our)
        run["processed"] += 1
        run["buckets"][bucket] = run["buckets"].get(bucket, 0) + 1
        # cumulative totals, split by DM/broadcast
        totals[bucket] = totals.get(bucket, 0) + 1
        key_dm = bucket + ("_dm" if dm else "_bc")
        totals[key_dm] = totals.get(key_dm, 0) + 1

        if bucket != "GAP":
            continue
        run["new_gaps"] += 1
        key = normalize(rec.get("text", "")) or "(empty)"
        c = clusters.setdefault(key, {
            "count": 0, "dm_count": 0, "examples": [], "replies": [],
            "froms": [], "first_ts": ts, "last_ts": ts, "generic_smell": False})
        c["count"] += 1
        if dm:
            c["dm_count"] += 1
        c["last_ts"] = ts
        frm = rec.get("from", "")
        if frm and frm not in c["froms"]:
            c["froms"].append(frm)
        ex = (rec.get("text") or "").strip()
        if ex and ex not in c["examples"] and len(c["examples"]) < 3:
            c["examples"].append(ex)
        rep = (rec.get("reply") or "").strip()
        if rep and rep not in c["replies"] and len(c["replies"]) < 3:
            c["replies"].append(rep)
        if rep and _GENERIC_SMELL.search(rep):
            c["generic_smell"] = True

    state["last_ts"] = new_watermark
    state["runs"] = state.get("runs", 0) + 1
    json.dump(agg, open(LEDGER_JSON, "w"), ensure_ascii=False, indent=2)
    json.dump(state, open(STATE, "w"))
    return agg, run, state


def rank(clusters):
    """DM-weighted rank: a DM gap counts double, then raw frequency, then recency. The build
    queue reads top-down."""
    def score(kv):
        _, c = kv
        return (c["count"] + c["dm_count"], c["count"], c["last_ts"])
    return sorted(clusters.items(), key=score, reverse=True)


def render_md(agg, state):
    t = agg["totals"]
    lines = []
    lines.append("# cal-mesh gap ledger")
    lines.append("")
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()} · "
                 f"run #{state.get('runs', 0)} · watermark `{state.get('last_ts','')}`_")
    lines.append("")
    lines.append("Propose-only. Every gap below is a candidate capability, not a change. "
                 "Each still walks the gate (off → eval → review → arm) before it answers.")
    lines.append("")
    lines.append("## Buckets (cumulative)")
    lines.append("")
    lines.append("| bucket | total | dm | broadcast |")
    lines.append("|---|---:|---:|---:|")
    for b in ("HIT", "GAP", "REFUSED", "GREETING", "FILTERED", "OTHER"):
        if t.get(b):
            lines.append(f"| {b} | {t.get(b,0)} | {t.get(b+'_dm',0)} | {t.get(b+'_bc',0)} |")
    lines.append("")

    ranked = rank(agg["clusters"])
    lines.append(f"## Gap queue — {len(ranked)} distinct asks Cal could not answer as Cal")
    lines.append("")
    if not ranked:
        lines.append("_No gaps recorded. Every matched message reached a capability._")
    for i, (key, c) in enumerate(ranked, 1):
        flag = " ⚠️ **answered as generic Claude**" if c["generic_smell"] else ""
        dm = f" · {c['dm_count']} over DM" if c["dm_count"] else ""
        lines.append(f"### {i}. `{key}`{flag}")
        lines.append(f"seen {c['count']}×{dm} · {len(c['froms'])} node(s) · "
                     f"last {c['last_ts']}")
        lines.append("")
        for ex in c["examples"]:
            lines.append(f"- asked: \"{ex}\"")
        for rep in c["replies"]:
            lines.append(f"- Cal said: \"{rep}\"")
        lines.append("")
    md = "\n".join(lines) + "\n"
    open(LEDGER_MD, "w").write(md)
    return md


def main():
    ap = argparse.ArgumentParser(description="Distill decisions.jsonl into a gap ledger.")
    ap.add_argument("--reset", action="store_true",
                    help="ignore the watermark and rebuild the aggregate from scratch")
    ap.add_argument("--quiet", action="store_true", help="write files, print only the summary line")
    args = ap.parse_args()

    agg, run, state = fold(reset=args.reset)
    render_md(agg, state)

    t = agg["totals"]
    ranked = rank(agg["clusters"])
    print(f"processed {run['processed']} new record(s) · {run['new_gaps']} new gap(s) · "
          f"{len(ranked)} distinct gap(s) total · ledger -> {LEDGER_MD}")
    if args.quiet:
        return
    print(f"buckets this run: {run['buckets']}")
    print(f"cumulative: HIT={t.get('HIT',0)} GAP={t.get('GAP',0)} "
          f"REFUSED={t.get('REFUSED',0)} GREETING={t.get('GREETING',0)} "
          f"FILTERED={t.get('FILTERED',0)} OTHER={t.get('OTHER',0)}")
    print()
    for i, (key, c) in enumerate(ranked[:10], 1):
        flag = "  [GENERIC-CLAUDE]" if c["generic_smell"] else ""
        dm = f" ({c['dm_count']} DM)" if c["dm_count"] else ""
        print(f"{i:>2}. {c['count']}×{dm}{flag}  {key}")


if __name__ == "__main__":
    main()
