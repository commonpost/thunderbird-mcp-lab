# Contributing

Thank you for helping. Contributions are welcome: new scenarios for behaviours of Commonpost MCP for Thunderbird that
are already public, harness improvements, better documentation, CI fixes.

## Ground rules

1. **No undisclosed vulnerabilities in public.** Do not open a public issue, pull request, scenario, log or discussion
   that demonstrates an unfixed and undisclosed vulnerability, in Commonpost MCP for Thunderbird, in Thunderbird or
   anywhere else.
   Report it privately to the affected project first: for Commonpost MCP for Thunderbird, following its
   [security policy](https://github.com/commonpost/thunderbird-mcp/security/policy); for the original thunderbird-mcp,
   following [its own](https://github.com/TKasperczyk/thunderbird-mcp/security/policy); for Thunderbird, through
   [Mozilla](https://www.mozilla.org/security/); for this lab, see [SECURITY.md](SECURITY.md). Once a fix is
   released and the issue is public, a regression scenario is welcome.
2. **Synthetic data only.** Use reserved example domains (`example.com`, `example.test`, ...) and invented content.
   Never include real messages, names, addresses, tokens, host names, paths or logs from a real machine.
3. **Known issues are `xfail`, not failures.** When a check fails because of a known issue that is already tracked
   publicly, mark that check (not the whole scenario) with `"xfail": "<reason and link>"`.
4. **Scenarios must pass against the latest release of Commonpost MCP for Thunderbird** (checks marked `xfail`
   aside). Put the `run-lab.sh`
   summary in your pull request description.
5. **Keep the isolation.** Changes to the `docker run` options, the process layout of the harness, the input
   protocol, the result block or the output sanitiser must keep `selftest/run.sh` passing, and the pull request must
   explain why they are safe. In particular, do not start the image with `--init`: the harness must be PID 1.
6. **Pin what you add.** GitHub Actions by full commit SHA (with the version in a comment), base images by digest.
7. **Test what you change.** New functionality in the harness, the launcher or the scenario format comes with a
   scenario, a self-test check or a validator case that exercises it, in the same pull request; a bug fix comes with
   a check that fails without the fix whenever that is practical. Say in the pull request which tests you added.
8. **Be respectful of other projects.** Findings about thunderbird-mcp, Thunderbird or any other project go to that
   project through its usual channels; the lab is not a place to criticise anyone's work.
9. **License header in each source file.** A new source file (code, Dockerfile, workflow) starts, after any `#!` line,
   with the two SPDX lines the other files carry: `SPDX-License-Identifier: MIT` and
   `SPDX-FileCopyrightText: 2026 Guillaume Laforest`, as comments. The "Static checks" job checks it. Scenario and
   other JSON files are data and carry no header.

## Working locally

```sh
docker build -t thunderbird-mcp-lab:local harness            # build the image
python3 harness/lab.py --validate scenarios/*.json            # validate scenarios (no container)
python3 selftest/validator-cases.py                           # invalid scenarios must be refused
./run-lab.sh path/to/commonpost-mcp scenarios/*.json          # run them
selftest/run.sh path/to/thunderbird-mcp                       # isolation self-test
shellcheck run-lab.sh selftest/run.sh .clusterfuzzlite/build.sh   # shell lint
docker run --rm -v "$PWD:/repo:ro" -w /repo rhysd/actionlint:latest   # workflow lint
```

The scenario validator is fuzzed by ClusterFuzzLite (`fuzz/`, `.clusterfuzzlite/`, workflow `fuzz`): any input must be
rejected with a clean `ValueError`, never another exception. To fuzz it locally (needs `pip install atheris`):

```sh
cp harness/lab.py fuzz/ && python3 fuzz/fuzz_load_scenario.py -dict=fuzz/fuzz_load_scenario.dict \
  -max_total_time=120 corpus/    # corpus/ = a copy of scenarios/*.json; crashes are written as crash-*
rm fuzz/lab.py
```

Tips for scenarios:

- Give every draft or message a unique subject marker such as `[x1]`, and match on it.
- End scenarios that must not send anything with `{"type": "mailpit_total", "equals": 0}`.
- Seeded messages need an indexing step before other tools can see them (see `scenarios/attachments.json`).
- Describe in `description` what the scenario proves and what it does not.

A change to the validator comes with a case in `selftest/invalid-scenarios/` (a small scenario with one thing wrong)
and its expected error message in `selftest/invalid-scenarios.json`; `selftest/validator-cases.py` checks both.

## Coding standards

The coding standard of the lab is what these tools accept; contributions must pass them:

- **Shell** (`run-lab.sh`, `selftest/run.sh`, `.clusterfuzzlite/build.sh`): Bash, stopping on errors (`set -euo
  pipefail`, or `-eu` for the ClusterFuzzLite build script); [ShellCheck](https://www.shellcheck.net/) at its default
  severity (style) reports nothing. A `# shellcheck disable=` comment must say why.
- **Python** (`harness/lab.py`, `fuzz/`): standard library only, for the Python 3 of the image (Debian 13);
  [pyflakes](https://github.com/PyCQA/pyflakes) reports nothing.
- **Workflows**: actions pinned by full commit SHA, least-privilege `permissions`, `persist-credentials: false`;
  [zizmor](https://docs.zizmor.sh/) with its pedantic persona reports nothing; actionlint is run locally.
- **Scenarios**: `python3 harness/lab.py --validate` accepts them.
- **License headers**: rule 9.

CI enforces ShellCheck, pyflakes, the scenario validation and its negative tests, and the license headers (job
"Static checks"), and zizmor; both jobs are required checks for merging into `main`.

## Code review

Every change reaches `main` through a pull request, and the
[required checks](GOVERNANCE.md#how-decisions-are-made) must pass before it is merged. The reviewer checks:

- **Isolation:** the `docker run` options, the process layout of the harness, the input protocol, the result block
  and the output sanitiser are unchanged, or the pull request explains why the change is safe and `selftest/run.sh`
  still passes (rule 5). Code under test never runs on the host or the CI runner.
- **Fail-closed behaviour:** invalid input is refused rather than ignored (validator, paths resolved under the
  profile, links not followed); a run with an unusable result is reported as unusable, never as a pass.
- **Scenarios:** they are valid, pass against the latest release of Commonpost MCP for Thunderbird, say in
  `description` what they prove, and use `xfail` only with a link to a public issue (rules 3 and 4). No scenario
  demonstrates an undisclosed vulnerability (rule 1).
- **Tests:** new behaviour of the harness, the launcher or the format comes with a scenario, a self-test check or a
  validator case (rule 7).
- **Data:** only synthetic data, in scenarios, fixtures and pull request descriptions (rule 2).
- **Dependencies:** actions pinned by commit SHA, base images by digest; workflows keep least-privilege permissions
  (rule 6).
- **Documentation:** the README (scenario table, security model), `docs/scenario-format.md` and the other documents
  describe the change in the same pull request.

A pull request is merged when the checks pass and a maintainer approves it. A maintainer's own pull request needs the
approval of another maintainer; while the lab has a single maintainer ([GOVERNANCE.md](GOVERNANCE.md)), the required
checks, including the isolation self-test, are the second line of review.

## Small tasks for new contributors

Issues labelled [good first issue](https://github.com/commonpost/thunderbird-mcp-lab/labels/good%20first%20issue) are
small, self-contained tasks: usually a new scenario or a validator case, which needs no knowledge of Thunderbird's
internals. Say in the issue that you are taking one, so that two people do not do the same work.

## Reporting bugs and requesting features

Open an [issue](https://github.com/commonpost/thunderbird-mcp-lab/issues): what you ran (lab commit, command,
scenario), what you expected and what happened, with the `run-lab.sh` summary if relevant. Feature requests are welcome
the same way. We aim to acknowledge every report within 14 days. Security problems go through [SECURITY.md](SECURITY.md)
instead, never through a public issue.

## Pull requests

- Keep pull requests small and focused, in English.
- Say what you tested and what you only inferred.
- AI-assisted contributions are welcome if you have reviewed and run them yourself; please say so in the pull request.
  (Commits of the maintainers made with an AI assistant carry a `Co-Authored-By` trailer.)
- By contributing, you agree that your contribution is licensed under the MIT license of this repository.

## Conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
