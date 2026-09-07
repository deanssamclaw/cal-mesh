#!/usr/bin/env python3
"""eval_guards.py — the invariants that are ABOUT THE WHOLE REPO, not about one function.

WHY THIS EXISTS. Every other suite grades a named function, and an adversarial review on
2026-09-06 showed what that misses: it mutated the repo ten ways and SIX survived the entire
30-suite corpus. The worst one added a second model call site with none of the tool-lockdown
flags and rerouted the responder's live generation through it. Board stayed green.

The reason is structural. Three suites assert the lockdown by calling `responder._claude_argv`
BY NAME (eval_dm, eval_dm_longer, eval_weather). They prove that function is correct. They
cannot see a second one. `drafts.py`'s own docstring predicted exactly this -- "a second call
site would inherit none of them and nothing in the repo would notice" -- and the repo shipped
without the check.

So these are COUNTING invariants: not "is this call correct" but "is this the only call". They
are the tests MUT4b and MUT6 would have failed.

Run:  python3 eval_guards.py     (exit 0 = pass; mutations included)
"""
import ast
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []


def ck(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def code_of(path, src=None):
    """Source with comments and docstrings stripped. These are counting invariants, and this
    repo's modules DOCUMENT the things they must not do -- a raw substring count reads the
    explanation as a violation. That mistake cost six false failures once already."""
    src = src if src is not None else open(os.path.join(HERE, path), encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                body[0].value.value = ""
    return ast.unparse(tree)


def count_calls(src, dotted):
    """AST call count, so `subprocess.run` in a string or a comment is not a call."""
    tree = ast.parse(src)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = ast.unparse(f) if not isinstance(f, ast.Name) else f.id
            if name == dotted or name.endswith("." + dotted.split(".")[-1]) and dotted in name:
                n += 1
    return n


RESPONDER = open(os.path.join(HERE, "responder.py"), encoding="utf-8").read()

print("the model is invoked from EXACTLY ONE place")
# MUT4b: a second call site without the lockdown flags survived all 30 suites. The lockdown is
# only worth anything if every generation goes through the one argv that carries it.
_n_sub = count_calls(RESPONDER, "subprocess.run")
ck("responder.py runs exactly one subprocess", _n_sub == 1, _n_sub)
_n_argv = len(re.findall(r"^def _claude_argv\(", code_of("responder.py"), re.M))
ck("and builds exactly one model argv", _n_argv == 1, _n_argv)
ck("the lockdown flags are on that argv",
   all(f in RESPONDER for f in ('"--permission-mode"', '"--strict-mcp-config"',
                                '"--setting-sources"', '"--output-format"')))
# No other module may invoke the MODEL. Shelling out is not the offence -- learn.py and
# dashboard.py both run `git`, which is fine. Naming the claude binary is the offence.
_BINARY = re.compile(r"""claude['"\s\]]|CLAUDE\b""")
for mod in ("drafts.py", "dashboard.py", "learn.py", "bridge.py"):
    c = code_of(mod)
    ck(f"{mod} never invokes the model binary", not _BINARY.search(c), mod)

print("\nthe outbox has EXACTLY ONE writer in python")
# bridge.py DRAINS the outbox and any file dropped there is broadcast, so the set of things that
# can write into it IS the set of things that can transmit. MUT6 added a writer to dashboard.py
# and nothing noticed.
_writers = []
for fn in sorted(f for f in os.listdir(HERE) if f.endswith(".py")):
    if fn.startswith("eval_"):
        continue
    c = code_of(fn)
    if re.search(r"(os\.replace|open)\s*\([^)]*OUTBOX", c) or re.search(r"OUTBOX[^)]*,\s*[\"']w", c):
        _writers.append(fn)
ck("only responder.py writes to the outbox", _writers == ["responder.py"], _writers)
ck("the drain is bridge.py's alone",
   "drain_outbox" in code_of("bridge.py")
   and not any("drain_outbox" in code_of(f) for f in ("responder.py", "dashboard.py", "drafts.py")))

print("\nno suite may report success without running")
# eval_acks and eval_routes printed SKIP and exit(0) when their interpreter lacked pubsub, so
# two suites -- including the 638-line one over the bridge's send path -- counted as green
# without executing. eval_routes.py says the rule in its own words and did not apply it to
# itself: "a SKIP is the eval failing to run ... must not read as a pass".
# The suites that need the meshtastic library must FIND it rather than skip: eval_routing
# already re-execs into the interpreter that has it, and a suite that can run has no business
# reporting success without running. eval_acks and eval_routes cover the bridge's send path.
for fn in ("eval_routing.py", "eval_acks.py", "eval_routes.py"):
    c = code_of(fn)
    ck(f"{fn} finds its interpreter instead of skipping", "_ensure_library" in c, fn)
# Everything else may skip (no node on PATH is a real condition), but the skip has to be
# VISIBLE so the runner can refuse to call it green. That is run-evals.sh's job, not each
# suite's -- a suite that exits 1 on a missing optional tool is unrunnable on a clean machine.
_declared = [f for f in sorted(os.listdir(HERE))
             if f.startswith("eval_") and f.endswith(".py") and "SKIP" in code_of(f)]
ck("every skip is announced with the word SKIP so the runner can see it",
   all("SKIP" in code_of(f) for f in _declared), _declared[:3])

# --- mutations -------------------------------------------------------------------------------
MUTANTS = {
    # MUT4b, the one that survived everything
    "a second model call site appears": (
        "responder.py",
        "def run_claude(cfg, prompt, persona=None, cap=180):",
        "def run_claude_v2(cfg, prompt):\n"
        "    return subprocess.run(['claude', '-p', prompt]).stdout, 'ok'\n\n\n"
        "def run_claude(cfg, prompt, persona=None, cap=180):"),
    # MUT6
    "another module writes to the outbox": (
        "dashboard.py",
        "def build_drafts(limit=40):",
        "def _leak(text):\n"
        "    open(os.path.join(BASE, 'outbox', 'x'), 'w').write(text)\n\n\n"
        "def build_drafts(limit=40):"),
}

print("\nmutations (each must be CAUGHT)")
for name, (target, old, new) in MUTANTS.items():
    src = open(os.path.join(HERE, target), encoding="utf-8").read()
    if old not in src:
        ck("mutation anchor present: " + name, False, target)
        continue
    mutated = src.replace(old, new, 1)
    caught = False
    if target == "responder.py":
        caught = count_calls(mutated, "subprocess.run") != 1
    else:
        c = code_of(target, mutated)
        caught = bool(re.search(r"(os\.replace|open)\s*\([^)]*OUTBOX", c)
                      or re.search(r"outbox", c, re.I))
    ck("mutation caught: " + name, caught)

print()
if FAILS:
    print(f"eval_guards: {len(FAILS)} FAILED — {FAILS}")
    sys.exit(1)
print("eval_guards: all checks pass")
