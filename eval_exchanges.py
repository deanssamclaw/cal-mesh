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
# Grade whatever "/" actually serves. Naming a template here means the suite keeps grading
# the page it was written against long after that page is retired to /old-N, which is the
# quiet way an eval stops covering what ships.
_cur = re.search(r"^CURRENT_PAGE = (PAGE_V\d+)",
                 open(os.path.join(HERE, "dashboard.py")).read(), re.M).group(1)
V5 = getattr(dash, _cur)


def _mutate_current(src, old, new):
    """Apply a mutation inside the CURRENT template only.

    Retiring a version duplicates every template in this file, so a bare src.replace(old,new,1)
    lands in the RETIRED copy -- it mutates a page nothing grades. The suite then reports
    "mutation not caught", which is true and completely misleading: the mutation was never
    applied to the page under test. Scope it, and assert the anchor is unique in that scope.
    """
    lo = src.index(_cur + ' = r"""')
    hi = src.index('"""\n', lo)
    n = src.count(old, lo, hi)
    assert n == 1, "anchor appears %d times inside %s" % (n, _cur)
    return src[:lo] + src[lo:hi].replace(old, new, 1) + src[hi:]

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
    MUTANTS = {
        "cap ignores an open trace":
            (r"if(extra<=0 || c.querySelector('details.tr[open]')){ c.classList.remove('capped'); return; }",
             r"if(extra<=0){ c.classList.remove('capped'); return; }"),
        "cap uses a fixed pixel height":
            (r"c.style.setProperty('--xcap',(cut-top)+'px');",
             r"c.style.setProperty('--xcap','320px');"),
        "hidden pane is measured anyway":
            (r"if(!c.offsetHeight) return;", r"if(false) return;"),
    }
    src = open(os.path.join(HERE, "eval_exchanges.py")).read()
    for name, (old, new) in MUTANTS.items():
        if old not in V5:
            print(f"  FAIL mutation anchor missing: {name}"); FAILS.append(name); continue
        mdir = tempfile.mkdtemp(prefix="evalxcmut-")
        mpath = os.path.join(mdir, "dashboard.py")
        open(mpath, "w").write(_mutate_current(
            open(os.path.join(HERE, "dashboard.py")).read(), old, new))
        shutil.copy(os.path.join(HERE, "eval_exchanges.py"), os.path.join(mdir, "eval_exchanges.py"))
        r = subprocess.run([sys.executable, os.path.join(mdir, "eval_exchanges.py")],
                           capture_output=True, text=True)
        print(f"  {'ok  ' if r.returncode != 0 else 'FAIL'} mutation caught: {name}")
        if r.returncode == 0:
            FAILS.append("self-test: " + name)

print()
if FAILS:
    print(f"eval_exchanges: {len(FAILS)} FAILED — {FAILS}")
    sys.exit(1)
print("eval_exchanges: all checks pass")
