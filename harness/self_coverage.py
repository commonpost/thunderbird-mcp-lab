# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Guillaume Laforest
"""Statement and branch coverage of the harness itself (harness/lab.py), for the coverage image only.

The "coverage" target of harness/Dockerfile installs coverage.py (Debian's python3-coverage) and this module, with a
.pth file that imports it when any Python process of that image starts. It then:

- starts coverage.py, measuring branches, for files named lab.py only;
- when the process ends, normally or through os._exit (which the PID 1 init of lab.py uses), writes one line

      [lab-self-coverage] {"file": "lab.py", "sha256": "...", "arcs": [[from, to], ...]}

  to a new file in the directory named by LAB_SELF_COVERAGE_OUT when that is set (runs on the runner, such as
  selftest/validator-cases.py), otherwise to standard output, after the result block (runs through run-lab.sh). In
  the lab container, only the harness and its PID 1 init hold standard output (see lab.py), so the code under test
  cannot write such a line there.

"python3 -m lab_self_coverage report DIR" reads every such line under DIR (run logs, files), keeps those measured on
the lab.py of this image (same SHA-256), and reports with coverage.py. selftest/coverage.sh runs the whole measure.
The normal lab image has neither coverage.py nor this module.
"""
import atexit
import hashlib
import json
import os
import sys
import time

PREFIX = "[lab-self-coverage] "
TARGET = "/opt/tblab/lab.py"
_cov = None
_done = False


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _emit():
    global _done
    if _done or _cov is None:
        return
    _done = True
    _cov.stop()
    data = _cov.get_data()
    lines = []
    for path in sorted(data.measured_files()):
        if os.path.basename(path) != "lab.py":
            continue
        record = {"file": "lab.py", "sha256": _sha256(path), "arcs": sorted(data.arcs(path) or [])}
        lines.append(PREFIX + json.dumps(record, separators=(",", ":")) + "\n")
    if not lines:
        return
    out = os.environ.get("LAB_SELF_COVERAGE_OUT")
    if out:
        name = os.path.join(out, "%d-%d.jsonl" % (os.getpid(), time.time_ns()))
        with open(name, "x", encoding="ascii") as f:
            f.writelines(lines)
    else:
        sys.stdout.writelines(lines)
        sys.stdout.flush()


def _start():
    global _cov
    if os.environ.get("LAB_SELF_COVERAGE") == "off":
        return
    import coverage
    _cov = coverage.Coverage(branch=True, data_file=None, include=["*/lab.py"], config_file=False)
    _cov.set_option("run:disable_warnings", ["no-data-collected"])      # processes that do not run lab.py
    _cov.start()
    atexit.register(_emit)
    real_exit = os._exit

    def _exit(status):
        _emit()
        real_exit(status)
    os._exit = _exit


def report(root, min_statements, min_branches):
    """Union of the arcs found under root, reported by coverage.py against TARGET. Returns the exit status."""
    import coverage
    want = _sha256(TARGET)
    arcs, runs, other = set(), 0, 0
    def unreadable(err):
        raise err
    for base, _dirs, files in os.walk(root, onerror=unreadable):
        for name in sorted(files):
            with open(os.path.join(base, name), encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if line.startswith("| "):
                        line = line[2:]
                    if not line.startswith(PREFIX):
                        continue
                    try:
                        record = json.loads(line[len(PREFIX):])
                        if record.get("sha256") != want:
                            other += 1
                            continue
                        arcs.update((int(a), int(b)) for a, b in record["arcs"])
                    except (ValueError, KeyError, TypeError):
                        other += 1
                        continue
                    runs += 1
    if not runs:
        print("lab-self-coverage: no measure of this lab.py under %s" % root)
        return 1
    data_file = "/tmp/lab-self-coverage.data"
    data = coverage.CoverageData(basename=data_file)
    data.add_arcs({TARGET: sorted(arcs)})
    data.write()
    cov = coverage.Coverage(data_file=data_file, branch=True, config_file=False)
    cov.load()
    cov.report(file=sys.stdout, show_missing=True)
    cov.json_report(outfile="/tmp/lab-self-coverage.json")
    with open("/tmp/lab-self-coverage.json", encoding="utf-8") as f:
        totals = json.load(f)["totals"]
    st = 100.0 * totals["covered_lines"] / totals["num_statements"]
    br = 100.0 * totals["covered_branches"] / totals["num_branches"]
    summary = {"statements": totals["num_statements"], "covered_statements": totals["covered_lines"],
               "statement_percent": round(st, 1), "branches": totals["num_branches"],
               "covered_branches": totals["covered_branches"], "branch_percent": round(br, 1),
               "measures": runs, "other_measures_skipped": other, "coverage_py": coverage.__version__}
    print("[lab-self-coverage-total] " + json.dumps(summary))
    ok = st >= min_statements and br >= min_branches
    print("lab-self-coverage: %s - statements %.1f%% (minimum %s), branches %.1f%% (minimum %s), %d measures"
          % ("PASS" if ok else "FAIL", st, min_statements, br, min_branches, runs))
    return 0 if ok else 1


if __name__ == "__main__":
    started = sys.modules.get("lab_self_coverage")       # imported by the .pth file: stop measuring this process
    if started is not None and started._cov is not None:
        started._done = True
        started._cov.stop()
    if len(sys.argv) != 5 or sys.argv[1] != "report":
        sys.exit("usage: python3 -m lab_self_coverage report DIR MIN_STATEMENTS MIN_BRANCHES")
    sys.exit(report(sys.argv[2], float(sys.argv[3]), float(sys.argv[4])))
elif __name__ == "lab_self_coverage":
    _start()
