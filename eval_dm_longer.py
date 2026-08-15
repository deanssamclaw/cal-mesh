#!/usr/bin/env python3
"""Offline adversarial eval for the authenticated-DM LENGTH budget (DM_LONGER_ENABLED).

Scope of the feature under test: an authenticated DM may use a longer reply budget with the SAME
hardened persona and NO injected context. It is not the unlock; it must not become the unlock.

Loads responder.py BY EXPLICIT PATH. `responder.py` inserts its own directory at sys.path[0], so
a plain `import responder` can silently pull the DEPLOYED copy instead of the one under test —
that happened in session 120 and three mutation tests "passed" against production code.

Run:  python3 eval_dm_longer.py            (assertions)
      python3 eval_dm_longer.py --self-test (negative controls: each mutation MUST be caught)
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("responder_under_test",
                                               os.path.join(HERE, "responder.py"))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

OURS = "!c0dec0de"
PEER = "!aaaaaaaa"

passed = 0
failures = []


def check(name, cond):
    global passed
    if cond:
        passed += 1
    else:
        failures.append(name)


def cfg(**over):
    c = dict(R.DEFAULTS)
    c["RESPONDER_MODEL"] = "claude-haiku-4-5-20251001"
    c["WEATHER_ENABLED"] = "false"
    c.update(over)
    return c


def rec(to=OURS, pki=True, **over):
    r = {"from": PEER, "to": to, "text": "cal how does a mesh work?"}
    if pki is not None:
        r["pki"] = pki
    r.update(over)
    return r


def run():
    global passed, failures
    passed, failures = 0, []

    # ---- gate: default state -------------------------------------------------------------
    ok, why, _ = R.dm_longer(cfg(), rec(), OURS)
    check("default OFF", ok is False and why == "dm_longer_disabled")
    check("DEFAULTS ship disabled", R.DEFAULTS["DM_LONGER_ENABLED"] == "false")

    on = cfg(DM_LONGER_ENABLED="true")

    # ---- gate: what must hold ------------------------------------------------------------
    ok, why, _ = R.dm_longer(on, rec(), OURS)
    check("authenticated DM passes", ok is True and why == "dm_longer")

    ok, why, _ = R.dm_longer(on, rec(to="^all"), OURS)
    check("broadcast refused", ok is False and why == "dm_longer_not_dm")

    ok, why, _ = R.dm_longer(on, rec(to="!deadbeef"), OURS)
    check("DM to another node refused", ok is False and why == "dm_longer_not_dm")

    ok, why, _ = R.dm_longer(on, rec(pki=False), OURS)
    check("pki false refused", ok is False and why == "dm_longer_not_authenticated")

    # proto3 drops false, so an ABSENT field must read as NOT authenticated (fails safe)
    ok, why, _ = R.dm_longer(on, rec(pki=None), OURS)
    check("pki absent refused", ok is False and why == "dm_longer_not_authenticated")

    # truthy-but-not-True must not pass: `is True`, not `== True`
    for bad in (1, "true", "yes", [1]):
        ok, _, _ = R.dm_longer(on, rec(pki=bad), OURS)
        check(f"pki {bad!r} refused (is True, not truthy)", ok is False)

    # ---- persona: same restrictions, only the budget differs -----------------------------
    p = R.PERSONA_DM_AUTHED
    check("DM persona refuses location", "NEVER reveal Dean's location" in p)
    for word in ("personal", "schedule", "work"):
        check(f"DM persona still refuses {word}", word in p)
    check("DM persona forbids markdown", "no markdown" in p)
    check("DM persona forbids URLs", "no URLs" in p)
    check("DM persona is not the unlock persona", p != R.PERSONA_PRIVATE)
    check("DM persona grants no context freedom", "speak freely" not in p)
    check("DM persona drops the 5-7 word rule", "5-7 words" not in p)
    check("public persona still capped at 5-7 words", "5-7 words" in R.PERSONA)

    # ---- plan_response: general path gets the budget -------------------------------------
    plan = R.plan_response(on, PEER, "cal how does a mesh work?", dm_authed=True)
    check("general+authed uses DM persona", plan["persona"] == R.PERSONA_DM_AUTHED)
    check("general+authed sets max_chars", plan["max_chars"] == int(on["DM_LOCKED_MAX_CHARS"]))
    check("general+authed injects no context", "Context you may use" not in (plan["prompt"] or ""))
    check("general+authed stays locked", plan["unlocked"] is False)

    plan = R.plan_response(on, PEER, "cal how does a mesh work?", dm_authed=False)
    check("general, not authed: no persona swap", plan["persona"] is None)
    check("general, not authed: no max_chars", plan["max_chars"] is None)

    # ---- weather path must be untouched --------------------------------------------------
    won = cfg(DM_LONGER_ENABLED="true", WEATHER_ENABLED="true")
    plan = R.plan_response(won, PEER, "cal will it rain tonight?", dm_authed=True)
    check("forecast refusal still fixed", plan["mode"] == "fixed")
    check("forecast refusal text unchanged",
          plan["fixed_reply"] == "Only current conditions, no forecast yet.")
    check("weather path takes no DM persona", plan["persona"] is None)
    check("weather path takes no max_chars", plan["max_chars"] is None)

    # ---- unlock must win, never be downgraded by this path --------------------------------
    unl = cfg(DM_LONGER_ENABLED="true")
    plan = R.plan_response(unl, PEER, "hello", unlocked=True, dm_authed=True,
                           dm_context="ctx line")
    check("unlock wins over longer-budget", plan["persona"] == R.PERSONA_PRIVATE)
    check("unlock still marks unlocked", plan["unlocked"] is True)
    check("unlock uses DM_MAX_CHARS", plan["max_chars"] == int(unl["DM_MAX_CHARS"]))

    # ---- the security invariant: agency is unconditional ----------------------------------
    a_pub = R._claude_argv(on, "p", None)
    a_dm = R._claude_argv(on, "p", R.PERSONA_DM_AUTHED)
    check("argv differs by exactly one element",
          sum(1 for x, y in zip(a_pub, a_dm) if x != y) == 1 and len(a_pub) == len(a_dm))
    for flag in ("--permission-mode", "plan", "--strict-mcp-config", "--setting-sources"):
        check(f"DM argv keeps {flag}", flag in a_dm)
    check("DM argv keeps empty setting-sources",
          a_dm[a_dm.index("--setting-sources") + 1] == "")
    check("persona is the only difference",
          a_dm[a_dm.index("--system-prompt") + 1] == R.PERSONA_DM_AUTHED)

    # ---- budget is a bound, not a target --------------------------------------------------
    check("budget is bounded", 0 < int(R.DEFAULTS["DM_LOCKED_MAX_CHARS"]) <= 500)

    return passed, failures


MUTATIONS = [
    ("gate ignores the enable flag",
     lambda: setattr(R, "dm_longer", lambda c, r, o: (True, "dm_longer", [{"gate": "x", "pass": True}]))),
    ("persona drops the location refusal",
     lambda: setattr(R, "PERSONA_DM_AUTHED", R.PERSONA_DM_AUTHED.replace(
         "NEVER reveal Dean's location, personal life, schedule, or work; ", ""))),
    ("DM persona becomes the unlock persona",
     lambda: setattr(R, "PERSONA_DM_AUTHED", R.PERSONA_PRIVATE)),
    ("defaults ship enabled",
     lambda: R.DEFAULTS.__setitem__("DM_LONGER_ENABLED", "true")),
    ("budget unbounded",
     lambda: R.DEFAULTS.__setitem__("DM_LOCKED_MAX_CHARS", "100000")),
]


def self_test():
    import copy
    base_defaults = copy.deepcopy(R.DEFAULTS)
    originals = {n: getattr(R, n) for n in ("dm_longer", "PERSONA_DM_AUTHED")}
    print("negative controls — each mutation MUST be caught:")
    all_caught = True
    for name, mutate in MUTATIONS:
        mutate()
        _, fails = run()
        caught = len(fails) > 0
        all_caught &= caught
        print(f"  [{'CAUGHT' if caught else 'SURVIVED'}] {name}")
        for n, v in originals.items():
            setattr(R, n, v)
        R.DEFAULTS.clear()
        R.DEFAULTS.update(copy.deepcopy(base_defaults))
    return all_caught


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        ok = self_test()
        print("\nall mutations caught" if ok else "\nA MUTATION SURVIVED — eval is vacuous")
        sys.exit(0 if ok else 1)
    p, f = run()
    print(f"eval_dm_longer: {p} passed, {len(f)} failed")
    for name in f:
        print("  FAIL:", name)
    sys.exit(1 if f else 0)
