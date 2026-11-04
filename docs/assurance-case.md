# Assurance case

This document argues why the lab meets its security requirements, and says where it does not. It covers the threat
model, the trust boundaries, the secure design principles applied, the common weaknesses countered, the supply chain,
and the known limits. Each claim names the file (and, where useful, the function) that implements it, and the test
that covers it when there is one. The components are described in [architecture.md](architecture.md); how to report
a vulnerability is in [SECURITY.md](../SECURITY.md).

Security requirements, in short: the code under test (possibly an unreviewed pull request) cannot reach the network,
the host's files, the host's other processes or the CI runner's credentials; it cannot read or forge the channel
that carries the verdict to the launcher; it cannot get terminal control sequences through to the user's terminal or
the logs; the launcher writes only into new directories it creates for the run; the Thunderbird in the image is the
one Mozilla signed. The lab does not promise that a verdict is true: the code under test runs next to the harness
and can influence what the harness observes, so **results are data, not proof**.

## 1. Threat model

### Assets

- The host that runs `run-lab.sh` and Docker: its files, its network, its other processes, the user's terminal.
- The GitHub Actions runner and its token, when the lab runs in CI.
- The integrity of the result channel: the nonce-tagged block that carries the verdict.
- The lab image: what is built from `harness/Dockerfile`, in particular the Thunderbird download.
- The lab's own repository and workflows.

### Actors

| Actor | Trust | What it can do |
|---|---|---|
| The user running the lab | Trusted | Chooses the tree, the scenarios, the image and the results directory. |
| The code under test (the tree's `extension/` and `mcp-bridge.cjs`) | Untrusted | Runs inside the container as uid 10001, next to the harness; can write any file that uid can write there, open sockets on loopback, start processes, print anything. |
| The source tree as a file tree | Untrusted | Can contain symbolic links, special file names and large files. |
| Scenario authors (pull requests) | Untrusted until reviewed | Provide JSON that the validator reads first, then the harness. |
| Upstream suppliers (Mozilla, Debian, Docker Hub images, GitHub Actions) | Trusted once verified or pinned | Provide the software in the image and the CI steps. |
| Other users of the host | Out of scope | Docker access is equivalent to root on the host (README). |

## 2. Trust boundaries

1. **Host to container.** The container's standard input (nonce, scenario, tar stream) and its merged standard output
   and error. There is no bind mount and no network. The container options are fixed in `run-lab.sh`.
2. **Harness to code under test, inside the container.** Same uid. The harness and PID 1 are non-dumpable, and only
   they hold the container's input and output. This boundary protects the channel, not the observations (section 6).
3. **Launcher output to terminal and disk.** Container output is untrusted text until `clean` in `run-lab.sh` has
   stripped control, format and bidirectional characters.
4. **Scenario files to the harness.** `load_scenario` in `lab.py` is the first code that interprets them.
5. **CI runner to code under test.** The tree is checked out and streamed into the container; it is never run on the
   runner (`lab.yml`).
6. **Build time to upstream.** The image build is the only time the network is used: the Thunderbird archive is
   verified, base images are pinned by digest, Debian packages come from Debian's signed archive.

## 3. Secure design principles applied

**Least privilege.**
- The container runs as uid 10001 with `--cap-drop ALL`, `--security-opt no-new-privileges:true`, a read-only root,
  `noexec` tmpfs mounts and no network (`run-lab.sh`). The image removes set-uid and set-gid bits and Thunderbird's
  updater (`harness/Dockerfile`). Self-test: `uid`, `cap_eff`, `no_new_privs`, `interfaces`, `internet`,
  `write_root`, `write_harness` (`selftest/isolation.json`).
- The bridge gets a fixed environment plus only `THUNDERBIRD_MCP_*` variables from the scenario (`BRIDGE_ENV_RE`,
  `Bridge` in `lab.py`): no `NODE_OPTIONS`, `LD_*` or `PATH`.
- Workflows run with `contents: read` by default; only the CodeQL, zizmor, fuzz and Scorecard jobs get
  `security-events: write`, and Scorecard `id-token: write` to publish its results. No secret is used.

**Fail-safe defaults.**
- The launcher reports a run as unusable (exit status 3) unless the output contains exactly one block tagged with the
  run's nonce, and that block is a JSON object (`run-lab.sh`).
- A scenario is rejected unless every key is known and every value is in range (`load_scenario`); the harness writes
  `"ok": false` on any error (`main`).
- The launcher refuses a results directory inside the tree, checked before and after it is created, and writes only
  into a new `mktemp -d` directory with `umask 077`.

**Complete mediation.** Every scenario passes through `load_scenario` before anything runs, both in the container and
in `lab.py --validate`; every path taken from a scenario is matched against a pattern, refused if it contains `..`, and
resolved with `realpath` under the profile before use (`seed_mbox`, `seed_files`, `inspect`, `install_prebuilt_xpi`).

**Economy of mechanism.** One launcher script, one harness file using only Python's standard library, one image; the
harness is the container's PID 1, with no other init.

**Separation.** The code under test never runs on the host or the CI runner, only in the container. Inside it, the
two processes that hold the container's input and output are made non-dumpable before any code under test starts
(`make_nondumpable`, `run_as_init`). The processes the harness starts after it has read its input (mailpit, Xvfb,
Thunderbird) get `/dev/null` or a log file as standard streams (`start`); the bridge's pipes go to the harness.

**Input validation and output sanitising.**
- Input from the host is bounded: scenario at most 1,000,000 bytes, tree at most 600 MiB (`run-lab.sh`), nonce
  `[0-9a-f]{32}`, length line at most 7 digits (`read_header`).
- Scenario values are checked against allowlists (`NAME_RE`, `TOOL_RE`, `PREF_RE`, `SEED_FOLDER_RE`, `SEED_FILE_RE`,
  `PROFILE_FILE_RE`, `XPI_REL_RE`, the sets `TOP_KEYS`, `STEP_KEYS`, `CHECK_TYPES`) with `fullmatch`, and numbers
  against ranges. Fuzzed: `fuzz/fuzz_load_scenario.py`.
- Container output is decoded as UTF-8 and stripped of control, format and bidirectional characters before it reaches
  the terminal or a log (`clean`). The result JSON is ASCII only (`emit`). Self-test: the stand-in prints control
  sequences, and `selftest/run.sh` fails if any reaches the terminal output or a log.

**Limits on size and time.** Per scenario: 900 s (`timeout` in `run-lab.sh`), and the harness stops itself after about
560 s (`DEADLINE`); memory 3 GiB, 2 CPUs, 1024 processes, no core dumps; container output 20 MiB; result JSON 3 MB
(`MAX_OUT`); mbox files read up to 20 MB, profile files 2 MB, bridge lines 30 MB; generated arguments 32 MiB.

## 4. Common weaknesses countered

| Weakness | Where it is handled | Tests |
|---|---|---|
| Network access from the code under test | `--network none` (`run-lab.sh`); the only network use is the image build. | `selftest/isolation.json`: `interfaces`, `internet` |
| Writing to the host or the image | No bind mount; read-only root; `noexec` tmpfs; harness files owned by root, mode 644. | `write_root`, `write_harness` |
| Privilege escalation | Unprivileged uid, no capabilities, `no-new-privileges`, set-uid bits removed. | `uid`, `cap_eff`, `no_new_privs` |
| Reading or forging the result channel through `/proc` | Harness and PID 1 non-dumpable; no `--init`; verdict only from the single nonce-tagged block. | `parent_*`, `pid1_*`, `reachable_stdio` |
| Terminal escape injection | `clean` in `run-lab.sh` (and in the verdict step); result JSON is ASCII. | `selftest/run.sh` (`noctl` on terminal and logs) |
| Symbolic links in the tree | `tar` archives links as links, it does not follow them; the three required files must be regular files; `extension/` and the XPI must not be links; `build_xpi` skips links; `selftest/run.sh` copies with `cp -RP`. | none specific |
| Path traversal through scenario paths | Patterns with `fullmatch`, `..` refused, `realpath` under the profile. | `selftest/validator-cases.py` (`*-traversal` cases) |
| Command injection | Every subprocess is started with an argument list, never through a shell (`start`, `subprocess.run`); shellcheck, which reports unquoted expansions, passes on the shell scripts. | shellcheck in CI |
| Environment injection into the bridge | Only `THUNDERBIRD_MCP_[A-Z0-9_]` names and printable ASCII values (`BRIDGE_ENV_RE`, `PRINTABLE_RE`). | `selftest/validator-cases.py` (`bridge-env-*` cases) |
| Validator crash on hostile input | Any invalid input must raise `ValueError`; a crash found by fuzzing was fixed in [#4](https://github.com/commonpost/thunderbird-mcp-lab/pull/4). | `fuzz/fuzz_load_scenario.py` (ClusterFuzzLite); `selftest/validator-cases.py` (`step-id-not-a-string`, `check-step-not-a-string`) |
| Resource exhaustion | Section 3, "Limits on size and time". | none specific |
| Script injection and token exposure in CI | Inputs passed through `env`, the pull request number checked with a pattern, `contents: read`, no `pull_request_target`, no secrets, `persist-credentials: false`, the code under test never run on the runner. | zizmor (pedantic), CodeQL for Actions |
| Cache poisoning in CI | No cache is used; the CodeQL alerts for it were dismissed as false positives, with the reason in `lab.yml`. | CodeQL for Actions |
| Tampered Thunderbird download | HTTPS only (`--proto '=https' --tlsv1.2`), OpenPGP signature of `SHA512SUMS` chained to a pinned primary key fingerprint, then the archive's SHA-512 (`harness/Dockerfile`). | The build fails otherwise. |

## 5. Supply chain and process

- **Dependencies.** The harness uses only Python's standard library. The image pins its base images by digest and
  Thunderbird by version plus signature; Debian packages are installed from Debian at build time and are not pinned.
  GitHub Actions are pinned by full commit SHA.
- **Dependabot** (`.github/dependabot.yml`) proposes updates of the actions and of the base images of
  `harness/Dockerfile` and `.clusterfuzzlite/Dockerfile`, weekly, with a 7-day cooldown. Dependabot alerts and
  security updates are enabled.
- **Required checks.** `main` is protected by a ruleset: no deletion, no force push, pull request required, review
  threads resolved, and these checks passing on a branch up to date with `main`: Static checks, Lab (scenarios and
  self-test), zizmor, CodeQL for Python and Actions. The organization requires two-factor authentication.
- **Static and dynamic analysis.** pyflakes and shellcheck (in Static checks), CodeQL, zizmor, ClusterFuzzLite,
  OpenSSF Scorecard.
- **Vulnerability handling.** Private vulnerability reporting and the response process are in
  [SECURITY.md](../SECURITY.md). Vulnerabilities in Commonpost MCP for Thunderbird, thunderbird-mcp or Thunderbird go to
  those projects, and no scenario that demonstrates one is published before the fix (CONTRIBUTING rule 1).

## 6. Known limits and residual risks

- **Results are data, not proof.** The code under test runs as the same uid as the harness and can write the files
  the harness inspects (drafts, profile files, `prefs.js`), or answer in its place. A passing verdict for an
  untrusted pull request means little until the code is reviewed.
- **The kernel is shared.** A container is not a virtual machine; a kernel or Docker escape is out of the lab's reach.
  Run code you consider hostile in a disposable virtual machine.
- **Docker access is root on the host.** Whoever runs the lab can already do anything on the host.
- **The protection of the result channel** rests on the kernel's non-dumpable flag and on the absence of
  `CAP_SYS_PTRACE` in the container. The self-test checks the access paths it knows; it does not prove that no other
  path exists.
- **Debian packages are not pinned.** apt fetches them over HTTPS from `deb.debian.org` and authenticates them by
  Debian's signed archive metadata; the CA bundle that apt trusts for that comes from the pinned mailpit image. Two
  builds at different times can differ.
- **Thunderbird updates are manual.** Dependabot does not track `TB_VERSION`; only 156.0.1 has been tested. Inside the
  lab, Thunderbird runs offline on synthetic data, and the code under test already runs with its privileges.
- **Validator tests.** Every refusal in the validator is reached by at least one case in `selftest/invalid-scenarios/`,
  but not every condition of each refusal; the 5 MB limit on seeded messages has no case, since a scenario cannot
  reach it under the 1,000,000-byte limit on the scenario itself.
- **Coverage.** The scenarios, the self-test and the validator cases cover 82% of the statements of `harness/lab.py`
  (measured by hand with coverage.py in November 2026, not in CI); branch coverage is not measured. The shell scripts
  are covered only by the scenarios and the self-test that run through them.
