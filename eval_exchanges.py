#!/usr/bin/env python3
"""eval_exchanges.py — the Open Exchanges list is capped, and the cap has edges.

`capExchanges()` shows XC_VISIBLE exchanges and scrolls for the rest. The height is measured
from the first child past the limit rather than typed as a pixel value, because an exchange is
as tall as its message. Four branches decide whether the cap goes on, and three of them are the
ones that bite: a trace opened inside a five-row window would be unreadable, a pane that is
hidden measures zero and would cap the list at nothing, and a list shorter than the limit must
not advertise a scroll that does not exist.

The function is executed in node against a stub DOM. Reading it is not the same as running it:
the hidden-pane branch was found this way, not by inspection.

Run:  python3 eval_exchanges.py                (exit 0 = pass)
      python3 eval_exchanges.py --self-test    also proves the checks can FAIL
"""
import os, sys, re, json, shutil, tempfile, subprocess, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dash", os.path.join(HERE, "dashboard.py"))
dash = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dash)
V5 = dash.PAGE_V5

FAILS = []
def ck(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)

print("markup and style contract")
ck("the caption element exists", 'id="xc-more"' in V5)
ck("the caption hides when empty", ".xcmore:empty{display:none}" in V5)
ck("the list scrolls rather than clipping", "#exchanges{overflow-y:auto" in V5)
ck("the cap is a class, not an inline default", "#exchanges.capped{max-height:var(--xcap" in V5)
ck("there is a fallback height if measurement never runs",
   re.search(r"--xcap,\s*\d+px", V5) is not None)
ck("the cap is re-applied when a tab is shown",
   V5.count("capExchanges()") >= 4, f"{V5.count('capExchanges()')} call sites")
ck("DM stream is deliberately NOT capped",
   "#dm-exchanges{overflow-y:auto" not in V5)

print("\nthe cap branches, EXECUTED")
node = shutil.which("node") or "/usr/local/opt/node@22/bin/node"
if not os.path.exists(node):
    print("  SKIP no node on PATH — the cap branches did not run")
else:
    i = V5.index("const XC_VISIBLE=5;")
    j = V5.index("function exchangeHtml")
    fn = V5[i:j]
    limit = int(re.search(r"const XC_VISIBLE=(\d+);", fn).group(1))
    d = tempfile.mkdtemp(prefix="evalxc-")
    js = os.path.join(d, "t.js")
    harness = """
let RAF=[]; const requestAnimationFrame=f=>RAF.push(f);
let CASE=null;
const $=s=> s==='#exchanges'?CASE.c : s==='#xc-more'?CASE.note : null;
function makeCase(n,open,visible,rowH){
  const kids=[]; for(let i=0;i<n;i++) kids.push({offsetTop:i*rowH});
  const c={children:kids, offsetHeight:visible?400:0, _cls:new Set(), _vars:{},
    classList:{add(x){c._cls.add(x)},remove(x){c._cls.delete(x)}},
    style:{setProperty(k,v){c._vars[k]=v}},
    querySelector:()=>open?{}:null};
  return {c, note:{textContent:''}};
}
""" + fn + """
const OUT=[];
for(const cs of CASES){
  CASE=makeCase(cs.n, cs.open, cs.visible, cs.rowH); RAF=[];
  capExchanges(); RAF.forEach(f=>f());
  OUT.push({name:cs.name, note:CASE.note.textContent,
            capped:CASE.c._cls.has('capped'), xcap:CASE.c._vars['--xcap']||null});
}
console.log(JSON.stringify(OUT));
"""
    cases = [
        {"name": "long list, pane visible", "n": 45, "open": False, "visible": True, "rowH": 64},
        {"name": "shorter than the limit",  "n": 3,  "open": False, "visible": True, "rowH": 64},
        {"name": "exactly at the limit",    "n": limit, "open": False, "visible": True, "rowH": 64},
        {"name": "a trace is open",         "n": 45, "open": True,  "visible": True, "rowH": 64},
        {"name": "pane is hidden",          "n": 45, "open": False, "visible": False, "rowH": 64},
        {"name": "tall rows still cap at N", "n": 20, "open": False, "visible": True, "rowH": 210},
    ]
    open(js, "w").write("const CASES=" + json.dumps(cases) + ";\n" + harness)
    r = subprocess.run([node, js], capture_output=True, text=True)
    if r.returncode != 0:
        ck("capExchanges executes", False, r.stderr[:400])
    else:
        got = {o["name"]: o for o in json.loads(r.stdout.strip().splitlines()[-1])}
        a = got["long list, pane visible"]
        ck("a long list is capped", a["capped"], a)
        ck("the cap equals N rows, measured", a["xcap"] == f"{limit*64}px", a)
        ck("the caption states real numbers",
           f"Showing {limit} of 45" in a["note"] and f"other {45-limit}" in a["note"], a["note"])
        b = got["shorter than the limit"]
        ck("a short list is not capped", not b["capped"], b)
        ck("and advertises no scroll", b["note"] == "", b)
        c = got["exactly at the limit"]
        ck("exactly at the limit does not cap", not c["capped"] and c["note"] == "", c)
        dd = got["a trace is open"]
        ck("an open trace lifts the cap", not dd["capped"], dd)
        ck("but the caption still tells the truth", "Showing" in dd["note"], dd)
        e = got["pane is hidden"]
        ck("a hidden pane never caps at zero", not e["capped"] and e["xcap"] is None, e)
        f = got["tall rows still cap at N"]
        ck("the cap follows content height, not a fixed pixel value",
           f["xcap"] == f"{limit*210}px", f)

if "--self-test" in sys.argv:
    print("\nself-test — each mutation must FAIL a check above")
    # Both the open-trace and hidden-pane rules are guarded TWICE -- once before the measurement
    # and again inside the requestAnimationFrame, because the pane can be hidden or a trace
    # opened between the two. So a mutation has to remove BOTH copies to model a real defect;
    # breaking one leaves the behaviour intact, and a mutant that is not a defect cannot be
    # caught by anything. Two mutations here were single-point and survived for that reason,
    # which read as missing coverage and was not.
    MUTANTS = {
        "cap ignores an open trace":
            [(r"if(extra<=0 || c.querySelector('details.tr[open]')){ c.classList.remove('capped'); return; }",
              r"if(extra<=0){ c.classList.remove('capped'); return; }"),
             (r"if(!c.offsetHeight || c.querySelector('details.tr[open]')) return;",
              r"if(!c.offsetHeight) return;")],
        "cap uses a fixed pixel height":
            (r"c.style.setProperty('--xcap',(cut-top)+'px');",
             r"c.style.setProperty('--xcap','320px');"),
        "hidden pane is measured anyway":
            [(r"if(!c.offsetHeight) return;", r"if(false) return;"),
             (r"if(!c.offsetHeight || c.querySelector('details.tr[open]')) return;",
              r"if(c.querySelector('details.tr[open]')) return;")],
    }
    src = open(os.path.join(HERE, "eval_exchanges.py")).read()
    for name, edits in MUTANTS.items():
        edits = [edits] if isinstance(edits, tuple) else edits
        if any(old not in V5 for old, _ in edits):
            print(f"  FAIL mutation anchor missing: {name}"); FAILS.append(name); continue
        mdir = tempfile.mkdtemp(prefix="evalxcmut-")
        mpath = os.path.join(mdir, "dashboard.py")
        mutated = open(os.path.join(HERE, "dashboard.py")).read()
        for old, new in edits:
            mutated = mutated.replace(old, new, 1)
        open(mpath, "w").write(mutated)
        shutil.copy(os.path.join(HERE, "eval_exchanges.py"), os.path.join(mdir, "eval_exchanges.py"))
        # PYTHONPATH so the mutant's siblings (console, calc, ...) import. Without it the child
        # died on `import console` before reaching a single check, and a non-zero exit was read
        # as "mutation caught" -- every mutation here passed unconditionally for two sessions.
        # sys.path[0] is still mdir, so the MUTATED dashboard.py is the one that loads.
        r = subprocess.run([sys.executable, os.path.join(mdir, "eval_exchanges.py")],
                           capture_output=True, text=True,
                           env=dict(os.environ, PYTHONPATH=HERE))
        # A crash is not a catch. Require the suite to have RUN and reported its own failures.
        caught = r.returncode != 0 and "FAILED" in r.stdout
        print(f"  {'ok  ' if caught else 'FAIL'} mutation caught: {name}"
              + ("" if caught else f"  {(r.stderr or r.stdout)[-160:]!r}"))
        if not caught:
            FAILS.append("self-test: " + name)

print()
if FAILS:
    print(f"eval_exchanges: {len(FAILS)} FAILED — {FAILS}")
    sys.exit(1)
print("eval_exchanges: all checks pass")
