# Security policy

## Supported versions

Only the `main` branch of this repository is supported.

## Reporting a vulnerability in this lab

Please **do not open a public issue**. Use GitHub's private vulnerability reporting instead: open the **Security** tab
of this repository and choose **Report a vulnerability**.

Please include what you observed, how to reproduce it, and the impact you expect. This is a volunteer project: we aim
to acknowledge reports within 7 days and will keep you informed until the issue is resolved. We are happy to credit
reporters who wish to be credited.

In scope, for example:

- a way for code under test to escape or weaken the lab's isolation (network, host files, capabilities, resource or
  time limits) as configured by `run-lab.sh`;
- a way for code under test to read or write the container's standard input or output (for example through `/proc`),
  to forge the nonce-tagged result block, or to get control or bidirectional characters through the launcher's output
  sanitiser;
- a way for a source tree to make `run-lab.sh` or `selftest/run.sh` read or write host files other than the tree itself
  and the run's own new directories (for example through symbolic links);
- a way to bypass the verification of the Thunderbird download in `harness/Dockerfile`;
- a weakness in the GitHub Actions workflow (token exposure, script injection, execution of untrusted code on the
  runner).

Known limitations documented in the [README](README.md#security-model-of-the-lab) are not vulnerabilities by
themselves: the kernel is shared with the host, access to Docker is equivalent to root, and code under test runs
under the same uid as the harness and can influence what the harness observes, so a verdict is data, not proof.

## Vulnerabilities in other projects

- **Commonpost MCP for Thunderbird** (the code this lab tests): report privately, following its
  [security policy](https://github.com/commonpost/thunderbird-mcp/security/policy).
- **thunderbird-mcp** (the original project): report privately, following its
  [security policy](https://github.com/TKasperczyk/thunderbird-mcp/security/policy).
- For either one, do not publish a scenario, log or issue that demonstrates an unfixed vulnerability, here or anywhere
  else.
- **Thunderbird**: report to [Mozilla](https://www.mozilla.org/security/).
- **Docker, the Linux kernel, mailpit, Debian packages**: report to their respective maintainers.

If you are unsure where a problem belongs, report it privately here and we will help route it.
