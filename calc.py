"""Level 3 COMPUTE doer — arithmetic, measurements, and an RF formula pack.

Spec: docs/proposals/level3-calc-and-knowledge.md (Part 1).

Governing invariant: **the model is not in the number path.** Python computes AND formats the
reply; the responder emits it as a fixed string. There is no prompt, no narration, no model call.
That makes this the most harness-owned capability in the project — stricter than weather, where
the model at least handles the digits.

Three defences, and they are not the same defence:

  1. NO eval/exec. Expressions are parsed with `ast` and walked against a node/operator
     whitelist. `Call`, `Name`, `Attribute`, `Subscript` and imports are rejected outright, which
     is what stops `__import__("os").system(...)` and `open(...)`.
  2. A SEPARATE cost bound. The whitelist does not stop `9**9**9` — every node in it is legal.
     So operand magnitude, operator count, and exponent size are bounded independently.
  3. Fail-safe on anything uncertain. Div-zero, overflow, unparseable, ambiguous unit → return
     None and say nothing. A missing answer is always better than a confident wrong number.

Exactness: `Decimal` for decimal arithmetic and `Fraction` for fractions. Every conversion factor
below is an EXACT defined constant with its convention noted — no rounded magic numbers.
"""
from decimal import (Decimal, localcontext, InvalidOperation, DivisionByZero,
                     Overflow)
from fractions import Fraction
import ast
import math
import re

# Set on OUR context only at call time (see _ctx); importing this module must not
# silently change a caller's Decimal precision.
PREC = 28

# ---------------------------------------------------------------------------------------------
# Exact constants. Each is a DEFINED value, not a measurement, except c which is defined by SI.
# ---------------------------------------------------------------------------------------------
FT_M = Decimal("0.3048")            # exact, international foot
IN_M = Decimal("0.0254")            # exact
YD_M = Decimal("0.9144")            # exact
MI_M = Decimal("1609.344")          # exact, international mile
NMI_M = Decimal("1852")             # exact, international nautical mile
ACRE_SQFT = Decimal("43560")        # exact
C_MS = Decimal("299792458")         # exact, SI definition
# End-effect factor for a real quarter-wave element. ARRL publishes 234/f(MHz) ft,
# which is 0.95 of the free-space quarter wave. Empirical, NOT exact — hence "about".
ANTENNA_K = Decimal("0.95")

# Cost bounds — defence 2. Deliberately small: this answers pocket questions over a radio link.
MAX_OPS = 12
MAX_ABS = Decimal("1e15")
MAX_EXP = 8
MAX_LITERALS = 16

_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
    ast.USub, ast.UAdd,
)


class CalcError(Exception):
    """Any refusal. Callers turn this into silence, never into a guess."""


# ---------------------------------------------------------------------------------------------
# Tier 0 — arithmetic
# ---------------------------------------------------------------------------------------------
def _check_ast(tree):
    ops = literals = 0
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise CalcError("node %s not allowed" % type(node).__name__)
        if isinstance(node, ast.BinOp):
            ops += 1
            if isinstance(node.op, ast.Pow):
                # bound the exponent before anything evaluates it
                e = node.right
                if (not isinstance(e, ast.Constant) or isinstance(e.value, bool)
                        or not isinstance(e.value, (int, float))):
                    raise CalcError("non-literal exponent")
                if abs(e.value) > MAX_EXP:
                    raise CalcError("exponent too large")
        if isinstance(node, ast.Constant):
            literals += 1
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalcError("non-numeric literal")
            if abs(Decimal(str(node.value))) > MAX_ABS:
                raise CalcError("operand too large")
    if ops > MAX_OPS:
        raise CalcError("too many operations")
    if literals > MAX_LITERALS:
        raise CalcError("too many literals")


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp):
        v = _eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else +v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left), _eval(node.right)
        try:
            if isinstance(node.op, ast.Add):
                r = a + b
            elif isinstance(node.op, ast.Sub):
                r = a - b
            elif isinstance(node.op, ast.Mult):
                r = a * b
            elif isinstance(node.op, ast.Div):
                if b == 0:
                    raise CalcError("division by zero")
                r = a / b
            elif isinstance(node.op, ast.Mod):
                if b == 0:
                    raise CalcError("modulo by zero")
                r = a % b
            elif isinstance(node.op, ast.Pow):
                r = a ** b
            else:
                raise CalcError("operator not allowed")
        except (InvalidOperation, DivisionByZero, Overflow, ValueError) as e:
            raise CalcError(str(e))
        if abs(r) > MAX_ABS:
            raise CalcError("result too large")
        return r
    raise CalcError("unsupported expression")


def safe_eval(expr):
    """Evaluate a bounded arithmetic expression. Raises CalcError on anything unsafe.

    Runs inside its OWN decimal context. Importing this module must not change a caller's
    precision, and our results must not silently change because a caller lowered theirs.
    """
    if len(expr) > 120:
        raise CalcError("expression too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        raise CalcError("unparseable")
    _check_ast(tree)
    with localcontext() as ctx:
        ctx.prec = PREC
        return _eval(tree)


def fmt(v, places=4, _widen=True):
    """Format a Decimal for a radio link: trim trailing zeros, group thousands, no sci-notation.

    A nonzero value must NEVER render as "0". Rounding -100 dBm to "0 mW" is exactly the
    confidently-wrong number this doer exists to prevent, so the places widen until 3 significant
    figures show, and if that still cannot be done the value is REFUSED rather than printed.
    """
    if isinstance(v, Fraction):
        return "%d/%d" % (v.numerator, v.denominator)
    d = Decimal(v)
    if d != 0 and _widen:
        # Guarantee 3 significant figures for magnitudes BELOW 1. Doing this only when the value
        # quantized to exactly zero left a whole band rounding to a wrong first digit; doing it
        # for every magnitude instead over-precised ordinary answers (8.2 cm became 8.19 cm).
        exp = d.copy_abs().adjusted()          # 10**exp <= |d| < 10**(exp+1)
        if exp < 0:
            places = max(places, min(-exp + 2, 25))
    try:
        q = d.quantize(Decimal(1).scaleb(-places))
    except InvalidOperation:
        raise CalcError("value out of displayable range")
    if q == 0 and d != 0:
        raise CalcError("value rounds to zero")
    s = format(q.normalize(), "f")
    if "e" in s or "E" in s:                   # normalize() can still go exponential
        s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    if s in ("", "-"):
        s = "0"
    neg = s.startswith("-")
    s = s.lstrip("-")
    whole, _, frac = s.partition(".")
    whole = "{:,}".format(int(whole))
    return ("-" if neg else "") + whole + ("." + frac if frac else "")


# ---------------------------------------------------------------------------------------------
# Tier 1 — length, area, fractions. Convention: US customary. Ambiguous units are refused.
# ---------------------------------------------------------------------------------------------
_LEN = {
    "m": Decimal(1), "meter": Decimal(1), "meters": Decimal(1), "metre": Decimal(1),
    "km": Decimal(1000), "kilometer": Decimal(1000), "kilometers": Decimal(1000),
    "cm": Decimal("0.01"), "centimeter": Decimal("0.01"), "centimeters": Decimal("0.01"),
    "mm": Decimal("0.001"), "millimeter": Decimal("0.001"), "millimeters": Decimal("0.001"),
    "ft": FT_M, "foot": FT_M, "feet": FT_M,
    "in": IN_M, "inch": IN_M, "inches": IN_M,
    "yd": YD_M, "yard": YD_M, "yards": YD_M,
    "mi": MI_M, "mile": MI_M, "miles": MI_M,
    "nmi": NMI_M,
}
# Units whose meaning depends on a convention we were not given. Refuse rather than pick.
AMBIGUOUS = {"ton", "tons", "gallon", "gallons", "gal", "cup", "cups", "pint", "pints",
             "quart", "quarts", "fl oz", "ounce", "ounces", "oz"}

# The lookbehind is load-bearing: without it ".5 mi" matched the 5 and aired a 10x wrong answer,
# and "10^3 w" matched the 3.
#
# The first fix for that made a bare leading decimal UNMATCHABLE, which stopped the wrong answer
# by removing the shape from the module entirely — and said so nowhere. Live traffic on
# 2026-08-17 (".5 mi in km") fell straight through to the general model, which produced the
# digits itself; it happened to be right, which is exactly the failure that hides. The premise
# here is that Python owns every digit, so the trailing branch PARSES the leading decimal
# instead. The lookbehind still governs it: a '.' preceded by a digit is a decimal point inside
# a number, never the start of a new one, so "10.5" and "1.5.2" behave as before.
_NUM = (r"(?<![\d.,^eE])(?<!\^[-+])"
        r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)")


def _dec(s):
    return Decimal(s.replace(",", ""))


def convert_length(value, frm, to):
    frm, to = frm.lower(), to.lower()
    if frm in AMBIGUOUS or to in AMBIGUOUS:
        raise CalcError("ambiguous unit")
    if frm not in _LEN or to not in _LEN:
        raise CalcError("unknown unit")
    return value * _LEN[frm] / _LEN[to]


def area_acres(a_ft, b_ft):
    return (a_ft * b_ft) / ACRE_SQFT


# ---------------------------------------------------------------------------------------------
# Tier 2 — RF pack. Closed-form only. Every reply states its idealization.
# ---------------------------------------------------------------------------------------------
_FREQ_MULT = {"hz": Decimal(1), "khz": Decimal(1000), "mhz": Decimal(10) ** 6,
              "ghz": Decimal(10) ** 9}
# Correct casing matters on a technical channel: "MHZ" reads as a typo and undercuts the answer.
_FREQ_CASE = {"hz": "Hz", "khz": "kHz", "mhz": "MHz", "ghz": "GHz"}


def money(v):
    """Currency always carries two decimal places. $80.5 is a typo; $80.50 is an amount."""
    q = Decimal(v).quantize(Decimal("0.01"))
    neg = q < 0
    whole, _, frac = format(abs(q), "f").partition(".")
    return ("-" if neg else "") + "{:,}".format(int(whole)) + "." + (frac or "00")


def wavelength_m(freq_hz):
    if freq_hz <= 0:
        raise CalcError("frequency must be positive")
    return C_MS / freq_hz


def dbm_to_w(dbm):
    if dbm > 90 or dbm < -200:
        raise CalcError("dBm out of range")
    return Decimal(str(10 ** ((float(dbm) - 30) / 10)))


def w_to_dbm(w):
    if w <= 0:
        raise CalcError("power must be positive")
    return Decimal(str(10 * math.log10(float(w)) + 30))


# ITU-R P.525-5 eq. (6) publishes 32.4; the exact constant is 20*log10(4*pi*1e9/c) = 32.4477832.
# The rounded 32.44 airs 105.6 dB at 915 MHz/5 km where the true value is 105.7 — and the spec's
# own worked example carries the same error.
FSPL_K = Decimal("32.4477832")


def fspl_db(dist_km, freq_mhz):
    """Free-space path loss. IDEALIZED: no terrain, no obstruction, no multipath."""
    if dist_km <= 0 or freq_mhz <= 0:
        raise CalcError("distance and frequency must be positive")
    return (Decimal(str(20 * math.log10(float(dist_km))))
            + Decimal(str(20 * math.log10(float(freq_mhz)))) + FSPL_K)


# ---------------------------------------------------------------------------------------------
# Intent: a SUCCESSFUL BOUNDED PARSE, never "contains a number".
# Each handler returns a finished reply string or None.
# ---------------------------------------------------------------------------------------------
def _h_wavelength(t, trig=None):
    m = re.search(r"(?:wavelength|wave length|quarter.?wave|antenna).*?" + _NUM + r"\s*(hz|khz|mhz|ghz)"
                  r"|" + _NUM + r"\s*(hz|khz|mhz|ghz).*?(?:wavelength|quarter.?wave|antenna)", t)
    if not m:
        return None
    num = m.group(1) or m.group(3)
    unit = (m.group(2) or m.group(4)).lower()
    hz = _dec(num) * _FREQ_MULT[unit]
    lam = wavelength_m(hz)
    quarter = lam / 4
    label = num + " " + _FREQ_CASE[unit]
    # If the asker said "antenna" they are cutting metal, not doing physics. A real wire is
    # electrically longer than free space (end effect); ARRL's 234/f_MHz implies ~0.95. Airing
    # only the free-space figure answers the wrong question by ~5%.
    if "antenna" in t or "cut" in t:
        return "%s: quarter-wave %s cm free space, cut about %s cm (end effect)" % (
            label, fmt(quarter * 100, 1), fmt(quarter * ANTENNA_K * 100, 1))
    return "%s: wavelength %s cm, quarter-wave %s cm (free space)" % (
        label, fmt(lam * 100, 1), fmt(quarter * 100, 1))


def _h_fspl(t, trig=None):
    m = re.search(r"(?:path loss|fspl|free.?space).*?" + _NUM + r"\s*(km|mi|miles?)\b", t)
    f = re.search(_NUM + r"\s*(hz|khz|mhz|ghz)", t)
    if not m or not f:
        return None
    d = _dec(m.group(1))
    if m.group(2).startswith("mi"):
        d = d * MI_M / 1000
    mhz = _dec(f.group(1)) * _FREQ_MULT[f.group(2).lower()] / (Decimal(10) ** 6)
    return "Path loss %s dB at %s MHz over %s km (free space; real loss is higher)" % (
        fmt(fspl_db(d, mhz), 1), fmt(mhz, 3), fmt(d, 2))


def _h_dbm(t, trig=None):
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*dbm\b", t)
    if m and ("watt" in t or "w)" in t or "in w" in t or "to w" in t or "power" in t):
        w = dbm_to_w(Decimal(m.group(1)))
        # Scale to a sensible unit. A true-but-unreadable "0.0000000001 mW" is not an answer;
        # receiver sensitivity in dBm is the most likely question on a LoRa channel.
        for factor, unit in ((Decimal(1), "W"), (Decimal(10) ** 3, "mW"),
                             (Decimal(10) ** 6, "uW"), (Decimal(10) ** 9, "nW"),
                             (Decimal(10) ** 12, "pW")):
            scaled = w * factor
            if scaled >= 1:
                return "%s dBm = %s %s" % (m.group(1), fmt(scaled, 3), unit)
        return "%s dBm = %s pW" % (m.group(1), fmt(w * Decimal(10) ** 12, 4))
    m = re.search(_NUM + r"\s*(?:w|watts?)\b.*?dbm|dbm.*?" + _NUM + r"\s*(?:w|watts?)\b", t)
    if m:
        val = _dec(m.group(1) or m.group(2))
        return "%s W = %s dBm" % (fmt(val, 3), fmt(w_to_dbm(val), 2))
    return None


_OHM_WORDS = {"v", "volt", "volts", "a", "amp", "amps", "ampere", "amperes",
              "ohm", "ohms", "at", "across", "and", "into", "\u03a9"}


def _h_ohm(t, trig=None):
    # Every other handler has a distinctive keyword ("wavelength", "acres", "dbm"); this one is
    # two single letters. Unless the sender explicitly asked to calculate, the whole message must
    # BE the expression -- "need a 12 v 20 a battery" is a shopping note, not Ohm's law.
    if not _CUE.search(t):
        rest = _strip_trigger(t, trig)
        for tok in rest.replace(",", " ").split():
            if tok.strip("().") in _OHM_WORDS:
                continue
            if re.fullmatch(r"[-+]?\d[\d,.]*", tok.strip("().")):
                continue
            return None
    v = re.search(_NUM + r"\s*(?:v|volts?)\b", t)
    i = re.search(_NUM + r"\s*(?:a|amps?|amperes?)\b", t)
    r = re.search(_NUM + r"\s*(?:ohms?|Ω)\b", t)
    if v and i:
        V, I = _dec(v.group(1)), _dec(i.group(1))
        if I == 0:
            raise CalcError("zero current")
        return "%s V at %s A = %s ohms, %s W" % (fmt(V, 3), fmt(I, 3), fmt(V / I, 3), fmt(V * I, 3))
    if v and r:
        V, R = _dec(v.group(1)), _dec(r.group(1))
        if R == 0:
            raise CalcError("zero resistance")
        return "%s V across %s ohms = %s A, %s W" % (fmt(V, 3), fmt(R, 3), fmt(V / R, 4),
                                                     fmt(V * V / R, 3))
    return None


def _h_acres(t, trig=None):
    m = re.search(_NUM + r"\s*(?:ft|feet|foot)?\s*(?:x|by|\*|×)\s*" + _NUM +
                  r"\s*(?:ft|feet|foot)\b", t)
    if not m or "acre" not in t and "sq" not in t:
        return None
    a, b = _dec(m.group(1)), _dec(m.group(2))
    sqft = a * b
    return "%s ft x %s ft = %s sq ft, %s acres" % (fmt(a, 2), fmt(b, 2), fmt(sqft, 2),
                                                   fmt(area_acres(a, b), 3))


def _h_convert(t, trig=None):
    m = re.search(_NUM + r"\s*([a-z]+)\s*(?:in|to|into)\s*([a-z]+)\b", t)
    if not m:
        return None
    val, frm, to = _dec(m.group(1)), m.group(2), m.group(3)
    if frm in AMBIGUOUS or to in AMBIGUOUS:
        raise CalcError("ambiguous unit")
    if frm not in _LEN or to not in _LEN:
        return None
    return "%s %s = %s %s" % (fmt(val, 4), frm, fmt(convert_length(val, frm, to), 4), to)


def _h_fraction(t, trig=None):
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*([+\-*x×])\s*(\d+)\s*/\s*(\d+)", t)
    if not m:
        return None
    if any(len(m.group(i)) > 9 for i in (1, 2, 4, 5)):
        raise CalcError("fraction operand too large")
    if int(m.group(2)) == 0 or int(m.group(5)) == 0:
        raise CalcError("zero denominator")
    a = Fraction(int(m.group(1)), int(m.group(2)))
    b = Fraction(int(m.group(4)), int(m.group(5)))
    op = m.group(3)
    r = a + b if op == "+" else a - b if op == "-" else a * b
    return "%s %s %s = %s" % (a, op, b, r)


def _h_percent(t, trig=None):
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(off|of|tip on)\s*\$?" + _NUM, t)
    if not m:
        return None
    pct, kind, base = Decimal(m.group(1)), m.group(2), _dec(m.group(3))
    amt = base * pct / 100
    if kind == "off":
        saved = Decimal(money(amt).replace(",", ""))
        return "%s%% off $%s = $%s (saved $%s)" % (fmt(pct, 2), money(base),
                                                   money(base - saved), money(saved))
    if kind == "tip on":
        return "%s%% tip on $%s = $%s total" % (fmt(pct, 2), money(base), money(base + amt))
    return "%s%% of %s = %s" % (fmt(pct, 2), fmt(base, 2), fmt(amt, 2))


# An explicit request to calculate. Without one of these, a bare expression must be the WHOLE
# message — "cal temp 90-95" is a weather question, not a subtraction.
_CUE = re.compile(r"\b(calc|calculate|what\s+is|what's|whats|how\s+much\s+is|equals?)\b")


def _trim_edges(s):
    """Trim surrounding whitespace and TRAILING sentence punctuation only.

    The old form was .strip(" ?.") on both edges, which ate a LEADING decimal point and put a
    10x-1000x wrong number on air as a fixed Python-authored string: "whats .5 * 4" answered
    "5 * 4 = 20", and ".25 * 4" answered "25 * 4 = 100". _NUM's lookbehind never protected this
    path because _h_arith does not use _NUM — so the leading-decimal fix landed on five handlers
    and missed the one that needed it most. A leading '.' is a digit, never punctuation.
    """
    return re.sub(r"[\s?.]+$", "", s.lstrip(" \t?")).strip()


def _strip_trigger(t, trig=None):
    """Drop a leading TRIGGER token ("cal", "Cal,") and nothing else.

    Stripping any leading alphabetic token instead meant "temp 12*12" reduced to an expression and
    computed. That is reachable: a DM counts as addressed with no trigger word, and DMs are
    published to the public page. Punctuation is stripped first because "cal," is not .isalpha()
    and 8 of 25 real inbound messages use that form.
    """
    toks = t.split()
    if toks and trig and toks[0].strip(",:;.!?").lower() == trig.lower():
        toks = toks[1:]
    return _trim_edges(" ".join(toks))


# Shapes that are a calculation only if the sender explicitly asked for one. All three accept
# decimals and commas: an integer-only version let "39.0,-95.0" through as arithmetic, airing
# "-56" for a COORDINATE PAIR onto a public page.
# Each accepts a LEADING DECIMAL POINT as well as a digit. They previously required a leading
# digit, which was safe only because the old both-edges .strip(" ?.") had already eaten any
# leading '.' — so fixing that strip silently un-refused every bare shape whose left operand
# starts with a point: ".8-.9" computed -0.1, "-.5" computed -0.5. A fix that removes a
# normalisation has to widen whatever depended on it.
_BARE_RANGE = re.compile(r"[\d.][\d,.]*-[\d,.]*\d")
_BARE_DIMS = re.compile(r"[\d.][\d,.]*x[\d,.]*\d")
_BARE_NUM = re.compile(r"[-+]?[\d.][\d,.]*")

_EMBED_RUN = re.compile(r"[\d,.]+(?:\s*[-+*/×^]\s*[\d,.]+)*")
# '^' was here and is deliberately gone. _embedded_expr EXTRACTS a substring and re-dispatches on
# it alone, which drops _NUM's caret lookbehind — so "temp 10^3 w in dbm" answered "10^3 = 1,000"
# instead of refusing, i.e. arithmetic offered as the answer to a dBm question. The default path
# refused it correctly the whole time; the embedded path introduced the hole in the same commit
# that claimed "the caret forms stay refused". Carets in mesh traffic sit next to units far more
# often than they are a bare calculation, so the shape is not unambiguous enough to rescue.
_UNAMBIG = re.compile(r"[*×]")


def _embedded_expr(s):
    """A calculation typed INSIDE prose ("temp 12*12"). Returns the expression, or None.

    NOT used by the ordinary path, and deliberately so. Round 3 closed a hole where
    _strip_trigger dropped any leading word, so "rssi -105+5" computed as arithmetic; the rule
    that came out of it — prose containing an expression does not compute — is correct and
    stays. "box 5 * 3" is a box and "set gain 3 * 2" is gain staging, and no property of the
    TEXT separates either from "temp 12*12". The discriminator is not in the message.

    It is in which capability would otherwise claim it. See try_answer(embedded=True): the
    caller opts in only for a message the WEATHER capability is about to answer, because that
    is the collision that produced a real wrong reply — "temp 12*12" was answered "70F, clear,
    north wind 5 mph" twice on 2026-08-17, an observation offered as the answer to a sum.
    Nothing here fires for prose that no other capability wants.

    Even then, only operators carrying unambiguous typing intent qualify: '*', '×', '^'.
    Excluded on purpose:
      '-'  ranges, coordinate pairs, repeater pairs, negative telemetry ("temp 90-95", "10-4")
      'x'  dimensions ("8 x 10") — the module already treats 'x' as ambiguous
      '/'  dates ("see you 10/4")
    Two candidates in one message is an intent we will not guess at, so that returns None
    rather than picking one.
    """
    # _trim_edges, NOT .strip(" ,."). Round 2 fixed the leading-decimal bug in _strip_trigger and
    # the cue path and left this one — the path _calc_collision uses for exactly the armed
    # weather+calc traffic that fix was written about. "temp .5*4" answered "5*4 = 20". Same
    # defect, third path, found by the third review.
    cands = [_trim_edges(m.group(0).strip(",")) for m in _EMBED_RUN.finditer(s)
             if _UNAMBIG.search(m.group(0))]
    return cands[0] if len(cands) == 1 else None


def _h_arith(t, trig=None):
    """Plain arithmetic. Runs LAST so a unit/RF question is never eaten as bare maths.

    Intent is the hard part here, not evaluation. Mesh traffic is full of hyphenated ranges and
    dimensions -- "temp 90-95", "gusts 20-30", "10-4", "146-600", "8 x 10" -- and an earlier
    version answered every one of them with arithmetic, including questions the WEATHER
    capability was already live to handle. So a bare expression is accepted only when it is the
    entire message, and two shapes are refused even then unless the sender explicitly asked to
    calculate.
    """
    cue = _CUE.search(t)
    if cue:
        expr = _trim_edges(t[cue.end():])
    elif "=" in t:
        # "12 * 12 =" is a calculation; "temp = 90-95" and "rssi=-105" are telemetry. The test is
        # whether the LEFT side is itself an expression. '=' is NOT treated as a word cue, so the
        # bare-shape refusals below stay active for it.
        expr = _strip_trigger(t.split("=")[0], trig)
        if re.search(r"[a-wyz]", expr) or not re.search(r"[+\-*/x×^]", expr):
            return None
    else:
        rest = _strip_trigger(t, trig)
        # any remaining letter other than 'x' (the multiply symbol) means this is prose
        if re.search(r"[a-wyz]", rest):
            return None
        expr = rest
    if not expr or not re.search(r"[+\-*/x×^]", expr):
        return None

    if not cue:
        bare = expr.replace(" ", "")
        # "10-4", "90-95", "5.5-6.5", "39.0,-95.0", "146.520-146.940": a single subtraction is a
        # range, a coordinate pair, a repeater pair or radio shorthand far more often than a sum.
        if _BARE_RANGE.fullmatch(bare):
            return None
        # "8 x 10", "8.5x11": a dimension. An explicit '*' is unambiguous typing intent; 'x' is not.
        if _BARE_DIMS.fullmatch(bare):
            return None
        # "-600", "-105": a bare signed number is not a calculation.
        if _BARE_NUM.fullmatch(bare):
            return None

    norm = expr.replace(",", "").replace("×", "*").replace("^", "**")
    norm = re.sub(r"(?<=[\d\s)])x(?=[\d\s(])", "*", norm).strip()
    if not norm or re.search(r"[a-z]", norm):
        return None
    val = safe_eval(norm)
    return "%s = %s" % (expr.strip(), fmt(val, 6))


HANDLERS = (_h_wavelength, _h_fspl, _h_dbm, _h_ohm, _h_acres, _h_convert,
            _h_fraction, _h_percent, _h_arith)


def try_answer(text, max_chars=160, trigger="cal", embedded=False):
    """Return (reply, meta). reply is None when nothing parsed — the caller then says nothing.

    Never raises: a CalcError anywhere becomes a refusal, because a missing answer beats a
    confident wrong one. `meta` records which handler fired for the public decision trace.

    embedded=True additionally accepts an unambiguous calculation typed inside prose. It is OFF
    by default and must stay that way: the caller opts in only when ANOTHER capability is about
    to answer the message, which is the only context where the ambiguity resolves. See
    _embedded_expr. A refusal already recorded is never overridden — if the direct parse
    refused, that verdict stands.
    """
    meta = {"handler": None, "refused": None}
    if not text:
        return None, meta
    # Own length bound. The responder truncates to 120 chars, but that is the CALLER's promise;
    # measured 13 s of regex backtracking at 5 kB, so this module refuses long input itself.
    if len(text) > 200:
        meta["refused"] = "input too long"
        return None, meta
    # _NUM matches a bare digit run, so "2e3" parses as "3". Refusing beats mis-reading.
    if re.search(r"\d[eE][-+]?\d", text):
        meta["refused"] = "scientific notation not supported"
        return None, meta
    t = text.lower().strip()
    with localcontext() as ctx:
        ctx.prec = PREC
        r, meta = _dispatch(t, meta, max_chars, trigger)
        if r is None and embedded and meta["refused"] is None:
            expr = _embedded_expr(t)
            if expr:
                r2, m2 = _dispatch(expr, {"handler": None, "refused": None}, max_chars, trigger)
                if r2:
                    m2["embedded"] = True
                    return r2, m2
                # the embedded expression parsed but hit a bound (cost, length, div-zero). The
                # primary verdict still stands, but the trace should say why the second look
                # produced nothing rather than showing a bare "no handler".
                if m2["refused"]:
                    meta["embedded_refused"] = m2["refused"]
        return r, meta


def _dispatch(t, meta, max_chars, trig=None):
    for h in HANDLERS:
        try:
            r = h(t, trig)
        except CalcError as e:
            meta["handler"] = h.__name__[3:]
            meta["refused"] = str(e)
            return None, meta
        except Exception:
            continue
        if r:
            meta["handler"] = h.__name__[3:]
            if len(r) > max_chars:
                meta["refused"] = "too long"
                return None, meta
            return r, meta
    return None, meta
