#!/bin/bash -eu
# ClusterFuzzLite build: bundles each fuzz target (with harness/lab.py) and its seed corpus (the public scenarios).
cp harness/lab.py fuzz/lab.py
for fuzzer in fuzz/fuzz_*.py; do
  compile_python_fuzzer "$fuzzer"
done
python3 -m zipfile -c "$OUT/fuzz_load_scenario_seed_corpus.zip" scenarios/*.json selftest/isolation.json
cp fuzz/fuzz_load_scenario.dict "$OUT/"
