#!/bin/bash
# The definition of GREEN for this repo. There was not one.
#
# "Corpus 30/30" was said all day on 2026-09-06 and was never true: eval_acks and eval_routes
# printed SKIP and exited 0 when their interpreter lacked the meshtastic library, so two suites
# -- including the 638-line one over the bridge's SEND path, 161 checks between them -- counted
# as passes without executing a single check. eval_routes.py states the rule in its own words at
# line 621 and did not apply it to itself: "a SKIP is the eval failing to run, which is not the
# same thing and must not read as a pass."
#
# So: exit 0 is necessary and not sufficient. A suite that announces SKIP did not run, and this
# refuses to call that green. Optional tooling (node) is still allowed to be missing -- the
# result is amber, which is honest, rather than a green that is a lie.
#
#   ./run-evals.sh              every suite
#   ./run-evals.sh --self-test  also run each suite's own mutation controls
set -u
cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)" || exit 2

SELF=""
[ "${1:-}" = "--self-test" ] && SELF="--self-test"

pass=0; fail=0; skip=0
failed=""; skipped=""
out=$(mktemp)

for f in eval_*.py; do
  if python3 "./$f" $SELF >"$out" 2>&1; then
    # Anchored: a suite that PRINTS a skip line, not one that merely says the word. eval_guards
    # asserts "every skip is announced with the word SKIP" and was itself reported as skipped.
    if grep -qE '^[[:space:]]*SKIP' "$out"; then
      skip=$((skip+1)); skipped="$skipped $f"
      printf '  SKIP %s\n    %s\n' "$f" "$(grep -m1 'SKIP' "$out" | cut -c1-96)"
    else
      pass=$((pass+1))
    fi
  else
    fail=$((fail+1)); failed="$failed $f"
    printf '  FAIL %s\n    %s\n' "$f" "$(grep -m1 -iE 'FAIL|Error' "$out" | cut -c1-96)"
  fi
done
rm -f "$out"

echo
echo "ran=$pass  skipped=$skip  failed=$fail"
[ -n "$failed" ] && echo "failed: $failed"
[ -n "$skipped" ] && echo "skipped (did NOT run): $skipped"

if [ "$fail" -ne 0 ]; then
  echo "NOT GREEN — a suite failed."
  exit 1
fi
if [ "$skip" -ne 0 ]; then
  echo "AMBER — everything that ran passed, but $skip suite(s) did not run. Not green."
  exit 2
fi
echo "GREEN — every suite ran and passed."
exit 0
