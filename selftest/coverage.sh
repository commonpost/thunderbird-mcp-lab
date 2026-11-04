#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Guillaume Laforest
# selftest/coverage.sh - statement and branch coverage of the harness (harness/lab.py), measured with coverage.py.
#
# Usage: selftest/coverage.sh [--image IMAGE] [--out DIR] [--min-statements P] [--min-branches P] SOURCE_TREE
#
#   --image           the coverage image (default: thunderbird-mcp-lab:coverage), built with
#                     docker build --target coverage -t thunderbird-mcp-lab:coverage harness
#   --out             results directory (default: ./coverage-results); its runner/, lab/ and report files are replaced
#   --min-statements  exit status 1 when fewer statements of lab.py ran (percent, default 0)
#   --min-branches    exit status 1 when fewer branches of lab.py were taken (percent, default 0)
#   SOURCE_TREE       a Commonpost MCP for Thunderbird checkout, as for run-lab.sh
#
# What runs, all in containers of IMAGE (see harness/self_coverage.py for how a measure is taken and written):
#   1. selftest/validator-cases.py and selftest/harness-tests.py, with this checkout mounted read-only;
#   2. the public scenarios, through run-lab.sh on SOURCE_TREE;
#   3. the isolation self-test (selftest/run.sh).
# A failing step is reported and the measure goes on: lab.yml is the check that gates on them. coverage.py then
# reports the union: the table on standard output and in DIR/report.txt, the totals in DIR/summary.json.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd -P)"
root="$(dirname "$here")"
IMAGE="thunderbird-mcp-lab:coverage"
OUT="./coverage-results"
MIN_ST=0
MIN_BR=0
usage() { echo "usage: $0 [--image IMAGE] [--out DIR] [--min-statements P] [--min-branches P] SOURCE_TREE" >&2; exit 2; }
percent() { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]+)?$ ]] || usage; }
while [ $# -gt 1 ]; do
  case "$1" in
    --image)          IMAGE="$2"; shift 2 ;;
    --out)            OUT="$2"; shift 2 ;;
    --min-statements) percent "$2"; MIN_ST="$2"; shift 2 ;;
    --min-branches)   percent "$2"; MIN_BR="$2"; shift 2 ;;
    *) usage ;;
  esac
done
[ $# -eq 1 ] || usage
SRC="$1"
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not found; build it first:" \
  "docker build --target coverage -t $IMAGE harness" >&2; exit 2; }

mkdir -p -- "$OUT"
out="$(cd -- "$OUT" && pwd -P)"
rm -rf -- "$out/runner" "$out/lab" "$out/selftest.txt" "$out/report.txt" "$out/summary.json"
mkdir -- "$out/runner" "$out/lab"
warn() { echo "::warning::coverage: $*"; }

# 1. Scripts that run lab.py on the runner side, as the invoking user so that measures can be written to runner/.
for script in validator-cases.py harness-tests.py; do
  docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
      --user "$(id -u):$(id -g)" -v "$root":/src:ro -v "$out/runner":/out -e LAB_SELF_COVERAGE_OUT=/out \
      --entrypoint python3 "$IMAGE" -B -I "/src/selftest/$script" || warn "selftest/$script failed"
done

# 2. The public scenarios: each container writes its measure after the result block, which run-lab.sh logs.
"$root/run-lab.sh" --image "$IMAGE" --out "$out/lab" "$SRC" "$root"/scenarios/*.json || warn "some scenarios failed"

# 3. The isolation self-test; --verbose keeps every container line in its output.
"$here/run.sh" --image "$IMAGE" --verbose "$SRC" > "$out/selftest.txt" 2>&1 || warn "the isolation self-test failed"

set +e
docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
    --user "$(id -u):$(id -g)" -v "$out":/cov:ro -e LAB_SELF_COVERAGE=off --entrypoint python3 "$IMAGE" \
    -I -m lab_self_coverage report /cov "$MIN_ST" "$MIN_BR" > "$out/report.txt"
rc=$?
set -e
cat -- "$out/report.txt"
sed -n 's/^\[lab-self-coverage-total\] //p' "$out/report.txt" > "$out/summary.json"
exit "$rc"
