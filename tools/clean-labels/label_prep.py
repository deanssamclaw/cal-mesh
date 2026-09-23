"""Mechanical evidence per message. NOT a label: the rules decide, a person applies them."""
import json, os, sys
sys.path.insert(0, os.path.expanduser("~/cal-mesh")); os.chdir(os.path.expanduser("~/cal-mesh"))
import responder as R, sigreport as S, calc as C, weather as W, sunmoon as SM, capabilities as CAP
cfg = R.load_config()
rows = json.load(open("/Users/systems/.claude/jobs/569b6618/tmp/eval_data.json"))["rows"]
out = []
for r in rows:
    t = r["clean"]
    cr, cm = C.try_answer(t, max_chars=160, trigger="cal")
    sm = SM.explain_match(t) or {}
    wm = W.explain_weather_match(t) or {}
    out.append({"text": r["text"], "clean": t,
                "sig": (S.match(t) or {}).get("via"), "other_node": S.names_other_node(r["text"]),
                "calc": (cm or {}).get("handler") if cr else None,
                "sun": sm.get("via"), "weather": wm.get("via"),
                "caps": (CAP.explain_match(t) or {}).get("via"),
                "greet": R.is_bare_greeting(t),
                "addressed": bool(r["dm"] or r["calch"] or r["kw"])})
json.dump(out, open("/Users/systems/.claude/jobs/569b6618/tmp/label_evidence.json", "w"), indent=0)
print(len(out), "messages;", sum(1 for x in out if not any((x["sig"], x["calc"], x["sun"], x["weather"], x["caps"], x["greet"]))), "where no rule fires at all")
