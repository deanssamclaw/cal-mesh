#!/usr/bin/env python3
"""export_judgment.py — the decision record, in a form that can be committed.

THE PROBLEM. Every artifact in this repo that encodes a HUMAN JUDGMENT is gitignored:
triage.json, gap-ledger.json, learn-history.jsonl, drafts.jsonl, draft-grades.json,
decisions.jsonl. The 48 tracked .py files are the part a successor needs least -- they are
recoverable by reading the code. The verdicts are not recoverable by anything.

The reason for the gitignore is correct and must not be undone: those files are keyed on, and
full of, STRANGER-AUTHORED TEXT. Publishing them would put other people's messages in a public
repo forever. So the record lives on one Mac, on one disk, unversioned, and the backup is on
the same disk.

THE PROJECTION. What a successor needs is the JUDGMENT, not the text that provoked it: which
asks were adjudicated, what oracle was chosen, what it was measured against, which commit armed
it, whether a person or the loop found it, and what was later corrected. None of that requires
a stranger's words. So each cluster key is replaced by a stable digest, the examples and replies
are dropped entirely, and what remains is committed.

WHAT IS DELIBERATELY NOT HERE: message text, replies, node ids, timestamps finer than a day.
The digest is one-way; it links a verdict to a cluster across exports without naming it. It is
NOT a secret -- a short ask has little entropy and could be brute-forced -- which is why the
text is dropped rather than obfuscated. Nothing here should be treated as protecting content;
the protection is that the content is absent.

Run:  python3 export_judgment.py          (writes judgment.json)
      python3 export_judgment.py --check   (prints, writes nothing)
"""
import argparse
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "judgment.json")
SCHEMA = 1

# Fields carried through verbatim. Everything not named here is dropped, so a field added to
# triage.json later cannot leak into a public file by default -- the allow list is the control.
CARRY = ("oracle", "source", "note", "found_by", "armed", "commit", "commit_source",
         "pushed", "corrections")


def digest(key):
    """Stable, one-way, short. Links a verdict to its cluster across exports without naming it."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def load(path, dflt):
    try:
        return json.load(open(os.path.join(BASE, path)))
    except Exception:
        return dflt


def _clean(text, clusters):
    """True when a free-text field contains no cluster key and no captured message."""
    if not isinstance(text, str) or not text:
        return True
    for key, c in clusters.items():
        if key and len(key) > 6 and key in text:
            return False
        for ex in (c.get("examples") or []) + (c.get("replies") or []):
            if ex and len(ex) > 8 and ex in text:
                return False
    return True


def build():
    tr = load("triage.json", {})
    agg = load("gap-ledger.json", {})
    clusters = agg.get("clusters", {}) if isinstance(agg, dict) else {}
    grades = load("draft-grades.json", {})

    rows = []
    for key, v in sorted(tr.items()):
        if not isinstance(v, dict):
            continue
        c = clusters.get(key) or {}
        row = {"id": digest(key), "kind": "doer" if key.startswith("doer:") else "ask"}
        for f in CARRY:
            if f not in v:
                continue
            if f == "corrections":
                row[f] = len(v[f])
            elif f in ("note", "source"):
                # The reasoning is the most valuable thing here and the most likely to QUOTE.
                # Several notes cite a reply verbatim ("the reply ('Roger. Standing by for
                # tasking.') ignored the question"), which is exactly the text this file exists
                # to leave behind. Withhold the ones that quote and say so, rather than dropping
                # the field for everyone or shipping the quote.
                row[f] = v[f] if _clean(v[f], clusters) else "[withheld: quotes message text]"
            else:
                row[f] = v[f]
        # Shape of the evidence, never the evidence. Counts and dates are judgment context;
        # the words that produced them are not.
        if c:
            row["counts"] = c.get("counts", {})
            row["first_seen"] = (c.get("first_ts") or "")[:10]
            row["last_unanswered"] = (c.get("last_ts") or "")[:10]
            row["last_seen"] = (c.get("last_seen") or "")[:10]
            row["nodes"] = len(c.get("froms") or [])
        rows.append(row)

    verdicts = {}
    for did, g in sorted(grades.items()):
        if isinstance(g, dict) and g.get("verdict"):
            # draft ids embed a timestamp and a packet id; digest them too.
            verdicts[digest(did)] = {"verdict": g.get("verdict"), "by": g.get("by"),
                                     "note": g.get("note", ""), "day": (g.get("ts") or "")[:10]}

    return {
        "schema": SCHEMA,
        "what": "Adjudication record. Judgment only: no message text, no replies, no node ids. "
                "Cluster keys are one-way digests. See export_judgment.py for why.",
        "clusters": len(clusters),
        "adjudicated": len(rows),
        "armed": sum(1 for r in rows if r.get("armed")),
        "by_loop": sum(1 for r in rows if r.get("found_by") == "loop"),
        "by_hand": sum(1 for r in rows if r.get("found_by") == "manual"),
        "unattributed": sum(1 for r in rows if not r.get("found_by")),
        "draft_verdicts": verdicts,
        "rows": rows,
    }


def leaks(doc, clusters_src):
    """Refuse to write a document containing anything it promised to omit.

    Checked against the REAL keys and examples rather than a regex for 'looks like text': the
    only reliable test of "the stranger's words are absent" is that the stranger's actual words
    are absent. Node ids are matched by shape as well, since a new one could appear."""
    import re
    blob = json.dumps(doc, ensure_ascii=False)
    bad = []
    for key, c in clusters_src.items():
        if key and len(key) > 6 and key in blob:
            bad.append("cluster key: " + key[:32])
        for ex in (c.get("examples") or []) + (c.get("replies") or []):
            if ex and len(ex) > 8 and ex in blob:
                bad.append("message text: " + ex[:32])
    if re.search(r"![0-9a-f]{8}", blob):
        bad.append("node id")
    return bad


def main():
    ap = argparse.ArgumentParser(description="Export the judgment record, safe to commit.")
    ap.add_argument("--check", action="store_true", help="print, write nothing")
    args = ap.parse_args()

    doc = build()
    agg = load("gap-ledger.json", {})
    bad = leaks(doc, agg.get("clusters", {}) if isinstance(agg, dict) else {})
    if bad:
        print("REFUSING TO WRITE — the projection contains what it promised to omit:")
        for b in bad[:8]:
            print("   " + b)
        return 1

    text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.check:
        print(text)
        return 0
    open(OUT, "w", encoding="utf-8").write(text)
    print("judgment.json: %d adjudicated, %d armed, by_loop %d / by_hand %d / unattributed %d"
          % (doc["adjudicated"], doc["armed"], doc["by_loop"], doc["by_hand"],
             doc["unattributed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
