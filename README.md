# thunderbird-mcp lab

[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/14959/badge)](https://www.bestpractices.dev/projects/14959)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/commonpost/thunderbird-mcp-lab/badge)](https://scorecard.dev/viewer/?uri=github.com/commonpost/thunderbird-mcp-lab)

An offline, end-to-end test lab for [Commonpost MCP for Thunderbird](https://github.com/commonpost/thunderbird-mcp),
the continuation of [thunderbird-mcp](https://github.com/TKasperczyk/thunderbird-mcp).

The lab runs the real extension and the real MCP bridge (`mcp-bridge.cjs`) of a source tree inside a
real Thunderbird, drives them over MCP the way an AI client would, then checks what Thunderbird actually wrote:
drafts in the mbox store, filter rules in `msgFilterRules.dat`, preferences in `prefs.js`, and messages delivered to a
local SMTP sink. Everything runs in a disposable container with no network access.

It is the end-to-end test suite of Commonpost MCP for Thunderbird, kept in its own repository: it does not contain
or redistribute the extension's code (see [Relationship to thunderbird-mcp](#relationship-to-thunderbird-mcp)).

## Why

Unit tests exercise the extension with Thunderbird mocked out, and the Commonpost repository already runs a
Thunderbird compatibility smoke test that checks that the extension loads. This lab goes one step further and answers
questions such as:

- does `saveDraft` produce exactly one draft, with the requested `To`, `Cc`, `Bcc`, body and attachments, byte for byte?
- does a filter created with `createFilter` come back from `listFilters` as it was requested?
- does `updateFilter` keep the conditions it was not asked to change, byte for byte, in what Thunderbird writes to
  `msgFilterRules.dat`?
- do replies carry the right `In-Reply-To` and `References` headers?
- do the safety settings hold in a real Thunderbird: direct sending off by default, disabled tools, restricted
  accounts, refused attachment paths, and a fail-closed reading of unreadable settings?

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

# 2. Get the code to test (a release tag, main, or a pull request).
git clone https://github.com/commonpost/thunderbird-mcp.git commonpost-mcp
git -C commonpost-mcp checkout v0.18.0

# 3. Run the reference scenario, then all public scenarios.
./run-lab.sh commonpost-mcp scenarios/baseline.json
./run-lab.sh commonpost-mcp scenarios/*.json
```

To test a pull request, check it out first, for example
`git -C commonpost-mcp fetch origin pull/123/head && git -C commonpost-mcp checkout FETCH_HEAD`.

The harness recognises the extension id of Commonpost MCP for Thunderbird and that of the original thunderbird-mcp,
so a thunderbird-mcp tree can still be run locally; the scenarios target Commonpost (its preference names and its
safety rules, such as explicit recipients for a direct reply), so some of them may not apply to it.

Each scenario runs in a fresh container (typically 20 to 30 seconds once the image is built). Every run of
`run-lab.sh` writes into a new directory `./lab-results/run-<UTC time>-<random>/`: a sanitised log and the result JSON
for each scenario, plus a `summary.tsv`. `--out DIR` changes the parent directory, which must not be inside the tree
under test; existing files are never written to. Options: `--image IMAGE`, `--out DIR`, `--verbose` (show all
container output). Exit status: `0` every scenario passed, `1` at least one failed, `2` usage error, `3` at least one
result was unusable (no single nonce-tagged block).

A check can be marked `xfail` when it encodes a known, publicly tracked issue: it is reported but does not fail the
run, and it shows up as `xpass` once the issue no longer reproduces.

## Public scenarios

| Scenario | What it covers |
|---|---|
| `baseline.json` | `listAccounts` + one `saveDraft`: the draft exists with its `To`, `Cc` and `From` headers; nothing is sent. |
| `drafts.json` | `saveDraft` variants: several recipients, `Cc`/`Bcc`, HTML body, UTF-8 body, explicit sender identity, no recipient. |
| `attachments.json` | Inline base64 attachments (a generated 64 KiB binary, a text file, the `content` alias) come back byte for byte from the draft; `getMessage` lists the attachments of a seeded message. |
| `thread-headers.json` | `replyToMessage` (mode `send`, explicit `to` and `from`) on a seeded thread, delivered to the local mailpit sink: `In-Reply-To`, `References`, `Re:` subject, recipient and quoted text. |
| `compose-html-text.json` | HTML messages built without a compose window, with an identity that composes in HTML (`saveDraft` plain and HTML, a direct `sendMail`, reply and forward drafts of a quoted HTML message, an HTML signature): an apostrophe or a reference such as `&#39;` or `&nbsp;` reaches the message as the character, not as visible `&#39;`; a body that `scanHTML` cuts at a `>` inside a quoted value, and a hostile quoted image, keep their `moz-do-not-send` neutralized ([#76](https://github.com/commonpost/thunderbird-mcp/pull/76), fixed in 0.17.2). |
| `filters-roundtrip.json` | `createFilter` for every action type except forward/reply, then `listFilters` must report what was requested; `updateFilter` must keep what it was not asked to change. |
| `filters-update-copy.json` | A hand-made rule with 13 conditions of varied types: changing only its action (to `addTag`) must leave every condition byte-for-byte identical in `msgFilterRules.dat`. |
| `security-direct-send-blocked.json` | Direct sending is off by default: `sendMail` with `skipReview`, `replyToMessage` and `forwardMessage` with mode `send` are refused and nothing is delivered; `saveDraft` still works. |
| `security-disabled-tools.json` | Tools turned off in the settings are missing from `tools/list` and refused when called anyway; `listAccounts` cannot be turned off. |
| `cold-start.json` | The MCP client starts before Thunderbird (`bridge_first`): without Thunderbird and with no tool list cached yet, `tools/list` is answered with an empty list after the bridge's first-start wait rather than an error, `initialize` declares `tools.listChanged`, and `notifications/tools/list_changed` follows once Thunderbird is up; then the tools are listed and work. Fixed in 0.19.2 (commonpost/thunderbird-mcp#132); up to 0.19.1 that first `tools/list` failed. |
| `cold-start-late-thunderbird.json` | Thunderbird starts 10 s after the bridge's first `tools/list`, past the 5 s the bridge used to wait: that `tools/list` is answered with Thunderbird's own list within 20 s, and no `list_changed` follows. |
| `read-only-mode.json` | With `COMMONPOST_MCP_READ_ONLY=1` for the bridge (`bridge_env`), `tools/list` keeps only the 14 tools that change nothing (`getMessage` and `getMessages` without `saveAttachments`) and drops the 27 others; the read tools work; `getMessage` with `saveAttachments`, write tools and an unknown tool called anyway are refused by the bridge, and nothing is saved, sent or created. |
| `security-account-restriction.json` | With only Local Folders allowed, the other account and its identity are hidden, and reading its messages, listing its filters or writing from its identity is refused. |
| `security-attachment-paths.json` | Attachments pointing at secrets (the extension's own connection file, system files, `..`, dotfiles, key files) are refused and the message is not saved; a harmless file is still attached. |
| `security-fail-closed.json` | Unreadable restriction settings fail closed: every tool that can be turned off is off and no account is visible. |
| `security-profile-elsewhere.json` | The lab's profile is outside every default location: its files, its mail store and a `..` path into it are refused as attachments; a harmless file is still attached ([GHSA-65cp-vcv7-cf69](https://github.com/commonpost/thunderbird-mcp/security/advisories/GHSA-65cp-vcv7-cf69)). |
| `security-special-folders.json` | The Trash, Inbox, Junk, Templates and Drafts folders are not renamed, moved or deleted, and their messages stay; no top-level folder is created or renamed with a special folder's name; nothing is created or moved into Templates; an ordinary folder is still created, renamed and deleted ([GHSA-whwf-gghm-mwx9](https://github.com/commonpost/thunderbird-mcp/security/advisories/GHSA-whwf-gghm-mwx9)). |
| `security-event-tools-task-id.json` | With `updateTask` and `deleteTask` turned off in the settings, `updateEvent` and `deleteEvent` refuse the id of a task (`Event not found`) and the tasks stay unchanged in `listTasks` and in the `.ics` file; `updateTask` called anyway is refused; `deleteEvent` still deletes an event ([GHSA-8v8g-5qhh-q3wr](https://github.com/commonpost/thunderbird-mcp/security/advisories/GHSA-8v8g-5qhh-q3wr)). |
| `security-http-idle.json` | A client that opens a connection and never finishes its request is cut off by the extension's HTTP server (the `labHttpIdle` step). |
| `direct-send-refusals.json` | Direct sends are refused without recipients or above the large-message size; a normal one goes out. |
| `send-dryrun-refresh-headers.json` | `sendMail` with `dryRun`, `getMessages` with `headersOnly`, `listFolders` with `refresh`. |
| `headers-unsubscribe.json` | `getMessages` with `headersOnly` returns the parsed `List-Unsubscribe` and `List-Unsubscribe-Post` headers of seeded messages. |
| `attachment-flags.json` | Which seeded messages Thunderbird flags as having attachments (what attachment search can find), and a message of 40,000 MIME parts, over the part limit. |
| `attachment-search.json` | `searchMessages` with `attachmentName` and `attachmentType` on seeded local messages; two checks are `xfail` because Thunderbird does not flag a signed message as having attachments. |
| `attachment-search-encrypted-allowed.json` | Attachment search with encrypted content allowed in the settings. |
| `calendar-reminders.json` | Reminders set by `createEvent` and `updateEvent` and returned by `listEvents`; seeded alarms that are not display alarms are kept. |
| `contacts.json` | `createContact` and `searchContacts` on the Personal Address Book: every field, several addresses, a phone-only contact, a birthday without a year; search by name, organization, a secondary address, a domain, several words, in any case, `format: table` and `maxResults`; the errors of `createContact`, `searchContacts`, `getContact`, `updateContact` and `deleteContact`. The success paths of `getContact`, `updateContact` and `deleteContact` are not covered yet (a `$lab_value` can pass the id of a created contact to a later step). |
| `tasks-categories.json` | `listCategories`; `createTask` (direct, in a named calendar, in the default one); `listTasks` (all, by calendar, completed, `dueBefore`, `maxResults`, table); `updateTask` (complete, reopen, percentage, title, due date, description, priority) checked against `listTasks` and the `.ics` file; `deleteEvent` of a seeded event; the errors of these tools. |
| `messages-update.json` | `updateMessage` on seeded messages: read, flag, tags (added, removed, filtered on), one call on several messages, a move to a new folder, a move with tags, trash; every change is read back with `searchMessages`. Errors: both `messageId` and `messageIds`, `moveTo` with `trash`, unknown message or folder, a move into a Templates folder (refused). |
| `messages-delete-display.json` | `deleteMessages` (Inbox to Trash, several messages at once, from the Trash for good, from Drafts to the Trash of Local Folders) and `displayMessage` (3pane, tab, window); their errors. |
| `folders-trash-junk.json` | `renameFolder`, `moveFolder` and `deleteFolder` (to the Trash, then for good), the messages of the folder following it; `emptyTrash` (one account, then all accounts, the seeded POP3 Trash mbox file emptied on disk) and `emptyJunk`; `getAccountAccess`; their errors. |
| `filters-order-delete.json` | `reorderFilters` and `deleteFilter` on a seeded list holding a forwarding rule (both refused while it is there, deleting the rule itself allowed): the order shown by `listFilters` and written to `msgFilterRules.dat` after moves and deletions; their errors. Only moves towards the top of the list are checked. |
| `account-access.json` | With only Local Folders allowed: `getAccountAccess` reports the restricted mode and one account; the write tools aimed at the hidden account (`emptyTrash`, `emptyJunk`, `updateMessage`, `deleteMessages`, `displayMessage`, `renameFolder`, `deleteFolder`, `moveFolder`, `reorderFilters`, `deleteFilter`) are refused and its mbox files stay as seeded; without an account, `emptyTrash` and `emptyJunk` act on Local Folders only. |
| `getmessage-options.json` | `getMessage` options on seeded POP3 messages: paging a long body (`maxBodyChars`, `bodyOffset`); the body formats of an HTML message (hidden elements removed and counted) and of a multipart/alternative one; `rawSource` as text and as base64; inline images with `includeInlineImages`; `saveAttachments` (two attachments of one name, files attached again to a draft and compared byte for byte); a message with only an attachment, with no Subject, with an attachment with no name, with empty bodies; an encrypted message (content withheld, its reply and forward refused); a detached attachment (a link, never opened; the forward is refused); an attached message (`message/rfc822`) saved whole and attached again to a draft. Errors: unknown message or folder, arguments out of range. |
| `getmessage-hidden-text.json` | `getMessage` removes from an HTML body, in the markdown and text formats, the text a reader does not see and keeps the text a reader sees: an MJML layout (column wrappers with `font-size:0px`, text that sets its own size) is returned whole; sizes and opacities such as `0.875rem`, `0.9em` and `opacity:0.8` are kept, and a child that sets its own `font-size` or `visibility:visible` is shown; tiny and zero sizes, an opacity product below 0.1, an override Gecko drops, a hidden child of `<pre>` and the URL of a link with hidden text are removed and counted. Inline styles only. Up to 0.19.1 the MJML text and the small sizes were removed, while `1px` text and the hidden child of `<pre>` were kept. |
| `getmessage-hidden-text-engine.json` | `getMessage` judges hidden text on the message as Thunderbird's message pane renders it (markdown and text formats): text hidden by selectors on Thunderbird's own wrapper, header tables and print-only class, pushed out of a clipping box by the message's own (`cid:`) image, white on white, in a box of height 0 with `overflow:hidden`, or transparent without a background painted through it is removed; gradient text (`background-clip: text`), text in an inline box with `overflow:hidden` and the alt text of images that do not load are kept; a message with URLs of Thunderbird's message protocols (`mailbox:`, `imap:`, `cid:`) in `src`, `srcset`, `<audio>` and `<iframe>` is rendered, those URLs made inert first. The hidden preference `hiddenTextDiagnostics` makes `getMessage` say which judge decided. `xfail` until the release after 0.19.2. |
| `read-several.json` | `getMessages` (setting `getMessagesLimit`): several messages with `bodyFormat`, a body cut by `maxBodyChars` and continued with `bodyOffset`, `rawSource`, `saveAttachments`; item errors next to the messages that were read; a call above the limit, an empty list, an item without `folderPath`. With `headersOnly`: item errors, a call that runs out of its result budget (the rest is reported as not read), 201 items refused. |
| `draft-update.json` | `saveDraft` with `draftId` on drafts seeded in Drafts: changed fields replace, omitted ones are kept (recipients, body, subject, reply headers, priority, Reply-To, language), an empty `cc` clears the line, a new body keeps the cite line and the quote of a reply draft (plain and HTML), `isHtml` turns a plain draft into HTML, `from`, an identity key that is gone; `searchMessages` shows the old version is gone. Errors: unknown draft, not a Drafts folder, unknown folder, refused attachment path, encrypted draft (nothing changed). |
| `draft-update-attachments.json` | `saveDraft` with `draftId` and attachments: kept and added, `keepAttachments: false` (with and without a new one), a file attached by path, a forward draft whose intro is replaced (forwarded message and attachment stay), `keepQuote: false`, a detached attachment (refused unless it is dropped). |
| `calendar-update.json` | `listEvents` (one or all calendars, table, `maxResults`, a weekly series with a moved occurrence, a daily series cut by `maxResults`, read-only and unknown calendars, bad dates); `updateEvent` on seeded events read back with `listEvents` and in the `.ics` file (title, dates timed and all-day, location, description, status, `showAs`, categories, online meeting, reminders, warnings for a recurring event) and its errors; `deleteEvent` of a whole series. |
| `calendar-create.json` | `createEvent` with `skipReview` (`blockSkipReview` off) in an empty `.ics` calendar: every field, all-day events (one day, a same-day end, an `endDate` that is the last day), the default calendar; the errors (bad dates, an end not after the start, unknown, read-only and disabled calendars, status out of the list, negative reminder). |
| `security-calendar-skipreview-blocked.json` | `createEvent` and `createTask` with `skipReview` are refused by default (`blockSkipReview` is on) and nothing is created; without `skipReview` the review dialog still opens. |
| `security-calendar-skipreview-separate.json` | The block of `skipReview` for events and tasks (`blockCalendarSkipReview`) turned off, the one for mail left on: `createEvent` and `createTask` with `skipReview` add the event and the task at once, while `sendMail` with `skipReview` and `replyToMessage` / `forwardMessage` with `mode: "send"` are still refused and nothing reaches the SMTP sink (`xfail` until the release after 0.19.2). |
| `calendar-skipreview-own-block.json` | The reverse: the block for mail turned off, the one for events and tasks on: a direct `sendMail` is delivered, while `createEvent` and `createTask` with `skipReview` are refused with a message naming their block and create nothing (`xfail` until the release after 0.19.2). |
| `calendar-review-outcome.json` | The dialogs of `createEvent` and `createTask`, driven with the keyboard (`labKeys`): the `reviewId` of the result; `listEvents` / `listTasks` with it read a dialog closed with Escape as closed (nothing created), an event saved with Ctrl+S then Ctrl+L as saved once, with an `eventId` that `updateEvent` accepts, and a task saved with Ctrl+L as saved with its `taskId`; an unknown `reviewId` is refused, and `listTasks` does not read the dialog of an event (`xfail` until the release after 0.19.2; with 0.19.2 the event saved twice is there twice). |
| `reply-forward-options.json` | `replyToMessage` and `forwardMessage` (drafts, and direct sends with `blockSkipReview` off): the recipients Thunderbird computes (Reply-To, reply all, Mail-Followup-To), explicit `to`/`cc`/`bcc`/`from`, attachments, HTML, `latestInThread` (the newest message of the thread, here the user's own answer in Sent), a reply to an own message, forward with its attachment; a reply and a forward delivered to the local mailpit sink; the errors (send without `to` or `from`, `latestInThread` with `send`, unknown message, folder or sender, refused path). |
| `reply-catchall-identity.json` | An identity with a catch-all address: reply and forward drafts are sent from the address the message was written to (To, Cc, Delivered-To); an address the hint does not match keeps the identity's; an explicit `from` wins. |
| `signature-file.json` | An identity whose signature is a text file: it follows a new draft (plain and HTML), a reply (after the reply and the quote) and a forward (after the forwarded message); `saveDraft` with `draftId` replaces only the typed text of a reply draft and keeps the quote and the signature. |
| `compose-options.json` | `sendMail` direct sends with an inline attachment and an attachment by path (Organization header, automatic Bcc, both attachments in the delivered message) and in HTML; their errors (unknown sender, refused path, bad base64) and `dryRun`; compose windows opened for a new message, a reply and a forward (the call answers, nothing is sent or saved; what the window shows cannot be observed). |
| `search-recent-errors.json` | `searchMessages` `searchBody` and its refusals, bad dates, `legacy` with `groupBy`, a bare operator, an encrypted message never listed by a search; `getRecentMessages` (the encrypted row, table, offset); `updateMessage` and `emptyTrash` errors (empty folder or `moveTo`, both `messageId` and `messageIds`, an empty id reported in `notFound`, invalid tags dropped). |
| `delete-task.json` | `deleteTask` in an ics calendar seeded with an event and four tasks (one completed, one recurring): a task is deleted with no review dialog and with the default `blockSkipReview`, and is gone from `listTasks` and from the `.ics` file, the other tasks stay; a recurring task goes with its whole series and a warning. The id of an event is refused (`Task not found`) by `deleteTask` and by `updateTask`, and the event stays unchanged in `listEvents` and in the file. Errors: a task already deleted, an unknown task, another calendar, an unknown calendar, a read-only calendar, no `taskId`, no `calendarId`. The tool's definition in `tools/list`. The tasks deleted are seeded ones. |
| `new-tools-disabled.json` | With `disabledTools` holding one tool and no `knownTools` (a restriction saved before 0.19.0), `deleteTask`, the tool 0.19.0 adds, is off: missing from `tools/list`, refused when called anyway, nothing deleted; the other tools of 0.18.2 stay on. Without a restriction the tool is on: see `delete-task.json`. |
| `search-fields.json` | `fields` of `searchMessages` and `getRecentMessages` on seeded POP3 messages (flagged and tagged, with a Cc, with an attachment, encrypted, with a copy in Archives): rows cut to the asked columns plus `id` and `folderPath`, in objects and table format (one column, several, none, with a query, a page, a JSON string); the columns an option or a state adds stay (`attachments`, `dupLocations`, `encrypted`); the number of matches is unchanged. Refused with `countOnly`, `groupBy` and `format: legacy`; a name that is not a column or a value that is not a list is refused by the schema. |
| `getmessage-authentication.json` | `getMessage` and `getMessages` return `authentication` (SPF, DKIM, DMARC, `authservId`, domains, selectors) from the topmost `Authentication-Results` header of seeded POP3 messages: a folded header with two DKIM signatures; a message without the header has none; a header written lower down is never read, also when the topmost one holds no result; a header with no server name, comments, upper-case domains and an invalid selector is cleaned. |

The filter scenarios used to mark as `xfail` the checks that failed because of the filter id and value-typing issues
of the original thunderbird-mcp, discussed publicly in its pull requests
[#175](https://github.com/TKasperczyk/thunderbird-mcp/pull/175) and
[#195](https://github.com/TKasperczyk/thunderbird-mcp/pull/195). Commonpost fixed them: those checks are strict now.

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
file as standard streams, except the bridge, whose input and output are its own pipes to the harness, and the short
runs of xdotool for `labKeys`, whose output is a pipe to the harness. Do not add
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

- on pushes and pull requests to this repository, and every week, against the **latest published release** of
  [commonpost/thunderbird-mcp](https://github.com/commonpost/thunderbird-mcp):
  the release must not be a draft or a pre-release, its tag must resolve to the same commit through git and through
  the GitHub API, the checked-out commit must be that one, and the manifest version must match the tag;
- manually (`workflow_dispatch`) against the latest release, its `main` branch, or a given **pull request**
  (the pull request head is resolved through the API and through git, and both must agree).

`.github/workflows/coverage.yml` measures the statement and branch coverage of the harness (`harness/lab.py`) with
coverage.py, on `main` and every week: the validator cases, the harness tests (`selftest/harness-tests.py`), the
public scenarios against the latest release, and the self-test, all run in a variant of the image that adds
coverage.py (`docker build --target coverage`; the default image is unchanged). It fails below 90% of the statements
or 80% of the branches; see `selftest/coverage.sh`.

The code under test is only checked out and streamed into the lab container: it is never executed on the runner. The
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
- The lab can press keys in a Thunderbird window (`labKeys`, with `xdotool` on the Xvfb display), which drives the
  event and task dialogs that `createEvent` and `createTask` open for review. Compose windows are still not driven by
  any scenario: for mail the lab relies on drafts, or on direct delivery to the local mailpit sink when a scenario
  explicitly lifts the default `skipReview` block for itself.
- The bridge runs on the Node.js version shipped by Debian 13 (20.x).
- Results are data produced next to the code under test (see above). Output is bounded: result JSON 3 MB, container
  output 20 MiB, mbox files read up to 20 MB.

## Relationship to thunderbird-mcp

thunderbird-mcp is written and maintained by Tomasz Kasperczyk and its contributors, under the MIT license.
[Commonpost MCP for Thunderbird](https://github.com/commonpost/thunderbird-mcp) is an independent continuation of it,
maintained by the commonpost organization, and this lab is its end-to-end test suite. Neither is affiliated with or
endorsed by the thunderbird-mcp project. The lab does not ship any extension code: the code under test is whatever
tree you point the launcher at.

The lab started as a quality-assurance tool offered to thunderbird-mcp; since October 2026 its CI tests Commonpost.
It remains free to use, under the MIT license, for thunderbird-mcp or any other derivative.

Thunderbird and Mozilla are trademarks of the Mozilla Foundation. This project is not affiliated with Mozilla.

## Contributing, security, governance

- [CONTRIBUTING.md](CONTRIBUTING.md): how to propose scenarios and changes (please read the rule about undisclosed
  vulnerabilities).
- [SECURITY.md](SECURITY.md): how to report a vulnerability, in this lab or elsewhere, and how reports are handled.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md): Contributor Covenant 2.1.
- [GOVERNANCE.md](GOVERNANCE.md): roles and how decisions are made.
- [ROADMAP.md](ROADMAP.md): what the lab intends to do, and not do, over the next year.
- [docs/architecture.md](docs/architecture.md): components, data flow and CI.
- [docs/assurance-case.md](docs/assurance-case.md): threat model, trust boundaries, why the security requirements are
  met, and known limits.

## License

MIT, see [LICENSE](LICENSE). The image built from `harness/Dockerfile` contains third-party software under its own
licenses, none of which is stored in this repository: Thunderbird (Mozilla Public License 2.0 and others, see
`about:license`), mailpit (MIT) and Debian packages. This repository does not publish images.
