#!/usr/bin/env python3
"""eval_hops.py — what a hop is allowed to claim about a station.

Two different things look alike on this page and must not be conflated.

A TRACEROUTE hop arrives as a full node number, so naming it is a lookup, not a guess. A RELAY
BYTE is one byte the firmware reports, and it is genuinely a fragment: measured over the live
node database, only a minority of nodes have a last byte nobody else shares, and one value is
shared by nine. So the relay byte may be named ONLY on a unique match, and must otherwise report
the size of the candidate set. Naming the likeliest candidate is the mistake `clean_name` was
written for -- a truncation that looks like an identity belonging to no node.

Neither may ever carry a location: the bridge stores no coordinates at all, and these checks
assert that no coordinate field exists to render.

Run:  python3 eval_hops.py                (exit 0 = pass)
      python3 eval_hops.py --self-test    also proves the checks can FAIL
"""
import os, sys, re, json, shutil, tempfile, subprocess, importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("dash", os.path.join(HERE, "dashboard.py"))
dash = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(dash)
V5 = dash.PAGE_V5

FAILS = []
def ck(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond: FAILS.append(name)

print("location is a BUCKET, never a point")
nodes = json.load(open(os.path.join(HERE, "nodes.json")))["nodes"]
bridge_src = open(os.path.join(HERE, "bridge.py")).read()
fields = set()
for n in nodes: fields |= set(n.keys())
# `long` is the long NAME, not longitude -- the obvious regex matches it and would pass a file
# full of coordinates. Name the fields exactly.
coord = [f for f in fields if re.search(r"latitude|longitude|coord|altitude", f, re.I)]
ck("no precise coordinate field is stored", not coord, coord)
ck("the bridge buckets at capture rather than at render",
   "BUCKETED HERE, at capture" in bridge_src)
ck("the exact position is discarded in the same expression",
   "latitude and longitude are read and discarded" in bridge_src)
ck("the page never renders a raw coordinate",
   not re.search(r"\.latitude|\.longitude", V5), "a coordinate reaches the template")

grids = [n for n in nodes if n.get("grid")]
ck("grids are actually populated (else the checks below prove nothing)",
   len(grids) > 0, f"{len(grids)} of {len(nodes)}")
ck("every grid is a well-formed Maidenhead locator",
   all(re.fullmatch(r"[A-R]{2}[0-9]{2}([a-xA-X]{2})?", g["grid"]) for g in grids),
   [g["grid"] for g in grids if not re.fullmatch(r"[A-R]{2}[0-9]{2}([a-xA-X]{2})?", g["grid"])][:3])
ck("every grid is at one coarseness, so none is finer than configured",
   len({len(g["grid"]) for g in grids}) == 1, sorted({len(g["grid"]) for g in grids}))
ck("no grid is finer than a subsquare",
   max(len(g["grid"]) for g in grids) <= 6, max(len(g["grid"]) for g in grids))

me = (json.load(open(os.path.join(HERE, "status.json"))).get("node") or {}).get("id")
mine = [n for n in nodes if n.get("id") == me]
ck("Cal's own node carries no grid", not (mine and mine[0].get("grid")), mine[:1])
# Cal does not advertise a position today, so the line above would pass even with the exclusion
# deleted. The guarantee has to be structural or it is an accident that holds until a firmware
# setting changes.
ck("and the exclusion is STRUCTURAL, not just an accident of what Cal advertises",
   'n.get("num") != my_num' in bridge_src, "self-exclusion missing from write_nodes")
ck("the page states the trade rather than only the safeguard",
   "narrow where the receiver" in V5, "FAQ does not admit what a grid gives away")

print("\nthe relay byte may narrow, never name on a collision")
frag = {}
for n in nodes: frag.setdefault(n["id"][-2:], []).append(n)
uniq = [f for f, v in frag.items() if len(v) == 1]
ck("the database is large enough for collisions to be real", len(nodes) > 100, len(nodes))
ck("collisions actually exist (else this test proves nothing)",
   any(len(v) > 1 for v in frag.values()),
   "every fragment is unique — the ambiguity branch is untestable on this data")
ck("the relay row is gated on a UNIQUE match", "hits.length===1" in V5)
ck("the ambiguous branch reports a count, not a name",
   "narrows the candidates without naming one" in V5)
ck("the comment records the measured resolve rate", re.search(r"28%|only a minority", V5) is not None)

print("\nhop rendering, EXECUTED on the live database")
node = shutil.which("node") or "/usr/local/opt/node@22/bin/node"
if not os.path.exists(node):
    print("  SKIP no node on PATH")
else:
    src = (V5[V5.index("function hopTag"):V5.index("function pathHtml")]
           + "\n" + V5[V5.index("function nodeName"):V5.index("// A LINK diagram")])
    me = (json.load(open(os.path.join(HERE, "status.json"))).get("node") or {}).get("id")
    # Deliberately NOT node-id shaped. The branch under test only needs an id absent from
    # the database, and the staged-set scrub rightly blocks any unpublished node-id shape.
    stranger = "absent-from-db-ff"
    d = tempfile.mkdtemp(prefix="evalhops-")
    json.dump({"nodes": nodes, "me": me}, open(os.path.join(d, "data.json"), "w"))
    js = os.path.join(d, "t.js")
    open(js, "w").write(
        "const D=require(" + json.dumps(os.path.join(d, "data.json")) + ");\n"
        "let lastNodes=D.nodes; const ROUTES={me:D.me};\n"
        "const esc=s=>String(s).replace(/[&<>\"]/g,c=>"
        "({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));\n"
        + src +
        "\nconst known=D.nodes.find(n=>n.id!==D.me && n.short);\n"
        "const out={self:hopDetail([D.me]), known:hopDetail([known.id]),\n"
        "  unknown:hopDetail([" + json.dumps(stranger) + "]),\n"
        "  dedup:(hopDetail([known.id,known.id]).match(/phrow/g)||[]).length,\n"
        "  tag:hopTag(known.id), shortTag:hopTag('x'), nullTag:hopTag(null),\n"
        "  knownShort:known.short, knownId:known.id,\n"
        "  gridded:(function(){const g=D.nodes.find(n=>n.grid&&n.id!==D.me);"
        "return g?hopDetail([g.id]):null;})()};\n"
        "console.log(JSON.stringify(out));\n")
    r = subprocess.run([node, js], capture_output=True, text=True)
    if r.returncode != 0:
        ck("hopDetail executes", False, r.stderr[:400])
    else:
        o = json.loads(r.stdout.strip().splitlines()[-1])
        ck("a known hop is NAMED, not truncated", o["knownShort"] in o["known"], o["known"][:120])
        ck("a hop with a grid renders it", ("EM" in o["gridded"] or "grid" in o["gridded"])
           if o.get("gridded") else True, str(o.get("gridded"))[:140])
        ck("a known hop carries substance (hardware or distance)",
           ("hop" in o["known"] or "&middot;" in o["known"]), o["known"][:120])
        ck("an unknown hop says so rather than inventing a name",
           "not in Cal" in o["unknown"] and "&rsquo;s node database" in o["unknown"],
           o["unknown"][:140])
        ck("an unknown hop still shows its fragment", "·ff" in o["unknown"], o["unknown"][:140])
        ck("Cal's own node is not described as a distant neighbour",
           "this node" in o["self"] and "heard" not in o["self"], o["self"][:160])
        ck("a repeated hop is listed once", o["dedup"] == 1, o["dedup"])
        ck("the fragment is the last two characters",
           o["tag"] == "·" + o["knownId"][-2:], f'{o["tag"]} vs {o["knownId"]}')
        ck("a too-short id degrades without crashing", o["shortTag"] == "x", o["shortTag"])
        ck("a null id degrades without crashing", o["nullTag"] == "?", o["nullTag"])

if "--self-test" in sys.argv:
    print("\nself-test — each mutation must FAIL a check above")
    MUTANTS = {
        "relay named on any match":
            ("dashboard.py", "hits.length===1", "hits.length>=1"),
        "unknown hop gets invented a name":
            ("dashboard.py", "phname unk\">not in Cal", "phname\">not in Cal"),
        "Cal's own node treated as a neighbour":
            ("dashboard.py", "if(id===ROUTES.me){", "if(false){"),
        # The one that matters most. Cal advertises no position today, so deleting the
        # self-exclusion changes nothing in the current data -- only the STRUCTURAL check can
        # catch it. This proves that check is load-bearing rather than decorative.
        "bridge stops excluding Cal's own node":
            ("bridge.py", 'n.get("num") != my_num', "True"),
    }
    for name, (target, old, new) in MUTANTS.items():
        src_txt = open(os.path.join(HERE, target)).read()
        if old not in src_txt:
            print(f"  FAIL mutation anchor missing: {name}"); FAILS.append(name); continue
        md = tempfile.mkdtemp(prefix="evalhopsmut-")
        for f in ("dashboard.py", "nodes.json", "status.json", "bridge.py"):
            shutil.copy(os.path.join(HERE, f), os.path.join(md, f))
        open(os.path.join(md, target), "w").write(src_txt.replace(old, new, 1))
        shutil.copy(os.path.join(HERE, "eval_hops.py"), os.path.join(md, "eval_hops.py"))
        r = subprocess.run([sys.executable, os.path.join(md, "eval_hops.py")],
                           capture_output=True, text=True)
        print(f"  {'ok  ' if r.returncode != 0 else 'FAIL'} mutation caught: {name}")
        if r.returncode == 0: FAILS.append("self-test: " + name)

print()
if FAILS:
    print(f"eval_hops: {len(FAILS)} FAILED — {FAILS}"); sys.exit(1)
print("eval_hops: all checks pass")
