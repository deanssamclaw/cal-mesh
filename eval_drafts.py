#!/usr/bin/env python3
"""eval_drafts.py — a draft must never reach the air, and never grade itself.

Two structural guarantees carry this feature, and both are tested the way they can actually
fail rather than the way that is easy to write:

  1. THE TRANSMIT BOUNDARY IS A DIRECTORY. bridge.py drains ~/cal-mesh/outbox/ and broadcasts
     any file dropped there -- a plain text file goes to ^all on channel 0. So "drafts.py does
     not import enqueue" is a check that CANNOT FAIL: it passes happily for
     open(OUTBOX + "/x", "w"). This suite instead executes drafts.run() with open() and
     os.replace() wrapped to raise on any path resolving inside outbox/, and mutation-proves it.

  2. THE MODEL CALL IS THE RESPONDER'S. `_claude_argv` is a lockdown (--permission-mode plan,
     --setting-sources "", --output-format text) and every eval guarding it names that function.
     A second argv construction would inherit none of it and nothing would notice, so this
     asserts drafts.py builds none.

Run:  python3 eval_drafts.py                (exit 0 = pass; mutations included)
"""
import importlib.util
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SRC = open(os.path.join(HERE, "drafts.py"), encoding="utf-8").read()


def code_only(src):
    """Source with comments and docstrings removed.

    These checks are substring searches for things the module must not DO. The module also
    explains, at length, why it does not do them -- so searching the raw text finds its own
    documentation and calls it a violation. Six checks failed that way on first run. Strip the
    prose and search the code."""
    import io, tokenize
    out, prev_end, prev_tok = [], (1, 0), tokenize.INDENT
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:
        return src
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            continue
        # a STRING that stands alone as its own statement is a docstring
        if tok.type == tokenize.STRING and prev_tok in (tokenize.INDENT, tokenize.NEWLINE,
                                                        tokenize.NL, tokenize.DEDENT):
            prev_tok = tok.type
            continue
        out.append(tok.string)
        if tok.type not in (tokenize.NL, tokenize.NEWLINE):
            prev_tok = tok.type
    return " ".join(out)


CODE = code_only(SRC)

FAILS = []


def ck(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def load(src=SRC, name="drafts_under_test"):
    d = tempfile.mkdtemp(prefix="evaldrafts-")
    p = os.path.join(d, "d.py")
    open(p, "w", encoding="utf-8").write(src)
    spec = importlib.util.spec_from_file_location(name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)          # sys.path has HERE, so siblings import
    return m


mod = load()
OUTBOX = os.path.join(HERE, "outbox")


# --- 1. it cannot transmit, executed ---------------------------------------------------------
def run_with_outbox_tripwire(m, rows_written):
    """Execute the real run() with a stub model, failing loudly on any write into outbox/."""
    import builtins
    real_open, real_replace = builtins.open, os.replace
    tripped = []

    def guard(path):
        try:
            rp = os.path.realpath(str(path))
        except Exception:
            return
        # Guard on any path COMPONENT named outbox, not on one absolute prefix: a mutant is
        # loaded from a temp dir, so its BASE differs and a prefix check silently watches a
        # directory nothing writes to -- the mutation then "fails to be caught" because it was
        # never observed.
        if (os.sep + "outbox" + os.sep) in rp + os.sep:
            tripped.append(rp)
            raise AssertionError("drafts wrote into the outbox: " + rp)

    def op(path, *a, **k):
        if a and "w" in str(a[0]) or k.get("mode", "").startswith(("w", "a")):
            guard(path)
        return real_open(path, *a, **k)

    def rp(src, dst, *a, **k):
        guard(dst)
        return real_replace(src, dst, *a, **k)

    m._r.run_claude = lambda cfg, prompt, persona=None, cap=180: ("drafted reply", "ok")
    builtins.open, os.replace = op, rp
    try:
        rows, made = m.run({"DRAFTS_MAX_PER_RUN": "3"}, limit=3)
        rows_written.extend(rows)
        m.save(rows)            # the write path is where a stray outbox write would live
        return made, tripped
    finally:
        builtins.open, os.replace = real_open, real_replace


print("the transmit boundary is a DIRECTORY, and it is executed")
rows = []
made, tripped = run_with_outbox_tripwire(mod, rows)
ck("drafts.run() executes", made > 0, made)
ck("and writes nothing into outbox/", not tripped, tripped)
ck("the weaker import check also holds",
   "enqueue" not in CODE and "sendText" not in CODE)
ck("and it does not name the outbox at all", "outbox" not in CODE.lower())

print("\nthe model call is the responder's, not a new one")
ck("no argv is constructed here", "_claude_argv" not in CODE)
ck("it calls run_claude", "run_claude" in SRC)
ck("no direct subprocess", "subprocess" not in CODE)
ck("no permission-mode flag is restated", "--permission-mode" not in CODE)

print("\nshape is measured, quality is not")
ck("empty draft is silent", mod.shape("") == "silent")
ck("prose is answered", mod.shape("Copy, 2 hops, SNR 6.0") == "answered")
ck("boilerplate is flagged generic",
   mod.shape("I'm Claude, an AI assistant made by Anthropic.") == "generic",
   mod.shape("I'm Claude, an AI assistant made by Anthropic."))
ck("shape never returns a quality word",
   set(mod.shape(x) for x in ("", "hi", "I am an AI assistant")) <= {"silent", "answered", "generic"})

print("\ndoer coverage reads the match FIELD, not truthiness")
# try_answer returns a (reply, meta) TUPLE that is truthy even when it declined, and the
# explain_* helpers return a dict either way. Reading those wrong produced a meaningless
# "27 of 27" on 2026-09-05.
ck("plain chatter matches no doer", mod.doer_coverage("I was most certainly not up 🤪") == [],
   mod.doer_coverage("I was most certainly not up 🤪"))
ck("an emoji matches no doer", mod.doer_coverage("👍") == [], mod.doer_coverage("👍"))
ck("a range test matches sigreport", "sigreport" in mod.doer_coverage("range test"))
ck("a conversion matches calc", any(d.startswith("calc") for d in mod.doer_coverage(".5 mi in km")))

print("\nretention is by timestamp, not line count")
old = {"ts": "2020-01-01T00:00:00+00:00", "draft_id": "a"}
new = {"ts": "2026-09-06T00:00:00+00:00", "draft_id": "b"}
kept = mod.prune([old, new])
ck("a year-old draft is pruned", [r["draft_id"] for r in kept] == ["b"], kept)
ck("retention is 365 days", mod.RETAIN_DAYS == 365, mod.RETAIN_DAYS)
ck("the line-count trim is not inherited", "5000" not in CODE and "trim_file" not in CODE)

print("\ndisarmed by default, and the budget fails closed")
ck("default is off", mod.DEFAULTS["DRAFTS_ENABLED"] == "false")
ck("an unparseable budget falls back rather than going unlimited",
   mod._int_cfg({"DRAFTS_MAX_PER_RUN": "abc"}, "DRAFTS_MAX_PER_RUN", "40") == 40)
ck("an absent budget falls back", mod._int_cfg({}, "DRAFTS_MAX_PER_RUN", "40") == 40)
ck("there is no --reset", "--reset" not in CODE)

print("\ngrades live in their OWN file, never triage.json")
# Any dict under a cluster key in triage.json reads as a triage verdict to four consumers, so a
# grade written there drops the cluster out of the build queue and lowers the untriaged count on
# the PUBLIC page within 60 seconds -- asserting a gap was triaged when nothing was built.
ck("triage.json is never written", "triage" not in CODE.lower())
ck("grades have their own path", "draft-grades.json" in CODE)
ck("the human verdicts are a closed set", mod.VERDICTS == ("good", "wrong", "harmful"))

print("\nCal does not grade Cal")
ck("no verdict is ever assigned by the module",
   not re.search(r'"verdict"\s*:\s*"(good|wrong|harmful)"', CODE.replace('args.verdict', '')),
   "a verdict literal is assigned somewhere other than the --grade CLI")

print("\nevery inbound except Cal's own")
ck("self is excluded", 'rec.get("from") != our' in CODE.replace(" ", ""). replace("!=our", "!= our") or "!=our" in CODE.replace(" ", ""))
ck("nothing filters emoji or short text out",
   "emoji" not in CODE.lower() and "isalpha" not in CODE)

# --- mutations ------------------------------------------------------------------------------
MUTANTS = {
    "it writes into the outbox": (
        '    tmp = DRAFTS + ".tmp"',
        '    tmp = os.path.join(BASE, "outbox", "x")'),
    "retention becomes a line count": (
        "    return [r for r in rows if (r.get(\"ts\") or \"\") >= cut]",
        "    return rows[-5000:]"),
    "the budget goes unlimited on garbage": (
        "        return int(dflt)",
        "        return 10 ** 9"),
    "shape starts judging quality": (
        '    if _learn._GENERIC_SMELL.search(draft):\n        return "generic"',
        '    if False:\n        return "generic"'),
}

print("\nmutations (each must be CAUGHT)")
for name, (old, new) in MUTANTS.items():
    if old not in SRC:
        ck("mutation anchor present: " + name, False, "anchor missing")
        continue
    m = load(SRC.replace(old, new, 1), "drafts_mut")
    caught = False
    try:
        if name == "it writes into the outbox":
            _, trip = run_with_outbox_tripwire(m, [])
            caught = bool(trip)
        elif name == "retention becomes a line count":
            pair = [{"ts": "2020-01-01T00:00:00+00:00", "draft_id": "a"},
                    {"ts": "2026-09-06T00:00:00+00:00", "draft_id": "b"}]
            caught = [r["draft_id"] for r in m.prune(pair)] != ["b"]
        elif name == "the budget goes unlimited on garbage":
            caught = m._int_cfg({"X": "abc"}, "X", "40") != 40
        else:
            caught = m.shape("I'm Claude, an AI assistant made by Anthropic.") != "generic"
    except AssertionError:
        caught = True          # the tripwire firing IS the catch
    except Exception as e:
        caught, name = False, name + f" (probe raised {e!r})"
    ck("mutation caught: " + name, caught)

print()
if FAILS:
    print(f"eval_drafts: {len(FAILS)} FAILED — {FAILS}")
    sys.exit(1)
print("eval_drafts: all checks pass")
