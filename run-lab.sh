#!/usr/bin/env bash
# run-lab.sh - run thunderbird-mcp lab scenarios in a disposable, offline container.
#
# Usage: ./run-lab.sh [--image IMAGE] [--out DIR] [--verbose] SOURCE_DIR SCENARIO.json [SCENARIO.json ...]
#
#   SOURCE_DIR  a thunderbird-mcp source tree (extension/, mcp-bridge.cjs and package.json at its root)
#   SCENARIO    a scenario file (see docs/scenario-format.md)
#   --image     lab image to use (default: $LAB_IMAGE or thunderbird-mcp-lab:local)
#   --out       parent directory of the results (default: $LAB_OUT or ./lab-results); it must not be inside
#               SOURCE_DIR. Every run writes into a NEW subdirectory run-<UTC time>-<random> of it: a log and a result
#               JSON per scenario, and summary.tsv. Nothing that already exists is ever written to.
#   --verbose   show every line of the container output, not only the progress lines
#
# Each scenario runs in a fresh container: no network, read-only root, no capabilities, unprivileged user, tmpfs only,
# memory/CPU/PID/time limits, no bind mount at all. The harness is PID 1 of the container (no --init) and makes itself
# non-dumpable. Everything the container gets arrives on its standard input: a one-time nonce, the scenario, then a tar
# stream of SOURCE_DIR (without .git). The verdict is read only from the result block tagged with that nonce; all
# container output is stripped of control characters before it is shown or written to disk.
#
# Exit status: 0 = every scenario passed, 1 = at least one failed, 2 = usage error, 3 = at least one result unusable.
set -euo pipefail

IMAGE="${LAB_IMAGE:-thunderbird-mcp-lab:local}"
OUT_DIR="${LAB_OUT:-./lab-results}"
VERBOSE=0
TIME_LIMIT=900            # seconds per scenario (the harness stops itself after ~560 s)
MAX_INPUT=629145600       # 600 MiB of source tree, at most
MAX_OUTPUT=20971520       # 20 MiB of container output, at most
MAX_SCENARIO=1000000      # bytes
LAB_UID=10001             # the image's unprivileged user

die() { printf 'run-lab: %s\n' "$*" >&2; exit 2; }
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit "${1:-0}"; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1"; }

# Output of the container is untrusted: decode as UTF-8 (invalid bytes become U+FFFD, which also neutralises raw C1
# controls), then drop every control/format character except tab and newline, bidi controls, line/paragraph
# separators, variation selectors and tag characters.
clean() {
  perl -MEncode=decode,encode -ne 'BEGIN{$|=1} $_=decode("UTF-8",$_); s/(?![\t\n\x{200D}])[\p{Cc}\p{Cf}\p{Bidi_Control}\x{2028}\x{2029}\x{FE00}-\x{FE0F}\x{E0100}-\x{E01EF}]//g; print encode("UTF-8",$_)'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --image)   [ $# -ge 2 ] || die "--image needs a value"; IMAGE="$2"; shift 2 ;;
    --out)     [ $# -ge 2 ] || die "--out needs a value"; OUT_DIR="$2"; shift 2 ;;
    --verbose) VERBOSE=1; shift ;;
    -h|--help) usage 0 ;;
    --)        shift; break ;;
    -*)        die "unknown option: $1 (see --help)" ;;
    *)         break ;;
  esac
done
[ $# -ge 2 ] || usage 2

for cmd in docker perl python3 tar timeout head od mktemp realpath awk; do need "$cmd"; done
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image $IMAGE not found; build it first: docker build -t $IMAGE harness"

SRC_ARG="$1"; shift
[ -d "$SRC_ARG" ] || die "not a directory: $SRC_ARG"
SRC="$(cd -- "$SRC_ARG" && pwd -P)"
for f in extension/manifest.json mcp-bridge.cjs package.json; do
  if [ ! -f "$SRC/$f" ] || [ -L "$SRC/$f" ]; then die "$SRC_ARG does not look like a thunderbird-mcp tree (missing $f)"; fi
done

# The results must not land inside the tree under test: a hostile tree could have planted links there (and the
# results would be sent into the next container). Checked before and after the directory is created.
inside_src() { case "$1/" in "$SRC"/*) return 0 ;; *) return 1 ;; esac; }
inside_src "$(realpath -m -- "$OUT_DIR")" && die "--out must not be inside SOURCE_DIR: $OUT_DIR"

WORK="$(mktemp -d)"
CNAME=""
# shellcheck disable=SC2317,SC2329  # invoked through the EXIT trap below
cleanup() {
  if [ -n "$CNAME" ]; then docker kill "$CNAME" >/dev/null 2>&1 || true; fi
  rm -rf -- "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# Scenarios are validated and copied first, so that a file changing during the run cannot desynchronise the input.
i=0
for s in "$@"; do
  i=$((i + 1))
  [ -f "$s" ] || die "scenario not found: $s"
  size=$(( $(wc -c < "$s") ))
  [ "$size" -le "$MAX_SCENARIO" ] || die "scenario larger than $MAX_SCENARIO bytes: $s"
  head -c "$MAX_SCENARIO" -- "$s" > "$WORK/scenario-$i.json"
  python3 -c 'import json, sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$WORK/scenario-$i.json" 2>/dev/null \
    || die "not valid JSON: $s"
done

umask 077
mkdir -p -- "$OUT_DIR"
OUT_ABS="$(cd -- "$OUT_DIR" && pwd -P)"
inside_src "$OUT_ABS" && die "--out must not be inside SOURCE_DIR: $OUT_DIR"
# A new, private directory for this run (mkdir fails rather than follow an existing name): every file below is created
# in it, so no link planted beforehand can redirect a write.
RUN_DIR="$(mktemp -d -- "$OUT_ABS/run-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
SUMMARY="$RUN_DIR/summary.tsv"
printf 'scenario\tresult\tpass\tfail\txfail\txpass\terrors\tjson\n' > "$SUMMARY"
printf '[run-lab] image %s | source %s | %d scenario(s) | results in %s\n' "$IMAGE" "$SRC" "$#" "$RUN_DIR"

worst=0
i=0
for s in "$@"; do
  i=$((i + 1))
  base="$(basename -- "$s" .json | tr -c 'A-Za-z0-9._\n-' '_')"
  log="$RUN_DIR/$i-$base.log"; out="$RUN_DIR/$i-$base.json"
  : > "$log"
  nonce="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
  size=$(( $(wc -c < "$WORK/scenario-$i.json") ))
  CNAME="tbmcp-lab-$(date -u +%Y%m%dT%H%M%SZ)-$$-$i"
  printf '\n[run-lab] scenario %s (%d/%d) -> %s\n' "$s" "$i" "$#" "$log"
  set +e
  {
    printf '%s\n%s\n' "$nonce" "$size"
    cat -- "$WORK/scenario-$i.json"
    tar --sparse -C "$SRC" --exclude=./.git -cf - . 2> "$WORK/tar-$i.err" | head -c "$MAX_INPUT"
  } | timeout -k 10 "$TIME_LIMIT" docker run -i --rm --name "$CNAME" --label thunderbird-mcp-lab=1 \
        --sig-proxy=false --log-driver none --pull never \
        --network none --read-only \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=512m \
        --tmpfs "/home/tblab:rw,nosuid,nodev,noexec,size=256m,uid=$LAB_UID,gid=$LAB_UID,mode=0700" \
        --tmpfs "/lab:rw,nosuid,nodev,noexec,size=1536m,uid=$LAB_UID,gid=$LAB_UID,mode=0700" \
        --shm-size 512m --cap-drop ALL --security-opt no-new-privileges:true --user "$LAB_UID:$LAB_UID" \
        --memory 3g --memory-swap 3g --cpus 2 --pids-limit 1024 --ulimit core=0:0 \
        "$IMAGE" 2>&1 \
    | head -c "$MAX_OUTPUT" | clean | sed -u 's/^/| /' | tee -a "$log" \
    | if [ "$VERBOSE" = 1 ]; then cat; else grep -a --line-buffered '^| \[lab ' || true; fi
  rc=${PIPESTATUS[1]}
  set -e
  docker kill "$CNAME" >/dev/null 2>&1 || true
  CNAME=""
  if [ -s "$WORK/tar-$i.err" ]; then
    printf '[run-lab] tar reported:\n' | tee -a "$log"
    head -c 4000 "$WORK/tar-$i.err" | clean | sed 's/^/[run-lab]   /' | tee -a "$log"
  fi
  printf '[run-lab] container exit status %s\n' "$rc" | tee -a "$log"

  # Verdict: taken from the ONE block tagged with this run's nonce; everything else is container data.
  set +e
  python3 - "$log" "$nonce" "$out" "$SUMMARY" "$base" <<'PY' | clean | tee -a "$log"
import json, sys
log, nonce, out, summary, label = sys.argv[1:6]
B = "| ===== THUNDERBIRD-MCP-LAB RESULT JSON %s =====" % nonce
E = "| ===== THUNDERBIRD-MCP-LAB END %s =====" % nonce
blocks, cur = [], None
with open(log, encoding="utf-8", errors="replace") as f:
    for line in f.read().split("\n"):
        if line == B:
            cur = []
        elif line == E and cur is not None:
            blocks.append(cur)
            cur = None
        elif cur is not None:
            cur.append(line[2:] if line.startswith("| ") else line)

def row(result, s=None, errors="", path=""):
    s = s or {}
    with open(summary, "a", encoding="utf-8") as f:
        f.write("\t".join(str(x) for x in (label, result, s.get("pass", ""), s.get("fail", ""), s.get("xfail", ""),
                                           s.get("xpass", ""), errors, path)) + "\n")

if len(blocks) != 1:
    print("[run-lab] verdict: %d tagged result block(s) instead of 1 -> result UNUSABLE" % len(blocks))
    row("UNUSABLE")
    sys.exit(3)
try:
    d = json.loads("\n".join(blocks[0]))
    assert isinstance(d, dict)
except Exception as e:
    print("[run-lab] verdict: tagged block unreadable (%s) -> result UNUSABLE" % type(e).__name__)
    row("UNUSABLE")
    sys.exit(3)
with open(out, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=True, indent=1)
s = d.get("summary") or {}
errors = d.get("errors") or []
result = "PASS" if d.get("ok") is True else "FAIL"
print("[run-lab] verdict: %s | checks pass %s, fail %s, xfail %s, xpass %s | errors %d | JSON: %s"
      % (result, s.get("pass", 0), s.get("fail", 0), s.get("xfail", 0), s.get("xpass", 0), len(errors), out))
for e in errors[:5]:
    print("[run-lab]   error: %s" % str(e)[:300].replace("\n", " "))
for c in d.get("checks") or []:
    if c.get("status") in ("fail", "xpass"):
        chk = c.get("check") or {}
        what = " ".join("%s=%s" % (k, chk[k]) for k in ("step", "subject", "name", "tool", "header") if k in chk)
        print("[run-lab]   %s: %s %s | %s" % (c["status"].upper(), chk.get("type"), what, str(c.get("detail"))[:200].replace("\n", " ")))
row(result, s, len(errors), out)
sys.exit(0 if result == "PASS" else 1)
PY
  vrc=${PIPESTATUS[0]}
  set -e
  if [ "$vrc" -eq 0 ]; then :; elif [ "$vrc" -eq 1 ]; then [ "$worst" -ge 1 ] || worst=1; else
    worst=3
    [ "$VERBOSE" = 1 ] || { printf '[run-lab] last lines of %s:\n' "$log"; tail -n 25 "$log"; }
  fi
done

printf '\n[run-lab] summary (%s):\n' "$SUMMARY"
cut -f1-7 "$SUMMARY" | clean | column -t -s "$(printf '\t')" 2>/dev/null || cut -f1-7 "$SUMMARY" | clean
printf '[run-lab] reminder: results are data produced next to the code under test (same uid as the harness), not proof.\n'
exit "$worst"
