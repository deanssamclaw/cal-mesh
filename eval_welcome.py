#!/usr/bin/env python3
"""Offline eval for the proactive welcome — a node's FIRST message on the public
channel earns one public hello. No radio, no model, no network.

  POSITIVE   a genuinely new off-list node's first public message is welcomed to
             ^all, with a line from the built-in table.
  NEGATIVE   everything that must NOT be welcomed: self, DM, private channel,
             tapback (incl. the fail-open string "true"), empty body, a known
             node, an already-welcomed node, min-gap, and the daily cap.
  INVARIANT  every built-in line is 5-7 words and carries no digit; the override
             wins verbatim; the line rotates deterministically by day-count.
  SEED       seeding from the node DB makes those nodes not-new (safe to arm).
  LADDER     a new node is welcomed AHEAD of the greeting ack; the gate list
             leads with welcome_enabled.

Run: python3 eval_welcome.py [-v]
"""
import json
import os
import sys
import tempfile
import time

import responder as R

V = "-v" in sys.argv
PASS = FAIL = 0
T = 1_780_000_000.0            # a fixed clock so budgets/days are deterministic
OURS = "!c0000001"
DAY = R.datetime.fromtimestamp(T, R.timezone.utc).strftime("%Y-%m-%d")


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        if V:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s: got %r want %r" % (name, got, want))


def truthy(name, cond):
    check(name, bool(cond), True)


def cfg(**over):
    c = dict(R.DEFAULTS)
    c.update({"WELCOME_ENABLED": "true", "WELCOME_TEXT": "",
              "WELCOME_MAX_PER_DAY": "4", "WELCOME_MIN_GAP_S": "300"})
    c.update(over)
    return c


def rec(text="hey all testing from 135th", to="^all", frm="!new00001", ch=0, **extra):
    r = {"from": frm, "to": to, "text": text, "channel": ch}
    r.update(extra)
    return r


def plan(c=None, s=None, r=None, is_new=True, ts=T):
    return R.plan_welcome(c or cfg(), s if s is not None else {}, r or rec(), OURS, is_new, ts)


# ── POSITIVE ──────────────────────────────────────────────────────
should, reason, dest, ch, text, gates = plan()
check("positive_should", should, True)
check("positive_reason", reason, "welcome")
check("positive_dest", dest, "^all")
check("positive_channel", ch, 0)
truthy("positive_text_from_table", text in R._WELCOME_LINES)
check("gate_ladder_leads_with_enabled", gates[0]["gate"], "welcome_enabled")
truthy("positive_all_gates_pass", all(g["pass"] for g in gates))

# ── NEGATIVE ──────────────────────────────────────────────────────
check("disarmed", plan(c=cfg(WELCOME_ENABLED="false"))[1], "welcome_disabled")
check("self", plan(r=rec(frm=OURS))[1], "self")
check("empty_sender", plan(r=rec(frm=None))[1], "self")
check("dm_to_node", plan(r=rec(to="!newdm001"))[1], "welcome_not_broadcast")
check("dm_to_us", plan(r=rec(to=OURS))[1], "welcome_not_broadcast")
check("private_channel", plan(r=rec(ch=1))[1], "welcome_not_public_channel")
check("reaction_true_bool", plan(r=rec(reaction=True))[1], "welcome_is_reaction")
check("reaction_string_fails_closed", plan(r=rec(reaction="true"))[1], "welcome_is_reaction")
check("empty_text", plan(r=rec(text="   "))[1], "welcome_no_text")
check("missing_text", plan(r=rec(text=None))[1], "welcome_no_text")
check("not_new_node", plan(is_new=False)[1], "welcome_not_new")
check("already_welcomed",
      plan(s={"welcome_sent": {"!new00001": T - 999999}})[1], "welcome_already_sent")
check("min_gap",
      plan(s={"welcome_last_ts": T - 100})[1], "welcome_min_gap")
check("min_gap_boundary_ok",
      plan(s={"welcome_last_ts": T - 300})[0], True)   # exactly the floor passes
check("daily_budget_spent",
      plan(s={"welcome_last_ts": 0, "welcome_day": {DAY: 4}})[1], "welcome_budget_spent")
check("daily_budget_last_slot_ok",
      plan(s={"welcome_last_ts": 0, "welcome_day": {DAY: 3}})[0], True)
# a broadcast with to=None (some clients omit it) is still a broadcast
check("broadcast_none_ok", plan(r=rec(to=None))[0], True)

# ── INVARIANTS: the built-in lines ────────────────────────────────
def words(s):
    return [w for w in s.replace("—", " ").split() if any(ch.isalnum() for ch in w)]

for i, line in enumerate(R._WELCOME_LINES):
    n = len(words(line))
    truthy("line%d_5to7_words(%d)" % (i, n), 5 <= n <= 7)
    truthy("line%d_no_digits" % i, not any(c.isdigit() for c in line))
truthy("lines_distinct", len(set(R._WELCOME_LINES)) == len(R._WELCOME_LINES))

# override wins verbatim
check("override_verbatim",
      plan(c=cfg(WELCOME_TEXT="Howdy neighbour, welcome in"))[4], "Howdy neighbour, welcome in")
# rotation is deterministic by the day's count
check("rotate_0", plan(s={"welcome_day": {DAY: 0}, "welcome_last_ts": 0})[4], R._WELCOME_LINES[0])
check("rotate_1", plan(s={"welcome_day": {DAY: 1}, "welcome_last_ts": 0})[4], R._WELCOME_LINES[1])

# ── welcome_is_new / mark_welcome_seen ────────────────────────────
truthy("new_when_absent", R.welcome_is_new({}, "!fresh999", OURS))
truthy("not_new_when_seen", not R.welcome_is_new({"welcome_seen": {"!x": 1}}, "!x", OURS))
truthy("self_never_new", not R.welcome_is_new({}, OURS, OURS))
truthy("empty_never_new", not R.welcome_is_new({}, None, OURS))
_s = {}
R.mark_welcome_seen(_s, "!m1", T)
truthy("mark_records", "!m1" in _s["welcome_seen"])
R.mark_welcome_seen(_s, "!m1", T + 50)
check("mark_keeps_first_ts", _s["welcome_seen"]["!m1"], T)   # setdefault: first wins
R.mark_welcome_seen(_s, None, T)
truthy("mark_ignores_empty", None not in _s.get("welcome_seen", {}))

# ── SEED from the node DB (safe-to-arm guarantee) ─────────────────
_orig = R.NODES
try:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"nodes": [{"id": "!reg00001"}, {"id": "!reg00002"}, {"id": None}]}, f)
        R.NODES = f.name
    s = {}
    R.seed_welcome_seen(s)
    truthy("seed_folds_known", "!reg00001" in s["welcome_seen"] and "!reg00002" in s["welcome_seen"])
    truthy("seed_skips_null_id", None not in s["welcome_seen"])
    truthy("seed_sets_flag", s.get("welcome_seeded") is True)
    truthy("seeded_node_not_new", not R.welcome_is_new(s, "!reg00001", OURS))
    truthy("unseeded_node_still_new", R.welcome_is_new(s, "!brandnew", OURS))
    # a seeded regular's first observed message must NOT be welcomed
    check("seeded_regular_refused",
          R.plan_welcome(cfg(), s, rec(frm="!reg00001"), OURS,
                         R.welcome_is_new(s, "!reg00001", OURS), T)[0], False)
    # idempotent: a second call with the flag set is a no-op even if DB changes
    with open(R.NODES, "w") as f:
        json.dump({"nodes": [{"id": "!late0001"}]}, f)
    R.seed_welcome_seen(s)
    truthy("seed_idempotent", "!late0001" not in s["welcome_seen"])
finally:
    R.NODES = _orig
    try:
        os.unlink(f.name)
    except Exception:
        pass

# missing DB does not wedge and does not set the flag (so a later run still seeds)
_orig = R.NODES
try:
    R.NODES = "/nonexistent/path/nodes.json"
    s = {}
    R.seed_welcome_seen(s)
    truthy("seed_missing_db_safe", s == {})
finally:
    R.NODES = _orig

# ── commit spends exactly one, once ───────────────────────────────
s = {}
R.commit_welcome(s, "!c1", T)
check("commit_records_sent", s["welcome_sent"]["!c1"], T)
check("commit_sets_last_ts", s["welcome_last_ts"], T)
check("commit_counts_day", s["welcome_day"][DAY], 1)
# now the same node is refused as already-welcomed
check("post_commit_refused",
      R.plan_welcome(cfg(), s, rec(frm="!c1"), OURS, False, T + 1000)[1], "welcome_not_new")
# old day counters are pruned
s2 = {"welcome_day": {"2020-01-01": 9}}
R.commit_welcome(s2, "!c2", T)
truthy("commit_prunes_old_days", "2020-01-01" not in s2["welcome_day"])

# ── LADDER: a bare greeting from a NEW node is a WELCOME, not an ack ──
# Both capabilities are armed; the dispatch tries welcome first. Here we assert
# the welcome path itself fires on a bare greeting (it does not require non-greeting text).
gc = cfg(GREETING_ENABLED="true")
check("welcome_fires_on_bare_greeting", R.plan_welcome(gc, {}, rec(text="hello"), OURS, True, T)[0], True)

# ── THE MEASUREMENT, which is the point of the feature ────────────────
# A bare "good to hear you" is a CLAIM: it reads the same whether the newcomer arrived direct
# and strong or scraped in at three hops, so it cannot show the one thing a newcomer wants to
# know. These assert the reply carries what the RADIO recorded, and — the case that matters
# more — that it says nothing about reception when nothing was recorded.
import sigreport as _sig

STRONG = {"snr": 6.0, "rssi": -35, "hops": 0}
WEAK = {"snr": -2.5, "rssi": -108, "hops": 2}
BLIND = {"snr": None, "rssi": None, "hops": None}

_strong = R.welcome_reply("", 0, dict(rec(), **STRONG), 96)
_weak = R.welcome_reply("", 0, dict(rec(), **WEAK), 96)
_blind = R.welcome_reply("", 0, dict(rec(), **BLIND), 96)

truthy("measured_welcome_is_a_welcome", _strong.lower().startswith("welcome"))
truthy("measured_welcome_carries_snr", "6.0" in _strong)
truthy("measured_welcome_carries_rssi", "-35" in _strong)
truthy("measured_welcome_says_direct", "direct" in _strong.lower())
# the weak arrival must read differently from the strong one, or the number is decoration
truthy("a weak arrival reads differently", _weak != _strong)
truthy("weak welcome reports the hops", "2 hops" in _weak)
truthy("weak welcome reports its own snr", "-2.5" in _weak)

# WITHOUT a measurement it must not claim to have heard anything.
check("no measurement falls back to the bare line", _blind, R._WELCOME_LINES[0])
truthy("and the bare line claims no numbers", not any(c.isdigit() for c in _blind))

# ONE FORMATTER. The numbers come from sigreport.report, not a second copy here that could
# drift about whose signal it is (last leg into Cal, not the sender's journey).
_direct, _ = _sig.report(dict(rec(), **STRONG), max_chars=64)
truthy("the measurement is sigreport's, verbatim", _direct in _strong)

# BOUNDED. A measured welcome is longer than a bare one and still must fit the air budget.
# INSTANTIATED AT SEVERAL BUDGETS, not just the default. At 96 the measured path cannot
# overflow by construction (19 + 3 + sigreport's own 74 cap = exactly 96), so asserting it
# only there is an invariant that cannot fail -- and it hid a real overflow at every smaller
# budget. The bare-line fallback is unbounded too: it is 37 chars whatever the budget says.
for _b in (30, 40, 44, 60, 96):
    truthy("measured welcome fits budget %d" % _b,
           len(R.welcome_reply("", 0, dict(rec(), **WEAK), _b)) <= _b)
    truthy("bare-line fallback fits budget %d" % _b,
           len(R.welcome_reply("", 0, dict(rec(), **BLIND), _b)) <= _b)
# The old form here asserted `is not None` against a function whose docstring says it never
# returns None -- unfalsifiable for every possible input. Assert the thing that can be wrong:
# a shed field must never leave a hops-only report, which sigreport itself refuses to build.
for _b in range(24, 40):
    _t = R.welcome_reply("", 0, dict(rec(), **WEAK), _b)
    truthy("budget %d never yields a hops-only measurement" % _b,
           not (_t.rstrip().endswith("hops") or _t.rstrip().endswith("hop")))

# The override still wins, even with a measurement available.
check("override beats the measurement", R.welcome_reply("Hi there", 0, dict(rec(), **STRONG)),
      "Hi there")

# And the planner actually threads the record through — a welcome planned from a real record
# must contain the measurement, not the bare line.
_pt = R.plan_welcome(cfg(), {}, dict(rec(), **STRONG), OURS, True, T)[4]
truthy("plan_welcome passes the record to the reply", "6.0" in _pt)

print("\n%s  eval_welcome: %d passed, %d failed" % ("OK" if FAIL == 0 else "FAIL", PASS, FAIL))
sys.exit(1 if FAIL else 0)
