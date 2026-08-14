#!/usr/bin/env python3
"""Discharge trend for Cal HT from battery-history.jsonl.

Only ON-BATTERY samples count. A plugged-in sample reads the charge rail
(battery pinned to the 101 sentinel), so including one would flatten the
slope toward zero and inflate the projection.

Caveat the numbers carry: the MAX17048 gauge is not initialising, so the
percentage is DERIVED FROM VOLTAGE by the firmware's fallback curve. It is
not an independent coulomb count. A LiPo's voltage curve is flat through the
middle and steep at both ends, so a slope fitted early reads optimistic and
one fitted near empty reads pessimistic. Treat a projection from under ~2h
of data as an order of magnitude, not a time.
"""
import json, sys, os

HIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "battery-history.jsonl")


def load():
    rows = []
    with open(HIST) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            b, v = r.get("battery"), r.get("voltage")
            # battery > 100 is the "external power" sentinel, not a charge level
            if b is None or v is None or b > 100:
                continue
            try:
                import datetime
                r["_t"] = datetime.datetime.fromisoformat(r["ts"]).timestamp()
            except Exception:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r["_t"])
    return rows


def slope(rows, key):
    """Least-squares slope of key per hour. Returns None if degenerate."""
    n = len(rows)
    if n < 2:
        return None
    t0 = rows[0]["_t"]
    xs = [(r["_t"] - t0) / 3600.0 for r in rows]
    ys = [r[key] for r in rows]
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def fmt_hours(h):
    if h is None:
        return "n/a"
    if h < 0:
        return "rising (charging or noise)"
    d, rem = int(h // 24), h % 24
    return ("%dd %.1fh" % (d, rem)) if d else ("%.1fh" % rem)


def main():
    if not os.path.exists(HIST):
        print("no history file yet:", HIST)
        return 1
    rows = load()
    if not rows:
        print("no on-battery samples yet — every sample so far is the 101 sentinel (plugged in).")
        return 0

    span_h = (rows[-1]["_t"] - rows[0]["_t"]) / 3600.0
    cur = rows[-1]
    print("on-battery samples : %d" % len(rows))
    print("window             : %s -> %s (%.2f h)" % (rows[0]["ts"], rows[-1]["ts"], span_h))
    print("current            : %s%%  %.3f V" % (cur["battery"], cur["voltage"]))
    print("range              : %s%% -> %s%%   %.3f V -> %.3f V"
          % (rows[0]["battery"], cur["battery"], rows[0]["voltage"], cur["voltage"]))

    # Coming off the charger, a cell dumps surface charge for the first minutes.
    # Fitting through that transient produced a -14 %/h slope when the settled
    # rate was under -3. Drop a settle window, then fit several trailing windows
    # and require them to AGREE before calling the result a projection.
    SETTLE_S = 300
    t0 = rows[0]["_t"]
    body = [r for r in rows if r["_t"] - t0 >= SETTLE_S]
    print()
    if len(body) < 3:
        print("slope              : still settling (need >%d min on battery)" % (SETTLE_S // 60))
        return 0

    print("(first %d min dropped as post-charge settling)" % (SETTLE_S // 60))
    body_span_h = (body[-1]["_t"] - body[0]["_t"]) / 3600.0
    fits, fits_n = [], {}
    for label, win_h in (("since settle", None), ("last 2h", 2.0), ("last 1h", 1.0), ("last 30m", 0.5)):
        # A trailing window longer than the data we hold is just an alias for the
        # whole set. Including it makes the convergence test compare a sample
        # against itself and always agree — a vacuous pass. Skip it.
        if win_h is not None and win_h >= body_span_h:
            print("  %-13s skipped (only %.2fh of settled data — window would alias the full set)"
                  % (label, body_span_h))
            continue
        rs = body if win_h is None else [r for r in body if r["_t"] >= body[-1]["_t"] - win_h * 3600]
        if len(rs) < 3:
            continue
        sp = slope(rs, "battery")
        sv = slope(rs, "voltage")
        if sp is None:
            continue
        span = (rs[-1]["_t"] - rs[0]["_t"]) / 3600.0
        proj = cur["battery"] / -sp if sp < 0 else None
        fits.append((label, sp, proj))
        fits_n[label] = len(rs)
        print("  %-13s n=%3d span=%.2fh  %+7.2f %%/h  %+.4f V/h  -> 0%% in %s"
              % (label, len(rs), span, sp, sv if sv is not None else float("nan"), fmt_hours(proj)))

    # VERDICT via split-half, not trailing windows. Trailing windows overlap —
    # "last 30m" shares almost every sample with "since settle", so they agree
    # whether or not the rate is steady. Halves are DISJOINT: if the rate is
    # genuinely constant the two halves match; if the cell is still relaxing,
    # the first half is steeper and they don't.
    MIN_SPAN_H = 1.5
    print()
    print("VERDICT (split-half on disjoint samples):")
    if body_span_h < MIN_SPAN_H:
        print("  NO ESTIMATE — %.2fh of settled data, need %.1fh." % (body_span_h, MIN_SPAN_H))
        print("  A slope fitted this early reflects relaxation, not discharge.")
        return 0
    mid = body[0]["_t"] + (body[-1]["_t"] - body[0]["_t"]) / 2.0
    h1 = [r for r in body if r["_t"] <= mid]
    h2 = [r for r in body if r["_t"] > mid]
    s1, s2 = slope(h1, "battery"), slope(h2, "battery")
    if s1 is None or s2 is None or s1 >= 0 or s2 >= 0:
        print("  NO ESTIMATE — a half shows flat or rising charge (n=%d/%d)." % (len(h1), len(h2)))
        return 0
    p1, p2 = cur["battery"] / -s1, cur["battery"] / -s2
    print("  first half  n=%3d  %+6.2f %%/h  -> %s" % (len(h1), s1, fmt_hours(p1)))
    print("  second half n=%3d  %+6.2f %%/h  -> %s" % (len(h2), s2, fmt_hours(p2)))
    ratio = max(p1, p2) / min(p1, p2)
    if ratio > 1.5:
        # Direction matters. Second half SHALLOWER = the cell is still relaxing
        # after a charge, and the early slope was an artifact. Second half
        # STEEPER = discharge is accelerating, i.e. we are leaving the flat
        # plateau for the knee of the curve, where a voltage-derived gauge
        # falls fast. Same "not converged", opposite meaning and opposite fix.
        if abs(s2) > abs(s1):
            print("  NOT CONVERGED — halves disagree by %.1fx, and the rate is ACCELERATING"
                  % ratio)
            print("  (%.2f -> %.2f %%/h). Not relaxation: the cell is entering the knee of" % (s1, s2))
            print("  the discharge curve. Expect the remaining runtime to undershoot any")
            print("  linear projection, including the %s above." % fmt_hours(p2))
        else:
            print("  NOT CONVERGED — halves disagree by %.1fx, rate DECAYING (%.2f -> %.2f %%/h)."
                  % (ratio, s1, s2))
            print("  Post-charge relaxation still washing out; no runtime estimate yet.")
    else:
        print("  CONVERGED — runtime to empty ~%s (halves within %.0f%%)."
              % (fmt_hours((p1 + p2) / 2), 100 * (ratio - 1)))

    # Cross-check against the only independent endurance figure we have.
    print()
    print("Cross-check: the Aug 9->13 run lasted ~4.0 days on one charge, which is")
    print("             ~1.0 %/h average. A converged slope far from that means either")
    print("             the 4-day figure or this discharge is atypical.")
    return 0


def self_test():
    """Negative controls: the verdict must FAIL on data it should not certify."""
    import datetime, tempfile, io, contextlib
    global HIST
    base = datetime.datetime(2026, 8, 13, 8, 0, 0).astimezone()

    def synth(points):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            for mins, pct, volt in points:
                f.write(json.dumps({
                    "ts": (base + datetime.timedelta(minutes=mins)).isoformat(timespec="seconds"),
                    "connected": True, "battery": pct, "voltage": volt, "uptime": 60 * mins}) + "\n")
        return path

    def run(path):
        global HIST
        old, HIST = HIST, path
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main()
        HIST = old
        return buf.getvalue()

    fails = []

    # 1. Steady 1 %/h over 4h -> must CONVERGE near 4 days of runtime from 96%.
    pts = [(m, 100 - m / 60.0, 4.1 - 0.002 * (m / 60.0)) for m in range(0, 241, 5)]
    out = run(synth(pts))
    if "CONVERGED" not in out or "NOT CONVERGED" in out:
        fails.append("steady discharge should converge, got:\n" + out)

    # 2. Relaxing curve (steep then flat) over 4h -> must NOT converge.
    pts = []
    for m in range(0, 241, 5):
        h = m / 60.0
        pct = 100 - 20 * (1 - pow(2.718281828, -h * 2)) - 1.0 * h
        pts.append((m, round(pct), 4.1 - 0.02 * (1 - pow(2.718281828, -h * 2))))
    out = run(synth(pts))
    if "NOT CONVERGED" not in out:
        fails.append("relaxing curve must NOT converge, got:\n" + out)

    # 3. Short window (40 min) -> must refuse outright.
    pts = [(m, 100 - m / 60.0, 4.1) for m in range(0, 41, 2)]
    out = run(synth(pts))
    if "NO ESTIMATE" not in out:
        fails.append("under-min-span data must be refused, got:\n" + out)

    # 4. All-sentinel data -> must report no on-battery samples.
    pts = [(m, 101, 4.9) for m in range(0, 241, 5)]
    out = run(synth(pts))
    if "no on-battery samples" not in out:
        fails.append("sentinel-only data must be rejected, got:\n" + out)

    # 5. Aliased trailing window must be skipped, never counted as agreement.
    pts = [(m, 100 - m / 60.0, 4.1) for m in range(0, 121, 5)]
    out = run(synth(pts))
    if "would alias the full set" not in out:
        fails.append("trailing window longer than the data must be skipped, got:\n" + out)

    for f in fails:
        print("FAIL:", f)
    print("self-test: %d/%d passed" % (5 - len(fails), 5))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(self_test() if "--self-test" in sys.argv else main())
