# Scenario format

A scenario is a UTF-8 JSON object of at most 1 MB. The harness validates it before doing anything else and rejects
unknown top-level and step keys, unknown check types and checks that name an unknown step, so a typo fails loudly
instead of silently skipping a check. Validate a file without running it:

```sh
python3 harness/lab.py --validate scenarios/*.json
```

## The lab environment

Every run starts from the same throw-away profile (`harness/user.js`):

| Item | Value |
|---|---|
| `account1` / `server1` | Local Folders (`mailbox://nobody@Local%20Folders/...`): `Drafts`, `Sent`, `Templates`, `Archives` |
| `account2` / `server2` | POP3 account `lab` on mailpit (`127.0.0.1:1110`); never fetched automatically |
| identity `id1` | `Lab User <lab@example.test>`, plain-text compose, drafts in Local Folders/Drafts, copies in Local Folders/Sent |
| SMTP `smtp1` | mailpit on `127.0.0.1:1025`, no authentication, no TLS |
| mailpit API | `http://127.0.0.1:8025` (used by the harness only) |

Use only reserved example domains (`example.com`, `example.test`, ...) in scenarios.

To seed messages in the POP3 inbox, fix its directory so that Thunderbird does not pick a different one:
`"prefs": {"mail.server.server2.directory-rel": "[ProfD]Mail/127.0.0.1"}`, then seed folder `127.0.0.1/Inbox`, whose
URI is `mailbox://lab@127.0.0.1/Inbox`.

## Top-level keys

| Key | Type | Meaning |
|---|---|---|
| `name`, `description` | string | Free text, copied into the result. |
| `steps` | list (<= 50) | MCP tool calls, in order (see below). |
| `checks` | list (1 to 200) | Assertions evaluated at the end (see below). Required. |
| `prefs` | object | Preferences appended to the profile's `user.js` (boolean, integer or string <= 2000). |
| `startup_timeout` | integer 10-540 | Seconds to wait for the extension to start (default 150). |
| `tb_sandbox` | boolean | `false` disables Thunderbird's own process sandbox (default `true`; the container is the boundary either way). |
| `schema_watch` | list of tool names | Records the `inputSchema` property names of these tools (`phases.mcp.schemas`). |
| `tools_dump` | list of tool names | Records the full `tools/list` definitions (`tools_detail`) and a canonical SHA-256 of each (`tools_detail_sha256`), e.g. to compare a pull request with a release. |
| `bridge_env` | object (<= 10) | Environment variables for the bridge process only. Names must match `THUNDERBIRD_MCP_[A-Z0-9_]+`; values are printable ASCII (<= 500). |
| `seed` | list (<= 10) | `{"folder": "127.0.0.1/Inbox", "messages": ["raw RFC 5322 message", ...]}` (1 to 20 messages, 5 MB in total): appended to mbox files of the profile before Thunderbird starts. |
| `seed_files` | list (<= 5) | `{"path": "Mail/<server folder>/msgFilterRules.dat", "content": "..."}` (<= 200 000 characters): filter rules written before Thunderbird starts, e.g. a rule made "by hand" in the filter editor. |
| `watch_prefs` | list of prefixes (<= 20) | Re-reads `prefs.js` every 0.2 s and records every change of matching lines (`prefs_timeline`); steps get `prefs_before`, `prefs_at_response` and `prefs_after_wait`. Thunderbird writes `prefs.js` about 0.5 s after a change, so shorter-lived states can be missed. |
| `inspect` | object | `{"prefs": [prefixes], "files": [paths]}`, see below. |
| `xpi` | string | Path, relative to the received tree, of a prebuilt XPI to install instead of zipping `extension/` (e.g. a release artifact). |

Seeded mbox files have no `.msf` summary and Thunderbird does not index them by itself. A reliable recipe: an
`applyFilters` step on the folder with a `retry`, then a `getMessage` step with a `retry` until the message is found
(see `scenarios/attachments.json`).

## Steps

```json
{"id": "draft", "tool": "saveDraft", "arguments": {"to": "a@example.com"}, "timeout": 90,
 "draft_subject": "[x1]", "draft_wait": 30,
 "probes": [{"header": "To", "needle": "a@example.com"}]}
```

| Key | Meaning |
|---|---|
| `id` | Unique name `[A-Za-z0-9._-]`, referenced by checks. |
| `tool` | MCP tool name. |
| `arguments` | Tool arguments (object). May contain generators (below). |
| `timeout` | Seconds, 1-180 (default 60). |
| `note` | Free text for readers. |
| `draft_subject` | After the call, wait up to `draft_wait` seconds (1-120, default 30) for a draft whose subject contains this text, and record its raw headers (`draft`). |
| `probes` | Up to 10 `{"header", "needle"}`: where did `needle` end up in that draft? See [Probes](#probes). |
| `part_probes` | Same, applied to the headers of each MIME part of the draft (3 levels, 30 parts). |
| `batch` | Consecutive steps with the same batch name are sent together and their responses awaited in parallel. |
| `retry` | `{"attempts": 1-20, "delay": 1-30}`: call again until the step succeeds (not with `batch`). |
| `wait_after` | Pause 0-120 s after the step (e.g. to let a timer fire). |
| `force_call` | Call the tool even when it is missing from `tools/list` (e.g. disabled by the user), to record the refusal. |

A step succeeds (`ok`) when the call returns a result that is not `isError` and whose text is not a JSON object with
an `error` field.

**Generators.** Anywhere in `arguments`, an object whose only key is `$lab_base64`, e.g.
`{"$lab_base64": {"bytes": 65536, "byte": 0}}`, is replaced when the call is sent by the base64 of `bytes` bytes
(1 to 32 MiB) of value `byte` (0-255, default 97). The result keeps the compact form and records the size, base64
length and SHA-256 in `generated_arguments`. This keeps scenarios and results small.

## Checks

Every check is an object with a `type`, optional `note`, and optional `xfail` (a reason, e.g. a link to a publicly
tracked issue). A check marked `xfail` is reported as `xfail` when it fails and as `xpass` when it passes; neither
fails the run. The run passes when no check has status `fail` and the harness reported no error.

| Type | Keys | Passes when |
|---|---|---|
| `tool_ok` | `step` | the step succeeded |
| `tool_error_contains` | `step`, `contains` | the step failed and its error or text contains the text |
| `tool_result_contains` | `step`, `contains`, `present` (default true) | the step succeeded and its full response text contains (or does not contain) the text |
| `tools_list_has` | `tool` | the tool is in `tools/list` |
| `schema_has` | `tool`, `property` | the property is in the tool's `inputSchema` (tool listed in `schema_watch`) |
| `tool_json_equals` / `tool_json_contains` | `tool`, `path` (dotted, empty = whole tool), `equals` / `contains` | the value at that path of the tool definition equals / contains the value |
| `draft_exists` | `subject` | a draft's subject contains the text |
| `draft_count` | `subject`, `equals` | that many drafts match |
| `draft_header_contains` | `subject`, `header`, `contains` | a matching draft has that header containing the text |
| `draft_body_contains` | `subject`, `contains` | the full raw body (MIME included) of a matching draft contains the text |
| `draft_attachment` | `subject`, `filename`, `bytes`, `byte` (optional) | the decoded attachment has that size (and every byte equals `byte`) |
| `mailpit_total` | `equals` | mailpit captured exactly that many messages |
| `mailpit_header_contains` | `subject`, `header`, `contains` | a captured message whose subject contains `subject` has that header containing the text |
| `mailpit_body_contains` | `subject`, `contains` | the full raw body of such a message contains the text |
| `prefs_js_line` | `contains`, `present` (default true) | a line of the final `prefs.js` (read through `inspect.prefs`) contains the text, or none does |
| `file_contains` | `file`, `contains`, `present` (default true) | an inspected profile file contains (or does not contain) the text |
| `listed_filter` | `step`, `name`, optional `account`, `action_types`, `action_values`, `terms`, `enabled`, `absent` | exactly one filter with that name in the response of a `listFilters` step matches (`null` in `action_values` = not checked) |
| `filter_rule` | `file`, `name`, optional `actions`, `action_values`, `condition`, `condition_contains`, `condition_same_as`, `enabled`, `filter_type`, `absent` | exactly one rule with that name in an inspected `msgFilterRules.dat` matches; `condition_same_as` compares the condition byte for byte with another rule of the file |

## Inspection

- `inspect.prefs` (prefixes): Thunderbird is stopped cleanly (SIGTERM) after the steps so that it writes `prefs.js`;
  matching lines are recorded in `prefs_js`.
- `inspect.files` (<= 10 paths relative to the profile, e.g. `Mail/127.0.0.1/msgFilterRules.dat`): size, SHA-256 and
  content are recorded in `files`; a `msgFilterRules.dat` is also parsed into `filter_rules` (rules with name,
  enabled, type, condition and actions, as Thunderbird serialised them).
- Always recorded: up to 20 drafts (raw header lines, header names, lines with a bare CR, body excerpt), the size of
  every mbox file, and up to 20 messages captured by mailpit with their raw headers.

## Probes

A probe reports where a string ended up in a saved draft. Every occurrence is listed, and the probe gets the first
status in this order:

| Status | Meaning |
|---|---|
| `OWN_HEADER` | on a line of its own that starts with the named header (`Name:` in column 0) |
| `IN_BODY` | after the end of the header block |
| `FOLDED` | on a continuation line of some header |
| `EMBEDDED` | on the same line as another header |
| `RFC2047_ENCODED` | only in encoded form (`=?...?=`) |
| `ABSENT` | nowhere |
| `NO_DRAFT` | no draft matched `draft_subject` |

## Result

The result JSON contains, among others: `ok`, `errors`, `summary` (`pass`, `fail`, `xfail`, `xpass` counts),
`checks` (each with `check`, `ok`, `status` and `detail`), `pid1` (whether the harness runs as PID 1 of the
container) and `nondumpable`, `phases` (versions of Thunderbird, mailpit, Node.js and the extension, MCP server info,
XPI hash), `tools`, `steps` (arguments, timing, response text), `drafts`, `mbox_folders`, `mailpit`, the optional
inspection results, and the tail of the Thunderbird, bridge, mailpit and Xvfb logs.

It is produced next to the code under test: treat it as data.
