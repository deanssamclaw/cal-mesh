#!/usr/bin/env python3
"""Measure the two s1route GUARDS on the live local scorer, against labels fixed BEFORE scoring.

WHY. The temperature the service applies (1.45) was fitted on the ROUTE question only; the two
yes/no guards were never measured on held-out text. Review 2026-09-24 found both sitting on their
bars on the live service (other_station 0.5166 vs 0.5 on a third-party ask; weather_now 0.8026 vs
0.8 on a plain weather ask). These probes are synthetic, written by hand, and share no text with
the corpus the floor was chosen on. The service returns p at T; the logit gap is T*ln(p/(1-p)),
so one pass reports the guard at any temperature.

Usage: python3 guard_probe.py URL T_SERVED > out.jsonl   (gates each call on jlab <= 70 C)
"""
import json, math, subprocess, sys, time, urllib.request
sys.path.insert(0, __import__("os").path.expanduser("~/cal-mesh"))
import s1route

# (text, guard, truth) -- truth is whether the guard SHOULD say yes.
PROBES = [
    # other_station: yes = about a third party's link
    ("Cal, how is the Olathe repeater doing tonight?", "other_station", True),
    ("cal what snr do you get from Bob", "other_station", True),
    ("Cal how well are you hearing the Lawrence node?", "other_station", True),
    ("cal can you hear my buddy up north?", "other_station", True),
    ("Cal, is the router on the water tower still coming through?", "other_station", True),
    ("cal hows the signal from the KC gateway", "other_station", True),
    ("Cal, how strong is Mike's station at your end?", "other_station", True),
    ("cal are you picking up the new relay on the hill", "other_station", True),
    # other_station: no = the sender's own link to Cal
    ("Cal, how's my signal?", "other_station", False),
    ("cal how do you hear me", "other_station", False),
    ("Cal, you copy me ok?", "other_station", False),
    ("cal what's my snr", "other_station", False),
    ("Cal how's the link between us tonight?", "other_station", False),
    ("cal am I coming in clear", "other_station", False),
    ("Cal, how's my new antenna sounding?", "other_station", False),
    ("cal radio check, how am I", "other_station", False),
    # weather_now: yes = conditions right now
    ("cal what's the weather", "weather_now", True),
    ("Cal, is it raining there?", "weather_now", True),
    ("cal how cold is it right now", "weather_now", True),
    ("Cal, windy out?", "weather_now", True),
    # weather_now: no = past or forecast
    ("cal what was the temp yesterday", "weather_now", False),
    ("Cal, how cold did it get last night?", "weather_now", False),
    ("cal will it rain tomorrow", "weather_now", False),
    ("Cal, what's the forecast for the weekend?", "weather_now", False),
]
CFG = {"S1_BACKEND": "local", "S1_ROUTE_ENABLED": "true"}


def body_for(text):
    cap = {}
    def post(url, body, headers, timeout):
        cap["body"] = body
        raise ValueError("capture")
    s1route.classify(CFG, text, post=post)
    return cap["body"]


def temp():
    return int(subprocess.check_output(["ssh", "jlab", "cat /sys/class/thermal/thermal_zone2/temp"])) // 1000


url, T = sys.argv[1], float(sys.argv[2])
for text, guard, truth in PROBES:
    while temp() > 70:
        time.sleep(10)
    req = urllib.request.Request(url, json.dumps(body_for(text)).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        a = json.loads(r.read())["answers"]
    p = min(max(a[guard]["noul"], 1e-6), 1 - 1e-6)
    gap = T * math.log(p / (1 - p))
    print(json.dumps({"text": text, "guard": guard, "truth": truth, "p_served": a[guard]["noul"],
                      "p_T1": round(1 / (1 + math.exp(-gap)), 4), "gap": round(gap, 3),
                      "route": a["route"]["choice"], "conf": a["route"]["confidence"]}), flush=True)
