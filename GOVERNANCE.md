# Governance

This lab is the end-to-end test suite of [Commonpost MCP for Thunderbird](https://github.com/commonpost/thunderbird-mcp).
Both repositories belong to the [commonpost](https://github.com/commonpost) GitHub organization and have the same
maintainer. The rules that apply to the whole organization (how a maintainer joins, continuity) are those of the
[main repository's GOVERNANCE.md](https://github.com/commonpost/thunderbird-mcp/blob/main/GOVERNANCE.md); this file
says how they apply here and what is specific to the lab.

## Roles

| Role | Who | Can do |
|---|---|---|
| Maintainer | Guillaume Laforest ([@sacrefizz](https://github.com/sacrefizz)), owner of the organization | Merge pull requests, handle security reports and publish advisories, change repository settings and rulesets |
| Contributor | Anyone | Open issues and pull requests (scenarios, harness changes, documentation), review them, report vulnerabilities privately ([SECURITY.md](SECURITY.md)) |
| Users of the lab | Commonpost MCP for Thunderbird, whose CI runs the lab at a pinned commit; anyone testing thunderbird-mcp or a derivative | Report problems and request scenarios through issues |

The maintainer also has to:

- keep the scenarios passing against the latest release of Commonpost MCP for Thunderbird, or mark a check `xfail`
  with a link to the public issue that explains it (CONTRIBUTING rule 3);
- review Dependabot updates of the pinned actions and base images;
- answer issues within 14 days ([CONTRIBUTING.md](CONTRIBUTING.md#reporting-bugs-and-requesting-features)) and
  security reports within 72 hours ([SECURITY.md](SECURITY.md)).

Today the lab has **one maintainer**. A second one is welcome, as in the main repository.

## How decisions are made

- Every change to `main` goes through a pull request. A repository ruleset forbids deleting `main` and force-pushing
  to it, requires review threads to be resolved, and requires these checks to pass on a branch that is up to date with
  `main`: Static checks (Python compile and lint, scenario validation and its negative tests, shellcheck, license
  headers), Lab (the public scenarios and the isolation self-test in a real Thunderbird), zizmor, and CodeQL for
  Python and GitHub Actions. With one maintainer, no approving review is required.
- A change to the isolation of the lab (the `docker run` options, the process layout of the harness, the input
  protocol, the result block, the output sanitiser) must keep `selftest/run.sh` passing, and its pull request must
  explain why it is safe (CONTRIBUTING rule 5).
- Larger changes (a new kind of step or check, a change of the scenario format, a change of what CI tests) start
  with an issue or are described in the pull request before they are merged.
- Findings about Commonpost MCP for Thunderbird, thunderbird-mcp or Thunderbird go to those projects. A scenario that
  demonstrates an undisclosed vulnerability is never published here before the fix (CONTRIBUTING rule 1).
- Security decisions about the lab itself follow [SECURITY.md](SECURITY.md) and are made in private until a fix is
  merged.
- With more than one maintainer, decisions are taken by consensus among the maintainers; if they cannot agree, the
  change waits.

The lab publishes no releases and no images: it is used from `main`, and each version is identified by its commit.
Commonpost MCP for Thunderbird runs it at a commit pinned in its own workflow and moves that pin by hand.

## Becoming a maintainer, continuity

As in the [main repository](https://github.com/commonpost/thunderbird-mcp/blob/main/GOVERNANCE.md#becoming-a-maintainer).
For the lab in particular:

- The code, its history, the issues and the workflows are public. Anyone can fork the repository and continue it
  under the MIT license.
- The lab image is built from this repository alone (`docker build -t thunderbird-mcp-lab:local harness`), with
  verified downloads. No secret is needed to build it, to run the scenarios, or to run CI.

What one maintainer cannot hand over alone is administrative access to the `commonpost` organization and to this
repository's settings. Until a second maintainer joins, that is the gap in continuity.

## Changes to this document

This document changes through pull requests, like the code.
