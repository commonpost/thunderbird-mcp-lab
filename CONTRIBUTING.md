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

## Working locally

```sh
docker build -t thunderbird-mcp-lab:local harness            # build the image
python3 harness/lab.py --validate scenarios/*.json            # validate scenarios (no container)
./run-lab.sh path/to/commonpost-mcp scenarios/*.json          # run them
selftest/run.sh path/to/thunderbird-mcp                       # isolation self-test
shellcheck run-lab.sh selftest/run.sh                         # shell lint
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
