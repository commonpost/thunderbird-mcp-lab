#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 Guillaume Laforest
"""Negative tests of the scenario validator: every file of selftest/invalid-scenarios/ must be rejected by
"harness/lab.py --validate", with the error message listed for it in selftest/invalid-scenarios.json.

Usage: python3 selftest/validator-cases.py    (from any directory; exit status 0 = every case behaved as expected)

Each case is a small scenario with one thing wrong. The manifest and the directory must list the same files. The
script also checks a scenario larger than the 1,000,000-byte limit (generated in a temporary directory) and that the
public scenarios are still accepted.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LAB = os.path.join(ROOT, "harness", "lab.py")
CASES = os.path.join(HERE, "invalid-scenarios")


def validate(paths):
    p = subprocess.run([sys.executable, "-B", "-I", LAB, "--validate"] + paths, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=120)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def main():
    with open(os.path.join(HERE, "invalid-scenarios.json"), encoding="utf-8") as f:
        expected = json.load(f)
    files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(CASES, "*.json")))
    bad = []
    for name in sorted(set(files) ^ set(expected)):
        bad.append("%s: listed in only one of invalid-scenarios/ and invalid-scenarios.json" % name)

    with tempfile.TemporaryDirectory() as tmp:
        big = os.path.join(tmp, "too-large.json")
        with open(big, "w", encoding="ascii") as f:
            f.write('{"description": "%s"}' % ("x" * 1_000_001))
        cases = [(os.path.join(CASES, n), expected[n]) for n in files if n in expected]
        cases.append((big, "larger than 1000000 bytes"))
        for path, message in cases:
            rc, out = validate([path])
            line = "INVALID  %s: " % path
            if rc != 1 or not out.startswith(line) or message not in out[len(line):]:
                bad.append("%s: expected exit status 1 and %r, got %d: %s" % (
                    os.path.relpath(path, ROOT), message, rc, out.strip()[:300]))

    valid = sorted(glob.glob(os.path.join(ROOT, "scenarios", "*.json"))) + [os.path.join(HERE, "isolation.json")]
    rc, out = validate(valid)
    if rc != 0:
        bad.append("public scenarios rejected:\n" + out)

    for b in bad:
        print("validator-cases: FAIL - " + b)
    if bad:
        return 1
    print("validator-cases: PASS - %d invalid scenarios rejected with the expected message, %d valid ones accepted"
          % (len(cases), len(valid)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
