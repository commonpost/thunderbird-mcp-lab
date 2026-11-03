# Roadmap

What the lab intends to do, and not do, over the next year (from November 2026). It is a plan, not a promise: the lab
follows [Commonpost MCP for Thunderbird](https://github.com/commonpost/thunderbird-mcp) and its
[roadmap](https://github.com/commonpost/thunderbird-mcp/blob/main/ROADMAP.md), so its priorities move with it.
Requests are welcome as [issues](https://github.com/commonpost/thunderbird-mcp-lab/issues).

## Follow Commonpost MCP for Thunderbird

- Add scenarios for new tools and options of each Commonpost release, and for each security fix once it is public
  (as for 0.18.0 and [GHSA-65cp-vcv7-cf69](https://github.com/commonpost/thunderbird-mcp/security/advisories/GHSA-65cp-vcv7-cf69)).
- Keep every public scenario passing against the latest Commonpost release, with known issues marked `xfail` and
  linked to a public issue.
- Follow the changes planned in the main repository: when the bridge and the add-on talk through native messaging
  instead of a local HTTP port, adapt the harness (start-up detection, the `labHttpIdle` step) and the self-test.

## Thunderbird versions

- Keep the image on a current Thunderbird release, and add a way to run the scenarios against Thunderbird ESR, which
  the main project targets for the long term (ESR 153).

## Quality of the lab itself

- Measure the statement coverage of `harness/lab.py` in CI (it is measured by hand today: 82% of statements in
  November 2026), and measure branch coverage.
- Keep the isolation self-test in step with every change to the container options or the process layout.
- Find a second maintainer ([GOVERNANCE.md](GOVERNANCE.md)).

## Not planned

- Publishing releases or prebuilt images: the lab is used from `main`, and users build the image themselves from a
  commit they choose.
- Windows or macOS hosts, real mail servers (TLS, IMAP, OAuth, Exchange) or network access from the container.
- Driving Thunderbird's user interface: tools that open a compose window for review are checked through drafts, or
  through direct delivery to the local SMTP sink.
- Treating a lab result as proof: the code under test runs next to the harness, so results stay data
  ([README](README.md#security-model-of-the-lab)).
