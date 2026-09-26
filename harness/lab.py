#!/usr/bin/env python3
"""End-to-end lab harness for thunderbird-mcp.

Runs inside a disposable container started with --network none --read-only --cap-drop ALL, as an unprivileged user
(see run-lab.sh). Nothing is read from the host except what arrives on standard input:

    line 1   a 32-character lowercase hex nonce chosen by the launcher
    line 2   the byte length N of the scenario (decimal, at most 1 000 000)
    N bytes  the scenario (JSON)
    rest     a tar stream of a thunderbird-mcp source tree (extension/ + mcp-bridge.cjs at its root)

Flow: mailpit (SMTP + POP3 + HTTP API on 127.0.0.1) -> Xvfb -> throw-away profile (user.js) + an XPI built from
extension/ -> Thunderbird -> wait for the extension's connection file -> JSON-RPC MCP session with the tree's own
"node mcp-bridge.cjs" -> inspection of drafts (mbox), of mailpit (raw source) and, on request, of profile files and
prefs.js -> checks.

Output: progress on stderr ("[lab ...]"); the result is ONE JSON object (ASCII, CR/LF escaped) on stdout, between two
marker lines that carry the nonce.

Process layout: run-lab.sh starts the image without --init, so this script is PID 1 of the container. It makes itself
non-dumpable, then forks: PID 1 stays a minimal init (it only reaps orphaned processes and exits with the harness's
status) and the child is the harness proper. Only these two processes hold the container's standard input and output,
and both are non-dumpable before any code under test starts, so code running as the same uid cannot open them through
/proc/<pid>/fd nor read their memory. The code under test can still influence what the harness observes (it runs as
the same uid and can write the files the harness inspects): the security boundary is the container, not the harness,
and results are data, not proof.
"""
import base64
import email.header
import glob
import hashlib
import json
import os
import poplib
import queue
import re
import shutil
import signal
import smtplib
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

HARNESS = "thunderbird-mcp-lab harness 1.0"
T0 = time.monotonic()
DEADLINE = T0 + 560                     # seconds; run-lab.sh stops the container after 900 s
LAB = "/lab"
SRC = LAB + "/src"
PROF = LAB + "/profile"
LOGS = "/tmp/lab-logs"
USERJS_BASE = "/opt/tblab/user.js"
TB_DIR = "/opt/thunderbird"
CONN = "/tmp/thunderbird-mcp/connection.json"   # written by the extension once its MCP server listens
API = "http://127.0.0.1:8025/api/v1"
POP3_USER = "lab"
MAX_TEXT = 20000
MAX_OUT = 3_000_000
MAX_SCENARIO = 1_000_000
EXPECTED_ID = "thunderbird-mcp@tkasperczyk.dev"
MARK_BEGIN = "===== THUNDERBIRD-MCP-LAB RESULT JSON %s ====="
MARK_END = "===== THUNDERBIRD-MCP-LAB END %s ====="
NONCE = ""          # read from the first input line; authenticates the result block
SCENARIO_RAW = b""  # read from the input right after the nonce

R = {"harness": HARNESS, "ok": False, "errors": [], "phases": {}}
PROCS = {}
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _read_line(maxlen):
    """Reads one line from fd 0, byte by byte (no buffering: the tar stream that follows stays intact)."""
    buf = b""
    while len(buf) <= maxlen:
        c = os.read(0, 1)
        if not c or c == b"\n":
            return buf
        buf += c
    return None


def make_nondumpable():
    """PR_SET_DUMPABLE 0. Processes of the same uid (the code under test) can then neither read this process's memory
    nor open its file descriptors through /proc: the kernel would require CAP_SYS_PTRACE, which nobody has in the
    container. The flag is inherited by fork() (not by execve()). Returns True when the kernel confirms it."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(4, 0, 0, 0, 0)                   # PR_SET_DUMPABLE = 4
        return libc.prctl(3, 0, 0, 0, 0) == 0       # PR_GET_DUMPABLE = 3
    except Exception:
        return False


def run_as_init():
    """When this script is PID 1 of the container (run-lab.sh does not use --init), it makes itself non-dumpable and
    forks. The parent stays PID 1: a minimal init that only reaps processes (orphans are re-parented to it) and exits
    with the harness's status; it never reads or writes anything. The child returns and runs the harness.
    A dumpable PID 1 such as docker-init would leave the container's standard input and output open to the code under
    test through /proc/1/fd (it could then read the result block off the output pipe, or write a forged one)."""
    if os.getpid() != 1:
        return False
    make_nondumpable()
    child = os.fork()
    if child == 0:
        return True
    status = 1
    while True:
        try:
            pid, st = os.wait()
        except ChildProcessError:
            break
        if pid == child:
            code = os.waitstatus_to_exitcode(st)
            status = code if code >= 0 else 128 - code
            break
    os._exit(status)


def read_header():
    """Nonce, then the scenario. The process is already non-dumpable when it runs under the PID 1 init (run_as_init);
    it is made so here too in case the image was started some other way."""
    global NONCE, SCENARIO_RAW
    line = _read_line(64)
    nonce = (line or b"").decode("ascii", "replace")
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        sys.stdout.write("missing or invalid nonce: stopping\n")
        sys.stdout.flush()
        sys.exit(2)
    NONCE = nonce
    R["nondumpable"] = make_nondumpable()
    line = _read_line(10)
    if line is None or not re.fullmatch(rb"[0-9]{1,7}", line) or int(line) > MAX_SCENARIO:
        raise ValueError("scenario length line missing or invalid (at most %d bytes)" % MAX_SCENARIO)
    n, chunks = int(line), []
    while n > 0:
        c = os.read(0, min(n, 65536))
        if not c:
            raise ValueError("input ended inside the scenario")
        chunks.append(c)
        n -= len(c)
    SCENARIO_RAW = b"".join(chunks)


def log(msg):
    sys.stderr.write("[lab %6.1fs] %s\n" % (time.monotonic() - T0, msg))
    sys.stderr.flush()


def clip(s, n=MAX_TEXT):
    if s is None:
        return None
    if len(s) > n:
        return s[:n] + "...[truncated: %d characters in total]" % len(s)
    return s


def left(cap):
    return max(0.5, min(cap, DEADLINE - time.monotonic()))


def phase(name, **info):
    info["t"] = round(time.monotonic() - T0, 1)
    R["phases"][name] = info
    log("%s: %s" % (name, json.dumps(info, ensure_ascii=True)[:300]))


def fail(msg):
    R["errors"].append(msg)
    log("ERROR: " + msg)


def sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def tail(path, nlines=60, nchars=8000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200000))
            data = f.read().decode("utf-8", "replace")
        return clip("\n".join(data.splitlines()[-nlines:]), nchars)
    except OSError:
        return None


def start(name, argv, env=None, cwd=None, stdin=subprocess.DEVNULL, stdout=None):
    logf = open(os.path.join(LOGS, name + ".log"), "wb")
    p = subprocess.Popen(argv, env=env, cwd=cwd, stdin=stdin, stdout=stdout if stdout is not None else logf,
                         stderr=logf, start_new_session=True, close_fds=True)
    PROCS[name] = p
    return p


def stop(name, sig=signal.SIGKILL):
    p = PROCS.get(name)
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, sig)
        except OSError:
            pass


def http(method, url, timeout=10):
    req = urllib.request.Request(url, method=method)
    with HTTP.open(req, timeout=timeout) as r:
        return r.status, r.read(20_000_000)


def http_json(url, timeout=10):
    st, body = http("GET", url, timeout)
    return json.loads(body)


def wait_for(pred, timeout, step=0.25):
    end = time.monotonic() + left(timeout)
    while time.monotonic() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return None


# ------------------------------------------------------------------------------------------------ scenario
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
PREF_RE = re.compile(r"^[A-Za-z0-9._@{}-]{1,200}$")
# mbox folder to seed, relative to <profile>/Mail: 2 to 4 segments, no leading dot (e.g. "Local Folders/Inbox").
SEED_FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}(/[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}){1,3}$")
# Environment passed to the bridge only: the project's own namespace (no NODE_OPTIONS, LD_*, PATH...).
BRIDGE_ENV_RE = re.compile(r"THUNDERBIRD_MCP_[A-Z0-9_]{1,64}")   # fullmatch ("$" would accept a trailing \n)
PRINTABLE_RE = re.compile(r"[ -~]{0,500}")
XPI_REL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}(/[A-Za-z0-9][A-Za-z0-9._-]{0,63}){0,3}\.xpi")      # fullmatch
SEED_FILE_RE = re.compile(r"Mail/[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}/msgFilterRules\.dat")                       # fullmatch
PROFILE_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}(/[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}){0,4}")     # fullmatch
GEN_KEY = "$lab_base64"
MAX_GEN = 32 * 1024 * 1024
TOOLS = {}       # full tools/list definitions (for tool_json_* checks), not copied into the result
FULLTEXT = {}    # full text of tool responses, per step (for checks), not copied into the result
FILES_FULL = {}  # full content of inspected profile files (for checks), not copied into the result
MAILPIT_FULL = []  # raw messages captured by mailpit (for checks), not copied into the result

TOP_KEYS = {"name", "description", "tb_sandbox", "startup_timeout", "prefs", "schema_watch", "steps", "inspect",
            "checks", "seed", "watch_prefs", "bridge_env", "tools_dump", "xpi", "seed_files"}
STEP_KEYS = {"id", "tool", "arguments", "timeout", "force_call", "batch", "wait_after", "retry", "draft_subject",
             "draft_wait", "probes", "part_probes", "note"}
CHECK_TYPES = {"tool_ok", "tools_list_has", "draft_exists", "draft_header_contains", "schema_has", "mailpit_total",
               "tool_error_contains", "draft_count", "draft_body_contains", "prefs_js_line", "tool_json_equals",
               "tool_json_contains", "tool_result_contains", "listed_filter", "filter_rule", "file_contains",
               "draft_attachment", "mailpit_header_contains", "mailpit_body_contains"}


def load_scenario(raw):
    sc = json.loads(raw)
    if not isinstance(sc, dict):
        raise ValueError("scenario: a JSON object is expected")
    unknown = sorted(set(sc) - TOP_KEYS)
    if unknown:
        raise ValueError("scenario: unknown key(s) %s" % unknown)
    if not isinstance(sc.get("tb_sandbox", True), bool):
        raise ValueError("tb_sandbox: boolean")
    su = sc.get("startup_timeout", 150)
    if not isinstance(su, int) or isinstance(su, bool) or not 10 <= su <= 540:
        raise ValueError("startup_timeout: integer 10-540")
    steps = sc.get("steps", [])
    if not isinstance(steps, list) or len(steps) > 50:
        raise ValueError("steps: a list of at most 50 items")
    ids = set()
    for st in steps:
        if not isinstance(st, dict) or not NAME_RE.match(str(st.get("id", ""))) or not TOOL_RE.match(str(st.get("tool", ""))):
            raise ValueError("invalid step (id, tool)")
        if st["id"] in ids:
            raise ValueError("step %s: duplicate id" % st["id"])
        ids.add(st["id"])
        unknown = sorted(set(st) - STEP_KEYS)
        if unknown:
            raise ValueError("step %s: unknown key(s) %s" % (st["id"], unknown))
        if not isinstance(st.get("arguments", {}), dict):
            raise ValueError("step %s: arguments must be an object" % st["id"])
        t = st.get("timeout", 60)
        if not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= 180:
            raise ValueError("step %s: timeout 1-180" % st["id"])
        if not isinstance(st.get("force_call", False), bool):
            raise ValueError("step %s: force_call must be a boolean" % st["id"])
        if "batch" in st and not NAME_RE.match(str(st["batch"])):
            raise ValueError("step %s: batch = a name [A-Za-z0-9._-]" % st["id"])
        w = st.get("wait_after", 0)
        if not isinstance(w, int) or isinstance(w, bool) or not 0 <= w <= 120:
            raise ValueError("step %s: wait_after 0-120" % st["id"])
        dw = st.get("draft_wait", 30)
        if not isinstance(dw, int) or isinstance(dw, bool) or not 1 <= dw <= 120:
            raise ValueError("step %s: draft_wait 1-120" % st["id"])
        if "draft_subject" in st and (not isinstance(st["draft_subject"], str) or not st["draft_subject"]):
            raise ValueError("step %s: draft_subject must be a non-empty string" % st["id"])
        rt = st.get("retry")
        if rt is not None:
            if "batch" in st or not isinstance(rt, dict) or not isinstance(rt.get("attempts"), int) \
                    or not 1 <= rt["attempts"] <= 20 or not isinstance(rt.get("delay", 2), int) or not 1 <= rt.get("delay", 2) <= 30:
                raise ValueError("step %s: retry = {attempts 1-20, delay 1-30}, not in a batch" % st["id"])
        for key in ("probes", "part_probes"):
            pp = st.get(key, [])
            if not isinstance(pp, list) or len(pp) > 10 or not all(
                    isinstance(p, dict) and isinstance(p.get("header"), str) and isinstance(p.get("needle"), str) for p in pp):
                raise ValueError("step %s: %s = at most 10 {header, needle}" % (st["id"], key))
    be = sc.get("bridge_env", {})
    if not isinstance(be, dict) or len(be) > 10 or not all(
            BRIDGE_ENV_RE.fullmatch(str(k)) and isinstance(v, str) and PRINTABLE_RE.fullmatch(v) for k, v in be.items()):
        raise ValueError("bridge_env: at most 10 THUNDERBIRD_MCP_[A-Z0-9_] variables, printable ASCII values <= 500")
    for key in ("tools_dump", "schema_watch"):
        td = sc.get(key, [])
        if not isinstance(td, list) or len(td) > 20 or not all(isinstance(t, str) and TOOL_RE.match(t) for t in td):
            raise ValueError("%s: at most 20 tool names" % key)
    prefs = sc.get("prefs", {})
    if not isinstance(prefs, dict):
        raise ValueError("prefs: an object is expected")
    for k, v in prefs.items():
        if not PREF_RE.match(k) or not isinstance(v, (bool, int, str)) or (isinstance(v, str) and len(v) > 2000):
            raise ValueError("preference refused: %r" % k)
    wp = sc.get("watch_prefs", [])
    if not isinstance(wp, list) or len(wp) > 20 or not all(isinstance(p, str) and PREF_RE.match(p) for p in wp):
        raise ValueError("watch_prefs: at most 20 preference prefixes")
    seed = sc.get("seed", [])
    if not isinstance(seed, list) or len(seed) > 10:
        raise ValueError("seed: a list of at most 10 folders")
    total = 0
    for s in seed:
        folder = s.get("folder") if isinstance(s, dict) else None
        if not isinstance(folder, str) or not SEED_FOLDER_RE.match(folder) or ".." in folder:
            raise ValueError("seed: folder refused %r (e.g. \"127.0.0.1/Inbox\")" % folder)
        msgs = s.get("messages")
        if not isinstance(msgs, list) or not 1 <= len(msgs) <= 20 or not all(isinstance(m, str) for m in msgs):
            raise ValueError("seed %s: messages = 1 to 20 strings" % folder)
        total += sum(len(m) for m in msgs)
    if total > 5_000_000:
        raise ValueError("seed: at most 5 MB")
    xp = sc.get("xpi")
    if xp is not None and (not isinstance(xp, str) or not XPI_REL_RE.fullmatch(xp) or ".." in xp):
        raise ValueError("xpi: a path relative to the received tree, [A-Za-z0-9._-] and /, ending in .xpi")
    sf = sc.get("seed_files", [])
    if not isinstance(sf, list) or len(sf) > 5 or not all(
            isinstance(x, dict) and isinstance(x.get("path"), str) and SEED_FILE_RE.fullmatch(x["path"])
            and ".." not in x["path"] and isinstance(x.get("content"), str) and len(x["content"]) <= 200_000 for x in sf):
        raise ValueError("seed_files: at most 5 {path: Mail/<server folder>/msgFilterRules.dat, content <= 200 000}")
    ins = sc.get("inspect", {})
    if not isinstance(ins, dict) or set(ins) - {"prefs", "files"}:
        raise ValueError("inspect: an object with \"prefs\" and/or \"files\"")
    ip = ins.get("prefs", [])
    if not isinstance(ip, list) or len(ip) > 20 or not all(isinstance(p, str) and PREF_RE.match(p) for p in ip):
        raise ValueError("inspect.prefs: at most 20 preference prefixes")
    fi = ins.get("files", [])
    if not isinstance(fi, list) or len(fi) > 10 or not all(
            isinstance(x, str) and PROFILE_FILE_RE.fullmatch(x) and ".." not in x for x in fi):
        raise ValueError("inspect.files: at most 10 paths relative to the profile")
    checks = sc.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 200:
        raise ValueError("checks: a list of 1 to 200 checks")
    for c in checks:
        if not isinstance(c, dict) or c.get("type") not in CHECK_TYPES:
            raise ValueError("unknown check type: %r" % (c.get("type") if isinstance(c, dict) else c))
        if "step" in c and c["step"] not in ids:
            raise ValueError("check %s: unknown step %r" % (c["type"], c["step"]))
        for k in ("xfail", "note"):
            if k in c and (not isinstance(c[k], str) or not 1 <= len(c[k]) <= 500):
                raise ValueError("check %s: %s must be a string of 1-500 characters" % (c["type"], k))
    for st in steps:
        expand_args(st.get("arguments", {}), "", None, generate=False)   # validates generators
    return sc


def expand_args(obj, path, notes, generate=True):
    """Replaces every object {"$lab_base64": {"bytes": N, "byte": B}} (its only key) with the base64 string of N bytes
    of value B (default 97, "a") when the call is sent. The result keeps the compact form; "notes" receives the size,
    base64 length and SHA-256 of the generated bytes."""
    if isinstance(obj, dict):
        if set(obj) == {GEN_KEY}:
            spec = obj[GEN_KEY]
            if not isinstance(spec, dict) or set(spec) - {"bytes", "byte"} or not isinstance(spec.get("bytes"), int) \
                    or isinstance(spec.get("bytes"), bool) or not 1 <= spec["bytes"] <= MAX_GEN \
                    or not isinstance(spec.get("byte", 97), int) or not 0 <= spec.get("byte", 97) <= 255:
                raise ValueError("%s: %s = {bytes 1-%d, byte 0-255}" % (path or "arguments", GEN_KEY, MAX_GEN))
            if not generate:
                return obj
            raw = bytes([spec.get("byte", 97)]) * spec["bytes"]
            b64 = base64.b64encode(raw).decode("ascii")
            if notes is not None:
                notes.append({"path": path, "bytes": spec["bytes"], "byte": spec.get("byte", 97),
                              "base64_length": len(b64), "sha256": hashlib.sha256(raw).hexdigest()})
            return b64
        return {k: expand_args(v, "%s.%s" % (path, k) if path else k, notes, generate) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_args(v, "%s[%d]" % (path, i), notes, generate) for i, v in enumerate(obj)]
    return obj


def pref_line(k, v):
    if isinstance(v, bool):
        val = "true" if v else "false"
    elif isinstance(v, int):
        val = str(v)
    else:
        val = json.dumps(v, ensure_ascii=True)
    return 'user_pref(%s, %s);\n' % (json.dumps(k), val)


# ------------------------------------------------------------------------------------------------ mbox
FROM_TB = re.compile(rb"^From - \w{3} \w{3} ")


def split_mbox(data):
    lines = data.split(b"\n")
    msgs, cur = [], None
    for i, ln in enumerate(lines):
        full = ln + (b"\n" if i < len(lines) - 1 else b"")
        prev_blank = i == 0 or lines[i - 1] in (b"", b"\r")
        if ln.startswith(b"From ") and (prev_blank or FROM_TB.match(ln)):
            cur = {"from_line": full, "lines": []}
            msgs.append(cur)
            continue
        if cur is not None:
            cur["lines"].append(full)
    return msgs


HDR_NAME = re.compile(rb"^([!-9;-~]+):")


def analyse_message(m):
    hdr, body, in_hdr = [], [], True
    for full in m["lines"]:
        content = full[:-1] if full.endswith(b"\n") else full
        if in_hdr and content in (b"", b"\r"):
            in_hdr = False
            continue
        (hdr if in_hdr else body).append(full)
    fields = []
    for line in hdr:
        content = line.rstrip(b"\n")
        if content[:1] in (b" ", b"\t") and fields:
            fields[-1]["cont"].append(line)
            continue
        mm = HDR_NAME.match(content)
        fields.append({"name": mm.group(1).decode("latin-1") if mm else None, "first": line, "cont": []})
    bare_cr = [l.decode("utf-8", "replace") for l in hdr if b"\r" in l.rstrip(b"\n").rstrip(b"\r")]
    subject = ""
    for f in fields:
        if (f["name"] or "").lower() == "subject":
            subject = unfold(f)
    raw_hdr = b"".join(hdr)
    return {
        "hdr": hdr, "fields": fields, "body": b"".join(body), "subject": subject,
        "view": {
            "from_line": m["from_line"].decode("utf-8", "replace"),
            "raw_headers": clip(raw_hdr.decode("utf-8", "replace"), 30000),
            "header_lines": [l.decode("utf-8", "replace") for l in hdr][:200],
            "header_names": [f["name"] for f in fields],
            "lines_with_bare_cr": bare_cr[:20],
            "body_excerpt": clip(b"".join(body).decode("utf-8", "replace"), 2000),
        },
    }


def parse_raw(raw):
    """A raw RFC 5322 message (bytes) analysed like an mbox entry."""
    parts = raw.split(b"\n")
    lines = [p + b"\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    return analyse_message({"from_line": b"", "lines": lines})


def unfold(f):
    raw = f["first"] + b"".join(f["cont"])
    val = raw.split(b":", 1)[1] if b":" in raw else raw
    return re.sub(rb"\r?\n", b"", val).strip().decode("utf-8", "replace")


def header_values(msg, name):
    return [unfold(f) for f in msg["fields"] if (f["name"] or "").lower() == str(name).lower()]


SEVERITY = ["OWN_HEADER", "IN_BODY", "FOLDED", "EMBEDDED", "RFC2047_ENCODED", "ABSENT"]


def probe(msg, header, needle):
    """Where did a given string end up in a saved message? Every occurrence is listed, then the first status in this
    order: OWN_HEADER (a standalone "Name:" line of the named header) > IN_BODY (after the end of the header block) >
    FOLDED (continuation line of some header) > EMBEDDED (same line as another header) > RFC2047_ENCODED > ABSENT."""
    nb = needle.encode("utf-8")
    occ = []
    for f in msg["fields"]:
        name = f["name"] or "?"
        if name.lower() == header.lower() and nb in (f["first"] + b"".join(f["cont"])):
            occ.append({"status": "OWN_HEADER", "carrier": name, "line": f["first"].decode("utf-8", "replace")})
            continue
        for c in f["cont"]:
            if nb in c:
                occ.append({"status": "FOLDED", "carrier": name, "line": c.decode("utf-8", "replace")})
        if nb in f["first"]:
            occ.append({"status": "EMBEDDED", "carrier": name, "line": f["first"].decode("utf-8", "replace")})
        raw = f["first"] + b"".join(f["cont"])
        if b"=?" in raw and nb not in raw:
            try:
                dec = str(email.header.make_header(email.header.decode_header(unfold(f))))
            except Exception:
                dec = ""
            if needle in dec:
                occ.append({"status": "RFC2047_ENCODED", "carrier": name, "line": raw.decode("utf-8", "replace")})
    if nb in msg["body"]:
        occ.append({"status": "IN_BODY", "carrier": None, "line": None})
    status = min((o["status"] for o in occ), key=SEVERITY.index) if occ else "ABSENT"
    return {"header": header, "needle": needle, "status": status, "occurrences": occ[:10]}


def mbox_files():
    out = []
    for p in glob.glob(PROF + "/Mail/**/*", recursive=True):
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        base = os.path.basename(p)
        if base.endswith((".msf", ".dat", ".json", ".html")) or base in ("filterlog.html",):
            continue
        out.append(p)
    return sorted(out)


def read_mbox(path):
    with open(path, "rb") as f:
        data = f.read(20_000_000)
    return [analyse_message(m) for m in split_mbox(data)]


def drafts():
    res = []
    for p in mbox_files():
        if os.path.basename(p) == "Drafts":
            for m in read_mbox(p):
                m["folder"] = os.path.relpath(p, PROF)
                res.append(m)
    return res


BOUNDARY_RE = re.compile(r'boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))', re.I)


def mime_parts(msg):
    """MIME parts of an analysed message: the RAW body is split on the boundary of its Content-Type ("--boundary"
    lines, CRs kept), and each part is analysed again as a message (part headers + body)."""
    ct = ""
    for f in msg["fields"]:
        if (f["name"] or "").lower() == "content-type":
            ct = unfold(f)
    mm = BOUNDARY_RE.search(ct)
    if not mm:
        return []
    delim = b"--" + (mm.group(1) or mm.group(2)).encode("utf-8", "replace")
    parts, cur = [], None
    lines = msg["body"].split(b"\n")
    for i, ln in enumerate(lines):
        full = ln + (b"\n" if i < len(lines) - 1 else b"")
        s = ln.rstrip(b"\r \t")
        if s in (delim, delim + b"--"):
            if cur is not None:
                parts.append(cur)
            cur = [] if s == delim else None
            if s != delim:
                break
            continue
        if cur is not None:
            cur.append(full)
    return [analyse_message({"from_line": b"", "lines": p}) for p in parts[:30]]


def part_report(msg, probes, depth=0, prefix=""):
    """"part_probes": for every part (recursive, 3 levels) its raw headers and the probes, with the probe() statuses
    applied to THE PART'S HEADERS (OWN_HEADER = standalone "Name:" line in the part header block)."""
    res = []
    for k, pm in enumerate(mime_parts(msg), 1):
        path = "%s%d" % (prefix, k)
        v = pm["view"]
        res.append({"part": path, "header_names": v["header_names"], "header_lines": v["header_lines"][:40],
                    "lines_with_bare_cr": v["lines_with_bare_cr"],
                    "probes": [probe(pm, p["header"], p["needle"]) for p in probes]})
        if depth < 2:
            res.extend(part_report(pm, probes, depth + 1, path + "."))
    return res[:30]


def all_parts(msg, depth=0):
    out = []
    for pm in mime_parts(msg):
        out.append(pm)
        if depth < 2:
            out += all_parts(pm, depth + 1)
    return out


# ------------------------------------------------------------------------------------------------ MCP bridge
class Bridge:
    def __init__(self, extra_env=None):
        env = {"HOME": os.environ.get("HOME", "/home/tblab"), "PATH": "/usr/local/bin:/usr/bin:/bin",
               "LANG": "C.UTF-8", "TMPDIR": "/tmp", "TZ": "UTC"}
        if extra_env:
            # "bridge_env": THUNDERBIRD_MCP_* variables (validated at load time) for the BRIDGE only;
            # Thunderbird and its extension do not see them.
            env.update(extra_env)
            phase("bridge", env=dict(sorted(extra_env.items())))
        self.p = start("bridge", ["node", SRC + "/mcp-bridge.cjs"], env=env, cwd=SRC,
                       stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.q = queue.Queue()
        self.notes = []
        self.next_id = 1
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            line = self.p.stdout.readline(30_000_000)
            if not line:
                self.q.put(None)
                return
            self.q.put(line)

    def send(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def send_request(self, method, params):
        rid = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return rid

    def wait_responses(self, rids, timeout):
        """Waits for the responses to the given ids (sent together for a batch): {id: (response, arrival time)}."""
        want, got = set(rids), {}
        end = time.monotonic() + left(timeout)
        while want - set(got):
            rem = end - time.monotonic()
            if rem <= 0:
                break
            try:
                line = self.q.get(timeout=rem)
            except queue.Empty:
                break
            if line is None:
                for r in want - set(got):
                    got[r] = ({"_bridge_exit": self.p.poll()}, None)
                break
            try:
                msg = json.loads(line)
            except ValueError:
                self.notes.append(clip(line.decode("utf-8", "replace"), 500))
                continue
            if isinstance(msg, dict) and msg.get("id") in want and msg.get("id") not in got:
                got[msg["id"]] = (msg, round(time.monotonic() - T0, 2))
                continue
            self.notes.append(clip(json.dumps(msg, ensure_ascii=True), 1000))
        for r in want - set(got):
            got[r] = ({"_timeout": True}, None)
        return got

    def request(self, method, params, timeout):
        rid = self.send_request(method, params)
        return self.wait_responses([rid], timeout)[rid][0]


def full_text(resp):
    """Full text (<= 5 MB) of a tools/call response, for the checks."""
    res = (resp or {}).get("result") or {}
    if not isinstance(res, dict):
        return ""
    return "\n".join(c.get("text", "") for c in res.get("content", [])
                     if isinstance(c, dict) and c.get("type") == "text")[:5_000_000]


def summarize_call(resp):
    out = {}
    if resp.get("_timeout"):
        out.update(ok=False, error="timed out")
        return out
    if "_bridge_exit" in resp:
        out.update(ok=False, error="the bridge exited (code %s)" % resp["_bridge_exit"])
        return out
    if "error" in resp:
        out.update(ok=False, rpc_error=clip(json.dumps(resp["error"], ensure_ascii=True), 4000))
        return out
    res = resp.get("result") or {}
    texts = [c.get("text", "") for c in res.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
    text = "\n".join(texts)
    out["isError"] = bool(res.get("isError"))
    out["text"] = clip(text, 8000)
    tool_err = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("error"):
            tool_err = clip(str(parsed["error"]), 2000)
    except ValueError:
        pass
    if tool_err:
        out["tool_error"] = tool_err
    out["ok"] = not out["isError"] and not tool_err
    return out


# ------------------------------------------------------------------------------------------------ stages
def extract_source():
    os.makedirs(SRC, mode=0o700)
    p = subprocess.run(["tar", "-xf", "-", "-C", SRC, "--no-same-owner", "--no-same-permissions"],
                       stdin=sys.stdin.buffer, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=left(120))
    try:
        sys.stdin.close()
    except OSError:
        pass
    if p.returncode != 0:
        raise RuntimeError("tar extraction failed: " + p.stderr.decode("utf-8", "replace")[:500])
    for need in ("extension/manifest.json", "mcp-bridge.cjs", "package.json"):
        path = os.path.join(SRC, need)
        if os.path.islink(path) or not os.path.isfile(path):
            raise RuntimeError("expected file missing from the received tree: " + need)
    phase("source", files=sum(len(f) for _, _, f in os.walk(SRC)),
          sha256={k: sha256(os.path.join(SRC, k)) for k in ("extension/manifest.json", "extension/mcp_server/api.js",
                                                            "mcp-bridge.cjs")})


def _gecko(mf):
    gecko = (mf.get("browser_specific_settings") or mf.get("applications") or {}).get("gecko") or {}
    gid = str(gecko.get("id", ""))
    if not re.match(r"^[A-Za-z0-9._@{}-]{3,100}$", gid) or ".." in gid:
        raise RuntimeError("invalid gecko id in the manifest: %r" % gid)
    if gid != EXPECTED_ID:
        fail("unexpected gecko id: %s (expected %s); installed under the manifest's id" % (gid, EXPECTED_ID))
    return gecko, gid


def install_prebuilt_xpi(rel):
    """"xpi": installs a prebuilt XPI found in the received tree as is (e.g. a release artifact), instead of zipping
    extension/. The path is validated (inside the received tree, regular file, not a link); the archive is tested."""
    p = os.path.join(SRC, rel)
    if os.path.islink(p) or not os.path.isfile(p) or \
            not os.path.realpath(p).startswith(os.path.realpath(SRC) + "/"):
        raise RuntimeError("xpi: missing, a link, or outside the received tree: %r" % rel)
    if os.path.getsize(p) > 60_000_000:
        raise RuntimeError("xpi too large (> 60 MB)")
    with zipfile.ZipFile(p) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError("xpi: corrupt entry %r" % bad)
        names = z.namelist()
        mf = json.loads(z.read("manifest.json")[:1_000_000])
    gecko, gid = _gecko(mf)
    os.makedirs(PROF + "/extensions", mode=0o700)
    dst = "%s/extensions/%s.xpi" % (PROF, gid)
    shutil.copyfile(p, dst)
    phase("xpi", source="prebuilt", path=rel, id=gid, version=mf.get("version"), files=len(names),
          sha256=sha256(dst), strict_min_version=gecko.get("strict_min_version"))
    return gid, mf.get("version")


def build_xpi(sc):
    if sc.get("xpi"):
        return install_prebuilt_xpi(sc["xpi"])
    ext = os.path.join(SRC, "extension")
    if os.path.islink(ext) or not os.path.isdir(ext):
        raise RuntimeError("extension/ missing or a link")
    with open(os.path.join(ext, "manifest.json"), "rb") as f:
        mf = json.loads(f.read(1_000_000))
    gecko, gid = _gecko(mf)
    os.makedirs(PROF + "/extensions", mode=0o700)
    xpi = "%s/extensions/%s.xpi" % (PROF, gid)
    skipped, total, count = [], 0, 0
    with zipfile.ZipFile(xpi, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(ext, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(root, d)))
            for fn in sorted(files):
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, ext)
                if os.path.islink(p) or not os.path.isfile(p):
                    skipped.append(rel)
                    continue
                total += os.path.getsize(p)
                if total > 60_000_000:
                    raise RuntimeError("extension too large (> 60 MB)")
                z.write(p, rel)
                count += 1
    phase("xpi", source="extension/", id=gid, version=mf.get("version"), files=count, skipped=skipped[:20],
          sha256=sha256(xpi))
    return gid, mf.get("version")


def start_mailpit():
    with open(LAB + "/pop3-auth", "w") as f:
        f.write("%s:%s\n" % (POP3_USER, POP3_USER))
    start("mailpit", ["/usr/local/bin/mailpit", "--listen", "127.0.0.1:8025", "--smtp", "127.0.0.1:1025",
                      "--pop3", "127.0.0.1:1110", "--pop3-auth-file", LAB + "/pop3-auth",
                      "--smtp-auth-accept-any", "--smtp-auth-allow-insecure", "--smtp-disable-rdns",
                      "--database", LAB + "/mailpit.db", "--disable-version-check", "--max", "1000"])
    if not wait_for(lambda: http("GET", API + "/info", 2)[0] == 200, 30):
        raise RuntimeError("mailpit does not answer:\n" + (tail(LOGS + "/mailpit.log") or ""))
    # Self-check: a message sent over SMTP must show up in the API, and POP3 must accept the lab account.
    marker = "lab-selfcheck-%d" % os.getpid()
    with smtplib.SMTP("127.0.0.1", 1025, timeout=10) as s:
        s.sendmail("selfcheck@example.test", ["lab@example.test"],
                   ("From: selfcheck@example.test\r\nTo: lab@example.test\r\nSubject: %s\r\n\r\nok\r\n" % marker).encode())
    got = wait_for(lambda: http_json(API + "/search?query=" + marker).get("messages_count", 0) >= 1, 15)
    pop = None
    try:
        pc = poplib.POP3("127.0.0.1", 1110, timeout=10)
        pc.user(POP3_USER)
        pc.pass_(POP3_USER)
        pop = pc.stat()[0]
        pc.quit()
    except Exception as e:
        pop = "failed: %s" % e
    http("DELETE", API + "/messages", 10)
    phase("mailpit", smtp_to_api=bool(got), pop3_messages=pop, version=http_json(API + "/info").get("Version"))
    if not got:
        raise RuntimeError("mailpit self-check failed")


def start_xvfb():
    os.makedirs("/tmp/.X11-unix", exist_ok=True)
    os.chmod("/tmp/.X11-unix", 0o1777)
    start("xvfb", ["Xvfb", ":99", "-screen", "0", "1280x900x24", "-nolisten", "tcp", "-noreset"])
    if not wait_for(lambda: os.path.exists("/tmp/.X11-unix/X99"), 20):
        raise RuntimeError("Xvfb did not start:\n" + (tail(LOGS + "/xvfb.log") or ""))
    phase("xvfb", display=":99")


def seed_mbox(sc):
    """"seed": raw messages appended, BEFORE Thunderbird starts, to mbox files of the throw-away profile
    (e.g. "127.0.0.1/Inbox"), in Thunderbird's format ("From - ..." line, CRLF, escaped "From " lines).
    Thunderbird indexes them itself (no .msf file)."""
    out = {}
    for s in sc.get("seed", []):
        path = os.path.join(PROF, "Mail", s["folder"])
        if not os.path.realpath(path).startswith(os.path.realpath(PROF + "/Mail") + "/"):
            raise RuntimeError("seed: path outside the profile: %r" % s["folder"])
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "ab") as f:
            for raw in s["messages"]:
                lines = raw.replace("\r\n", "\n").split("\n")
                if lines and lines[-1] == "":
                    lines.pop()
                body = ["From - Thu Jan 01 00:00:00 2026"] + [(">" + l if l.startswith("From ") else l) for l in lines] + [""]
                f.write(("\r\n".join(body) + "\r\n").encode("utf-8"))
        out[s["folder"]] = {"messages": len(s["messages"]), "bytes": os.path.getsize(path)}
    if out:
        phase("seed", folders=out)


def seed_files(sc):
    """"seed_files": msgFilterRules.dat files written into the profile BEFORE Thunderbird starts (e.g. a rule
    created by hand in the filter editor). The path is validated and resolved under <profile>/Mail."""
    out = {}
    for x in sc.get("seed_files", []):
        path = os.path.join(PROF, x["path"])
        if not os.path.realpath(path).startswith(os.path.realpath(PROF + "/Mail") + "/"):
            raise RuntimeError("seed_files: path outside the profile: %r" % x["path"])
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(x["content"])
        out[x["path"]] = {"bytes": os.path.getsize(path), "sha256": sha256(path)}
    if out:
        phase("seed_files", files=out)


def make_profile(sc):
    os.makedirs(PROF + "/Mail/Local Folders", exist_ok=True)
    with open(USERJS_BASE) as f:
        base = f.read()
    extra = "".join(pref_line(k, v) for k, v in sc.get("prefs", {}).items())
    with open(PROF + "/user.js", "w") as f:
        f.write(base + "\n// --- Scenario preferences\n" + extra)
    phase("profile", scenario_prefs=len(sc.get("prefs", {})))
    seed_mbox(sc)
    seed_files(sc)


# ------------------------------------------------------------------------------------------------ prefs.js watch
class PrefWatch:
    """"watch_prefs": re-reads prefs.js every 0.2 s and records every change of the watched lines (prefixes), with
    the current step. Thunderbird rewrites prefs.js ~0.5 s after a change: a shorter-lived state can be missed."""

    def __init__(self, prefixes):
        self.prefixes = prefixes
        self.stage = "startup"
        self.timeline = []
        self.last = None
        self.stop_flag = threading.Event()
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def snapshot(self):
        lines = []
        try:
            with open(PROF + "/prefs.js", "rb") as f:
                data = f.read(5_000_000).decode("utf-8", "replace")
        except OSError:
            return None
        for line in data.splitlines():
            mm = re.match(r'^user_pref\("([^"]+)"', line)
            if mm and any(mm.group(1).startswith(p) for p in self.prefixes):
                lines.append(line)
        return lines

    def _run(self):
        while not self.stop_flag.is_set():
            snap = self.snapshot()
            if snap != self.last and len(self.timeline) < 300:
                self.timeline.append({"t": round(time.monotonic() - T0, 2), "step": self.stage,
                                      "prefs_js": "missing" if snap is None else snap})
                self.last = snap
            self.stop_flag.wait(0.2)

    def stop(self):
        self.stop_flag.set()
        self.th.join(timeout=2)
        snap = self.snapshot()
        if snap != self.last:
            self.timeline.append({"t": round(time.monotonic() - T0, 2), "step": self.stage,
                                  "prefs_js": "missing" if snap is None else snap})
        R["prefs_timeline"] = self.timeline


PW = None


def tb_version():
    info = {}
    try:
        with open(TB_DIR + "/application.ini") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k in ("Version", "BuildID", "SourceRepository", "SourceStamp"):
                    info[k] = v
    except OSError:
        pass
    return info


def start_tb(sc):
    os.makedirs("/tmp/xdg", mode=0o700, exist_ok=True)
    env = {"HOME": os.environ.get("HOME", "/home/tblab"), "PATH": "/usr/local/bin:/usr/bin:/bin",
           "LANG": "C.UTF-8", "TZ": "UTC", "TMPDIR": "/tmp", "DISPLAY": ":99", "XDG_RUNTIME_DIR": "/tmp/xdg",
           "MOZ_CRASHREPORTER_DISABLE": "1", "MOZ_DISABLE_AUTO_SAFE_MODE": "1", "MOZ_NO_REMOTE": "1",
           "DBUS_SESSION_BUS_ADDRESS": "disabled:", "NO_AT_BRIDGE": "1", "GSETTINGS_BACKEND": "memory",
           "MOZ_ENABLE_WAYLAND": "0"}
    sandbox = sc.get("tb_sandbox", True)
    if not sandbox:
        # Scenario option ("tb_sandbox": false). By default Thunderbird's own sandbox stays on: without user
        # namespaces (cap-drop ALL, Docker seccomp) it only partially initialises, but Thunderbird and the extension
        # work either way; the container remains the real boundary.
        for k in ("MOZ_DISABLE_CONTENT_SANDBOX", "MOZ_DISABLE_GMP_SANDBOX", "MOZ_DISABLE_RDD_SANDBOX",
                  "MOZ_DISABLE_SOCKET_PROCESS_SANDBOX", "MOZ_DISABLE_UTILITY_SANDBOX", "MOZ_DISABLE_GPU_SANDBOX"):
            env[k] = "1"
    start("thunderbird", [TB_DIR + "/thunderbird", "--profile", PROF, "--no-remote"], env=env)
    ok = wait_for(lambda: os.path.isfile(CONN) or PROCS["thunderbird"].poll() is not None,
                  sc.get("startup_timeout", 150), 0.5)
    if not ok or not os.path.isfile(CONN):
        raise RuntimeError("the extension never wrote its connection file (Thunderbird %s); end of its log:\n%s" % (
            "exited with code %s" % PROCS["thunderbird"].poll() if PROCS["thunderbird"].poll() is not None else "still running",
            tail(LOGS + "/thunderbird.log", 40) or ""))
    try:
        with open(CONN) as f:
            port = json.load(f).get("port")
    except Exception:
        port = None
    phase("thunderbird", version=tb_version(), content_sandbox=sandbox, port=port)


def node_version():
    try:
        return subprocess.run(["node", "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=10).stdout.decode("ascii", "replace").strip()
    except Exception:
        return None


def run_mcp(sc, br):
    init = br.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "thunderbird-mcp-lab", "version": "1"}}, 30)
    if "result" not in init:
        raise RuntimeError("initialize failed: " + clip(json.dumps(init, ensure_ascii=True), 2000))
    br.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    ires = init["result"]
    tl = br.request("tools/list", {}, 60)
    tools = (tl.get("result") or {}).get("tools") or []
    by_name = {t.get("name"): t for t in tools if isinstance(t, dict)}
    phase("mcp", server=ires.get("serverInfo"), protocol=ires.get("protocolVersion"), tools=len(tools),
          node=node_version(),
          schemas={n: sorted(((by_name.get(n) or {}).get("inputSchema") or {}).get("properties", {}).keys())
                   for n in sc.get("schema_watch", [])})
    R["tools"] = sorted(by_name)
    TOOLS.update(by_name)
    dump = sc.get("tools_dump", [])
    if dump:
        # Full definitions (description, inputSchema...) + a canonical fingerprint to compare two runs.
        R["tools_detail"] = {n: by_name.get(n) for n in dump}
        R["tools_detail_sha256"] = {n: hashlib.sha256(json.dumps(by_name[n], sort_keys=True, ensure_ascii=True)
                                                      .encode("ascii")).hexdigest() if n in by_name else None for n in dump}
    R["steps"] = []
    steps = sc.get("steps", [])
    i = 0
    while i < len(steps):
        # Consecutive steps with the same "batch": requests sent together, responses awaited in parallel.
        grp = [steps[i]]
        if steps[i].get("batch"):
            while i + len(grp) < len(steps) and steps[i + len(grp)].get("batch") == steps[i]["batch"]:
                grp.append(steps[i + len(grp)])
        i += len(grp)
        run_group(grp, by_name, br)
    if PW:
        PW.stage = "after_steps"
    if br.notes:
        R["bridge_other_messages"] = br.notes[:20]


def run_group(grp, by_name, br):
    entries = []
    for st in grp:
        entry = {"id": st["id"], "tool": st["tool"], "arguments": st.get("arguments", {})}
        if st.get("batch"):
            entry["batch"] = st["batch"]
        if st["tool"] not in by_name:
            entry["not_in_tools_list"] = True
            if not st.get("force_call"):
                entry.update(ok=False, error="tool not in tools/list")
        entries.append((st, entry))
    # "force_call": call a tool even when it is hidden (e.g. disabled by the user) to record the refusal.
    live = [(st, e) for st, e in entries if st["tool"] in by_name or st.get("force_call") is True]
    if PW:
        PW.stage = grp[0].get("batch") or grp[0]["id"]
        before = PW.snapshot()
        for _, e in live:
            e["prefs_before"] = before
    rt = grp[0].get("retry") if len(grp) == 1 and live else None
    attempts = rt["attempts"] if rt else 1
    for k in range(attempts):
        rids = {}
        for st, e in live:
            log("call %s (%s)%s" % (st["tool"], st["id"], " [batch %s]" % st["batch"] if st.get("batch") else ""))
            e["sent_at"] = round(time.monotonic() - T0, 2)
            gen = []
            args = expand_args(st.get("arguments", {}), "", gen)      # generators; the result keeps the compact form
            if gen:
                e["generated_arguments"] = gen
            rids[br.send_request("tools/call", {"name": st["tool"], "arguments": args})] = (st, e)
            del args
        got = br.wait_responses(list(rids), max([st.get("timeout", 60) for st, _ in live] or [1])) if rids else {}
        for rid, (st, e) in rids.items():
            resp, t = got[rid]
            e.update(summarize_call(resp))
            FULLTEXT[st["id"]] = full_text(resp)
            e["answered_at"] = t
        if rt:
            live[0][1]["attempts"] = k + 1
            if live[0][1].get("ok") or k + 1 == attempts:
                break
            time.sleep(min(rt.get("delay", 2), max(0.0, DEADLINE - time.monotonic())))
    if PW:
        at = PW.snapshot()
        for _, e in live:
            e["prefs_at_response"] = at
    for st, entry in entries:
        subj = st.get("draft_subject")
        if subj:
            found = wait_for(lambda: [m for m in drafts() if subj in m["subject"]], st.get("draft_wait", 30), 0.5)
            if found:
                m = found[-1]
                entry["draft"] = dict(m["view"], folder=m["folder"])
                entry["probes"] = [probe(m, p["header"], p["needle"]) for p in st.get("probes", [])]
                if st.get("part_probes"):
                    entry["mime_parts"] = part_report(m, st["part_probes"])
            else:
                entry["draft"] = None
                entry["probes"] = [{"header": p["header"], "needle": p["needle"], "status": "NO_DRAFT"}
                                   for p in st.get("probes", [])]
    w = max(st.get("wait_after", 0) for st in grp)
    if w:
        log("waiting %d s after %s" % (w, grp[0].get("batch") or grp[0]["id"]))
        time.sleep(min(w, max(0.0, DEADLINE - time.monotonic() - 30)))
        if PW:
            after = PW.snapshot()
            for _, e in live:
                e["prefs_after_wait"] = after
    R["steps"].extend(e for _, e in entries)


def inspect(sc):
    ins = sc.get("inspect", {})
    ds = drafts()
    R["drafts"] = [dict(m["view"], folder=m["folder"]) for m in ds[:20]]
    R["mbox_folders"] = {os.path.relpath(p, PROF): os.path.getsize(p) for p in mbox_files()}
    mp = []
    try:
        lst = http_json(API + "/messages?limit=50")
        for m in (lst.get("messages") or [])[:20]:
            st, raw = http("GET", "%s/message/%s/raw" % (API, m.get("ID")), 10)
            MAILPIT_FULL.append(dict(parse_raw(raw), mailpit_subject=m.get("Subject") or ""))
            head = raw.split(b"\r\n\r\n", 1)[0] if b"\r\n\r\n" in raw else raw.split(b"\n\n", 1)[0]
            mp.append({"ID": m.get("ID"), "Subject": m.get("Subject"),
                       "From": m.get("From"), "To": m.get("To"), "Cc": m.get("Cc"), "Bcc": m.get("Bcc"),
                       "raw_headers": clip(head.decode("utf-8", "replace"), 20000)})
        R["mailpit"] = {"total": lst.get("total"), "messages": mp}
    except Exception as e:
        R["mailpit"] = {"error": str(e)[:500]}
    files = ins.get("files", [])
    if files:
        # Profile files (paths validated at load time), read as is; msgFilterRules.dat is also parsed.
        R["files"], R["filter_rules"] = {}, {}
        for rel in files:
            p = os.path.join(PROF, rel)
            if os.path.islink(p) or not os.path.realpath(p).startswith(os.path.realpath(PROF) + "/"):
                R["files"][rel] = {"error": "a link, or outside the profile"}
                continue
            try:
                with open(p, "rb") as f:
                    data = f.read(2_000_000)
            except OSError as e:
                R["files"][rel] = {"error": str(e)[:300]}
                continue
            text = data.decode("utf-8", "replace")
            FILES_FULL[rel] = text
            R["files"][rel] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "content": clip(text, 100_000)}
            if rel.endswith("msgFilterRules.dat"):
                R["filter_rules"][rel] = parse_filter_rules(text)
    prefixes = ins.get("prefs", [])
    if prefixes:
        # Clean shutdown (SIGTERM) so that Thunderbird writes prefs.js, then a filtered read.
        tb = PROCS.get("thunderbird")
        if tb and tb.poll() is None:
            try:
                tb.send_signal(signal.SIGTERM)
                tb.wait(timeout=left(20))
            except Exception:
                pass
        lines = []
        try:
            with open(PROF + "/prefs.js", encoding="utf-8", errors="replace") as f:
                for line in f:
                    mm = re.match(r'^user_pref\("([^"]+)"', line)
                    if mm and any(mm.group(1).startswith(p) for p in prefixes):
                        lines.append(line.rstrip("\n"))
        except OSError as e:
            lines.append("prefs.js unreadable: %s" % e)
        R["prefs_js"] = lines[:300]
    if PW:
        PW.stage = "after_thunderbird_exit" if prefixes else "inspection"
        PW.stop()


RULE_LINE = re.compile(r'^([A-Za-z]+)="(.*)"$')


def parse_filter_rules(text):
    """msgFilterRules.dat -> header (version, logging) + rules {name, enabled, type, condition, actions: [{action,
    actionValue}]}, as Thunderbird serialised them (what it actually understood)."""
    head, rules, cur = {}, [], None
    for line in text.splitlines():
        m = RULE_LINE.match(line.rstrip("\r"))
        if not m:
            continue
        k, v = m.group(1), m.group(2).replace('\\"', '"')
        if k == "name":
            cur = {"name": v, "actions": []}
            rules.append(cur)
        elif cur is None:
            head[k] = v
        elif k == "action":
            cur["actions"].append({"action": v})
        elif k in ("actionValue", "customId") and cur["actions"]:
            cur["actions"][-1][k] = v
        else:
            cur[k] = v
    return {"header": head, "rules": rules[:200]}


# ------------------------------------------------------------------------------------------------ checks
def _values_match(expected, got):
    return len(expected) == len(got) and all(e is None or e == g for e, g in zip(expected, got))


def check_listed_filter(c):
    """"listed_filter": THE named filter (or its absence) in the response of a listFilters step."""
    try:
        data = json.loads(FULLTEXT.get(c.get("step"), ""))
    except ValueError:
        return False, "listFilters response unreadable"
    found = []
    for acc in data if isinstance(data, list) else [data]:
        if not isinstance(acc, dict) or (c.get("account") and acc.get("accountId") != c["account"]):
            continue
        found += [f for f in acc.get("filters") or [] if isinstance(f, dict) and f.get("name") == c.get("name")]
    if c.get("absent"):
        return not found, "%d filter(s) named %r" % (len(found), c.get("name"))
    if len(found) != 1:
        return False, "%d filter(s) named %r" % (len(found), c.get("name"))
    f = found[0]
    acts = f.get("actions") or []
    ok = True
    if "action_types" in c:
        ok = ok and [a.get("type") for a in acts] == c["action_types"]
    if "action_values" in c:
        ok = ok and _values_match(c["action_values"], [a.get("value") for a in acts])
    if "terms" in c:
        ok = ok and [{k: t.get(k) for k in ("attrib", "op", "value")} for t in f.get("terms") or []] == c["terms"]
    if "enabled" in c:
        ok = ok and f.get("enabled") == c["enabled"]
    return ok, json.dumps({k: f.get(k) for k in ("index", "enabled", "type", "terms", "actions")}, ensure_ascii=True)


def check_filter_rule(c):
    """"filter_rule": THE named rule (or its absence) in an inspected msgFilterRules.dat."""
    rules = ((R.get("filter_rules") or {}).get(c.get("file")) or {}).get("rules") or []
    found = [r for r in rules if r.get("name") == c.get("name")]
    if c.get("absent"):
        return not found, "%d rule(s) named %r" % (len(found), c.get("name"))
    if len(found) != 1:
        return False, "%d rule(s) named %r" % (len(found), c.get("name"))
    r = found[0]
    ok = True
    if "actions" in c:
        ok = ok and [a.get("action") for a in r["actions"]] == c["actions"]
    if "action_values" in c:
        ok = ok and _values_match(c["action_values"], [a.get("actionValue") for a in r["actions"]])
    if "condition" in c:
        ok = ok and r.get("condition") == c["condition"]
    if "condition_contains" in c:
        ok = ok and str(c["condition_contains"]) in (r.get("condition") or "")
    if "condition_same_as" in c:     # same condition, byte for byte, as another rule of the file (a reference rule)
        other = [x for x in rules if x.get("name") == c["condition_same_as"]]
        ok = ok and len(other) == 1 and bool(r.get("condition")) and other[0].get("condition") == r.get("condition")
    # "filter_type" (not "type", which is the check's own type key): the rule's type="..." value.
    for k, ck in (("enabled", "enabled"), ("type", "filter_type")):
        if ck in c:
            ok = ok and r.get(k) == str(c[ck])
    return ok, json.dumps(r, ensure_ascii=True)


def check_draft_attachment(c):
    """"draft_attachment": the attachment "filename" of the draft, decoded, is "bytes" long (all bytes equal to
    "byte" when given)."""
    want = str(c.get("filename", "\0"))
    for m in drafts():
        if c.get("subject", "\0") not in m["subject"]:
            continue
        for pm in all_parts(m):
            hdrs = {(f["name"] or "").lower(): unfold(f) for f in pm["fields"]}
            names = re.findall(r'(?:file)?name\*?=\s*"?([^";]+)"?', hdrs.get("content-disposition", "") + ";" + hdrs.get("content-type", ""))
            if want not in names:
                continue
            cte = hdrs.get("content-transfer-encoding", "").strip().lower()
            body = pm["body"]
            data = base64.b64decode(re.sub(rb"\s+", b"", body)) if cte == "base64" else body
            ok = len(data) == c.get("bytes") and (c.get("byte") is None or data == bytes([c["byte"]]) * len(data))
            return ok, "%d bytes decoded (CTE %s), sha256 %s" % (len(data), cte or "none", hashlib.sha256(data).hexdigest())
    return False, "attachment %r not found in drafts %r" % (want, c.get("subject"))


def tool_path(tool, path):
    """(found, value) at the given path of the tool definition as tools/list returned it."""
    if tool not in TOOLS:
        return False, None
    cur = TOOLS[tool]
    for k in [x for x in str(path or "").split(".") if x]:
        if not isinstance(cur, dict) or k not in cur:
            return False, None
        cur = cur[k]
    return True, cur


def step_error_text(e):
    return " | ".join(str(e.get(k, "")) for k in ("error", "rpc_error", "tool_error", "text") if e.get(k))


def mailpit_matching(subject):
    return [m for m in MAILPIT_FULL if str(subject) in m["mailpit_subject"]]


def evaluate(c, steps):
    typ = c.get("type")
    if typ == "tool_ok":
        e = steps.get(c.get("step"))
        ok = bool(e and e.get("ok"))
        return ok, (e or {}).get("text", "") if ok else json.dumps(e, ensure_ascii=True)[:500]
    if typ == "tools_list_has":
        return c.get("tool") in R.get("tools", []), ""
    if typ == "draft_exists":
        return any(c.get("subject", "\0") in m["subject"] for m in drafts()), ""
    if typ == "draft_header_contains":
        for m in drafts():
            if c.get("subject", "\0") in m["subject"]:
                for v in header_values(m, c.get("header", "")):
                    if str(c.get("contains", "\0")) in v:
                        return True, v
        return False, ""
    if typ == "schema_has":
        return c.get("property") in (R["phases"].get("mcp", {}).get("schemas", {}).get(c.get("tool")) or []), ""
    if typ == "mailpit_total":
        total = (R.get("mailpit") or {}).get("total")
        return total == c.get("equals"), "total=%s" % total
    if typ == "tool_error_contains":         # the step FAILED and its error contains the text
        e = steps.get(c.get("step")) or {}
        detail = step_error_text(e)
        return bool(e) and not e.get("ok") and str(c.get("contains", "\0")) in detail, detail
    if typ == "draft_count":                 # number of drafts whose subject contains "subject"
        n = sum(1 for m in drafts() if c.get("subject", "\0") in m["subject"])
        return n == c.get("equals"), "%d draft(s)" % n
    if typ == "draft_body_contains":         # FULL raw body (MIME included) of a draft
        hits = [m for m in drafts() if c.get("subject", "\0") in m["subject"]
                and str(c.get("contains", "\0")).encode("utf-8") in m["body"]]
        return bool(hits), "%d matching draft(s)" % len(hits)
    if typ == "prefs_js_line":               # final prefs.js (inspect.prefs): a line containing the text, or none
        lines = [l for l in R.get("prefs_js", []) if str(c.get("contains", "\0")) in l]
        return (bool(lines) if c.get("present", True) else not lines), lines[:3]
    if typ in ("tool_json_equals", "tool_json_contains"):
        # Definition of a tool in tools/list, at the dotted path "a.b.c" (empty = the whole tool).
        found, v = tool_path(c.get("tool"), c.get("path", ""))
        if typ == "tool_json_equals":
            ok = found and v == c.get("equals")
        else:
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
            ok = found and str(c.get("contains", "\0")) in text
        return ok, json.dumps(v, ensure_ascii=True) if found else "path not found"
    if typ == "tool_result_contains":        # the step SUCCEEDED and its full text contains (or not) the text
        e = steps.get(c.get("step")) or {}
        text = FULLTEXT.get(c.get("step"), "")
        present = str(c.get("contains", "\0")) in text
        return bool(e.get("ok")) and present == bool(c.get("present", True)), "present=%s; %s" % (present, text[:300])
    if typ == "listed_filter":
        return check_listed_filter(c)
    if typ == "filter_rule":
        return check_filter_rule(c)
    if typ == "file_contains":               # inspected profile file (inspect.files) contains the text, or not
        text = FILES_FULL.get(c.get("file"))
        present = text is not None and str(c.get("contains", "\0")) in text
        return text is not None and present == bool(c.get("present", True)), \
            "file missing" if text is None else "present=%s" % present
    if typ == "draft_attachment":
        return check_draft_attachment(c)
    if typ == "mailpit_header_contains":     # a message captured by mailpit (by subject) has a header containing text
        values = [v for m in mailpit_matching(c.get("subject", "\0")) for v in header_values(m, c.get("header", ""))]
        return any(str(c.get("contains", "\0")) in v for v in values), json.dumps(values[:5], ensure_ascii=True)
    if typ == "mailpit_body_contains":       # FULL raw body (MIME included) of a message captured by mailpit
        hits = [m for m in mailpit_matching(c.get("subject", "\0")) if str(c.get("contains", "\0")).encode("utf-8") in m["body"]]
        return bool(hits), "%d matching message(s)" % len(hits)
    return False, "unknown check type"


def run_checks(sc):
    res = []
    steps = {e["id"]: e for e in R.get("steps", [])}
    counts = {"pass": 0, "fail": 0, "xfail": 0, "xpass": 0}
    for c in sc.get("checks", []):
        try:
            ok, detail = evaluate(c, steps)
        except Exception as ex:
            ok, detail = False, "exception: %s" % ex
        ok = bool(ok)
        if c.get("xfail"):
            status = "xpass" if ok else "xfail"   # known failure (e.g. an open upstream issue): reported, not fatal
        else:
            status = "pass" if ok else "fail"
        counts[status] += 1
        res.append({"check": c, "ok": ok, "status": status, "detail": clip(str(detail), 500)})
    R["checks"] = res
    R["summary"] = counts
    return counts["fail"] == 0


def emit():
    R["duration_s"] = round(time.monotonic() - T0, 1)
    R["logs"] = {n: tail(os.path.join(LOGS, n + ".log")) for n in ("thunderbird", "bridge", "mailpit", "xvfb")}
    out = json.dumps(R, ensure_ascii=True, indent=1)
    if len(out) > MAX_OUT:
        R["logs"] = {"truncated": True}
        R["tools"] = R.get("tools", [])[:5] + ["..."]
        out = json.dumps(R, ensure_ascii=True, indent=1)[:MAX_OUT]
    sys.stdout.write((MARK_BEGIN + "\n%s\n" + MARK_END + "\n") % (NONCE, out, NONCE))
    sys.stdout.flush()


def main():
    global PW
    as_init = run_as_init()           # returns in the harness process only
    R["pid1"] = "harness init" if as_init else "not the harness"
    os.makedirs(LOGS, mode=0o700, exist_ok=True)
    if not as_init:
        log("warning: PID 1 is not the harness (image started with --init?): the container's input and output may be"
            " reachable by the code under test")
    read_header_error = None
    try:
        read_header()
    except ValueError as e:
        read_header_error = e
    try:
        if read_header_error:
            raise read_header_error
        sc = load_scenario(SCENARIO_RAW)
        R["scenario"] = {"name": sc.get("name"), "description": sc.get("description"),
                         "sha256": hashlib.sha256(SCENARIO_RAW).hexdigest()}
        extract_source()
        R["extension"] = dict(zip(("id", "version"), build_xpi(sc)))
        start_mailpit()
        start_xvfb()
        make_profile(sc)
        if sc.get("watch_prefs"):
            PW = PrefWatch(sc["watch_prefs"])
        start_tb(sc)
        br = Bridge(sc.get("bridge_env"))
        run_mcp(sc, br)
        inspect(sc)
        checks_ok = run_checks(sc)
        R["ok"] = checks_ok and not R["errors"]
    except Exception as e:
        fail("%s: %s" % (type(e).__name__, e))
    finally:
        for n in ("bridge", "thunderbird", "xvfb", "mailpit"):
            stop(n)
        if PW and "prefs_timeline" not in R:
            PW.stop()
        emit()
    return 0 if R["ok"] else 1


def validate_files(paths):
    """"lab.py --validate FILE...": static validation of scenario files (no container, nothing executed)."""
    bad = 0
    for p in paths:
        try:
            with open(p, "rb") as f:
                raw = f.read(MAX_SCENARIO + 1)
            if len(raw) > MAX_SCENARIO:
                raise ValueError("larger than %d bytes" % MAX_SCENARIO)
            sc = load_scenario(raw)
            print("ok       %s (%d steps, %d checks, %d xfail)" % (p, len(sc.get("steps", [])), len(sc["checks"]),
                                                               sum(1 for c in sc["checks"] if c.get("xfail"))))
        except Exception as e:
            bad += 1
            print("INVALID  %s: %s" % (p, e))
    return 1 if bad or not paths else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--validate":
        sys.exit(validate_files(sys.argv[2:]))
    sys.exit(main())
