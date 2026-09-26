#!/usr/bin/python3
"""Fuzz target for the scenario validator (load_scenario in harness/lab.py).

Scenario files come from contributors, through pull requests, and the validator is the first code that reads them.
Its contract: any input that is not a valid scenario is rejected with ValueError (json.JSONDecodeError and
UnicodeDecodeError are subclasses), or RecursionError, which the json module itself raises on deeply nested input.
Any other exception is a bug and is reported as a crash.

Built by .clusterfuzzlite/build.sh, which copies harness/lab.py next to this file so that it is bundled with it.
"""
import sys

import atheris

with atheris.instrument_imports():
    import lab


def TestOneInput(data):
    if len(data) > lab.MAX_SCENARIO:
        return
    try:
        lab.load_scenario(data)
    except (ValueError, RecursionError):
        pass


def main():
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
