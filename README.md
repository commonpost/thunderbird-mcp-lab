# thunderbird-mcp lab

[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/14959/badge)](https://www.bestpractices.dev/projects/14959)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/commonpost/thunderbird-mcp-lab/badge)](https://scorecard.dev/viewer/?uri=github.com/commonpost/thunderbird-mcp-lab)

An offline, end-to-end test lab for [thunderbird-mcp](https://github.com/TKasperczyk/thunderbird-mcp).

The lab runs the real extension and the real MCP bridge (`mcp-bridge.cjs`) of a thunderbird-mcp source tree inside a
real Thunderbird, drives them over MCP the way an AI client would, then checks what Thunderbird actually wrote:
drafts in the mbox store, filter rules in `msgFilterRules.dat`, preferences in `prefs.js`, and messages delivered to a
local SMTP sink. Everything runs in a disposable container with no network access.

It is a quality-assurance tool offered to the thunderbird-mcp project. It is **not a fork**, it does not contain or
redistribute thunderbird-mcp code, and it is not affiliated with the upstream project (see
[Relationship to upstream](#relationship-to-upstream)).

## Why

Unit tests exercise the extension with Thunderbird mocked out, and upstream already runs a Thunderbird compatibility
smoke test that checks that the extension loads. This lab goes one step further and answers questions such as:

- does `saveDraft` produce exactly one draft, with the requested `To`, `Cc`, `Bcc`, body and attachments, byte for byte?
- does a filter created with `createFilter` come back from `listFilters` as it was requested?
- does `updateFilter` keep the conditions it was not asked to change, byte for byte, in what Thunderbird writes to
  `msgFilterRules.dat`?
- do replies carry the right `In-Reply-To` and `References` headers?

## How it works

```
 run-lab.sh (host)                             container: --network none, read-only, no capabilities, uid 10001
 -----------------                             ------------------------------------------------------------------
 nonce + scenario + tar of source tree --stdin-->  lab.py (PID 1 init + harness, non-dumpable)
                                                     +- mailpit      SMTP 1025 / POP3 1110 / API 8025 on 127.0.0.1
                                                     +- Xvfb :99
                                                     +- Thunderbird 156.0.1, throw-away profile (user.js)
                                                     |    +- extension/ of the tree, zipped into an XPI
                                                     +- node mcp-bridge.cjs (from the tree) <-- JSON-RPC (MCP)
                                                     +- inspection: drafts (mbox), mailpit, profile files, prefs.js
 verdict from the nonce-tagged block <--stdout--  result JSON between two marker lines carrying the nonce
 (output stripped of control characters)
```

1. The launcher streams, on the container's standard input, a one-time nonce, the scenario, then a tar archive of the
   source tree (without `.git`). There is no bind mount at all.
2. The harness zips `extension/` into an XPI, installs it into a fresh profile, starts mailpit, Xvfb and Thunderbird,
   and waits until the extension has written its connection file.
3. It starts the tree's own `mcp-bridge.cjs`, runs `initialize` and `tools/list`, then every `tools/call` of the
   scenario.
4. It inspects the drafts folder, the messages captured by mailpit, and any profile file or preference the scenario
   asks for, then evaluates the scenario's checks.
5. It prints one JSON result between two marker lines that carry the nonce; the launcher reads the verdict from that
   block only.

## Quick start

Requirements: a Linux x86_64 host with Docker, bash, perl, python3, GNU tar and coreutils.

```sh
# 1. Build the image (the only step that uses the network).
docker build -t thunderbird-mcp-lab:local harness

# 2. Get the thunderbird-mcp code to test (a release tag, main, or a pull request).
git clone https://github.com/TKasperczyk/thunderbird-mcp.git
git -C thunderbird-mcp checkout v0.7.4

# 3. Run the reference scenario, then all public scenarios.
./run-lab.sh thunderbird-mcp scenarios/baseline.json
./run-lab.sh thunderbird-mcp scenarios/*.json
```

To test an upstream pull request, check it out first, for example
`git -C thunderbird-mcp fetch origin pull/123/head && git -C thunderbird-mcp checkout FETCH_HEAD`.

Each scenario runs in a fresh container (typically 20 to 30 seconds once the image is built). Every run of
`run-lab.sh` writes into a new directory `./lab-results/run-<UTC time>-<random>/`: a sanitised log and the result JSON
for each scenario, plus a `summary.tsv`. `--out DIR` changes the parent directory, which must not be inside the tree
under test; existing files are never written to. Options: `--image IMAGE`, `--out DIR`, `--verbose` (show all
container output). Exit status: `0` every scenario passed, `1` at least one failed, `2` usage error, `3` at least one
result was unusable (no single nonce-tagged block).

A check can be marked `xfail` when it encodes a known, publicly discussed upstream issue: it is reported but does not
fail the run, and it shows up as `xpass` once the issue no longer reproduces.

## Public scenarios

| Scenario | What it covers |
|---|---|
| `baseline.json` | `listAccounts` + one `saveDraft`: the draft exists with its `To`, `Cc` and `From` headers; nothing is sent. |
| `drafts.json` | `saveDraft` variants: several recipients, `Cc`/`Bcc`, HTML body, UTF-8 body, explicit sender identity, no recipient. |
| `attachments.json` | Inline base64 attachments (a generated 64 KiB binary, a text file, the `content` alias) come back byte for byte from the draft; `getMessage` lists the attachments of a seeded message. |
| `thread-headers.json` | `replyToMessage` on a seeded thread, delivered to the local mailpit sink: `In-Reply-To`, `References`, `Re:` subject, recipient and quoted text. |
| `filters-roundtrip.json` | `createFilter` for every action type except forward/reply, then `listFilters` must report what was requested; `updateFilter` must keep what it was not asked to change. |
| `filters-update-copy.json` | A hand-made rule with 13 conditions of varied types: changing only its action (to `addTag`) must leave every condition byte-for-byte identical in `msgFilterRules.dat`. |

The filter scenarios mark as `xfail` the checks that fail because of the filter id and value-typing issues discussed
publicly in upstream pull requests
[#175](https://github.com/TKasperczyk/thunderbird-mcp/pull/175) and
[#195](https://github.com/TKasperczyk/thunderbird-mcp/pull/195).

## Writing a scenario

A scenario is a JSON file: MCP tool calls (`steps`) followed by `checks` on what the tools returned and on what
Thunderbird wrote. A minimal one:

```json
{
  "name": "my-draft",
  "description": "saveDraft stores one draft with the requested recipient.",
  "steps": [
    {"id": "draft", "tool": "saveDraft", "timeout": 90,
     "arguments": {"to": "someone@example.com", "subject": "My lab draft [x1]", "body": "Hello.\n"}}
  ],
  "checks": [
    {"type": "tool_ok", "step": "draft"},
    {"type": "draft_count", "subject": "[x1]", "equals": 1},
    {"type": "draft_header_contains", "subject": "[x1]", "header": "To", "contains": "someone@example.com"},
    {"type": "mailpit_total", "equals": 0}
  ]
}
```

Scenarios can also seed messages or filter rules into the profile before Thunderbird starts, set preferences, pass
`THUNDERBIRD_MCP_*` variables to the bridge, run calls in parallel, retry, watch `prefs.js`, and generate large
arguments. The full reference is in [docs/scenario-format.md](docs/scenario-format.md). Validate a scenario without
running it with `python3 harness/lab.py --validate my-scenario.json`.

## Security model of the lab

The code under test is treated as untrusted: it may be a pull request from anyone.

**Isolation.** Each run uses a fresh container started with `--network none`, `--read-only`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, an unprivileged user (uid 10001), `noexec` tmpfs mounts only, and limits on memory
(3 GiB), CPU (2), processes (1024), core dumps (none) and time (900 s). Nothing from the host is mounted: the scenario
and the source tree arrive on standard input (at most 1 MB and 600 MiB). The source tree's `.git` directory is not
sent. On the host, the launcher writes only into new directories it creates for the run (the results directory,
outside the tree under test, and a temporary one).

**Result integrity.** The container is started without `--init`: the image's entry point is PID 1. It makes itself
non-dumpable, then forks into a minimal init (PID 1, which only reaps orphaned processes) and the harness proper. These
two are the only processes that hold the container's standard input and output, and both are non-dumpable before any
code under test starts: code running under the same uid can neither read their memory nor open that input or output
through `/proc/<pid>/fd` (the kernel would require `CAP_SYS_PTRACE`). The other processes get `/dev/null` or a log
file as standard streams, except the bridge, whose input and output are its own pipes to the harness. Do not add
`--init`: a dumpable PID 1 such as `docker-init` would hold the container's output where the code under test can reach
it (the harness logs a warning when it is not PID 1). The launcher trusts only the result block tagged with the nonce
and reports a run as unusable unless there is exactly one. Container output is bounded (20 MiB) and stripped of
control, format and bidirectional characters before it reaches the terminal or the log. This protects the channel,
not the observations: see the last point below.

**Self-test.** `selftest/run.sh` replaces the bridge with a stand-in (`selftest/stand-in-bridge.cjs`, not
thunderbird-mcp) that tries to reach the network, to write to the image, and to reach the memory, environment and file
descriptors of the harness and of PID 1; it also lists the standard streams of every other process it can inspect,
and it emits terminal control sequences and a forged result marker. The self-test passes only if every one of those
attempts fails, no other process it can inspect holds anything but `/dev/null` or a log file on its standard
streams, and no control character reaches the launcher's output. CI runs it on every build.

**Supply chain.** Thunderbird is downloaded at build time from Mozilla and checked twice: the GPG signature of
`SHA512SUMS` must chain to a pinned primary key fingerprint, then the SHA-512 of the archive must match. Base images
are pinned by digest and GitHub Actions by full commit SHA. Debian packages are installed from Debian at build time
and are not pinned.

**What this does not give you.**

- A container is not a virtual machine: the kernel is shared. Run code you consider hostile in a disposable VM.
- Access to the Docker daemon is equivalent to root on the host; run the lab where that is acceptable.
- The code under test runs next to the harness and can influence what the harness observes (it can, for example,
  write the very files the harness inspects). **Results are data, not proof**, especially for untrusted pull requests.
- The protection of the output channel rests on the kernel's non-dumpable flag and on the absence of
  `CAP_SYS_PTRACE` in the container. The self-test checks the access paths listed above; it does not prove that no
  other path exists.

## Continuous integration

`.github/workflows/lab.yml` builds the image and runs the public scenarios and the self-test:

- on pushes and pull requests to this repository, and every week, against the **latest published upstream release**:
  the release must not be a draft or a pre-release, its tag must resolve to the same commit through git and through
  the GitHub API, the checked-out commit must be that one, and the manifest version must match the tag;
- manually (`workflow_dispatch`) against the latest release, upstream `main`, or a given upstream **pull request**
  (the pull request head is resolved through the API and through git, and both must agree).

The upstream code is only checked out and streamed into the lab container: it is never executed on the runner. The
workflow uses no secrets, its token is read-only (`contents: read`) and is not passed to the container, it does not
use `pull_request_target`, and credentials are not persisted by `actions/checkout`. Only the summary table is published
(in the job summary, and as a `lab-summary` artifact kept for 14 days): the logs and result JSON are output of the code
under test, which may be an unreviewed pull request, so they are not redistributed. Run the lab locally to get them.

## Limitations

- Linux x86_64 only, for the host and for Thunderbird. Nothing specific to Windows or macOS is covered (file
  permissions, paths, temporary directories).
- A single Thunderbird build: 156.0.1, en-US. Other versions can be built with `--build-arg TB_VERSION=...`
  (if Mozilla has rotated its signing key, verify the new primary key out of band and pass it with
  `--build-arg MOZ_RELEASE_FPR=...`), but only 156.0.1 has been tested.
- The mail server is simulated: mailpit accepts everything. There is no TLS, OAuth, IMAP, Exchange or realistic
  rejection; messages are seeded directly into the profile's mbox files.
- Every run starts from an empty profile: there is no real user state (accounts, filters, tags, address books,
  calendars), and the global search index is disabled.
- Tools that open a compose window for review cannot be driven (there is no UI automation). The lab relies on
  drafts, or on direct delivery to the local mailpit sink when a scenario explicitly lifts the default `skipReview`
  block for itself.
- The bridge runs on the Node.js version shipped by Debian 13 (20.x).
- Results are data produced next to the code under test (see above). Output is bounded: result JSON 3 MB, container
  output 20 MiB, mbox files read up to 20 MB.

## Relationship to upstream

thunderbird-mcp is written and maintained by Tomasz Kasperczyk and its contributors, under the MIT license. This
repository is an independent quality-assurance tool maintained by the commonpost organization. It is not affiliated
with or endorsed by the upstream project, it is not a fork, and it does not ship upstream code: the code under test is
whatever tree you point the launcher at.

The intent is to be useful to upstream. Findings are shared with the upstream project through its usual channels
(issues and pull request reviews, or private vulnerability reporting for anything security-relevant). A scenario that
encodes a known upstream issue links to the public upstream discussion and marks the affected checks `xfail`, rather
than presenting them as failures. If the upstream maintainers would like the lab, or parts of it, in their own
repository or CI, we will gladly help adapt it.

Thunderbird and Mozilla are trademarks of the Mozilla Foundation. This project is not affiliated with Mozilla.

## Contributing, security, conduct

- [CONTRIBUTING.md](CONTRIBUTING.md): how to propose scenarios and changes (please read the rule about undisclosed
  vulnerabilities).
- [SECURITY.md](SECURITY.md): how to report a vulnerability, in this lab or elsewhere.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md): Contributor Covenant 2.1.

## License

MIT, see [LICENSE](LICENSE). The image built from `harness/Dockerfile` contains third-party software under its own
licenses, none of which is stored in this repository: Thunderbird (Mozilla Public License 2.0 and others, see
`about:license`), mailpit (MIT) and Debian packages. This repository does not publish images.
