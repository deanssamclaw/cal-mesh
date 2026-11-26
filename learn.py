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

    FILTERED   matched=false, and dropped for a reason that is not about us: an off-list
               sender, or a message never addressed to Cal. Not a gap.
    THROTTLED  matched=false because OUR OWN limit refused it -- rate window or cooldown. The
               sender was allowed and the message WAS addressed to Cal; both of those gates
               passed before this one. So it is a question Cal was asked and chose not to
               answer, which is demand that exceeded capacity and the single record most worth
               having. It bucketed as FILTERED until 2026-08-22 -- the same bin as 44 strangers'
               chatter -- so the loop was structurally blind to being overloaded.
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

    OTHER is the tripwire, and it now has a second job: a reply the responder stamped `fixed_*`
    whose doer this classifier does not know about. That is a doer armed without this file being
    told, and it must be loud rather than counted as a gap.
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

STREAMS. Three of them, and they are not one population. A DM to Cal (to == our node id) is
authenticated and high-intent. Cal's OWN CHANNEL is a keyed two-node link — arriving there means
the sender holds a PSK we chose — so it carries the same weight as a DM for the same reason. The
public channel is everyone else.

Splitting on `to` alone cannot see this: a message on Cal's channel is addressed ^all exactly
like one on the public channel, so from the moment that channel was armed the ledger ranked a
question asked in the working channel as though a stranger had shouted it in public. The record
carries `channel` now, and CAL_CHANNEL says which index is ours. Unset means two streams, which
is what every record written before 2026-08-22 has.

PRIVACY. Output carries inbound text and Cal's replies, including DM content. This repo is
public; `gap-ledger.json`, `gap-ledger.md` and `learn-state.json` are gitignored alongside
`decisions.jsonl` for the same reason. `learn.py` itself is source and is tracked.
"""
import os, sys, re, json, argparse
from datetime import datetime, timedelta, timezone

BASE       = os.path.expanduser("~/cal-mesh")
DECISIONS  = os.path.join(BASE, "decisions.jsonl")
STATUS     = os.path.join(BASE, "status.json")
STATE      = os.path.join(BASE, "learn-state.json")     # watermark + run counter
TRIAGE     = os.path.join(BASE, "triage.json")          # oracle verdicts, arm dates, corrections
HISTORY    = os.path.join(BASE, "learn-history.jsonl")  # one line per run — the loop's own motion
LEDGER_JSON = os.path.join(BASE, "gap-ledger.json")     # accumulated aggregate
LEDGER_MD  = os.path.join(BASE, "gap-ledger.md")        # rendered human view

OUR_ID_FALLBACK = "!xxxxxxxx"   # Cal HT (real id lives in gitignored status.json; read via our_id())
# Every doer whose answer counts as ANSWERED. sigreport was missing from this set, so the
# signal readback's own correct replies were filed as gaps -- inflating the denominator of the
# one metric this whole file exists to report. A hand-kept list drifts on every arming, so the
# fall-through below turns the next omission into a tripwire instead of a silent gap.
DOER_CAPS = {"weather", "calc", "sunmoon", "nav", "navigation", "sigreport", "capabilities"}

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


def cluster_key(text):
    """normalize(), or a key that still DISTINGUISHES when normalize strips everything away.

    normalize() drops punctuation and symbols so that "Cal, what do you know?" and "what do you
    know" are one gap. An emoji-only message has nothing left afterwards, and every one of them
    was landing on the constant "(empty)". Measured 2026-09-12: 34 of 343 drafts are emoji-only
    and 9 graded rows shared that single nameless bucket.

    Two things were wrong with that. Triaging "(empty)" would mark every emoji ask handled at
    once, and a genuinely distinct emoji gap afterwards would be invisible because its key was
    already triaged. Fixed at the CALL SITES rather than inside normalize(), which is documented
    as mirroring capabilities._normalize and has to keep doing so.

    "(empty)" is kept for text that really is empty, so the two cases stay tellable apart.
    """
    k = normalize(text)
    if k:
        return k
    raw = " ".join((text or "").split())
    return "(symbols %s)" % raw[:24] if raw else "(empty)"


def classify(rec, our):
    """One bucket per record, by priority. Returns a bucket name from the docstring set."""
    if not rec.get("matched"):
        # Read the REASON, not just the flag. sender_allowed and addressed both sit above the
        # rate gate in the ladder, so these two reasons can only be reached by a legitimate
        # question from an allowed node.
        if rec.get("reason") in ("rate_limited", "cooldown"):
            return "THROTTLED"
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
    # A `fixed_*` status is the RESPONDER'S OWN statement that a doer wrote this reply and no
    # model ran. Reaching here means it is a doer this classifier cannot place -- which is what
    # happened to sigreport: 4 correct readbacks filed as gaps, in the ledger's own words
    # ("Cal said: Copy 16: SNR 6.0, RSSI -35, direct") while counted as reaching the model.
    # Do not guess it into HIT, which would hide a doer that matched and could not answer, and
    # do not leave it a GAP. OTHER is already documented as the tripwire; this is what trips it.
    if gen.startswith("fixed_"):
        return "OTHER"
    # Matched, but no doer / refusal / greeting claimed it: it reached the model. That is a
    # gap whether the record stamps prompt_kind='general' (current schema) or nothing at all
    # (records before the field existed).
    if rec.get("matched"):
        return "GAP"
    return "OTHER"


def cal_channel():
    """Cal's own channel index from the live config, or -1. Read here rather than passed in so a
    ledger rebuilt later classifies old records the same way the live responder did."""
    try:
        for ln in open(os.path.join(BASE, "config")):
            k, _, v = ln.partition("=")
            if k.strip() == "CAL_CHANNEL":
                n = int(v.strip())
                return n if 0 <= n <= 7 else -1
    except Exception:
        pass
    return -1


def stream(rec, our, own_ch):
    """Which of the three a record belongs to: 'dm', 'cal' or 'pub'.

    A DM outranks the channel it arrived on -- it is addressed to us by node, which is the
    stronger statement. `own_ch < 0` means the private channel is disarmed, and then everything
    that is not a DM is public, exactly as it was before."""
    if is_dm(rec, our):
        return "dm"
    if own_ch < 0:
        return "pub"        # no private channel exists, so every broadcast is the public one
    ch = rec.get("channel")
    if ch is None:
        # Written before the responder carried `channel`. We know it was a broadcast and we do
        # NOT know which one. Calling it public would be a guess dressed as a measurement --
        # the same absent-reads-as-a-known-value trap this codebase has been bitten by three
        # times. It gets its own column and drains to nothing as new records arrive.
        return "pre"
    return "cal" if ch == own_ch else "pub"


def is_dm(rec, our):
    return rec.get("to") == our


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def iter_decisions():
    """Yield parsed records in file order. A malformed line -- torn, not an object, bad
    UTF-8 -- is skipped, not fatal."""
    if not os.path.exists(DECISIONS):
        return
    with open(DECISIONS, encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if isinstance(r, dict):
                yield r


# THROTTLED clusters with the rest: a question refused for capacity is still an ask, and it is
# the one bucket that says the limits themselves need looking at rather than a new capability.
# Every bucket classify() can return. Named once: the audit sums records over these and
# would double-count if it also summed the per-stream keys built from them.
BUCKETS = ("HIT", "GAP", "CLARIFY", "NO_TABLE", "THROTTLED", "REFUSED", "GREETING",
           "FILTERED", "OTHER")

CLUSTERED = ("GAP", "CLARIFY", "NO_TABLE", "THROTTLED")
SCHEMA = 3


def total(c, bucket=None):
    """Cluster count, overall or for one bucket. Tolerates the v1 shape so a ledger written
    before the CLARIFY/NO_TABLE split still reads."""
    if "counts" not in c:
        return c.get("count", 0) if bucket in (None, "GAP") else 0
    if bucket:
        return c["counts"].get(bucket, 0)
    return sum(c["counts"].values())


def intent_total(c):
    """Times this ask arrived on a channel only a key-holder can reach — a DM, or Cal's own
    channel. Both mean the same thing for ranking: somebody who holds a key we issued asked
    this, which is a different signal from a stranger shouting it on the public channel."""
    st = c.get("streams") or {}
    if st:
        return st.get("dm", 0) + st.get("cal", 0)
    return dm_total(c)      # pre-split ledger: DM was the only high-intent stream


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
    # Counted as DISTINCT CLUSTERS TOUCHED, not as edits applied: one cluster needing both the
    # v2 and the v3 rewrite is one migrated cluster, and counting the steps made the run line
    # over-report the moment a second migration existed.
    touched = set()
    for key, c in agg.get("clusters", {}).items():
        if "counts" in c:
            continue
        c["counts"] = {"GAP": c.pop("count", 0)}
        c["dm_counts"] = {"GAP": c.pop("dm_count", 0)}
        c["last_by_bucket"] = {"GAP": c.get("last_ts", "")}
        touched.add(key)

    # v3: last_seen. Backfilled by SCANNING the log, not by rebuilding the aggregate --
    # `--reset` would drop every cluster older than the 5000-line rotation, which is the
    # same trap the note above describes. A scan only adds a field and can lose nothing.
    if any("last_seen" not in c for c in agg.get("clusters", {}).values()):
        newest = {}
        for rec in iter_decisions():
            k = cluster_key(rec.get("text", ""))
            ts = _s(rec.get("ts"))
            if ts > newest.get(k, ""):
                newest[k] = ts
        for key, c in agg.get("clusters", {}).items():
            # last_ts is the floor: the ask was certainly seen when it last went unanswered,
            # even if the record proving it has since rotated out of the log.
            c["last_seen"] = max(c.get("last_ts", ""), newest.get(key, ""))
            touched.add(key)

    agg["schema"] = SCHEMA
    return agg, len(touched)


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
    own_ch = cal_channel()
    watermark = state.get("last_ts", "")
    new_watermark = watermark

    run = {"processed": 0, "new_gaps": 0, "buckets": {}, "migrated": migrated}
    for rec in iter_decisions():
        ts = _s(rec.get("ts"))
        if watermark and ts <= watermark:
            continue          # already folded on a prior run
        if ts > new_watermark:
            new_watermark = ts
        bucket = classify(rec, our)
        strm = stream(rec, our, own_ch)
        dm = strm == "dm"
        run["processed"] += 1
        run["buckets"][bucket] = run["buckets"].get(bucket, 0) + 1
        # cumulative totals, split by DM/broadcast
        totals[bucket] = totals.get(bucket, 0) + 1
        # ONE key per record. Writing the legacy _dm/_bc pair alongside the three-way keys
        # double-counted every DM, because "dm" is the name in both schemes -- GAP read 27 total
        # against 24 dm and 15 public. The legacy readers below tolerate the key being absent.
        totals[bucket + "_" + strm] = totals.get(bucket + "_" + strm, 0) + 1

        # Three buckets cluster, not one. A GAP that becomes a CLARIFY is progress and has to be
        # visible as such; a NO_TABLE is a request for a capability by name. Counting only GAPs
        # means the loop goes blind at exactly the moment a doer starts half-working.
        key = cluster_key(rec.get("text", ""))
        # LAST_SEEN vs LAST_TS, and they are not the same question.
        #
        # last_ts moves only when an ask goes UNANSWERED, because only those buckets cluster.
        # That is right for ranking a build queue and wrong for every other reading: an ask a
        # doer now answers correctly stops updating, so the page dated `test` to 2026-08-22
        # while sigreport had answered it four times on 09-04. The entries that looked most
        # neglected were the ones working best -- exactly backwards for a reader deciding
        # where to look.
        #
        # So a HIT touches last_seen and nothing else. Only the clustered buckets below may
        # CREATE a cluster; an answered ask must never mint one, or the queue fills with work
        # that is already done.
        existing = clusters.get(key)
        if existing is not None:
            if ts > existing.get("last_seen", ""):
                existing["last_seen"] = ts
        if bucket not in CLUSTERED:
            continue
        if bucket == "GAP":
            run["new_gaps"] += 1
        run["new_" + bucket.lower()] = run.get("new_" + bucket.lower(), 0) + 1
        c = clusters.setdefault(key, {
            "counts": {}, "dm_counts": {}, "streams": {}, "examples": [], "replies": [],
            "froms": [], "first_ts": ts, "last_ts": ts, "last_seen": ts,
            "last_by_bucket": {},
            "generic_smell": False})
        c.setdefault("streams", {})
        c["streams"][strm] = c["streams"].get(strm, 0) + 1
        c["counts"][bucket] = c["counts"].get(bucket, 0) + 1
        if dm:
            c["dm_counts"][bucket] = c["dm_counts"].get(bucket, 0) + 1
        c["last_ts"] = ts
        if ts > c.get("last_seen", ""):
            c["last_seen"] = ts
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
        # High-intent asks count double. That used to mean DM only; Cal's own channel earns the
        # same weight on the same argument, and NOT giving it to that channel ranked a question
        # asked in the working channel as though a passer-by had shouted it.
        return (total(c) + intent_total(c), total(c), c["last_ts"])
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
    strm = c.get("streams") or {}
    if strm:
        bits.append("via " + ", ".join(f"{k} {v}" for k, v in sorted(strm.items())))
    dm = dm_total(c)
    flag = " ⚠️ **answered as generic Claude**" if c["generic_smell"] else ""
    if recurred(c, v):
        flag += " 🔴 **RECURRED after arming**"
    elif partial(c, v):
        flag += " 🟡 **still asking for input after arming**"
    lines.append(f"### {i}. `{key}`{flag}")
    meta = ", ".join(bits) + (f" · {dm} over DM" if dm else "")
    seen = c.get("last_seen") or c["last_ts"]
    asked = f"last asked {seen[:10]}"
    # Only worth showing both when they disagree; when they agree the second is noise.
    unans = "" if seen[:10] == c["last_ts"][:10] else f" · last unanswered {c['last_ts'][:10]}"
    lines.append(f"{meta} · {len(c['froms'])} node(s) · {asked}{unans}")
    if v:
        src = f" — {v['source']}" if v.get("source") else ""
        sha = ""
        if v.get("commit"):
            sha = f" · commit `{v['commit']}`" + (" pushed" if v.get("pushed") else " LOCAL ONLY")
        lines.append("")
        lines.append(f"**oracle: {v.get('oracle','?')}**{src}"
                     + (f" · armed {v['armed']}" if v.get("armed") else "") + sha
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
    try:
        h = health()
    except Exception as e:      # the ledger still renders; the header says why it cannot judge
        h = {"state": "UNKNOWN", "reason": f"health check raised {type(e).__name__}",
             "run_age_h": None, "input_age_h": None, "stale": None, "audited": 0, "runs": 0}
    lines.append(f"**Loop health: {h['state']}** — {h['reason']}  ")
    lines.append(f"_last run {_fmt_age(h['run_age_h'])} ago · last inbound "
                 f"{_fmt_age(h['input_age_h'])} ago · "
                 f"bank check {h['stale'] if h['stale'] is not None else '?'} stale of "
                 f"{h['audited']} · {h['runs']} runs_")
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
    lines.append("| bucket | total | dm | cal ch | public | unsplit |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for b in ("HIT", "GAP", "CLARIFY", "NO_TABLE", "THROTTLED", "REFUSED", "GREETING",
              "FILTERED", "OTHER"):
        if t.get(b):
            # Records written before the split have no _cal/_pub key. They show under public
            # with a marker rather than being silently redistributed into a stream nobody
            # observed them on.
            lines.append(f"| {b} | {t.get(b,0)} | {t.get(b+'_dm',0)} | {t.get(b+'_cal',0)} "
                         f"| {t.get(b+'_pub',0)} | {t.get(b+'_pre',0)} |")
    lines.append("")
    lines.append("_unsplit: broadcasts logged before the responder recorded which channel "
                 "they arrived on. Known not to be DMs, not known to be public._")
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

    try:
        o = overheard()
    except Exception as e:
        o = {"days": OVERHEARD_DAYS, "heard": "?", "questions": "?", "unthreaded": "?",
             "doer_could": "?", "drafts_failed": "?", "ungraded": "?", "last_grade": None,
             "top": [], "error": type(e).__name__}
    lines.append(f"## Overheard — last {o['days']} days")
    if o.get("error"):
        lines.append("")
        lines.append(f"_Could not read the drafts ({o['error']})._")
    lines.append("")
    lines.append("_Public traffic Cal stayed silent on, read from the drafts. Propose-only: this "
                 "is what the mesh asks, not a build order._")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---:|")
    lines.append(f"| messages Cal stayed silent on | {o['heard']} |")
    lines.append(f"| question-shaped | {o['questions']} |")
    lines.append(f"| … with no threaded reply from anyone | **{o['unthreaded']}** |")
    lines.append(f"| a doer had an answer | {o['doer_could']} |")
    lines.append(f"| model drafts that FAILED | {o['drafts_failed']} |")
    lines.append(f"| model drafts awaiting a grade | {o['ungraded']} |")
    lines.append(f"| last grade given | {(o['last_grade'] or 'never')[:10]} |")
    lines.append("")
    for i, q in enumerate(o["top"], 1):
        lines.append(f"{i}. \"{q['text']}\" — {q['n']}×, {q['unthreaded']} unthreaded, "
                     f"last {q['last'][:10]}")
    if not o["top"]:
        lines.append("_(no unthreaded questions)_")
    lines.append("")

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


GRADES = os.path.join(BASE, "draft-grades.json")   # human verdicts on simulated replies
DRAFTS = os.path.join(BASE, "drafts.jsonl")


def grade_queue(tr=None):
    """Human verdicts on simulated replies that still want work, as cluster keys.

    THE JOIN HAPPENS HERE AND NOWHERE ELSE. A grade is an OPINION about a draft; the ledger is a
    MEASUREMENT of what actually happened on the radio. They live in separate files on purpose
    and are joined at read time, so a verdict can never rewrite the measurement it was given
    about -- which is what would happen if grading appended to gap-ledger.json.

    Until now nothing consumed grades at all. drafts.py could record one, the page could render
    one, export_judgment.py could project one, and no code anywhere turned a verdict into work.
    That is the difference between a log and a loop, and it is the reason this exists.

    "doer" is the verdict with leverage: it says an ask got prose where a deterministic answer
    would have served better, which is exactly the question triage.json already adjudicates. So
    a "doer" grade lands in the same queue a distilled GAP lands in, and is worked the same way.
    "wrong" and "harmful" ride along carrying `better` -- the reply the grader typed instead --
    because a correction is the only feedback that says what SHOULD have been said rather than
    that something was off.

    An item drops off the queue when its cluster key is TRIAGED, not when it is graded again:
    the queue measures outstanding work, and a second opinion on the same ask is not progress.
    """
    tr = load_triage() if tr is None else tr
    try:
        grades = json.load(open(GRADES, encoding="utf-8"))
        if not isinstance(grades, dict):
            return []
    except (OSError, json.JSONDecodeError):
        return []
    # draft_id -> the message that was drafted for. Read fresh; drafts.jsonl is rewritten daily.
    texts = {}
    try:
        for ln in open(DRAFTS, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("draft_id"):
                texts[r["draft_id"]] = r
    except OSError:
        pass

    out = []
    for did, g in sorted(grades.items()):
        if not isinstance(g, dict):
            continue
        v = g.get("verdict")
        if v not in ("doer", "wrong", "harmful"):
            continue                      # "good" is not work
        row = texts.get(did) or {}
        key = cluster_key(row.get("text", ""))
        out.append({
            "key": key,
            "verdict": v,
            "better": g.get("better", ""),
            "note": g.get("note", ""),
            "by": g.get("by", ""),
            "day": (g.get("ts") or "")[:10],
            "draft_id": did,
            # What Cal said, so the queue shows the thing being objected to. Already published
            # on the Simulated Replies tab; this is the same sentence, not a new disclosure.
            "draft": row.get("draft", ""),
            "via": row.get("via", ""),
            "triaged": bool(verdict(tr, key)),
        })
    return out


INBOX = os.path.join(BASE, "inbox.jsonl")
OVERHEARD_DAYS = 14

# Question-shaped: a question mark anywhere, or an interrogative opener. Shape only -- it
# chooses which overheard messages are LISTED, and it never decides anything Cal says.
_QUESTION = re.compile(
    r"\?|^\W*(?:who|whos|what|whats|when|where|wheres|why|how|hows|is|are|can|could|does|do|"
    r"did|will|would|anyone|anybody|any)\b", re.I)


def _packet_id(draft_id):
    """drafts.py writes draft_id as '<ts>:<packet id>'."""
    try:
        return int(str(draft_id).rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def overheard(now=None, days=OVERHEARD_DAYS, top=10):
    """What the mesh said that Cal did not answer, from drafts.jsonl joined to inbox.jsonl.

    THE GAP LEDGER ONLY SEES ASKS ADDRESSED TO CAL. From 2026-09-25 to 2026-11-25 that was one
    message in 706, while the public channel carried newcomers, radio questions and
    conversations the loop never read. drafts.py already drafts every one of them; this reads
    those drafts for three things that need no human:

      * questions  -- public, question-shaped, and Cal stayed silent
      * unthreaded -- of those, how many no other node answered with a threaded reply
                      (reply_to = that packet, not a tapback). A lower bound on "unanswered":
                      plenty of people answer without threading.
      * doer_could -- Cal stayed silent, but a deterministic doer had an answer

    plus the human side: model drafts waiting for a grade, and when the last grade came in.

    Public broadcasts only (to ^all, not Cal's own channel). Every text listed here is already
    published on the Simulated Replies tab. Propose-only: nothing here changes what Cal says.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    own_ch = cal_channel()
    pk = {}
    answered = set()
    for p in _iter_jsonl(INBOX):
        if _s(p.get("ts")) < cutoff:
            continue
        if _key(p.get("id")) is not None:
            pk[p["id"]] = p
        rt = _key(p.get("reply_to"))
        if rt and p.get("reaction") in (None, False):
            answered.add(rt)
    try:
        grades = json.load(open(GRADES, encoding="utf-8"))
        grades = grades if isinstance(grades, dict) else {}
    except (OSError, json.JSONDecodeError):
        grades = {}
    last_grade = max((_s(g.get("ts")) for g in grades.values() if isinstance(g, dict)),
                     default="") or None

    heard = questions = unthreaded = doer_could = failed = ungraded = 0
    clusters = {}
    for r in _iter_jsonl(DRAFTS):
        ts = _s(r.get("ts"))
        if ts < cutoff:
            continue
        p = pk.get(_packet_id(r.get("draft_id")))
        if not p or p.get("to") != "^all" or (own_ch >= 0 and p.get("channel") == own_ch):
            continue
        if r.get("sent_reply"):
            continue                      # Cal answered it; that is the ledger's business
        heard += 1
        via = str(r.get("via") or "")
        g = r.get("gen_status")
        if via.startswith("model") and isinstance(g, str) and g.startswith("gen_"):
            failed += 1
        elif via.startswith("model") and g == "ok" and _s(r.get("draft_id")) not in grades:
            ungraded += 1
        if r.get("doers"):
            doer_could += 1
        text = _s(r.get("text")).strip()
        if not _QUESTION.search(text):
            continue
        questions += 1
        thread = p.get("id") in answered
        if not thread:
            unthreaded += 1
        c = clusters.setdefault(cluster_key(text), {"text": text, "n": 0, "last": ts,
                                                   "unthreaded": 0})
        c["n"] += 1
        c["unthreaded"] += 0 if thread else 1
        if ts > c["last"]:
            c["last"], c["text"] = ts, text
    ranked = sorted(clusters.items(),
                    key=lambda kv: (kv[1]["unthreaded"], kv[1]["n"], kv[1]["last"]), reverse=True)
    return {"days": days, "heard": heard, "questions": questions, "unthreaded": unthreaded,
            "doer_could": doer_could, "drafts_failed": failed, "ungraded": ungraded,
            "last_grade": last_grade,
            "top": [{"key": k, **v} for k, v in ranked if v["unthreaded"]][:top]}


def snapshot(agg, tr):
    """The scoreboard as numbers, for one run.

    Written every run so the loop's own motion is visible as a series rather than as a table
    that always looks the same. A ledger that is only ever read as "now" cannot answer the
    question this exists to answer -- is it doing anything -- and neither can one nobody opens.
    """
    ranked = rank(agg["clusters"])
    armed = [(k, c) for k, c in ranked if (verdict(tr, k) or {}).get("armed")]
    return {
        "clusters": len(ranked),
        "untriaged": sum(1 for k, _ in ranked if not verdict(tr, k)),
        "ready": sum(1 for k, _ in ranked
                     if (verdict(tr, k) or {}).get("oracle") in ("derivable", "needs-source")
                     and not (verdict(tr, k) or {}).get("armed")),
        "armed": len(armed),
        "closed": sum(1 for k, _ in ranked
                      if (verdict(tr, k) or {}).get("oracle") == "none"
                      and not (verdict(tr, k) or {}).get("armed")),
        "recurred": sum(1 for k, c in armed if recurred(c, verdict(tr, k))),
        "partial": sum(1 for k, c in armed if partial(c, verdict(tr, k))),
        "corrections": sum(len(v.get("corrections", []))
                           for v in tr.values() if isinstance(v, dict)),
        "by_loop": sum(1 for v in tr.values()
                       if isinstance(v, dict) and v.get("found_by") == "loop"),
        "by_hand": sum(1 for v in tr.values()
                       if isinstance(v, dict) and v.get("found_by") == "manual"),
        # HUMAN VERDICTS STILL OPEN. Counted separately from `untriaged`, which the distiller
        # produces on its own: a person taking the trouble to say "this was wrong" is a
        # different and scarcer signal than a machine noticing an ask went unanswered, and
        # folding the two into one number would hide it inside a bigger one.
        "graded_open": sum(1 for g in grade_queue(tr) if not g["triaged"]),
        "graded_total": len(grade_queue(tr)),
        **_overheard_counts(),
    }


def _overheard_counts():
    """overheard() as flat numbers for the history line. An unreadable drafts file is zeros
    plus `heard_ok: False`, never a missing key a plot would read as zero anyway."""
    try:
        o = overheard()
        ok = True
    except Exception:
        o, ok = {}, False
    return {"heard_ok": ok, "heard_q": o.get("questions", 0),
            "heard_unthreaded": o.get("unthreaded", 0), "heard_doer": o.get("doer_could", 0),
            "drafts_failed": o.get("drafts_failed", 0), "ungraded": o.get("ungraded", 0)}


def append_history(run, snap):
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "processed": run.get("processed", 0),
           "new_gaps": run.get("new_gaps", 0),
           "new_clarify": run.get("new_clarify", 0),
           "new_no_table": run.get("new_no_table", 0)}
    rec.update(snap)
    try:
        with open(HISTORY, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    return rec


# ---------------------------------------------------------------------------
# Health -- is this loop actually running, and is what it published still true?
# ---------------------------------------------------------------------------
# Thresholds are MEASURED, not chosen. Taken 2026-08-31 over the whole of
# decisions.jsonl (147 records, 2026-08-08 .. 2026-08-31) and all 19 recorded runs:
#
#   RUN_LATE_H = 26     Scheduled runs land 24.00 h apart, eight consecutive days, with no
#                       drift at all. 26 h is that cadence plus two hours of grace for a host
#                       that happened to be asleep at 06:15.
#   INPUT_STALL_H = 48  The largest natural silence between two inbound records is 39.3 h
#                       (median 0.47 h, p90 11.5 h). 48 h clears the observed worst case with
#                       headroom. Set below ~40 h this fires on a genuinely quiet mesh, and a
#                       health signal that cries wolf is one nobody reads.
#
# Re-derive both if the traffic pattern changes. A threshold inherited from a different
# traffic shape is a guess wearing a measurement's clothes.
RUN_LATE_H = 26
INPUT_STALL_H = 48
#   GEN_FAIL_RUN = 3    A model reply attempt is a decision with gen_status "ok" or "gen_*".
#                       Over all 40 attempts on record (2026-08-08 .. 2026-09-21): 35 ok, and
#                       5 failures in ONE six-minute burst on 2026-09-20 (gen_rc1, ~1 s each,
#                       empty stderr) -- nothing else. Three consecutive failures never happen
#                       in healthy operation, and that burst went unseen for a day because the
#                       loop was FRESH: it watches the distiller, and a responder whose model
#                       calls all fail looks exactly like a quiet mesh from here.
GEN_FAIL_RUN = 3
#   STARVE_D = 30       Days without an ASK: a record classify() files as GAP, CLARIFY,
#                       NO_TABLE, THROTTLED, REFUSED, or a HIT from any doer but sigreport.
#                       Greetings and signal tests are excluded because they teach the loop
#                       nothing. Over all 53 asks on record (2026-08-08 .. 2026-09-30) the gap
#                       between asks has median 15 min, p90 2.1 d, largest 22.7 d (09-01 ->
#                       09-23). 30 d clears that worst case. Then nobody asked for 56 days while
#                       the strip read FRESH, because input kept flowing -- all of it greetings
#                       and radio checks. STALLED watches the radio; this watches the questions.
STARVE_D = 30
#   DRAFTS_STALE_H = 72 drafts.py runs daily at 06:30; three missed days means it is not running,
#                       and its last failures no longer describe the model.
DRAFTS_STALE_H = 72

# The closed set. A verdict outside it is a bug, not a new state -- and UNKNOWN is what an
# unreadable artefact produces, never a cheerful default.
STATES = ("FRESH", "LATE", "STALLED", "FAILING", "DRIFT", "STARVED", "UNKNOWN")

ASK_BUCKETS = ("GAP", "CLARIFY", "NO_TABLE", "THROTTLED", "REFUSED")


def is_ask(rec, our):
    """True if this record is a question the loop can learn from. See STARVE_D."""
    b = classify(rec, our)
    return b in ASK_BUCKETS or (b == "HIT" and rec.get("capability") != "sigreport")


def audit(agg=None, watermark=None):
    """Re-classify what has already been folded, and compare it against what was banked.

    THE HOLE THIS CLOSES. The aggregate is cumulative and the watermark means each record is
    classified exactly once, ever. So a fix to classify() corrects every FUTURE record and
    cannot reach the ones already counted. That is not hypothetical: adding sigreport to
    DOER_CAPS fixed the classifier, and three sigreport readbacks still sat in the published
    build queue as unanswered gaps for six days afterwards -- because nothing compared the
    bank against the code. This is that comparison, and it is the reason DRIFT is a state.

    Only records AT OR BEFORE the watermark are audited. Records the loop has not folded yet
    are legitimately absent from the bank, and counting them would report drift every day in
    the window between the responder writing and the job running -- a false alarm on a
    schedule, which is exactly how a true one gets ignored.

    Returns counts only. No message text: a public page reads this.
    """
    agg = agg if agg is not None else (load_json(LEDGER_JSON, {}) or {})
    if watermark is None:
        watermark = (load_json(STATE, {}) or {}).get("last_ts", "")
    banked = dict(agg.get("totals") or {})
    if not banked or not watermark:
        return {"ok": None, "stale": None, "audited": 0, "banked": 0,
                "delta": {}, "reason": "no bank, or no watermark to audit against"}
    our, own_ch = our_id(), cal_channel()
    rebuilt, n = {}, 0
    for rec in iter_decisions():
        ts = _s(rec.get("ts"))
        if not ts or ts > watermark:
            continue
        n += 1
        b = classify(rec, our)
        # Mirror fold() EXACTLY -- one plain key and one stream key per record. Rebuilding only
        # the plain keys reported a false deficit on every `HIT_dm`-style key while `stale`
        # read 0, so the audit contradicted itself; an instrument that disagrees with its own
        # summary is worse than none.
        rebuilt[b] = rebuilt.get(b, 0) + 1
        sk = b + "_" + stream(rec, our, own_ch)
        rebuilt[sk] = rebuilt.get(sk, 0) + 1
    delta = {k: rebuilt.get(k, 0) - banked.get(k, 0)
             for k in sorted(set(banked) | set(rebuilt))
             if rebuilt.get(k, 0) != banked.get(k, 0)}
    # `stale` counts RECORDS, so it is summed over the plain buckets only. Every record also
    # lands in a stream key, and counting both families would report each moved record twice.
    # A pure reclassification moves a record between buckets, so the positive side of the
    # delta counts it once; summing absolute values would double it again.
    moved = sum(v for k, v in delta.items() if k in BUCKETS and v > 0)
    return {"ok": not delta, "stale": moved, "audited": n,
            "banked": sum(banked.get(b, 0) for b in BUCKETS), "delta": delta,
            "reason": "" if not delta else "bank disagrees with the current classifier"}


def _age_h(iso, now):
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(iso)
    except Exception:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0.0, (now - t).total_seconds() / 3600.0)


def _fmt_age(h):
    """Ages read as durations, not decimals. One formatter for the CLI and the ledger header,
    because two would drift and this number is the whole point of the strip."""
    if h is None:
        return "?"
    if h < 1:
        return f"{int(round(h * 60))} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} d"


def _gen_health(now):
    """The responder's model replies, from decisions.jsonl: trailing consecutive failures, and
    when the last success and failure were. Attempts only -- a fixed reply never ran a model."""
    trail, last_ok, last_fail, last_status, n = 0, None, None, None, 0
    for rec in iter_decisions():
        g = rec.get("gen_status")
        if not isinstance(g, str) or not (g == "ok" or g.startswith("gen_")):
            continue
        n += 1
        if g == "ok":
            trail, last_ok = 0, _s(rec.get("ts"))
        else:
            # The CODE only ("gen_rc1"), never the stderr tail gen_status carries after the colon:
            # this reason is published, and a CLI error can hold account detail.
            trail, last_fail, last_status = trail + 1, _s(rec.get("ts")), g.split(":", 1)[0][:24]
    return {"attempts": n, "recent_failed": trail, "last_ok": last_ok, "last_fail": last_fail,
            "last_status": last_status, "last_fail_age_h": _age_h(last_fail, now),
            "fail_after": GEN_FAIL_RUN}


def _iter_jsonl(path):
    """Dict rows only. A torn line, a non-object (`[]`, `3`) or bad UTF-8 is skipped: one bad
    line must not take down the health check, which is the thing that would report it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict):
                    yield r
    except OSError:
        return


def _s(v):
    """A field as a string, whatever was written there. Comparisons and .strip() then cannot
    raise on a number or a list."""
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _key(v):
    """A packet id usable as a dict key, or None."""
    return v if isinstance(v, (int, str)) and not isinstance(v, bool) else None


def _drafts_gen_health(now):
    """The drafts job's model calls, read from drafts.jsonl: the same trailing-failure count as
    _gen_health. None when the file does not exist (drafts disarmed).

    WHY A SECOND SOURCE. drafts.py calls responder.run_claude, the same function and CLI the
    live responder uses. On 2026-10-24 the CLI login on rflab expired and every model draft
    failed for a month. _gen_health saw none of it: the responder had not attempted a model
    reply since 2026-09-23, so its trailing count stayed 0. Drafts run daily and are the first
    place a broken model shows up.

    Row `ts` is the MESSAGE time, not the draft time, so the age of the newest failure is not
    reported. The file's mtime says when drafting last ran."""
    if not os.path.exists(DRAFTS):
        return None
    # A drafts file nobody has written for DRAFTS_STALE_H says nothing about the model today --
    # drafts disarmed with the file left behind would otherwise read FAILING forever.
    try:
        run_age = max(0.0, now.timestamp() - os.path.getmtime(DRAFTS)) / 3600.0
    except OSError:
        return None
    if run_age > DRAFTS_STALE_H:
        return None
    trail, last_status, n = 0, None, 0
    for r in _iter_jsonl(DRAFTS):
        if not str(r.get("via") or "").startswith("model"):
            continue
        g = r.get("gen_status")
        if not isinstance(g, str) or not (g == "ok" or g.startswith("gen_")):
            continue
        n += 1
        if g == "ok":
            trail = 0
        else:
            trail, last_status = trail + 1, g.split(":", 1)[0][:24]
    return {"attempts": n, "recent_failed": trail, "last_status": last_status,
            "run_age_h": run_age, "fail_after": GEN_FAIL_RUN}


def _ask_health(now):
    """When someone last asked Cal something the loop can learn from (see STARVE_D).

    decisions.jsonl is trimmed to its last 5000 lines, so an ask can rotate out. If none is in
    the log, the age is AT LEAST the age of the oldest record, and `in_log` says it is a
    lower bound, not a date."""
    our = our_id()
    last, oldest = None, None
    for rec in iter_decisions():
        ts = _s(rec.get("ts"))
        if not ts:
            continue
        if oldest is None or ts < oldest:
            oldest = ts
        if is_ask(rec, our) and (last is None or ts > last):
            last = ts
    age_h = _age_h(last or oldest, now)
    return {"last_ask": last, "in_log": last is not None,
            "age_d": None if age_h is None else age_h / 24.0, "starve_after_d": STARVE_D}


def _health_reason(state, run_age, in_age, a, gen=None, drafts=None, ask=None):
    """One sentence per state, and each one names ITS OWN mechanism.

    A fall-through that describes some other state's mechanism is worse than no sentence: it
    is the page asserting a cause it did not measure. There is no default branch here on
    purpose -- an unrecognised state says so.
    """
    if state == "FRESH":
        return "ran on schedule, input flowing, bank agrees with the current classifier"
    if state == "UNKNOWN":
        return ("cannot read the run history, the decision log, or the bank "
                "-- reporting unknown rather than healthy")
    if state == "LATE":
        return f"no distiller run in {run_age:.0f} h; it is scheduled every 24 h"
    if state == "STALLED":
        return f"the loop is running, but nothing has been received in {in_age:.0f} h"
    if state == "DRIFT":
        return (f"{a.get('stale') or 0} banked record(s) would classify differently under the "
                f"code running now -- rebuild with `learn.py --reset`")
    if state == "FAILING":
        g = gen or {}
        dg = drafts or {}
        if (g.get("recent_failed") or 0) < GEN_FAIL_RUN and (dg.get("recent_failed") or 0) >= GEN_FAIL_RUN:
            return (f"the loop is running, but the last {dg.get('recent_failed')} drafted replies "
                    f"all failed ({dg.get('last_status')}) -- drafts call the same model the "
                    f"responder does, so a live question would fail the same way")
        return (f"the loop is running, but the last {g.get('recent_failed')} model replies all "
                f"failed ({g.get('last_status')}, newest {_fmt_age(g.get('last_fail_age_h'))} ago) "
                f"-- 'nothing new' here may be replies that never happened")
    if state == "STARVED":
        k = ask or {}
        d = k.get("age_d")
        span = "?" if d is None else f"{d:.0f}"
        since = f"in {span} days" if k.get("in_log", True) else f"in the retained log ({span}+ days)"
        return (f"the loop is running and input is flowing, but nobody has asked Cal anything "
                f"it could learn from {since} -- greetings and signal tests do not count")
    return f"unrecognised state {state!r}"


def health(now=None):
    """Liveness from OBSERVABLE EVIDENCE, never from the loop's own say-so.

    A heartbeat written by the job being monitored cannot report that the job stopped -- it
    just keeps saying whatever it last said, and reads healthy forever. So every field here is
    derived from artefacts carrying their own timestamps (the newest run in learn-history.jsonl,
    the newest record in decisions.jsonl), and the dashboard recomputes it on each poll instead
    of reading a status file that something may have quietly stopped refreshing.

    THREE STATES THAT USED TO LOOK IDENTICAL, all of them reported as "0 new gaps":
    a healthy loop over a quiet mesh, a dead responder, and a bank that no longer matches the
    code. They are FRESH, STALLED and DRIFT here. Telling them apart is the entire point --
    one green light says the same thing in all three, which is why the six-day staleness was
    invisible while every number on the page looked right.
    """
    now = now or datetime.now(timezone.utc)

    last_run, runs = None, 0
    try:
        with open(HISTORY) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if isinstance(rec, dict) and _s(rec.get("ts")):
                    last_run, runs = _s(rec["ts"]), runs + 1
    except Exception:
        pass

    last_input = ""
    for rec in iter_decisions():
        ts = _s(rec.get("ts"))
        if ts > last_input:
            last_input = ts
    last_input = last_input or None

    a = audit()
    gen = _gen_health(now)
    dgen = _drafts_gen_health(now)
    ask = _ask_health(now)
    run_age, in_age = _age_h(last_run, now), _age_h(last_input, now)

    # Flags are collected, not short-circuited: two things can be wrong at once, and a strip
    # that can only ever name one of them hides the second until the first is fixed.
    flags = []
    if run_age is None or in_age is None or a.get("stale") is None:
        flags.append("UNKNOWN")
    if run_age is not None and run_age > RUN_LATE_H:
        flags.append("LATE")
    if in_age is not None and in_age > INPUT_STALL_H:
        flags.append("STALLED")
    if gen["recent_failed"] >= GEN_FAIL_RUN or (dgen or {}).get("recent_failed", 0) >= GEN_FAIL_RUN:
        flags.append("FAILING")
    if a.get("stale"):
        flags.append("DRIFT")
    if ask["age_d"] is not None and ask["age_d"] > STARVE_D:
        flags.append("STARVED")

    # Precedence is upstream-first: a loop that has not run makes every number under it old,
    # so naming DRIFT while the job itself is dead would point at the wrong repair. The strip
    # renders all four facts regardless, so precedence chooses the chip -- it hides nothing.
    # STARVED is last: everything is working, there is just nothing to learn from.
    state = "FRESH"
    for s in ("UNKNOWN", "LATE", "STALLED", "FAILING", "DRIFT", "STARVED"):
        if s in flags:
            state = s
            break

    next_expected = None
    if last_run:
        try:
            t = datetime.fromisoformat(last_run)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            next_expected = (t + timedelta(hours=24)).isoformat()
        except Exception:
            next_expected = None

    return {"state": state, "flags": flags, "runs": runs,
            "last_run": last_run, "run_age_h": run_age,
            "next_expected": next_expected, "late_after_h": RUN_LATE_H,
            "last_input": last_input, "input_age_h": in_age,
            "stall_after_h": INPUT_STALL_H,
            "stale": a.get("stale"), "audited": a.get("audited"),
            "delta": a.get("delta") or {},
            "gen": gen, "drafts_gen": dgen, "ask": ask,
            "reason": _health_reason(state, run_age, in_age, a, gen, dgen, ask)}


def git_head():
    """(short sha, on_origin) for the current checkout, or (None, False).

    The commit is what turns "armed" from a claim into something a reader can go and check --
    and `on_origin` separates work that is only on this Mac from work that has actually shipped,
    which is the distinction a public page has to be honest about."""
    import subprocess
    try:
        sha = subprocess.run(["git", "-C", BASE, "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if not sha:
            return None, False
        out = subprocess.run(["git", "-C", BASE, "branch", "-r", "--contains", sha],
                             capture_output=True, text=True, timeout=5).stdout
        return sha, "origin/main" in out
    except Exception:
        return None, False


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
    if args.commit:
        if args.commit == "auto":
            # HEAD AT TRIAGE TIME, WHICH IS NOT THE SAME AS THE COMMIT THAT ARMED THIS.
            # On 2026-09-05 nine clusters were triaged in one 1.6-second batch and every one
            # recorded the same sha -- a commit about neighbour position, unrelated to torque,
            # unit conversion, sigreport or the capability list. dashboard.py publishes this
            # field, and the page calls it "the commit that armed each answer": 9 of 14 were
            # wrong, presented as evidence.
            #
            # `auto` is honest only when the triage IS the arming, i.e. run right after the
            # commit that shipped it. It cannot know that, so it records HOW the sha was
            # obtained and lets the reader weigh it. Pass an explicit sha for anything armed
            # earlier; `git log --diff-filter=A -- <module>` finds it.
            sha, on_origin = git_head()
            if sha:
                v["commit"], v["pushed"] = sha, on_origin
                v["commit_source"] = "auto: HEAD at triage time, not verified as the arming commit"
        else:
            v["commit"] = args.commit
            v["pushed"] = args.pushed
            v["commit_source"] = "stated"
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
    ap.add_argument("--check", action="store_true",
                    help="print the health verdict and exit (0 fresh, 1 unhealthy, 2 unknown)")
    ap.add_argument("--audit", action="store_true",
                    help="re-classify the banked records with the current code and report drift")
    ap.add_argument("--triage", metavar="KEY", help="record an oracle verdict for a cluster key")
    ap.add_argument("--oracle", choices=("derivable", "needs-source", "none"))
    ap.add_argument("--source", help="the ground truth this will be measured against")
    ap.add_argument("--note")
    ap.add_argument("--found-by", dest="found_by", choices=("loop", "manual"))
    ap.add_argument("--arm", metavar="ISO|now", help="mark a triaged cluster as armed")
    ap.add_argument("--commit", metavar="SHA|auto",
                    help="record the commit that armed this capability ('auto' reads HEAD)")
    ap.add_argument("--pushed", action="store_true", help="with --commit SHA: mark it as pushed")
    ap.add_argument("--correct", metavar="WHAT",
                    help="record a post-arming correction (the counter-metric)")
    args = ap.parse_args()

    if args.triage:
        cmd_triage(args)
        return

    # --check and --audit READ ONLY. A health probe that mutates the thing it measures cannot
    # be run from a monitor, a cron, or twice in a row, so neither folds, renders or advances
    # the watermark.
    if args.audit:
        a = audit()
        print(f"audited {a['audited']} banked record(s) against the current classifier")
        if a["stale"] is None:
            print(f"  UNKNOWN -- {a['reason']}")
            return 2
        if not a["delta"]:
            print("  0 stale -- the bank agrees with the code")
            return 0
        print(f"  {a['stale']} stale record(s) -- {a['reason']}")
        print("  (delta is rebuilt minus banked, over plain and per-stream keys)")
        for b, d in sorted(a["delta"].items()):
            print(f"    {b:<10} {d:+d}")
        print("  rebuild with: learn.py --reset")
        return 1
    if args.check:
        h = health()
        print(f"{h['state']}: {h['reason']}")
        print(f"  last run {_fmt_age(h['run_age_h'])} ago · last inbound "
              f"{_fmt_age(h['input_age_h'])} ago · {h['runs']} run(s) · "
              f"bank check {h['stale'] if h['stale'] is not None else '?'} stale")
        return {"FRESH": 0, "UNKNOWN": 2}.get(h["state"], 1)

    agg, run, state = fold(reset=args.reset)
    tr = load_triage()
    snap = snapshot(agg, tr)
    # Banked on the run as well as computed live by the dashboard. The live read is what
    # catches a classifier edited between runs; this one gives the number a history, so a
    # drift that came and went is still visible afterwards.
    snap["stale"] = audit(agg, state.get("last_ts"))["stale"]
    append_history(run, snap)
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
          f"THROTTLED={t.get('THROTTLED',0)} "
          f"REFUSED={t.get('REFUSED',0)} GREETING={t.get('GREETING',0)} "
          f"FILTERED={t.get('FILTERED',0)} OTHER={t.get('OTHER',0)}")
    print()
    for i, key in enumerate(untriaged[:10], 1):
        c = agg["clusters"][key]
        print(f"{i:>2}. {total(c)}×  [needs oracle]  {key}")


if __name__ == "__main__":
    sys.exit(main() or 0)
