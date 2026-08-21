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

    CLARIFY    a doer parsed the ask and asked for one input it refused to assume
               ("Which grade -- 2, 5 or 8?"). A HALF-BUILT capability, and under the old
               classifier indistinguishable from a clean HIT.
    NO_TABLE   a doer recognised the ask and has no ground truth for it. The most valuable
               record here: a labelled request for a capability, in the asker's own words.
               Bucketing it as a designed refusal — "working as built" — buried it.

    OTHER exists only as a tripwire: nothing should land here. If it does, the record has a
    shape the rules above did not anticipate and the classifier needs a look.

THE ORACLE TIER. A gap is not a build order until someone decides what the RIGHT answer is
measured against. Everything armed in this repo that stayed correct was pinned outside itself
— SAE J429 for proof stress, ASME B1.1 for stress area, the yield printed on the bag. Mutation
counts prove the assertions bind; they do not prove the constants are right, and a loop that
writes both a doer and its eval produces a model answer with a green test suite laundering it.
So every cluster carries a triage verdict, and the ledger's actionable queue is the UNTRIAGED
one — the question the loop puts to a human is "what is the oracle", never "shall I build it".

    derivable     ground truth already cited in this repo or in a named standard -> buildable
    needs-source  real ask, no source in hand -> the deliverable is the SOURCE, not the numbers
    none          no oracle exists -> permanent refusal, and it stops being re-proposed

TWO METRICS, and the second one is the point. Coverage is gap RECURRENCE: after a doer arms,
does that ask still reach the model? Alongside it runs the counter-metric, CORRECTIONS: doers
that had to be fixed after arming. A loop scored only on what it builds will build.

THE BASELINE. `found_by` records whether a cluster was surfaced by this loop or spotted by
hand. KarpStack's local driver lost to random search and only measurement showed it; the same
question is open here, and it cannot be answered without keeping score.

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
TRIAGE     = os.path.join(BASE, "triage.json")          # oracle verdicts, arm dates, corrections
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
    # The doer's own verdict, stamped by calc._dispatch. Read BEFORE the HIT rule, because both
    # of these reached a doer and would otherwise be counted as answered.
    outcome = (rec.get("calc") or {}).get("outcome")
    if outcome == "clarify":
        return "CLARIFY"
    if outcome == "no_table":
        return "NO_TABLE"
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


CLUSTERED = ("GAP", "CLARIFY", "NO_TABLE")
SCHEMA = 2


def total(c, bucket=None):
    """Cluster count, overall or for one bucket. Tolerates the v1 shape so a ledger written
    before the CLARIFY/NO_TABLE split still reads."""
    if "counts" not in c:
        return c.get("count", 0) if bucket in (None, "GAP") else 0
    if bucket:
        return c["counts"].get(bucket, 0)
    return sum(c["counts"].values())


def dm_total(c, bucket=None):
    if "dm_counts" not in c:
        return c.get("dm_count", 0) if bucket in (None, "GAP") else 0
    if bucket:
        return c["dm_counts"].get(bucket, 0)
    return sum(c["dm_counts"].values())


def migrate(agg):
    """v1 clusters carried a single GAP count. Rewrite them in place rather than resetting:
    decisions.jsonl keeps only its last 5000 lines, so a rebuild silently drops every gap
    older than the rotation and the ledger would look like the mesh had gone quiet."""
    if agg.get("schema") == SCHEMA:
        return agg, 0
    n = 0
    for c in agg.get("clusters", {}).values():
        if "counts" in c:
            continue
        c["counts"] = {"GAP": c.pop("count", 0)}
        c["dm_counts"] = {"GAP": c.pop("dm_count", 0)}
        c["last_by_bucket"] = {"GAP": c.get("last_ts", "")}
        n += 1
    agg["schema"] = SCHEMA
    return agg, n


def fold(reset=False):
    """Read decisions newer than the watermark, fold them into the aggregate, advance the
    watermark. Returns (aggregate, run_summary)."""
    our = our_id()
    state = {"last_ts": "", "runs": 0} if reset else load_json(STATE, {"last_ts": "", "runs": 0})
    agg = {"totals": {}, "clusters": {}, "schema": SCHEMA} if reset else load_json(
        LEDGER_JSON, {"totals": {}, "clusters": {}})
    agg, migrated = migrate(agg)
    totals = agg.setdefault("totals", {})
    clusters = agg.setdefault("clusters", {})
    watermark = state.get("last_ts", "")
    new_watermark = watermark

    run = {"processed": 0, "new_gaps": 0, "buckets": {}, "migrated": migrated}
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

        # Three buckets cluster, not one. A GAP that becomes a CLARIFY is progress and has to be
        # visible as such; a NO_TABLE is a request for a capability by name. Counting only GAPs
        # means the loop goes blind at exactly the moment a doer starts half-working.
        if bucket not in CLUSTERED:
            continue
        if bucket == "GAP":
            run["new_gaps"] += 1
        run["new_" + bucket.lower()] = run.get("new_" + bucket.lower(), 0) + 1
        key = normalize(rec.get("text", "")) or "(empty)"
        c = clusters.setdefault(key, {
            "counts": {}, "dm_counts": {}, "examples": [], "replies": [],
            "froms": [], "first_ts": ts, "last_ts": ts, "last_by_bucket": {},
            "generic_smell": False})
        c["counts"][bucket] = c["counts"].get(bucket, 0) + 1
        if dm:
            c["dm_counts"][bucket] = c["dm_counts"].get(bucket, 0) + 1
        c["last_ts"] = ts
        c["last_by_bucket"][bucket] = ts
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
        return (total(c) + dm_total(c), total(c), c["last_ts"])
    return sorted(clusters.items(), key=score, reverse=True)


def load_triage():
    return load_json(TRIAGE, {})


def save_triage(tr):
    json.dump(tr, open(TRIAGE, "w"), ensure_ascii=False, indent=2, sort_keys=True)


def verdict(tr, key):
    """Triage record for a cluster, or None. Never invents one: an ask with no verdict is the
    actionable state, and defaulting it to anything at all would empty the only queue a human
    is asked to work."""
    v = tr.get(key)
    return v if isinstance(v, dict) else None


def recurred(c, v):
    """True when the ask reached the MODEL again after a doer armed for it.

    Keyed on the GAP bucket alone, deliberately. Once a doer arms, that ask stops clustering as
    a GAP, so a GAP timestamp after the arm date is the doer failing to cover a phrasing it was
    built for -- the one thing a coverage metric has to be able to see. A CLARIFY after arming
    is a half-built doer, which is reported separately and is not this alarm."""
    if not v or not v.get("armed"):
        return False
    return (c.get("last_by_bucket", {}).get("GAP", "") or "") > v["armed"]


def partial(c, v):
    if not v or not v.get("armed"):
        return False
    return (c.get("last_by_bucket", {}).get("CLARIFY", "") or "") > v["armed"]


def _cluster_lines(i, key, c, v, lines):
    bits = []
    for b in CLUSTERED:
        if total(c, b):
            bits.append(f"{b.lower()} {total(c, b)}×")
    dm = dm_total(c)
    flag = " ⚠️ **answered as generic Claude**" if c["generic_smell"] else ""
    if recurred(c, v):
        flag += " 🔴 **RECURRED after arming**"
    elif partial(c, v):
        flag += " 🟡 **still asking for input after arming**"
    lines.append(f"### {i}. `{key}`{flag}")
    meta = ", ".join(bits) + (f" · {dm} over DM" if dm else "")
    lines.append(f"{meta} · {len(c['froms'])} node(s) · last {c['last_ts']}")
    if v:
        src = f" — {v['source']}" if v.get("source") else ""
        lines.append("")
        lines.append(f"**oracle: {v.get('oracle','?')}**{src}"
                     + (f" · armed {v['armed']}" if v.get("armed") else "")
                     + (f" · found by {v['found_by']}" if v.get("found_by") else ""))
        if v.get("note"):
            lines.append(f"> {v['note']}")
        for corr in v.get("corrections", []):
            lines.append(f"- ❗ corrected {corr.get('ts','')}: {corr.get('what','')}")
    lines.append("")
    for ex in c["examples"]:
        lines.append(f"- asked: \"{ex}\"")
    for rep in c["replies"]:
        lines.append(f"- Cal said: \"{rep}\"")
    lines.append("")


def render_md(agg, state, tr):
    t = agg["totals"]
    ranked = rank(agg["clusters"])
    lines = ["# cal-mesh gap ledger", ""]
    lines.append(f"_Generated {datetime.now(timezone.utc).isoformat()} · "
                 f"run #{state.get('runs', 0)} · watermark `{state.get('last_ts','')}`_")
    lines.append("")
    lines.append("Propose-only. Every cluster below is a candidate capability, not a change. "
                 "Each still walks the gate (off → eval → review → arm) before it answers. "
                 "The loop's question to a human is **what is the oracle**, never *shall I "
                 "build it* — a doer graded only by its own eval is a model answer with a "
                 "green test suite laundering it.")
    lines.append("")

    untriaged = [(k, c) for k, c in ranked if not verdict(tr, k)]
    ready = [(k, c) for k, c in ranked
             if verdict(tr, k) and verdict(tr, k).get("oracle") in ("derivable", "needs-source")
             and not verdict(tr, k).get("armed")]
    armed = [(k, c) for k, c in ranked if verdict(tr, k) and verdict(tr, k).get("armed")]
    closed = [(k, c) for k, c in ranked
              if verdict(tr, k) and verdict(tr, k).get("oracle") == "none"
              and not verdict(tr, k).get("armed")]
    recur = [k for k, c in armed if recurred(c, verdict(tr, k))]
    corrections = sum(len(v.get("corrections", [])) for v in tr.values() if isinstance(v, dict))
    by_loop = sum(1 for v in tr.values() if isinstance(v, dict) and v.get("found_by") == "loop")
    by_hand = sum(1 for v in tr.values() if isinstance(v, dict) and v.get("found_by") == "manual")

    lines.append("## Scoreboard")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---:|")
    lines.append(f"| needs an oracle verdict | **{len(untriaged)}** |")
    lines.append(f"| triaged, ready to build | {len(ready)} |")
    lines.append(f"| armed | {len(armed)} |")
    lines.append(f"| closed (no oracle exists) | {len(closed)} |")
    lines.append(f"| **recurred after arming** | **{len(recur)}** |")
    lines.append(f"| **corrections after arming** | **{corrections}** |")
    lines.append(f"| surfaced by loop / by hand | {by_loop} / {by_hand} |")
    lines.append("")
    lines.append("_Coverage is recurrence, not doers built. Corrections runs beside it on "
                 "purpose: a loop scored only on what it builds will build. `found_by` is the "
                 "baseline race — whether this loop beats reading the log once a week is an "
                 "open question, and it cannot be answered without keeping score._")
    lines.append("")

    lines.append("## Buckets (cumulative)")
    lines.append("")
    lines.append("| bucket | total | dm | broadcast |")
    lines.append("|---|---:|---:|---:|")
    for b in ("HIT", "GAP", "CLARIFY", "NO_TABLE", "REFUSED", "GREETING", "FILTERED", "OTHER"):
        if t.get(b):
            lines.append(f"| {b} | {t.get(b,0)} | {t.get(b+'_dm',0)} | {t.get(b+'_bc',0)} |")
    lines.append("")

    for title, group, blurb in (
        ("Needs an oracle", untriaged,
         "The actionable queue. Decide what the right answer is measured against, then "
         "`learn.py --triage`."),
        ("Ready to build", ready, "Oracle decided, not yet armed."),
        ("Armed", armed, "A red flag here means the doer is not covering what it was built for."),
        ("Closed — no oracle exists", closed,
         "Permanent refusals. Listed so they stop being re-proposed, not to be worked."),
    ):
        lines.append(f"## {title} — {len(group)}")
        lines.append("")
        lines.append(f"_{blurb}_")
        lines.append("")
        if not group:
            lines.append("_(none)_")
            lines.append("")
        for i, (key, c) in enumerate(group, 1):
            _cluster_lines(i, key, c, verdict(tr, key), lines)

    # Every correction, including ones on doers that never appeared as a cluster. A doer fixed
    # after arming is the counter-metric; if it only ever showed as a number in the scoreboard
    # it would be the easiest thing in this file to stop looking at.
    lines.append("## Corrections after arming")
    lines.append("")
    lines.append("_The counter-metric, itemised. A doer that had to be fixed is not a failure "
                 "of the loop — hiding it would be._")
    lines.append("")
    any_corr = False
    for k in sorted(tr):
        v = tr[k]
        if not isinstance(v, dict):
            continue
        for corr in v.get("corrections", []):
            any_corr = True
            lines.append(f"- `{k}` — {corr.get('ts','')}: {corr.get('what','')}")
    if not any_corr:
        lines.append("_(none)_")
    lines.append("")

    md = "\n".join(lines) + "\n"
    open(LEDGER_MD, "w").write(md)
    return md


def cmd_triage(args):
    tr = load_triage()
    v = tr.setdefault(args.triage, {})
    if args.oracle:
        v["oracle"] = args.oracle
    if args.source:
        v["source"] = args.source
    if args.note:
        v["note"] = args.note
    if args.found_by:
        v["found_by"] = args.found_by
    if args.arm:
        v["armed"] = args.arm if args.arm != "now" else datetime.now(timezone.utc).isoformat()
    if args.correct:
        v.setdefault("corrections", []).append(
            {"ts": datetime.now(timezone.utc).isoformat(), "what": args.correct})
    save_triage(tr)
    print(f"triage[{args.triage}] = {json.dumps(v, ensure_ascii=False)}")


def main():
    ap = argparse.ArgumentParser(description="Distill decisions.jsonl into a gap ledger.")
    ap.add_argument("--reset", action="store_true",
                    help="ignore the watermark and rebuild the aggregate from scratch")
    ap.add_argument("--quiet", action="store_true", help="write files, print only the summary line")
    ap.add_argument("--triage", metavar="KEY", help="record an oracle verdict for a cluster key")
    ap.add_argument("--oracle", choices=("derivable", "needs-source", "none"))
    ap.add_argument("--source", help="the ground truth this will be measured against")
    ap.add_argument("--note")
    ap.add_argument("--found-by", dest="found_by", choices=("loop", "manual"))
    ap.add_argument("--arm", metavar="ISO|now", help="mark a triaged cluster as armed")
    ap.add_argument("--correct", metavar="WHAT",
                    help="record a post-arming correction (the counter-metric)")
    args = ap.parse_args()

    if args.triage:
        cmd_triage(args)
        return

    agg, run, state = fold(reset=args.reset)
    tr = load_triage()
    render_md(agg, state, tr)

    t = agg["totals"]
    ranked = rank(agg["clusters"])
    untriaged = [k for k, c in ranked if not verdict(tr, k)]
    recur = [k for k, c in ranked if recurred(c, verdict(tr, k))]
    print(f"processed {run['processed']} new record(s) · {run['new_gaps']} new gap(s) · "
          f"{len(ranked)} distinct cluster(s) · {len(untriaged)} awaiting an oracle · "
          f"{len(recur)} recurred · ledger -> {LEDGER_MD}")
    if run.get("migrated"):
        print(f"migrated {run['migrated']} cluster(s) to schema {SCHEMA}")
    if args.quiet:
        return
    print(f"buckets this run: {run['buckets']}")
    print(f"cumulative: HIT={t.get('HIT',0)} GAP={t.get('GAP',0)} "
          f"CLARIFY={t.get('CLARIFY',0)} NO_TABLE={t.get('NO_TABLE',0)} "
          f"REFUSED={t.get('REFUSED',0)} GREETING={t.get('GREETING',0)} "
          f"FILTERED={t.get('FILTERED',0)} OTHER={t.get('OTHER',0)}")
    print()
    for i, key in enumerate(untriaged[:10], 1):
        c = agg["clusters"][key]
        print(f"{i:>2}. {total(c)}×  [needs oracle]  {key}")


if __name__ == "__main__":
    main()
