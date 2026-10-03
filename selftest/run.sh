#!/usr/bin/env bash
# selftest/run.sh - checks the lab's own isolation and output-sanitising claims with a stand-in bridge.
#
# Usage: selftest/run.sh [--image IMAGE] SOURCE_TREE
#
# SOURCE_TREE is a Commonpost MCP for Thunderbird (or thunderbird-mcp) checkout: only its extension/ directory is used
# (the extension is what signals readiness). The self-test tree is built in a temporary directory: extension/ is
# copied without following any link, package.json is a minimal one written here, and mcp-bridge.cjs is
# selftest/stand-in-bridge.cjs. Nothing else is read from SOURCE_TREE, and it is not modified.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd -P)"
opts=()
while [ $# -gt 1 ]; do opts+=("$1"); shift; done
usage() { echo "usage: $0 [--image IMAGE] SOURCE_TREE" >&2; exit 2; }
[ $# -eq 1 ] || usage
if [ ! -d "$1/extension" ] || [ -L "$1/extension" ]; then usage; fi
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
mkdir "$tmp/tree" "$tmp/out"
cp -RP -- "$1/extension" "$tmp/tree/extension"
printf '{"name": "lab-selftest-stand-in", "version": "0.0.0", "private": true}\n' > "$tmp/tree/package.json"
cp -- "$here/stand-in-bridge.cjs" "$tmp/tree/mcp-bridge.cjs"

set +e
bash "$here/../run-lab.sh" "${opts[@]}" --out "$tmp/out" "$tmp/tree" "$here/isolation.json" > "$tmp/terminal.txt" 2>&1
rc=$?
set -e
cat "$tmp/terminal.txt"

bad=0
noctl() { perl -MEncode=decode -0777 -ne '$_=decode("UTF-8",$_); exit(/(?![\t\n\x{200D}])[\p{Cc}\p{Cf}\p{Bidi_Control}\x{2028}\x{2029}]/ ? 1 : 0)'; }
[ "$rc" -eq 0 ] || { echo "selftest: FAIL - run-lab exit status $rc"; bad=1; }
grep -q 'verdict: PASS' "$tmp/terminal.txt" || { echo "selftest: FAIL - no PASS verdict"; bad=1; }
grep -q 'XPASS: tool_ok step=probe' "$tmp/terminal.txt" || { echo "selftest: FAIL - the sanitiser was not exercised"; bad=1; }
# The control sequences really were in the data (JSON-escaped in the result file), so the sanitiser had work to do.
grep -q 'u001b\[2J' "$tmp"/out/run-*/*.json || { echo "selftest: FAIL - stand-in control sequences missing from the result"; bad=1; }
noctl < "$tmp/terminal.txt" || { echo "selftest: FAIL - control characters reached the terminal output"; bad=1; }
for f in "$tmp"/out/run-*/*.log; do noctl < "$f" || { echo "selftest: FAIL - control characters in $f"; bad=1; }; done
[ "$bad" -eq 0 ] && echo "selftest: PASS - isolation checks passed, no control character leaked, one tagged result block"
exit "$bad"
