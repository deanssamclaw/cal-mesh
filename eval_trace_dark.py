#!/usr/bin/env python3
"""Eval for v4's one claim: the trace panel is dark, and it is dark everywhere.

WHY THIS FILE EXISTS
--------------------
v4 inverts the ground under `.tp` and nothing else. That is a claim about EVERY element the
trace draws, and the way it fails is not a crash — it is one box that kept its light styling
and now glows white in the middle of a dark panel. Nothing in the existing corpus can see
that: `eval_page.py` parses, `eval_render.py` asserts on the TEXT of the rendered HTML, and
neither of them has ever looked at a colour.

The failure mode is specific and it has already happened once on this page. The 2026-08-12
light switch shipped with the link diagram still dark, because the diagram's colours are
written into `linkSvg` rather than read from the sheet, so swapping the palette did not
reach them. v4 changes the exact same two places (`linkSvg`, and the inline `warn` style in
`flowHtml`) for the exact same reason — which is the strongest possible hint that a THIRD
such place is what will be missed next time. So this does not check the two known ones. It
resolves the cascade over the rendered markup and checks whatever is actually there.

WHAT IT DOES
------------
  1. Executes PAGE_V4's render functions under node (same approach as eval_render) over the
     record shapes the responder really writes, and collects the markup a trace produces.
  2. Parses PAGE_V4's stylesheet into ordered rules and resolves the cascade — specificity
     then source order, with `var()` resolved against the token block declared on `.tp` —
     for every element in that markup.
  3. Asserts the result: surfaces are dark, text clears AA against the lightest surface it
     can land on, boundaries that carry meaning clear the 3:1 non-text bar, and the gauge
     ramp is one hue running monotonically dark-to-bright.

THE INSTRUMENT IS TESTED, NOT ASSUMED
-------------------------------------
Every check is also run against PAGE_V3, which is the same page on a light palette. A check
that passes on v3 cannot be measuring darkness, so v3 MUST fail — and this file fails if v3
comes back clean. That is the negative control, and it is free: the retired version is
already in the file, kept as a record.

Run:  python3 eval_trace_dark.py     (exit 0 = pass; skips cleanly if node is unavailable)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "dashboard.py")).read()

node = shutil.which("node")
if not node:
    print("SKIP: node not found — cannot execute the render functions")
    sys.exit(0)


def page_block(name):
    m = re.search(name + r' = r"""(.*?)"""\n', SRC, re.S)
    return m.group(1) if m else None


# --------------------------------------------------------------------------------------
# colour
# --------------------------------------------------------------------------------------
def _lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def rgb(col):
    """#rgb / #rrggbb / rgba(...) -> (r,g,b,a). Returns None for anything else."""
    col = col.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{3})", col)
    if m:
        h = m.group(1)
        return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16), 1.0)
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", col)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", col)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        if len(parts) >= 3:
            try:
                r, g, b = (int(float(p)) for p in parts[:3])
                a = float(parts[3]) if len(parts) > 3 else 1.0
                return (r, g, b, a)
            except ValueError:
                return None
    return None


def over(fg, bg):
    """Composite a possibly-translucent colour over an opaque one."""
    if fg[3] >= 1.0:
        return fg
    return tuple(fg[i] * fg[3] + bg[i] * (1 - fg[3]) for i in range(3)) + (1.0,)


def lum(c):
    return 0.2126 * _lin(c[0]) + 0.7152 * _lin(c[1]) + 0.0722 * _lin(c[2])


def contrast(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hue(c):
    r, g, b = (x / 255.0 for x in c[:3])
    mx, mn = max(r, g, b), min(r, g, b)
    if mx == mn:
        return 0.0
    d = mx - mn
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60.0


def sat(c):
    r, g, b = (x / 255.0 for x in c[:3])
    mx, mn = max(r, g, b), min(r, g, b)
    return 0.0 if mx == 0 else (mx - mn) / mx


COLOUR_RE = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b|rgba?\([^)]*\)")


def colours_in(value):
    return [c for c in (rgb(x) for x in COLOUR_RE.findall(value)) if c]


# --------------------------------------------------------------------------------------
# a small CSS cascade, enough for this sheet: class/tag/pseudo selectors, descendant and
# child combinators, specificity then source order. At-rule blocks (@media, @keyframes) are
# deliberately skipped — nothing inside them declares a colour on this page, and that is
# asserted below rather than trusted.
# --------------------------------------------------------------------------------------
def strip_comments(css):
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def parse_rules(css):
    """-> [(selector, {prop: value}, order)] for top-level rules; plus the raw at-rule text."""
    css = strip_comments(css)
    rules, atrules = [], []
    i, n, order = 0, len(css), 0
    while i < n:
        at = css.find("@", i)
        brace = css.find("{", i)
        if brace == -1:
            break
        if at != -1 and at < brace:
            # skip a whole at-rule block, balancing braces
            j = css.find("{", at)
            if j == -1:
                break
            depth, k = 1, j + 1
            while k < n and depth:
                if css[k] == "{":
                    depth += 1
                elif css[k] == "}":
                    depth -= 1
                k += 1
            atrules.append(css[at:k])
            i = k
            continue
        sel = css[i:brace].strip()
        close = css.find("}", brace)
        if close == -1:
            break
        body = css[brace + 1:close]
        decls = {}
        for d in body.split(";"):
            if ":" not in d:
                continue
            p, _, v = d.partition(":")
            decls[p.strip().lower()] = v.strip()
        for one in sel.split(","):
            one = one.strip()
            if one:
                rules.append((one, decls, order))
                order += 1
        i = close + 1
    return rules, atrules


COMPOUND_RE = re.compile(
    r"(?P<tag>[a-zA-Z][\w-]*)?"
    r"(?P<rest>(?:\.[\w-]+|#[\w-]+|\[[^\]]*\]|::?[\w-]+(?:\([^)]*\))?)*)")


def parse_compound(tok):
    m = COMPOUND_RE.fullmatch(tok)
    if not m:
        return None
    tag = m.group("tag")
    rest = m.group("rest") or ""
    classes = set(re.findall(r"\.([\w-]+)", rest))
    attrs = re.findall(r"\[([^\]]*)\]", rest)
    pseudo_el = re.findall(r"::([\w-]+)", rest)
    pseudo_cl = [p for p in re.findall(r"(?<!:):([\w-]+)", rest)]
    return {"tag": tag, "classes": classes, "attrs": attrs,
            "pseudo_el": pseudo_el[0] if pseudo_el else None, "pseudo_cl": pseudo_cl}


def parse_selector(sel):
    """-> ([(combinator, compound)], specificity) or None if unsupported."""
    parts = re.split(r"\s*(>)\s*|\s+", sel.strip())
    parts = [p for p in parts if p]
    seq, comb = [], " "
    for p in parts:
        if p == ">":
            comb = ">"
            continue
        c = parse_compound(p)
        if c is None:
            return None
        seq.append((comb, c))
        comb = " "
    ids = sum(len(re.findall(r"#[\w-]+", sel)) for _ in [0])
    cls = sum(len(c["classes"]) + len(c["attrs"]) + len(c["pseudo_cl"]) for _, c in seq)
    els = sum((1 if c["tag"] else 0) + (1 if c["pseudo_el"] else 0) for _, c in seq)
    return seq, (ids, cls, els)


# --------------------------------------------------------------------------------------
# the rendered markup, as a tree
# --------------------------------------------------------------------------------------
from html.parser import HTMLParser   # noqa: E402

VOID = {"br", "img", "input", "hr", "meta", "link", "circle", "path", "line", "rect", "use"}


class Node:
    __slots__ = ("tag", "classes", "attrs", "style", "parent", "kids", "text")

    def __init__(self, tag, attrs, parent):
        self.tag = tag
        d = dict(attrs)
        self.classes = set((d.get("class") or "").split())
        self.attrs = d
        self.style = d.get("style") or ""
        self.parent = parent
        self.kids = []
        self.text = ""


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", [], None)
        self.cur = self.root
        self.all = []

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.kids.append(n)
        self.all.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        n = Node(tag, attrs, self.cur)
        self.cur.kids.append(n)
        self.all.append(n)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        p = self.cur
        while p is not None and p.tag != tag:
            p = p.parent
        if p is not None and p.parent is not None:
            self.cur = p.parent

    def handle_data(self, data):
        if data.strip():
            self.cur.text += data


def matches(node, seq):
    def compound_ok(nd, c):
        if c["tag"] and c["tag"] != nd.tag:
            return False
        if not c["classes"] <= nd.classes:
            return False
        for a in c["attrs"]:
            key = a.split("=")[0].split("^")[0].split("*")[0].strip()
            if key not in nd.attrs:
                return False
        for p in c["pseudo_cl"]:
            if p in ("hover", "focus-visible", "focus", "active", "empty", "last-child",
                     "first-child", "not"):
                return False       # a state we are not rendering; never the resting style
            if p == "open" or p.startswith("open"):
                return False
        return True

    # match right-to-left
    comb, last = seq[-1]
    if not compound_ok(node, last):
        return False
    nd = node
    for comb, c in reversed(seq[:-1]):
        if comb == ">":
            nd = nd.parent
            if nd is None or not compound_ok(nd, c):
                return False
        else:
            nd = nd.parent
            while nd is not None and not compound_ok(nd, c):
                nd = nd.parent
            if nd is None:
                return False
    return True


PROPS = ("color", "background", "background-color", "background-image",
         "border-color", "border", "border-left-color", "border-left",
         "text-decoration-color", "box-shadow", "stroke", "fill")


def resolve(node, rules, pseudo=None):
    """-> {prop: (value, specificity, order)} winning declarations for this element."""
    won = {}
    for sel, decls, order in rules:
        parsed = parse_selector(sel)
        if parsed is None:
            continue
        seq, spec = parsed
        want_pseudo = seq[-1][1]["pseudo_el"]
        if (want_pseudo or None) != (pseudo or None):
            continue
        if not matches(node, seq):
            continue
        for p in PROPS:
            if p in decls:
                key = (spec, order)
                if p not in won or key > won[p][1:]:
                    won[p] = (decls[p], spec, order)
    # inline style beats everything
    if pseudo is None and node.style:
        for d in node.style.split(";"):
            if ":" in d:
                p, _, v = d.partition(":")
                p = p.strip().lower()
                if p in PROPS:
                    won[p] = (v.strip(), (1, 0, 0), 10 ** 9)
    return {p: v[0] for p, v in won.items()}


def token_map(rules, scope_classes):
    """Custom properties declared on a rule whose subject carries one of scope_classes."""
    toks = {}
    for sel, decls, _order in rules:
        parsed = parse_selector(sel)
        if parsed is None:
            continue
        seq, _ = parsed
        if seq[-1][1]["classes"] & scope_classes and len(seq) == 1:
            for p, v in decls.items():
                if p.startswith("--"):
                    toks[p] = v
    return toks


def expand(value, toks, root_toks):
    for _ in range(4):
        def rep(m):
            name = m.group(1).strip()
            return toks.get(name, root_toks.get(name, m.group(0)))
        new = re.sub(r"var\(\s*(--[\w-]+)\s*(?:,[^)]*)?\)", rep, value)
        if new == value:
            break
        value = new
    return value


# --------------------------------------------------------------------------------------
# the record shapes, taken from eval_render so the two cannot drift
# --------------------------------------------------------------------------------------
_ER = open(os.path.join(HERE, "eval_render.py")).read()
SHIM = re.search(r'SHIM = r"""(.*?)"""', _ER, re.S).group(1)

# One contiguous slice of eval_render, executed as-is, rather than a mirrored copy: a second
# copy of these record shapes would drift the moment the responder's output changed, and the
# whole point of them is that they match what it really writes.
_WL = re.search(r'rec\["trace"\] = \{k: dec\.get\(k\) for k in\s*\((.*?)\)', SRC, re.S)
if _WL is None:
    print("FAIL: could not read the trace whitelist out of dashboard.py")
    sys.exit(1)
_ns = {"TRACE_KEYS": tuple(re.findall(r'"([a-z_]+)"', _WL.group(1)))}
_a = _ER.index("GATES_OK = ")
_b = _ER.index("# (case, must-contain")
exec(compile(_ER[_a:_b], "<eval_render slice>", "exec"), _ns)
CASES = _ns["CASES"]
if len(CASES) < 10:
    print(f"FAIL: only {len(CASES)} record shapes lifted from eval_render — the import broke")
    sys.exit(1)


def render(page):
    script = re.search(r"<script>(.*?)</script>", page, re.S)
    if not script:
        return None, "no <script> block"
    driver = ('\n// Harvested-path fixture. Without this, every check below runs with ROUTES empty and pathHtml\n// returns \'\' — so the whole feature would be "covered" by assertions that never reach it.\nROUTES = {me:"!cccccccc", ours:{\n  "!aaaaaaaa": {ts:new Date(Date.now()-240000).toISOString(),\n    path:["!cccccccc","!deadbeef","!aaaaaaaa"],\n    snr_towards:[6.25,-3.5], snr_back:[6.75,-4.25],\n    snr_towards_complete:true, snr_back_complete:true,\n    route_back:["!deadbeef"], links:2, witness:"addressed",\n    requester:"!cccccccc", traced:"!aaaaaaaa"}\n}, others:[]};\n' + "const OUT={};" +
              "".join(f"OUT[{json.dumps(k)}]=traceHtml({json.dumps(v)});" for k, v in CASES.items()) +
              "console.log(JSON.stringify(OUT));")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(SHIM + script.group(1) + "\n" + driver)
        path = f.name
    try:
        r = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)
    if r.returncode != 0:
        return None, (r.stderr.strip().splitlines() or ["(no detail)"])[-1]
    return json.loads(r.stdout.strip().splitlines()[-1]), None


# --------------------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------------------
AA_TEXT = 4.5          # WCAG 1.4.3
AA_NONTEXT = 3.0       # WCAG 1.4.11
MAX_SURFACE_LUM = 0.12

# A background that ENCODES A VALUE is not a surface, and holding a bright gauge fill to a
# darkness threshold would be measuring the wrong thing. These are checked by their own rule
# (monotonic single-hue ramp) instead of by the surface rule.
DATA_MARKS = {"track", "band", "mk", "sdot", "arw", "fill", "spark", "parr", "plink"}
# A connector drawn with `background` because it is a 2px box is a RAIL, not a surface.
# `.parr` is `.arw` by another name, and filing it as a surface made it the "lightest
# surface in the panel" -- which every text check is then measured against, so ONE
# misclassified 2px line reported 966 failures across correct markup. Rails are held to
# the 3:1 boundary bar below instead.
# On this sheet a ::before is never a surface — it is a rail, a spine or a boundary line drawn
# with `background` because it is a 2px box. Measuring one as a surface asked the wrong question
# of it: a rail is SUPPOSED to be lighter than its ground, which is the whole reason it is
# visible. They are held to the non-text boundary bar instead.
PSEUDO_IS_A_LINE = True
# Boundaries that carry meaning: which stage a box is, and where a scale begins and ends.
MEANINGFUL_BORDER = {"fb", "bx", "b2", "b3", "stg", "track", "bl", "sw", "phop", "parr"}


def audit(page, label):
    """-> list of failure strings."""
    fails = []
    style = re.search(r"<style>(.*?)</style>", page, re.S)
    if not style:
        return [f"{label}: no <style> block"]
    rules, atrules = parse_rules(style.group(1))
    for a in atrules:
        if re.search(r"(?:^|[;{\s])(?:background|color|border-color|fill|stroke)\s*:", a):
            fails.append(f"{label}: an at-rule block declares a colour — the cascade here skips "
                         f"at-rules, so this eval would not see it: {a[:70]}…")
    root_toks = token_map(rules, {"__root__"})
    for sel, decls, _o in rules:
        if sel.strip() == ":root":
            root_toks.update({p: v for p, v in decls.items() if p.startswith("--")})
    tp_toks = token_map(rules, {"tp"})

    rendered, err = render(page)
    if rendered is None:
        return [f"{label}: the page script threw when executed — {err}"]

    # The panel's own background has to be known first: every translucent colour in the panel
    # is composited over it before it is measured. Measuring rgba(63,185,80,.16) as though it
    # were opaque reported a bright green surface for a tint that is, in fact, nearly black —
    # the check would have failed on correct markup, which is a broken instrument, not a bug.
    _probe = Node("div", [("class", "tp")], None)
    _pd = resolve(_probe, rules)
    _praw = _pd.get("background") or _pd.get("background-color") or _pd.get("background-image")
    panel_bg = None
    if _praw:
        _pc = colours_in(expand(_praw, tp_toks, root_toks))
        if _pc:
            panel_bg = max(_pc, key=lum)
    if panel_bg is None:
        return [f"{label}: could not resolve a background for .tp — the audit measured nothing"]

    surfaces = []          # (lum, rgb, where)
    texts = []             # (rgb, where)
    borders = []           # (rgb, where)
    ramps = []             # (stops, where)

    for case, html in rendered.items():
        t = Tree()
        t.feed(html)
        for nd in t.all:
            # only elements inside the trace panel
            anc, inside = nd, False
            while anc is not None:
                if "tp" in anc.classes:
                    inside = True
                    break
                anc = anc.parent
            if not inside:
                continue
            where = f"{label}/{case} <{nd.tag} class=\"{' '.join(sorted(nd.classes))}\">"
            for pseudo in (None, "before", "after"):
                d = resolve(nd, rules, pseudo)
                if not d:
                    continue
                w = where + (f"::{pseudo}" if pseudo else "")
                bgraw = d.get("background") or d.get("background-color") or d.get("background-image")
                if bgraw:
                    v = expand(bgraw, tp_toks, root_toks)
                    cols = colours_in(v)
                    if "gradient" in v and ("track" in nd.classes and "ramp" in nd.classes):
                        ramps.append((cols, w))
                    elif cols and (pseudo or (nd.classes & {"parr"})):
                        for c in cols:
                            borders.append((c, w))
                    elif cols and not (nd.classes & DATA_MARKS):
                        for c in cols:
                            oc = over(c, panel_bg)
                            surfaces.append((lum(oc), oc, w))
                if "color" in d:
                    v = expand(d["color"], tp_toks, root_toks)
                    for c in colours_in(v):
                        texts.append((c, w))
                for bp in ("border-color", "border", "border-left-color", "border-left"):
                    if bp in d and (nd.classes & MEANINGFUL_BORDER or pseudo):
                        v = expand(d[bp], tp_toks, root_toks)
                        for c in colours_in(v):
                            borders.append((c, w))
            # inline/attribute colours on SVG primitives — the place the light switch missed
            for attr in ("fill", "stroke"):
                if attr in nd.attrs:
                    for c in colours_in(nd.attrs[attr]):
                        # A stroke is the box's edge, a fill is the box's face. Filing a stroke
                        # as a surface asked a bright green outline to be dark, which is exactly
                        # backwards: an outline on a dark panel has to be light to exist.
                        if attr == "fill" and nd.tag == "text":
                            texts.append((c, where + f" @{attr}"))
                        elif attr == "fill" and nd.tag == "rect":
                            oc = over(c, panel_bg)
                            surfaces.append((lum(oc), oc, where + f" @{attr}"))
                        else:
                            borders.append((c, where + f" @{attr}"))

    if not surfaces or not texts:
        return [f"{label}: resolved no surfaces or no text colours — the audit measured nothing"]

    for l, c, w in surfaces:
        if l > MAX_SURFACE_LUM:
            fails.append(f"{label}: SURFACE not dark (luminance {l:.3f} > {MAX_SURFACE_LUM}) — {w}")

    lightest = max(surfaces, key=lambda s: s[0])
    for c, w in texts:
        r = contrast(over(c, lightest[1]), lightest[1])
        if r < AA_TEXT:
            fails.append(f"{label}: TEXT {r:.2f}:1 against the lightest surface in the panel "
                         f"(needs {AA_TEXT}) — {w}")

    for c, w in borders:
        r = contrast(over(c, panel_bg), panel_bg)
        if r < AA_NONTEXT:
            fails.append(f"{label}: BOUNDARY {r:.2f}:1 against the panel (needs {AA_NONTEXT}) — {w}")

    # Darkness is not the only thing that can be lost in an inversion. The three stage boxes are
    # colour-CODED — blue is what the software recognised, green is what it fetched, purple is
    # what went out — and an override that darkened them into three identical greys would pass
    # every check above while destroying the only thing the colour was there for.
    coded = {}
    for case, html in rendered.items():
        t = Tree()
        t.feed(html)
        for nd in t.all:
            for k in ("bx", "b2", "b3"):
                if k in nd.classes and "fb" in nd.classes and k not in coded:
                    d = resolve(nd, rules)
                    raw = d.get("border-color") or d.get("border")
                    if raw:
                        cs = colours_in(expand(raw, tp_toks, root_toks))
                        if cs:
                            coded[k] = (cs[0], f"{label}/{case} .fb.{k}")
    if len(coded) == 3:
        for k, (c, w) in coded.items():
            if sat(c) < 0.25:
                fails.append(f"{label}: STAGE COLOUR .fb.{k} is effectively grey "
                             f"(saturation {sat(c):.2f} < 0.25) — the coding is gone — {w}")
        ks = sorted(coded)
        for i in range(len(ks)):
            for j in range(i + 1, len(ks)):
                a, b = coded[ks[i]][0], coded[ks[j]][0]
                dh = abs(hue(a) - hue(b))
                dh = min(dh, 360 - dh)
                if dh < 40:
                    fails.append(f"{label}: STAGE COLOUR .fb.{ks[i]} and .fb.{ks[j]} are only "
                                 f"{dh:.0f}° apart in hue — they no longer distinguish the stages")
    else:
        fails.append(f"{label}: resolved a border colour for only {len(coded)}/3 stage boxes — "
                     f"the stage-colour check measured nothing")

    for stops, w in ramps:
        ls = [lum(c) for c in stops]
        if len(ls) < 2:
            fails.append(f"{label}: the gauge ramp has fewer than two stops — {w}")
        elif not all(b > a for a, b in zip(ls, ls[1:])):
            fails.append(f"{label}: the gauge ramp is not monotonic dark-to-bright "
                         f"({', '.join(f'{x:.3f}' for x in ls)}) — {w}")
    return fails


v4 = page_block("PAGE_V4")
v3 = page_block("PAGE_V3")
if v4 is None or v3 is None:
    print("FAIL: PAGE_V4 or PAGE_V3 not found in dashboard.py")
    sys.exit(1)

# --------------------------------------------------------------------------------------
# Mutations. The negative control below proves the audit can fail on a whole light page; it
# does NOT prove it can fail on the realistic defect, which is one element that was missed.
# Each of these reverts exactly one v4 change and must be CAUGHT. Every one of them is a
# regression that could actually be committed: three are "I forgot a rule", two are the two
# places a palette swap cannot reach, and one inverts the ramp rule.
MUTATIONS = [
    ("the raised-plane override is missed, so every flow box stays a white card",
     ".tp .fb{border-color:#5c6673;", ".tp .fbXX{border-color:#5c6673;"),
    ("a stage box is darkened into the same grey as its neighbours, losing the coding",
     ".tp .fb.b2{border-color:#3a8752;", ".tp .fb.b2{border-color:#5c6673;"),
    ("the link diagram's label colour is left as the light page had it",
     'fill="#e6edf3" font-size="13"', 'fill="#1a1f26" font-size="13"'),
    ("the link diagram's node boxes are left light",
     "const fill=s.fill||(s.self?'#221a35':'#1c222b');",
     "const fill=s.fill||(s.self?'#f7f4fd':'#f2f4f7');"),
    ("the inline 'nothing was looked up' box is left light",
     "const warn='border-color:#8a6d1f;background:linear-gradient(180deg,#2a2213,#1f190e)';",
     "const warn='border-color:#e6c98a;background:linear-gradient(180deg,#fffdf5,#fdf6e3)';"),
    ("the dim token is not re-picked for a dark ground",
     "--accent:#6cb6ff; --ok:#3fb950;", "--dim:#5c6672; --accent:#6cb6ff; --ok:#3fb950;"),
    ("the gauge ramp runs bright-to-dark",
     ".tp .track.ramp{background:linear-gradient(90deg,#0f2a19,#2a8f4c,#56d364)}",
     ".tp .track.ramp{background:linear-gradient(90deg,#56d364,#2a8f4c,#0f2a19)}"),
    ("the chip keeps the light page's pale blue",
     ".tp .chip{background:#17324f;color:#9ecbff}", ".tp .chipXX{background:#17324f;color:#9ecbff}"),
]

if "--self-test" in sys.argv:
    print("\n--- self-test: each mutation must be CAUGHT ---")
    bad = 0
    for why, old, new in MUTATIONS:
        if v4.count(old) != 1:
            print(f"  !! could not apply mutation ({v4.count(old)} matches): {why}")
            bad += 1
            continue
        got = audit(v4.replace(old, new), "mut")
        print(f"  {'ok' if got else '!!'} {why}: {'CAUGHT' if got else 'SURVIVED'}")
        if not got:
            bad += 1
    if bad:
        print(f"\n{bad} mutation(s) not caught — the audit is decorative for those cases")
        sys.exit(1)

v4_fails = audit(v4, "v4")
v3_fails = audit(v3, "v3")

print()
# The negative control comes first, because if it does not fire nothing below it means anything.
if not v3_fails:
    print("FAIL: the same audit run against v3 — the LIGHT version of this page — came back clean.")
    print("      A darkness check that passes on the light palette is not measuring darkness.")
    sys.exit(1)

for f in v4_fails:
    print("  " + f)

n_surfaces = len(v3_fails)
print()
print(f"trace-panel colour audit over {len(CASES)} rendered record shapes; "
      f"{len(v4_fails)} problem(s) in v4")
print(f"negative control: the same audit finds {n_surfaces} problem(s) in v3 (the light page), "
      f"so it can fail")
sys.exit(1 if v4_fails else 0)
