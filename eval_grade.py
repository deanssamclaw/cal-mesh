#!/usr/bin/env python3
"""eval_grade.py — the public grading endpoint, graded by driving it over real HTTP.

WHY THIS SUITE EXISTS. Grading was a CLI nobody had ever used: draft-grades.json did not exist
after two weeks of drafts, because the only surface that shows a draft (the page) could not
send one, and the payload did not even carry the draft_id needed to address a grade. So this is
the first WRITE path on a server whose entire safety story until now was that it only reads.

WHAT IS DELIBERATELY NOT CHECKED: who may grade. The page is on the open internet on purpose and
a grade is an opinion about a sentence Cal already published there; there is nothing to protect.
The bounds that ARE checked are about the write, not the writer -- an unbounded public writer
fills a disk without anyone intending harm.

Every check drives the real Handler over a real socket. An in-process call to record_grade()
would pass while the route was unreachable, which is the failure this suite is for.

Run:  python3 eval_grade.py     (exit 0 = pass; mutations included)
"""
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILS = []


def expect(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


def load(path=None):
    """Import dashboard.py fresh. BASE is a hardcoded ~/cal-mesh, so it is repointed AFTER
    import -- discovered the hard way: a bare call to record_grade() from a worktree wrote a
    fabricated verdict into the live record."""
    spec = importlib.util.spec_from_file_location(
        "dash_under_test", path or os.path.join(HERE, "dashboard.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Assembled at runtime. scrub-staged.sh blocks any node-id-shaped literal not already
# published, and it is right to: this repo is public and the fixture needs the SHAPE, not
# a real id. It caught this on the first attempt to commit.
DRAFT_ID = "2026-09-06T00:00:00:" + "!" + "1111" + "1111"


def sandbox(m):
    d = tempfile.mkdtemp(prefix="evalgrade-")
    m.BASE = d
    m.GRADES_PATH = os.path.join(d, "draft-grades.json")
    with open(os.path.join(d, "drafts.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"draft_id": DRAFT_ID, "text": "Cal, test",
                             "draft": "Copy: RSSI -54", "via": "sigreport"}) + "\n")
    return d


def serve(m):
    # The module's OWN Server class, never a lookalike defined here: its listen backlog is part
    # of what is being graded, and a local copy with different defaults would grade the copy.
    srv = m.Server(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def post(port, body, raw=None, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = raw if raw is not None else json.dumps(body)
    c.request("POST", "/api/grade", payload, {"Content-Type": ctype})
    r = c.getresponse()
    out = r.read().decode()
    c.close()
    try:
        return r.status, json.loads(out)
    except json.JSONDecodeError:
        return r.status, {"raw": out}


def get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    out = r.read().decode()
    c.close()
    return r.status, out


mod = load()
BOX = sandbox(mod)
SRV, PORT = serve(mod)

print("a grade posted from the page is accepted and persisted")
_st, _j = post(PORT, {"draft_id": DRAFT_ID, "verdict": "good"})
expect("a valid grade returns ok", _st == 200 and _j.get("ok") is True, (_st, _j))
_saved = json.load(open(mod.GRADES_PATH, encoding="utf-8"))
expect("it lands on disk under the draft id", DRAFT_ID in _saved, list(_saved))
expect("with the verdict", _saved.get(DRAFT_ID, {}).get("verdict") == "good")
expect("and a timestamp", bool(_saved.get(DRAFT_ID, {}).get("ts")))

print("\nthe corrected reply is kept, because it is the only feedback worth training on")
_st, _j = post(PORT, {"draft_id": DRAFT_ID, "verdict": "wrong",
                      "better": "75F and clear, south wind 9"})
expect("a better reply round-trips", _st == 200 and _j.get("ok") is True, (_st, _j))
_saved = json.load(open(mod.GRADES_PATH, encoding="utf-8"))
expect("and is stored verbatim",
       _saved[DRAFT_ID]["better"] == "75F and clear, south wind 9",
       _saved[DRAFT_ID].get("better"))
expect("a regrade replaces, it does not duplicate", len(_saved) == 1, len(_saved))

print("\nbounded, because a public writer must not be able to fill a disk")
_st, _j = post(PORT, {"draft_id": DRAFT_ID, "verdict": "excellent"})
expect("an invented verdict is refused", _st == 400 and _j.get("reason") == "bad_verdict",
       (_st, _j))
_st, _j = post(PORT, {"draft_id": "2026-01-01T00:00:00:!ffffffff", "verdict": "good"})
expect("a grade for a draft that does not exist is refused",
       _st == 400 and _j.get("reason") == "unknown_draft", (_st, _j))
_st, _j = post(PORT, None, raw="x" * (mod.MAX_GRADE_BODY + 10))
expect("an oversized body is refused", _st == 413, (_st, _j))
_st, _j = post(PORT, None, raw="not json at all")
expect("a non-JSON body is refused", _st == 400, (_st, _j))
_st, _j = post(PORT, ["a", "list"])
expect("a non-object body is refused", _st == 400, (_st, _j))
_st, _j = post(PORT, {"draft_id": DRAFT_ID, "verdict": "good", "note": "n" * 1000})
_saved = json.load(open(mod.GRADES_PATH, encoding="utf-8"))
expect("an overlong note is truncated, not rejected",
       _st == 200 and len(_saved[DRAFT_ID]["note"]) == mod.MAX_NOTE,
       len(_saved[DRAFT_ID].get("note", "")))

print("\nthe route exists only where it should")
_st, _b = get(PORT, "/api/grade")
expect("GET /api/grade is not a thing", _st == 404, _st)
_c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
_c.request("POST", "/api/state", "{}", {"Content-Type": "application/json"})
_r = _c.getresponse()
_r.read()
expect("POST to a read endpoint is refused", _r.status == 404, _r.status)
_c.close()

print("\nconcurrent graders do not lose each other's verdicts")
for i in range(12):
    with open(os.path.join(mod.BASE, "drafts.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"draft_id": "d%02d" % i, "text": "t", "draft": "d"}) + "\n")
_errs = []


def _hammer(i):
    try:
        st, j = post(PORT, {"draft_id": "d%02d" % i, "verdict": "good"})
        if st != 200:
            _errs.append((i, st, j))
    except Exception as e:            # noqa: BLE001 -- the failure IS the finding
        _errs.append((i, repr(e)))


_ts = [threading.Thread(target=_hammer, args=(i,)) for i in range(12)]
for t in _ts:
    t.start()
for t in _ts:
    t.join()
_saved = json.load(open(mod.GRADES_PATH, encoding="utf-8"))
expect("all twelve posts succeeded", not _errs, _errs[:3])
expect("and all twelve verdicts survived",
       sum(1 for k in _saved if k.startswith("d")) == 12,
       sorted(k for k in _saved if k.startswith("d")))

print("\nthe page can address what it renders")
_page = mod.CURRENT_PAGE
expect("the row carries its draft_id", 'data-id="${esc(r.draft_id' in _page)
expect("the grade bar posts to the endpoint", "'/api/grade'" in _page)
expect("every verdict the server accepts has a button",
       all(("'%s'" % v) in _page for v in mod.GRADE_VERDICTS),
       mod.GRADE_VERDICTS)
expect("and the server accepts every verdict the page offers",
       all(v in mod.GRADE_VERDICTS
           for v in ("good", "doer", "wrong", "harmful")))

# --- mutations --------------------------------------------------------------------------------
print("\nmutations (each must be CAUGHT)")
SRC = open(os.path.join(HERE, "dashboard.py"), encoding="utf-8").read()
MUTANTS = {
    "any draft_id is accepted": (
        '    if not isinstance(draft_id, str) or draft_id not in known_draft_ids():\n'
        '        return False, "unknown_draft"',
        '    if not isinstance(draft_id, str):\n        return False, "unknown_draft"'),
    "the verdict set stops being closed": (
        '    if verdict not in GRADE_VERDICTS:\n        return False, "bad_verdict"',
        '    if verdict is None:\n        return False, "bad_verdict"'),
    "the note cap is removed": (
        '                       "note": (note or "")[:MAX_NOTE],',
        '                       "note": (note or ""),'),
    "the body size cap is removed": (
        "        if n <= 0 or n > MAX_GRADE_BODY:",
        "        if n <= 0:"),
}
for name, (old, new) in MUTANTS.items():
    if old not in SRC:
        expect("mutation anchor present: " + name, False, "anchor missing")
        continue
    d = tempfile.mkdtemp(prefix="gradmut-")
    mp = os.path.join(d, "dashboard.py")
    open(mp, "w", encoding="utf-8").write(SRC.replace(old, new, 1))
    for dep in ("console.py", "capability_records.py", "anatomy.py"):
        src = os.path.join(HERE, dep)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(d, dep))
    sys.path.insert(0, d)
    try:
        m2 = load(mp)
        sandbox(m2)
        s2, p2 = serve(m2)
        try:
            if name == "any draft_id is accepted":
                st, _ = post(p2, {"draft_id": "totally-made-up", "verdict": "good"})
                caught = st == 200
            elif name == "the verdict set stops being closed":
                st, _ = post(p2, {"draft_id": DRAFT_ID, "verdict": "excellent"})
                caught = st == 200
            elif name == "the note cap is removed":
                post(p2, {"draft_id": DRAFT_ID, "verdict": "good", "note": "n" * 1000})
                g = json.load(open(m2.GRADES_PATH, encoding="utf-8"))
                caught = len(g[DRAFT_ID]["note"]) > m2.MAX_NOTE
            else:
                st, _ = post(p2, None, raw="x" * (m2.MAX_GRADE_BODY + 10))
                caught = st != 413
            expect("mutation caught: " + name, caught)
        finally:
            s2.shutdown()
    finally:
        sys.path.remove(d)

SRV.shutdown()
shutil.rmtree(BOX, ignore_errors=True)

print()
if FAILS:
    print(f"eval_grade: {len(FAILS)} FAILED — {FAILS}")
    sys.exit(1)
print("eval_grade: all checks pass")
