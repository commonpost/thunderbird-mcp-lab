# Architecture

This document describes how the lab is built: its components, where each one runs, how a scenario travels through
them, and how the lab itself is built and tested. The security reasoning built on this design is in
[assurance-case.md](assurance-case.md). For usage, see the [README](../README.md); for the scenario format, see
[scenario-format.md](scenario-format.md).

## What the lab is

An end-to-end test suite for [Commonpost MCP for Thunderbird](https://github.com/commonpost/thunderbird-mcp). Given
a source tree of that project (or of thunderbird-mcp) and one or more scenario files, it runs the tree's add-on and
bridge in a real Thunderbird inside a disposable, offline container, calls MCP tools as a client would, and checks
what Thunderbird actually wrote. The lab contains none of the add-on's code: the code under test is whatever tree the
launcher is given, and it is treated as untrusted.

## Components

```
 host                                               container (one per scenario; --network none, read-only root,
                                                    no capabilities, uid 10001, tmpfs only, no bind mount)
+-----------------------------+                    +---------------------------------------------------------------+
| run-lab.sh                  |  stdin: nonce,     | lab.py as PID 1: non-dumpable, forks                          |
|  validates arguments        |  scenario, tar of  |  +- PID 1: minimal init (reaps processes, exits with status)  |
|  copies the scenarios       |  the tree -------> |  +- harness (non-dumpable)                                    |
|  docker run (options fixed) |                    |      +- mailpit   SMTP/POP3/API on 127.0.0.1                  |
|  sanitises the output       |  stdout+stderr:    |      +- Xvfb :99                                              |
|  reads the nonce-tagged     | <----------------- |      +- Thunderbird + the tree's extension/ (as an XPI)       |
|  result block               |  progress lines,   |      +- node mcp-bridge.cjs (the tree's) <- JSON-RPC (MCP)    |
|  writes the results dir     |  result block      |      +- inspection: mbox, mailpit, profile files, prefs.js    |
+-----------------------------+                    +---------------------------------------------------------------+
```

### The launcher (`run-lab.sh`)

A Bash script that runs on the host. It:

- checks its arguments: the source tree must contain `extension/manifest.json`, `mcp-bridge.cjs` and `package.json`
  as regular files; the results directory must not be inside the tree;
- copies every scenario (at most 1,000,000 bytes, valid JSON) into a private temporary directory before the first
  run, so that a file changing during the run cannot change what is sent;
- for each scenario, starts a fresh container with fixed options (`--network none`, `--read-only`, `--cap-drop ALL`,
  `--security-opt no-new-privileges:true`, user 10001, `noexec` tmpfs mounts, memory 3 GiB, 2 CPUs, 1024 processes,
  no core dumps, `--pull never`, `--log-driver none`) under `timeout` (900 s);
- writes to the container's standard input a fresh nonce (16 bytes from `/dev/urandom`), the scenario's length and
  bytes, then a tar stream of the tree without `.git` (at most 600 MiB);
- reads at most 20 MiB of output, strips control, format and bidirectional characters (`clean`), and writes it to a
  log in a new results directory `run-<UTC time>-<random>` created with `mktemp -d` and `umask 077`;
- takes the verdict from the one block framed by marker lines that carry the nonce, and reports the run as unusable
  unless there is exactly one;
- writes a `summary.tsv` and exits with 0 (all passed), 1 (a failure), 2 (usage error) or 3 (an unusable result).

### The image (`harness/Dockerfile`)

Three stages:

1. the `mailpit` binary, copied from the official image, pinned by digest;
2. the Thunderbird download (`debian:13-slim`, pinned by digest): `SHA512SUMS` must carry a valid OpenPGP signature
   that chains to a pinned primary key fingerprint of Mozilla's release key, then the archive must match its SHA-512;
3. the runtime image (`debian:13-slim`, pinned by digest): the libraries Thunderbird needs, Xvfb, xdotool, Node.js and
   Python from Debian; set-uid and set-gid bits removed; Thunderbird's updater removed; `policies.json` that turns off
   application and extension updates and telemetry; the harness (`lab.py`) and the base profile (`user.js`) owned by
   root and read-only; user 10001; entry point `python3 -I /opt/tblab/lab.py`.

The network is used only while the image is built. The image is not published: users build it themselves.

### The harness (`harness/lab.py`)

One Python file that uses only the standard library. It runs as PID 1 of the container (the launcher does not pass
`--init`). In order:

1. **Init.** It makes itself non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`), then forks: the parent stays PID 1 and only
   reaps processes; the child is the harness proper (`run_as_init`).
2. **Input.** It reads the nonce (32 lowercase hex characters), the scenario length and the scenario from fd 0
   without buffering, so that the tar stream that follows stays intact, then validates the scenario (`load_scenario`), then extracts the tar stream into `/lab/src` with
   `--no-same-owner --no-same-permissions` (`extract_source`).
3. **Add-on.** It zips `extension/` into an XPI without following links (`build_xpi`), or installs a prebuilt XPI
   from the tree if the scenario names one (`install_prebuilt_xpi`), and reads the add-on id from the manifest.
4. **Environment.** It starts mailpit on 127.0.0.1 and checks it (one message through SMTP, read back through the
   API, and a POP3 login), starts Xvfb, writes a throw-away profile (`user.js` plus the scenario's preferences),
   seeds messages and filter files when asked (paths resolved under the profile), then starts Thunderbird and waits
   for the add-on's connection file.
5. **MCP session.** It starts the tree's own `node mcp-bridge.cjs` with a fixed environment (plus the scenario's
   `THUNDERBIRD_MCP_*` variables, if any), sends `initialize` and `tools/list`, then each step as a `tools/call`.
   Steps with the same `batch` are sent together; a step can be retried; the pseudo-tool `labHttpIdle` opens a raw
   connection to the add-on's port instead (`run_mcp`, `run_group`, `run_http_idle`), and the pseudo-tool `labKeys`
   presses keys in a Thunderbird window with `xdotool` (`run_lab_keys`).
6. **Inspection.** It reads the drafts from the mbox files, the messages captured by mailpit, the profile files the
   scenario names, and the watched or inspected preferences from `prefs.js` (`inspect`, `PrefWatch`).
7. **Checks and result.** It evaluates the scenario's checks (`run_checks`, `evaluate`), stops every process, and
   prints one JSON object (ASCII only) between the two nonce-tagged markers on its standard output (`emit`).

`lab.py --validate FILE...` runs only step 2's validator on scenario files, outside any container.

### Thunderbird and its profile (`harness/user.js`, `harness/policies.json`)

A Local Folders account and a POP3 account on mailpit, one identity, an SMTP server on mailpit, and preferences that
avoid first-run windows, update checks and network attempts. Mail is stored in mbox files so that the harness can
read drafts directly. The global search index is disabled.

### Scenarios (`scenarios/`) and their format (`docs/scenario-format.md`)

JSON files: steps (tool calls), optional seeds, preferences and bridge variables, inspection requests, and checks.
The format and every check type are documented in [scenario-format.md](scenario-format.md).

### Self-test (`selftest/`)

`selftest/run.sh` builds a temporary tree from the given tree's `extension/` (copied without following links), a
minimal `package.json` and `selftest/stand-in-bridge.cjs` in place of the bridge, and runs `selftest/isolation.json`
through `run-lab.sh`. The stand-in tries to reach the network, write to the image, and read the memory, environment
and file descriptors of the harness and of PID 1; it also prints terminal control sequences and a forged marker.
The self-test passes only if every attempt fails and no control character reaches the launcher's output or logs.

### Validator tests (`selftest/validator-cases.py`)

`selftest/invalid-scenarios/` holds small scenarios that each have one thing wrong; `selftest/invalid-scenarios.json`
gives the error message each must produce. `selftest/validator-cases.py` runs `lab.py --validate` on each, and on a
generated file over the size limit, and fails unless every one is refused with its message and the public scenarios
are still accepted. It needs only Python and runs in the "Static checks" job.

### Fuzzing (`fuzz/`, `.clusterfuzzlite/`)

`fuzz/fuzz_load_scenario.py` feeds arbitrary bytes to `load_scenario` with atheris; any exception other than
`ValueError` or `RecursionError` is a crash. ClusterFuzzLite builds it with the public scenarios as seed corpus.

## Continuous integration (`.github/workflows/`)

| Workflow | When | What |
|---|---|---|
| `lab.yml` | push and pull request to `main`, weekly, on demand | Static checks (`py_compile`, `--validate` on every scenario, the validator's negative tests, `bash -n`, shellcheck, pyflakes, SPDX license headers); resolves the code under test (the latest Commonpost release by default, verified through git and the API; or `main`, or a pull request, on demand); builds the image; runs every public scenario and the self-test; publishes only the summary table. |
| `codeql.yml` | push and pull request to `main`, weekly | CodeQL for Python and GitHub Actions. |
| `zizmor.yml` | push and pull request to `main` | zizmor (pedantic persona) on the workflows. |
| `fuzz.yml` | pull requests that touch `harness/lab.py` or the fuzzing files (5 minutes), weekly (30 minutes) | ClusterFuzzLite on `load_scenario`; crashes go to code scanning. |
| `scorecard.yml` | push to `main`, weekly | OpenSSF Scorecard. |

Every action is pinned by full commit SHA; workflow tokens default to `contents: read`; checkouts do not persist
credentials. The code under test is only checked out and streamed into the container: it never runs on the runner.
Dependabot (`.github/dependabot.yml`) proposes updates of the actions and of the base images of both Dockerfiles,
weekly, after a 7-day cooldown.

The main repository runs the lab's public scenarios on each of its pull requests and pushes to `main`, with the lab
checked out at a pinned commit (`e2e-lab.yml` there).

## Versions and distribution

The lab publishes no releases and no images. It is used from `main`; each version is a commit, and the harness
reports its own version string (`HARNESS` in `lab.py`) in every result.
