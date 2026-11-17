# AI accounts and roles

Static scripts that load this manifest select AI provider accounts and
user-defined roles from `$AGENT_HOME/ai-accounts.json`. Installation creates no
accounts, roles, or session selections. A missing file or an otherwise valid
manifest without `models` fails with `models is not configured`. When `models`
is set, null or empty `accounts`, `roles`, `sessions`, `lanes`, and
`usage_accounts` leave those collections empty. An absent `implementer` uses
`models.first`; neither selection authorizes a launch without lane bindings.
Every model-lane launch also requires an explicit top-level `lane_execution`
declaration written by the local harness. Missing or invalid declarations fail
closed because Agent cannot infer whether the current device is a user's work
device.
There is no
implicit provider profile, model, or role from the host environment, hub
pairing, or built-in defaults.

The repository ships no driver table either. Every provider that accounts,
`models`, `implementer`, lane slots and usage selections name is a driver that
the operator describes in a second file, `$AGENT_HOME/drivers.json`: its
executable and argument templates, how the spec and the output travel, its
configuration-directory variable, the credential variables to scrub, capability
flags, the usage adapter, the attribution display name and the terminal pane
markers. See [Drivers](#drivers). When anything names a driver and
`drivers.json` is missing or does not describe it, the load fails with an error
that names `drivers.json` and this document.

This device-local manifest is used by `agent lane run`, the lane steps of
`agent run`, interactive session starts, optional usage reads,
and, for its `models` and `implementer` keys only, by every task creation (`agent task create`,
the issue coordinator, error-fix tasks) and `agent models`; an unreadable file, an
unknown top-level key, a malformed model selection or an invalid `drivers.json`
therefore stops task creation. `agent task create` and error-fix tasks ignore errors inside
`accounts`, `roles`, `sessions`, `lanes` or `usage_accounts`; the issue
coordinator validates the whole manifest, including `lane_execution`, before it
accepts a task.
It does not implement the complete issue-to-PR coordinator or a sandbox.

The following is an **operator-supplied example**, never an installed default.
It configures model slot `first` = `driver-a` and model slot `second` =
`driver-b`, two drivers that `drivers.json` describes, and binds the lanes
device-wide:

```json
{
  "lane_execution": {
    "mode": "this-device-is-lane-host"
  },
  "models": {
    "first": "driver-a",
    "second": "driver-b"
  },
  "accounts": {
    "driver-a-profile": {
      "provider": "driver-a",
      "config_dir": "/operator/driver-a-config"
    },
    "driver-b-profile": {
      "provider": "driver-b",
      "config_dir": "/operator/driver-b-config"
    }
  },
  "roles": {
    "first-builder": {
      "account": "driver-a-profile",
      "model": "operator-selected-model",
      "access": "workspace-write"
    },
    "first-reader": {
      "account": "driver-a-profile",
      "model": "operator-selected-model",
      "access": "read-only"
    },
    "second-reader": {
      "account": "driver-b-profile",
      "model": "operator-selected-model",
      "access": "read-only"
    }
  },
  "lanes": {
    "driver-a:implementer": "first-builder",
    "driver-a:reviewer": "first-reader",
    "driver-a:pr-reviewer-quality": "first-reader",
    "driver-a:pr-reviewer-logic": "first-reader",
    "driver-b:pr-reviewer-quality": "second-reader",
    "driver-b:pr-reviewer-logic": "second-reader"
  },
  "sessions": {}
}
```

## Lane execution device

`lane_execution` has exactly one accepted form. The external lane host marks
itself explicitly:

```json
{
  "lane_execution": {"mode": "this-device-is-lane-host"}
}
```

Agent does not provide its own remote lane transport or workspace mirroring.
The local harness's lane starter starts the Agent lane command on the external
lane host, with its workspace and provider configuration already present there.
Every lane child, including subprocess, tmux, and injected-runner paths, clears
the four GitHub token environment variables and receives a fresh empty temporary
`GH_CONFIG_DIR` that exists for the lifetime of that child. It never inherits an
ambient `gh` login from the lane host.
Interactive tmux sessions may still use `$AGENT_HOME/runtime-targets.json` after
the `models` and `lane_execution` checks pass. The `remote` mode, remote
manifests, and copy-back are not supported by Agent.

`--no-tmux` changes only the process holder and never authorizes execution on
an undeclared device. Omitting `lane_execution`, adding fields, or using any
other mode refuses all model-lane starts with:

```text
lane_execution is not configured: lanes never run on a work device; start lanes through the local harness's lane starter on an external lane host, or mark this device as a lane host
```

The rejected `remote` mode also reports that it is not supported. Model
display and other read-only configuration inspection do not launch a lane and
remain available without this declaration.
`lane_execution` gates provider execution on this device; recording an Agent
for a lane started elsewhere does not need it.

Add as many named accounts, roles, and bindings as needed. No fixed account
list, role list, or count is built in. Configurable role names are chosen by
the operator; they are distinct from the fixed workflow kinds (`implementer`,
`reviewer`, `pr-reviewer-quality`, `pr-reviewer-logic`, `check`) and from the
driver ids that `drivers.json` configures. The `check` kind is
read-only and is required for the provider of model slot `first` when a worker
enables orders, through lane slot `<first>:check`. For an order worker without
a separate implementer, the implementer and first review roles must name one
model; with a separate implementer, the first review roles name one model among
themselves and the implementer may use another. Those workflow kinds are
protocol capabilities used when resolving a lane slot, and drivers are operator
configuration; neither is a preinstalled user role.

## Model slots

The lane chain has two logical review slots. Model slot `first` reviews the
inner loop and runs the first PR review pair; model slot `second` runs the
second PR review pair. The required top-level `models` block names the provider
of each review slot:

- `first`: a driver that `drivers.json` configures with a `lane` section
- `second`: a driver with a `lane` section, different from `first`,
  or JSON `null` when an A38 coordinator worker obtains the second review
  externally. An A38 worker with `a38_second_review: "model"` sets `second` to
  a provider.

The optional top-level `implementer` names the implementation provider. It may
be any driver that `drivers.json` configures, including a record-only driver
without a `lane` section. It must differ from
`models.second`; matching `models.first` is allowed and means there is no
separate implementation provider. When the key is absent, the implementer is
`models.first`. A record-only driver is an implementer value only and remains
invalid in either `models` slot. For example, a device may configure:

```json
{
  "models": {"first": "driver-b", "second": "driver-a"},
  "implementer": "driver-e"
}
```

A device selection outside the configured drivers fails with, for example,
`models.first must be one of driver-a|driver-b (see drivers.json,
docs/ai-accounts.md#drivers)`. A model pin stored on a task is checked for its
shape only, so a task recorded on another device stays readable.

Which provider a person works with is this device-local choice; no provider
name is fixed in the chain. Gate stages are `review-1` and `review-2`, with
checklist prefixes `review_1_` and `review_2_`, and the coordinator phases of
the two PR review pairs are `pr_gates_first` and `pr_gates_second`. Earlier
versions named these after the driver that ran the slot. Those stored names
stay readable through `recorded_slots` in `drivers.json` (see
[Recorded slot names](#recorded-slot-names)): CLI input accepts and warns about
them, and stored rows remain readable without a database rewrite. A device without a
`models` block has no slot assignment; task creation, device model display, and
launch configuration fail loudly.

Tasks recorded before model slots existed read their slots from
`recorded_slots` only to read or display their existing agent and gate rows; they
cannot start a new lane or write a new agent or gate row.
`agent models` prints `second=external` (or `external` for the selected slot)
for a pinned null second slot; JSON output preserves `null`. A null second slot
does not authorize an ordinary workflow to omit a review. The issue coordinator
accepts it only for a worker with explicit A38 mode and external reviewers. An
A38 worker with `a38_second_review: "model"` sets `second` to a provider.

A task pins the device's `models` when it is created (`task create`, the issue
coordinator, error-fix tasks). When the configured implementer differs from
`models.first`, the task also pins it in its own top-level `implementer` field;
the field stays absent when both are equal. Every later check for that task (`agent agent start` for `reviewer` and
`implementer`,
`gate record`, `allow`, `next`, `run`, the coordinator gates) uses the pinned
values, so changing the device's `models` or `implementer` configuration affects
only tasks created afterwards, whether by the CLI or error-fix. An existing
task's pin changes only through the explicit `agent task models` command below.
The issue coordinator is the exception: its task slots are the device AI
configuration pinned when the coordinator accepted the task, and
`agent task models` refuses coordinator tasks.
The coordinator's implementer is the task's `implementer` pin when present,
otherwise `models.first`, and its implementer lane runs on that provider. It
refuses an implementer driver that `drivers.json` configures with
`lane.may_run_commands: true`, and an implementer driver without a `lane`
section. Because slot `first` only runs read-only review lanes, `models.first`
may name a command-capable driver when a separate implementer is configured.
It hashes the device's `models`, `lane_execution`, and the `implementer`
selection only when it differs from `models.first` into each accepted
task's execution binding: a later change stops the task with "task execution
configuration changed" until the accepted configuration is restored. A task
accepted before the current hash formula accepts the matching legacy formula
once and is immediately repinned; this compatibility covers only
the newly added `lane_execution` field and the former omission of a `models`
value equal to the slots of `recorded_slots`. Other changes still stop the task.
Driver descriptions in `drivers.json` are not part of the execution binding, so
changing them does not stop an accepted task. For the PR reviewer roles, `agent agent start --stage review-1|review-2` requires the
provider pinned to that stage. A second-stage agent cannot start until both
first-stage dimensions are approved. `gate record` repeats the slot check and
verifies the agent's recorded stage. Older tasks without an `implementer` field use their
pinned `models.first`. Tasks recorded before model slots existed and therefore
missing `models` remain readable through `recorded_slots` (`first`, then
`second`) for existing agent and gate rows. Without that entry such a task
cannot be read and the command fails with `task <id> predates model pins;
configure recorded_slots in drivers.json to read it
(docs/ai-accounts.md#drivers)`. They cannot start a new lane or write
a new agent or gate row; recreate them after the local harness has written the
device's `models` selection. For a nonterminal legacy error-fix task, run
`agent task state <id> superseded`; the next `agent watch error-fix` scan ignores
the terminal task and creates a newly pinned task. The watcher performs no
staging or Git operation while that explicit transition is still required.
When `agent run --spec-file` resolves a record-only implementer driver (one
without a `lane` section), it deliberately uses the existing handoff path, names
the pinned provider as the implementer, and creates no agent row. A pinned
driver that `drivers.json` does not configure stops the command instead.

The issue coordinator refuses an implementer driver that `drivers.json`
configures with `lane.may_run_commands: true`: its implementer must not
execute software, and such a driver's write lane cannot be kept from running
commands. When the effective implementer is `models.first`, the error is
`models.first = <driver> is not supported by the coordinator: driver <driver> is configured
with may_run_commands, so its implementer lane cannot be restricted from
running commands`. A separate implementer reports `implementer = <driver> is
not supported by the coordinator: driver <driver> is configured with
may_run_commands, so its implementer lane cannot be restricted from running
commands`. It also refuses an implementer driver without a `lane` section,
because the coordinator cannot launch it: `implementer provider <driver> is
not supported by the lane launcher; expected one of: <drivers>`. `models.first`
may name a command-capable driver when a separate implementer is configured,
because slot `first` only runs the read-only `reviewer`, `pr-reviewer-quality`,
`pr-reviewer-logic` and `check` lanes. `agent lane run` and `agent run` accept
such a driver in `models.first`. A fixed setting that a lane must not take from
the account's own CLI configuration belongs in the driver's `lane.argv`
templates.

What a lane may read and write is the driver's argv templates: `lane.argv.read`
for a read-only lane, `lane.argv.write` for a write lane and the optional
`lane.argv.judgment` for a check lane without tools. A driver that configures
`lane.read_dir_args` receives an explicit read directory for control artifacts
on a read-only lane, and its write lane cannot receive an extra directory
(`write lane takes no extra directories`).

A driver with `lane.credential_file` keeps its key in
`<config_dir>/<credential_file.file>`. A generated shell wrapper reads it only
after launch and exports it as the configured `credential_file.env` variable;
the key never appears in argv. A missing, unreadable or empty key file ends the
lane as unavailable with `<driver> key missing`. Keep such an account's
`config_dir` outside every worktree and every added read directory. Launch is
refused when those paths overlap, for example with `<driver> worktree overlaps
account config_dir`. The model is the operator-supplied model id of the role;
there is no default. The `event-stream` parser stores reported token counts and
tool calls in `lane.run`; it stores no cost field a driver reports, because that
need not be the price of the endpoint the driver talks to.
Stored file-tool targets are fail-closed host evidence. Complete terminal
escapes are removed, separators are normalized, and regex backslash escapes are
removed first. A target containing `..` anywhere, `~`, a drive fragment, or `/`
at the start or immediately after a character outside `[A-Za-z0-9._-]` is stored
as `[redacted]`. A target containing a numeric or named escape (`\x`, `\u`,
`\U`, `\N{`, octal `\0` through `\7`, or `\c`) is also fully redacted without
decoding. Any percent escape of the form `%HH` is likewise fully redacted before
normalization, for both path and pattern targets; it is never decoded. A drive
fragment is one ASCII letter followed by `:` at that
same boundary, regardless of whether `/` follows. The only absolute-path
exception is a single plain whole target with no whitespace or regex
metacharacters other than glob `*`, `?`, `[` and `]`, starting with the absolute
worktree plus `/`; its relative remainder must pass the same checks and is then
stored worktree-relative.
Any raw ESC or C0/C1 control byte instead redacts the complete target before
terminal sanitization or path classification.

Lane final text, configured and reported model fields, diagnostics, agent notes,
terminal output, hub/GitHub errors, and evidence all use one shared pipeline:
one initial ESC/C1 search skips the structural rule entirely when the input has
no ESC/C1. For each controlled raw logical line, a compiled case-insensitive
candidate check runs over a control-byte-translated view. Only candidate lines
build the three primary views, one at a time: a control-stripped view
removes complete or unterminated controls and their payloads, a payload-kept
view removes the outer introducer, terminator, and CSI parameter bytes, and a
key-join view uses the assignment scanner's own classifier to retain only
control-sequence bytes that the scanner may join into a key. Those bytes include
a key-character CSI/ESC final and a `-` intermediate; string-control payloads
and all other control bytes are dropped. If any view contains a shared secret
keyword, case-insensitively, the raw logical line is replaced from the start of that
keyword's mapped whitespace token
through logical-line end. Standalone removable C0 bytes are removed from every
view without activating the rule by themselves. Nested string controls keep
their payload text at every nesting depth; their introducers and terminators
are omitted. Retained string-control payloads are checked in both a
payload-clean view and a payload-key-join view. The payload-clean view omits
complete nested CSI spans (parameters, intermediates, and final), other ESC
sequences, C0 bytes, and lone C1 bytes. The payload-key-join view contributes
exactly the key characters from nested CSI/ESC sequences that the assignment
scanner may join into a key and omits all other control bytes. The second view
is built lazily only for payloads that contain nested controls, and a keyword
in either view activates the line rule. An unterminated nested string control
fails closed and treats the whole logical line as matched. String-control spans are
indexed on the complete raw input first, so their payload newlines do not split
logical lines; a mapped start inside one moves to the introducer and the span is
never split by two replacement stages. An unterminated string control extends
through the end of the text and swallows every following physical line into its payload.
The same linear, nesting-aware scanner supplies every string-control span
boundary to redaction, terminal sanitization, logical-line splitting, and all
structural views, including both payload views.
Other normal lines remain independent, and controlled lines without a keyword
retain the existing control handling. A whitespace token exactly
`<config_dir>/` followed only by `[A-Za-z0-9._-]+` is the one structural
exception: keywords inside that placeholder do not trigger the line rule. Any
other byte in that token, or a keyword elsewhere on the line, still triggers
it. Lane sinks replace raw and control-interleaved config-directory paths on
the complete source before this structural rule, apply their length bound next,
and run precise redaction last. Credential assignments are
then classified on the full raw text, where
complete and unterminated OSC, DCS, PM, APC, and SOS spans are opaque and
payload whitespace is not a value delimiter. Before a complete secret keyword,
payload operators stay opaque so visible key fragments still join. After a
complete keyword, a terminal control ends an unquoted key only when, after
skipping whitespace and controls, a visible operator or, when none is visible,
a valid complete string-control payload operator occurs before any further key
character. Otherwise the control stays as an opaque key interleave, preserving
sticky keys such as `password` + control + `_file`. A raw whitespace token that
contained a string-control span and contains a secret keyword is redacted whole
as a fail-closed defence. The spans are then visibly redacted before whitespace
tokenization, including their payload. Every remaining raw token containing an
ESC or C1 sequence is redacted in place, including at a token boundary, while
tabs and newlines stay unchanged. The only exemption is one leading and/or one
trailing strict SGR wrapper whose parameter bytes joined to the visible text do
not match a secret pattern.
The pipeline then applies control-tolerant secret and raw/resolved config-path
redaction on raw text, terminal sanitization, a second redaction pass, then
bounding. Credential assignments use a lexical scan of complete, unbounded key
tokens, including quoted JSON keys. The scan is linear, except where whitespace
follows terminal controls that hold key characters (a key character (a letter,
a digit, `_` or `-`) among the parameter, intermediate or final bytes of a CSI
or ESC sequence, or a key character in a string-control payload);
such text costs time quadratic in the number of those controls. The bounded
control-run window of at most 64 runs in `TERMINAL_INTERLEAVE_PATTERN` applies
only to the raw regex consumers `_RAW_AUTH_HEADER` (authorization, set-cookie,
and cookie headers), `_RAW_BEARER` (generic Bearer tokens), `_RAW_KNOWN_TOKEN`
(`gh[pousr]_`, `github_pat_`, and `sk-` tokens), `_RAW_AKIA` (`AKIA` plus 16
of `0-9A-Z`), `_RAW_PRIVATE_PATH` (`/Users/` and `/home/` paths),
`_RAW_URL_AUTH` (control-tolerant URL schemes), and `_RAW_PRIVATE_KEY`
(private-key header words). CSI/ESC parameter and intermediate bytes are
matched with a fixed window of at most 64 bytes each
(`_MAX_ESCAPE_PARAMETER_BYTES`). The linear view matcher has no control-run
limit. Lane diagnostics (`lane.redact_lane_text` / `hide_config_dir`) cover
the configured config directory. The gh config directory and signing key are
covered in GitHub activity execution errors
(`github_act._safe_execution_error`). Coordinator path roots (the control dir,
the worktree, the workspace, the agent home, and the user home) are covered
via `replace_path_roots`. The device bearer is covered in hub assignment text.
Only the device bearer additionally gets the
percent-decoded views. For bounded lane fields, the
controlled-line structural rule first sees the complete raw source. Its result
is then cut to the limit plus a small inspection margin before the remaining
matching; a token split by that cut is redacted in full before the ordinary
truncation marker is added.
The known-secret pass builds a fixed set of views and searches each one with
KMP, overlaps included. Only the device bearer additionally gets the
percent-decoded views. Percent-tolerant matching of that bearer also searches a
percent-decoded copy of each view. The raw bearer text is percent-decoded once,
and when that decoded text contains a terminal control the same control views
are built on it and searched for the literal bearer. Their maps compose through
the decoded text back to raw spans, and those views are not percent-decoded
again. Consecutive `%HH` triplets (either hex case)
are one UTF-8 byte stream, decoded in a single pass: a valid sequence becomes
one character mapped to the raw span of all of its triplets, and an invalid or
truncated sequence falls back to one character per byte so those bytes are not
merged into a character they do not encode. An ASCII `%HH` stays one character
spanning its three-character raw span, and every other character is copied.
The literal bearer is matched on the undecoded view
and on that decoded view, and on the control views of the once-decoded raw
text. The union of the mapped spans is redacted. Raw,
fully encoded (`%25` for `%`), and mixed per-character forms of the bearer are
covered. A literal `%` in the bearer that itself starts a valid `%HH` next to
encoded neighbours in the same run is read decoded, so `a%41` in `%61%41` is
not matched. The bearer, including one that contains `%`, takes time linear in
the view length plus the bearer length. The views
are the raw text, text that drops complete string controls, complete ESC/CSI
sequences, and C0/C1 bytes, text with only
introducers, terminators, and C0/C1 bytes removed, and text with complete
ESC/CSI sequences removed while string-control payloads stay. One further view
keeps an ESC final that also introduces a string control and drops `ESC \`.
Another keeps every escape final, including the `\` of `ESC \`, while dropping
introducers, CSI parameters and intermediates, ESC intermediates, and C0/C1
bytes, and it keeps string payloads. Another keeps only each control's
introducer byte. Another keeps only each control's final or string terminator
and drops string payloads. Each view is searched with KMP, and so is the
device bearer's percent-decoded copy, so a repeated prefix does not rescan
the text per candidate.
A control's introducer, parameter, intermediate, final, or terminator byte
counts as a secret character only as far as a single view yields it. An
occurrence that needs different treatment
of two controls is not matched, for example one control's final byte used as a
secret character while another complete control between later secret characters
is skipped (`/opt/\x1b[31m\x1b[0my profile` for `/opt/my profile`), or one
control's introducer and final both used with parameters between them (`Q[w`
in `Q\x1b[32!w`). Such text never renders the secret on a terminal and does
not contain it contiguously in raw form; matching every per-control combination
would need a non-linear matcher.
String-control boundaries still come from the shared nesting-aware scanner.
Seven-bit DCS, PM, APC, and SOS strings accept
both the 8-bit ST byte and `ESC \`; an unterminated string control is redacted
through the end of the text. BEL terminates OSC only; DCS, PM, APC, and SOS
continue through BEL until an ST spelling.

`agent agent start --role reviewer --vendor X` requires `X` to be `models.first`
pinned on the task;
`--role implementer` requires the task's resolved implementer. The chain uses
that same provider (closing
`implementer_done` or `conflicts_resolved` of a task created before model slots,
which carries no pin, also accepts a finished row of the second provider); `agent gate record
--stage S --vendor X` requires `X` to be the provider of the slot `S` stands for.
`agent models implementer [--task <uuid>]` prints the device selection or task
pin by the same rules as `agent models first|second`.

## Change the provider of a task slot

The pinned provider is the default selected when the task was created. To run
one slot of an existing task with another provider, change the pin explicitly:

```bash
agent task models --id <task-uuid> --slot first|second|implementer \
  --vendor <provider> --reason "<why>"
```

`--reason` is required and must not be empty. The command appends the change to
the task's `model_changes` history with the old provider, the new provider, the
reason and the time. Later agent rows of that slot require the new provider;
for review slots, later `agent gate record` calls do as well. Agent and gate
rows already recorded keep their vendor. The command refuses `done`, `merged`
and `superseded` tasks, and coordinator tasks because their model slots are the
device AI configuration pinned when the coordinator accepted them. It also
refuses a task without a model pin, and a change of `second` when the task's
second review is external.

If the implementer cannot run, finish its agent row with
`agent agent finish --id <implementer-uuid> --verdict blocked`; the task becomes
`failed`. Change the implementer with `agent task models`, then start a new round
with `agent round start --task <task-uuid>` and start the implementer with the
new vendor. If a PR reviewer cannot run, finish its agent row with
`agent agent finish --id <reviewer-uuid> --verdict unavailable`, then
`agent gate record --verdict unavailable`, then
`agent close-step --status unavailable` for the matching gate key, and then
change the pin. No fallback happens automatically.

For a task pinned with `second: null`, `gate record --stage review-2` is refused;
the external result is recorded in the two `review_2_*` checklist keys with
`external second review by ...` evidence. That evidence counts only on a
coordinator A38 task whose stored second review for the evidence head is
posted and whose newest review does not block the merge.

## Required fields

Each account requires:

- `provider`: a driver id that `drivers.json` configures with `home_env`
- `config_dir`: absolute path to that profile's driver CLI configuration
  directory (no NUL, newline, CR, or parent traversal); the role prefix points
  the driver's `home_env` variable at it. When the driver configures
  `home_default_dir` and `config_dir` resolves to that directory, the prefix
  leaves the variable unset instead: some CLIs keep the login of their default
  profile outside that directory, so pointing the variable there would start an
  empty, logged-out profile. Any other directory is a named profile the
  operator logged in with the variable set. A driver without `home_default_dir`
  always sets the variable, also for a directory that another driver names as
  its default.

Each role requires:

- exactly one of
  - `account`: name of a configured account, or
  - `accounts`: an [account pool](#account-pools), a list of at least two
    distinct configured account names
- `model`: explicit model id string (no default)
- `access`: `read-only` or `workspace-write` (no default)

A role with both keys or with neither is rejected with an error naming the
role.

### Account pools

A role that names `accounts` uses a pool of subscriptions of one provider
evenly instead of one subscription until it is exhausted. Nothing is pooled
unless the operator writes the list; there is no built-in pool and no fallback
account. Example (placeholder names):

```json
{
  "accounts": {
    "seat-1": {"provider": "driver-w", "config_dir": "/operator/path/seat-1"},
    "seat-2": {"provider": "driver-w", "config_dir": "/operator/path/seat-2"}
  },
  "usage_accounts": ["seat-1", "seat-2"],
  "roles": {
    "pooled-builder": {
      "accounts": ["seat-1", "seat-2"],
      "model": "operator-selected-model",
      "access": "workspace-write"
    }
  }
}
```

The load rejects a pool whose members are unknown, listed twice, fewer than
two, or of different providers; whose provider's driver has no `windows` usage
adapter; or with a member that is not listed in `usage_accounts` (the error
names the role and the account). A pool role cannot be a session's
`interactive` role: an interactive session and its usage reads need one fixed
account. The pool's members share the role's `model` and `access`, and lane
slot provider checks use the members' common provider.

Only a coordinator lane start chooses a pool member: the implementer and
inner-reviewer lanes, the PR review lanes, the check lane, and each lane
attempt of the conflicts worker. Every fresh start chooses at that moment, from
local data only (no network request):

1. Login: the member's identity from its `account_file`, as the window-usage
   poll reads it. No login → not eligible (`no login`).
2. Usage: the newest own `usage.snapshot` for that driver and login, with the
   same match the poll uses (equal vendor, `account_email` equal without regard
   to case). None → not eligible (`no usage snapshot`). The snapshot's
   `fetched_at` age is not checked: a snapshot is stored only when a value
   changed, so an old one is still current.
3. Load: the largest `used_percent` of `windows.five_hour` and
   `windows.weekly`. A `null` window counts 0 %, and so does a window whose
   `resets_at` is at or before the start time, because it has reset since the
   snapshot. `weekly_models` is not used: its names are display names that do
   not map to role models. A load of 100 % or more → not eligible
   (`exhausted at N %`).
4. Running lanes: the agent rows of this device that are still `working` and
   recorded this account.

The eligible member with the smallest `(running lanes, load, position in
accounts)` is chosen. Lanes started close together spread across the pool
first, because a new lane's usage shows up in the snapshots only after the
next poll; among members with equally many running lanes the least-loaded one
wins, and on a tie the earlier member in the list. Without an eligible member
the lane does not start: the error `Role <role> has no usable pool account
(<account>: <reason>; ...)` names every member and its reason.

The launched process is isolated to the chosen account like a single-account
role (its `config_dir`, see [Process isolation prefix](#process-isolation-prefix)).
The lane's agent row records the account name as `account` (every coordinator
lane records it, also for a single-account role) and, for a pool, one line
`account_choice` with the chosen account name, its load and its running lanes;
neither holds a login identity or a credential. Every later step of the same
lane (recovery after a restart, output capture, result evaluation) uses the
recorded account and never chooses again. A recorded account that is no longer
the role's account or no longer a member of its pool is an error for that lane.
A lane recorded before pools existed has no `account`: a single-account role
uses its account; a pool role is an error naming the lane. A conflicts-worker
lane attempt finishes within one call and writes no agent row; it keeps its
chosen account for the whole attempt. `agent lane` and the `agent run` lane
launcher do not choose from a pool: a pool role is refused there when the
process isolation prefix is built.

The optional top-level `usage_session` selects an explicitly configured
interactive session for automatic billing reads. The driver of its interactive
role must have a `period` usage adapter (see [Usage adapters](#usage-adapters)).
Missing/null disables
those reads; it never chooses the host profile or the first available account.
The selected session must exist locally, be owned and active.
`agent watch session-usage` records a changed period snapshot as `usage.snapshot`.

The optional top-level `usage_accounts` is a list of explicitly named
accounts whose driver has a `spend` or `windows` usage adapter. Missing/null/`[]`
disables account polling. An account whose driver has a `windows` adapter is
read by `agent watch window-usage`; any other selected account must have a
`spend` adapter and is read by `agent watch account-usage`. Unknown names,
accounts whose driver has no `spend` adapter and no `windows` adapter and
duplicate names are rejected,
for example with `usage_accounts account <name> uses driver <driver>, which has
no spend usage section in drivers.json (docs/ai-accounts.md#drivers)` or the
same message with `windows`. `agent watch account-usage` records changed USD
key-usage values from the adapter's requests as `usage.snapshot`; it stores
only the configured spend fields and never the key. A spend account's key is
read from `<config_dir>/<usage.credential.file>`. With
`proxy_from_environment: false` its requests use an owned client with
`trust_env=False`, so ambient proxy and CA environment variables are ignored
and the bearer key cannot be routed through an intercepting proxy.

The device daemon polls `usage_accounts` on the same 60-second tick as the
period-usage read. Each scanner reads only its own accounts. A `windows`
account is read no more often than its adapter's `poll_seconds`
(1-86400 seconds); without pacing, as in `agent watch window-usage`, every
selected window account is read immediately. A rate-limit answer is reported as
`usage.snapshot error: <account>: <label> returned HTTP 429`, followed by
`; retry after <N> s` when its `Retry-After` header gives a usable number of
seconds, and postpones the next read of that account by its `Retry-After` when
that is longer than the adapter's `poll_seconds`. Usage-account failures are
reported and never stored as zero values.

Each `windows` account uses the login of its `config_dir`; the operator logs in
there with the driver's own tool, directly or through `agent account-login`
(see [Signing in a subscription](#signing-in-a-subscription)), which drives
that tool's own login. Agent never refreshes a token itself or reads the
refresh token. `agent watch window-usage` records changed subscription usage
from the adapter's configured requests as `usage.snapshot`:

- Identity: the configured `account_file.file`, read for `account_file.object`
  and `account_file.field`. `account_file.default_name` names the file in the
  home directory for a `config_dir` that is the driver's `home_default_dir`;
  otherwise the file is `<config_dir>/<account_file.file>`.
- Credentials: `credential.file` is the login file in the account's
  `config_dir`. A configured `credential.keychain` is read first on its
  `platform`; when that item does not exist, or on another platform, the file
  is used. A failed secret-store read falls back to an existing file and is
  reported only when there is no file. The configured `credential.object`,
  `token`, `expires_at`, `subscription` and optional `multiplier` fields select
  the login data; `label`, `token_name`, `login_name` and `renew` label errors
  and the renewal hint. `credential.expires_unit` is `epoch-ms`; when the
  subscription equals `multiplier.plan`, a match of its pattern in
  `multiplier.field` adds the captured value to the tier name.
- A missing login or an expired token skips the account quietly (the next
  start of the driver's tool, or the optional
  [token keep-alive](#token-keep-alive), renews the token); `agent watch
  window-usage` prints `usage.snapshot skipped <account>: <reason>`. Malformed
  files, a failed secret-store read without a credentials file, and request
  failures are reported as `usage.snapshot error: <account>: <reason>` and are
  never stored as 0 %.
- Request: every configured `requests` entry in order, with the access token,
  the fixed `Authorization` and `Accept` headers, its configured headers and an
  owned client whose `trust_env` follows `proxy_from_environment`.
- Snapshot: `vendor` and `provider` (the driver id), `account_email`, `tier`
  (the configured subscription plan, or null), and `windows` with `five_hour`,
  `weekly` (all models) and `weekly_models`
  (weekly limits scoped to one model, with the configured display-name key).
  `weekly` uses the configured pointer or the first all-models limit. Each
  window has `used_percent` and `resets_at` (UTC, rounded to the nearest
  minute); a window the response does not report, or whose configured
  `used_percent` is absent, is `null`, not 0 %. `fetched_at` records the scan.
  A snapshot is stored only when a value other than `fetched_at` changed. The
  token is never stored or logged.

## Token keep-alive

Some driver tools renew their login's access token only while they run; Agent
never renews a token itself. The token of a profile nobody uses therefore
expires, and from then on the window usage poll skips that account. The
optional top-level `token_keepalive` keeps those logins fresh:

```json
{
  "token_keepalive": {"refresh_before_minutes": 30}
}
```

- Missing or `null` is off, the default (DESIGN.md §19.8). Otherwise it is an
  object with exactly `refresh_before_minutes`, an integer from 5 to 480;
  anything else is rejected with an error naming `token_keepalive`.
- It acts only on the accounts listed in `usage_accounts` whose driver has a
  `windows` usage adapter; it has no account list of its own.
- The refresh command is the driver's `usage.keepalive.argv` in `drivers.json`
  (see [Usage keep-alive](#usage-keep-alive)). An account whose driver has no
  such section cannot be kept alive and is skipped with `driver <driver> has no
  usage.keepalive in drivers.json; it cannot be kept alive`.
- For each of those accounts it reads the login's expiry from the same sources
  as the window usage reader: the configured `credential.keychain` item of the
  directory first (on its `platform`), then `<config_dir>/<credential.file>`,
  using `credential.object`, `credential.expires_at` and
  `credential.expires_unit`. When the login expires within
  `refresh_before_minutes`, or has expired, it runs `usage.keepalive.argv` once
  for that profile in a fresh temporary directory. The first element is looked
  up on `PATH`. The child environment drops every variable a role prefix clears
  (all configured `credential_env` and `home_env` variables, the network and CA
  overrides) as well as `BROWSER`, then sets the driver's `home_env` to
  `config_dir`. The tool then renews the login itself. Agent never calls a
  token endpoint, never uses the refresh token and never reads, prints or
  stores a token or the tool's answer.
- A run counts as a renewal when the login's new expiry is later than the one
  before the run and still in the future; otherwise it is the error `token not
  renewed`. When the tool's token lifetime is not longer than
  `refresh_before_minutes`, the renewal is reported as `token.keepalive
  refreshed <account> until <UTC minute> (lifetime shorter than
  refresh_before_minutes)`; choose a smaller `refresh_before_minutes`.
- Each refresh holds an exclusive lock for the profile directory under
  `$AGENT_HOME/token-keepalive/` (one file per directory). After taking it,
  the expiry is read again, so a login another process renewed meanwhile is
  reported as fresh. When the lock is taken (another keep-alive pass or a
  running `agent account-login` for that profile), the account is skipped with
  `refresh in progress`.
- Cost: each renewal is one run of the configured command against that
  subscription; choose a minimal request.
- The device daemon checks at most every 5 minutes, on the usage tick and
  before the account usage scanners, so a renewed token is read in the same
  tick. It prints `token.keepalive refreshed <account> until <UTC minute>` to
  stdout, and `token.keepalive skipped <account>: <reason>` and
  `token.keepalive error: <account>: <reason>` to stderr; a fresh token prints
  nothing. An error names the exit code and at most 200 characters of the
  tool's redacted stderr. A failure of one account never stops the others or
  the tick.
- After an error the daemon leaves that account out (without output) for a
  growing wait: 10 minutes, doubled after each further error in a row, at most
  6 hours. A fresh or renewed login resets the wait. After a renewal whose
  lifetime is within `refresh_before_minutes`, the daemon leaves the account
  out until 10 minutes before the new expiry. These waits live in the daemon
  process and start over when it restarts.
- `agent watch token-keepalive` runs one pass immediately, without the
  daemon's waits, prints one line per account (a fresh token as
  `token.keepalive fresh <account> until <UTC minute>`), and exits 1 when an
  account failed. Without the key it fails with
  `token_keepalive is not configured`.
- An account without a login is skipped with
  `no login; run agent account-login`.
- On a platform whose secret store locks outside the logged-in user session
  (for example a keychain over SSH), the keep-alive must run in that session,
  or the tool cannot save the renewed token.
- A tool that renews a token from the GUI session may move a file login into
  the secret-store item of that directory and delete the credentials file; the
  usage reader already prefers that item. A tool that uses the same profile
  over SSH (locked secret store) then finds no login. The driver's
  `home_default_dir` profile is therefore never kept alive (it is reported as
  `default profile is not kept alive`): dedicate a separate profile directory
  to each subscription whose usage is polled.

## Signing in a subscription

`agent account-login ACCOUNT --email ADDRESS [--no-mailbox]` signs one
subscription into the profile of the configured account `ACCOUNT`. The
account's driver must have a `login` section in `drivers.json` (see
[Login section](#login-section)); otherwise the command fails with `account
<account> uses driver <driver>, which has no login section in drivers.json
(docs/ai-accounts.md#drivers)`. It does every step a script may do; sign-in
pages may block automated browsers, so the pages open in the user's normal
browser through the operating system (`open` on macOS, `xdg-open` elsewhere)
and the human clicks. For the whole sign-in the command holds the profile's
[token keep-alive](#token-keep-alive) lock, so no keep-alive run writes the
login at the same time; when the lock is taken it prints a note and waits for
it. The steps:

1. The command starts `login.argv` with `{email}` replaced by `ADDRESS` for
   that profile (the same environment as the keep-alive run) in a private
   temporary directory, on a pseudo-terminal whose output is discarded, and
   catches the authorize URL through `BROWSER`. The URL must be `https://` and
   contain `login.authorize_url_marker`.
2. It puts the e-mail address on the clipboard (macOS only; elsewhere it prints
   a note) and checks it.
3. It opens `login.logout_url` and waits 5 seconds, so the authorize page
   cannot belong to another account that is still signed in, then opens the
   authorize URL.
4. **Human:** paste the e-mail address if the field is empty and choose
   "continue with e-mail".
5. With a mailbox file, the command reads the sign-in e-mail over IMAP every 5
   seconds for up to 10 minutes and opens its sign-in link once. Without one,
   **human:** open the sign-in link from the e-mail.
6. **Human:** choose "Authorize".
7. The command waits up to 10 minutes for the login command to finish, then
   runs `login.status_argv` for the profile. It succeeds only when that prints
   a JSON object whose `login.logged_in_field` is `true` and whose
   `login.email_field` is exactly `ADDRESS` (case-insensitive), and prints
   `account.login ok <account> <address>`, followed by ` (<subscription>)` when
   `login.subscription_field` is configured. Otherwise it fails with `signed in
   as <found or nobody>, expected <address>`.

The optional mailbox file is
`$AGENT_HOME/account-login/<lower-case address>.json`, for example
`account-login/seat-1@example.com.json`:

```json
{
  "imap_host": "imap.example.com",
  "imap_user": "seat-1@example.com",
  "imap_password": "app-specific-password",
  "imap_port": 993
}
```

`imap_port` is optional (default 993). The file must have mode `0600` (no
group or other permission) and must never be committed to a repository; a
file with other keys, empty values or a wider mode is refused. The connection
uses IMAPS with certificate verification and a 30 second socket timeout (a
timeout is a failed check that is retried), opens `INBOX` read-only and reads
at most the newest 10 messages whose sender matches `login.mail_from` and that
were sent after the authorize page was opened. Each message is fetched only up
to 1 MiB plus one byte; a message larger than 1 MiB is skipped without being
parsed. In each message the first
`https://` match of `login.magic_link_pattern` is the sign-in link. A missing
file continues without the mailbox; `--no-mailbox` ignores an existing file.
The sign-in link, tokens and the mailbox password are never printed, logged or
stored.

## Subscription inventory

The optional, device-local file `$AGENT_HOME/ai-subscriptions.json` lists every
known AI subscription, whether or not a device has its login. It holds no
credentials and is separate from `ai-accounts.json`. A missing file means no
inventory is configured. Example (operator-supplied, never installed):

```json
{
  "subscriptions": [
    {"provider": "driver-a", "account": "seat-1@example.com", "label": "Team seat 1", "status": "active", "note": "Billed yearly"},
    {"provider": "driver-b", "account": "router-a"}
  ]
}
```

- `subscriptions` is the only top-level key and must be a list.
- `provider`: required lowercase token of `a-z`, `0-9` and `-`; it is not
  limited to providers with a usage reader, so other AI plans can be listed.
- `account`: required non-empty single-line string, unique per provider
  without regard to case. For a driver with a `windows` adapter it is the
  login's e-mail address; for a driver with a `spend` adapter it is the account
  name from `ai-accounts.json`.
- `label`, `status`, `note`: optional non-empty single-line strings.
- Unknown keys are rejected with an error naming the entry.

The dashboard's **Subscriptions** page (`/usage`, linked from the main page)
reads the file on every refresh. It lists every inventory entry with label,
status and note, joined with the newest `usage.snapshot` of the same provider
and account (case-insensitive). A listed subscription that no paired device
polls shows `No usage: no paired device polls this login`. Usage snapshots of
accounts missing from the inventory are still shown and marked `not listed`.
Windows rows show the five-hour, weekly and per-model weekly percentages with
their reset times. `status` is shown as an icon from the dashboard's state
mapping with the word as the cell's title; a word the mapping does not know
gets the neutral fallback icon. A file that does not validate is shown as an error on that
page; the rest of the dashboard keeps working. `/api/state` delivers the joined
rows as `subscriptions` (`configured`, `error`, `check_error`, `rows`).

Two columns show ages relative to the browser's clock, re-computed on every
refresh in the dashboard's shared elapsed-time format ("45 s ago",
"4 min ago", "2 h ago", "3 d ago"; a time in the future counts as "0 s ago"),
with the exact UTC time as the cell's title and `—` when there is none:

- **Checked**: the last successful usage read of the account on this device,
  whether or not its usage changed. After every successful read of a
  `usage_accounts` account (snapshot stored or unchanged) the poller writes
  `{"checked_at": <read time>, "identity": <identity read>}` to the local meta
  key `usage.checked.<provider>.<account name>`; the key holds the configured
  account name, never a token or an e-mail address. The identity is the
  case-folded account name of a `spend` driver or the case-folded login of a
  `windows` driver. Skipped, failed and rate-limited reads write nothing. A
  row gets the read time (`checked_at` in `rows`) when it shows that account:
  a row of a `spend` driver by the account name, only for this device's
  snapshot, a row of a `windows` driver by the identity of the account's
  login. A check whose identity is not the account's current identity, such as
  after another login in the same configuration directory, applies to no row
  until that login is read. Rows of other drivers and accounts this device
  does not poll show `—`. `/api/state` reads these values together with the
  rest of its store data; a lost database connection answers `503` as for the
  rest of the state. When `ai-accounts.json`, a login's account file or a
  stored check value cannot be read, the reason is shown on the page's error
  line (`check_error`).
- **Changed**: the `fetched_at` of the newest snapshot, which is stored only
  when a value other than `fetched_at` changed, so it is the last change, not
  the last check.

Above that table the page shows one compact line per provider: every driver of
`drivers.json` and every provider of the inventory or of a recorded snapshot,
named by the driver's `display_name` or else by its id. The line shows the time
until the quota resets, a bar and the used percentage of the headline window.
The window follows the snapshot kind, read from the payload as in the table:
`windows`: the weekly window; `period`: the period (`period_end`); `spend`: the
key limit, when it has one. A provider whose driver has no `usage` section
shows `not read`, unless another device recorded a snapshot for it. A provider
with a `usage` section shows `no data` when none of its snapshots has a
headline percentage or another figure; a spend snapshot without a limit shows
its USD usage instead. Neither is shown as 0 %, and both are shown as icons of
the dashboard's state mapping. When `drivers.json` cannot be read, the page
lists only the recorded providers and shows none of them as `not read`.
Opening a provider lists each quota window (`windows`: five hours, weekly and
the weekly windows per model named in the snapshot; `period`: the period and
each product) with its reset and bar, then each account with its own bar and
the age of its newest snapshot (`recorded 4 min ago`). A snapshot is stored
only when a value changed, so that age is the time of the last change. An
account this device polls also shows `checked 30 s ago` (`checked_at` of the
account entry): `checked` is the last successful read, `recorded` the last
change. With several accounts a provider window shows the highest use among
them.
`detailed` opens every provider, `sort by used` orders providers by the
headline percentage. The main page shows one summary line, for example
`Vendor A 32% 5 d 19 h · Vendor B ⊗ · Vendor D 12.5% 3 d 4 h`, where a status
shows as its icon with the word as its title. On `/usage`, a listed account of
a provider with a `usage` section and without any snapshot shows `No usage: no
paired device polls this login` on its name, as in the table. The figures come
only from stored `usage.snapshot` payloads, the inventory and `drivers.json`;
`/api/state` delivers them as `usage_overview`.

Each session may include:

- `interactive`: role name for the interactive runner, or omit/null when none
- `lanes`: map of slot → role name, or omit/null/`{}` when none

The optional top-level `lanes` map holds device-wide bindings with the same
keys. A session's own `lanes` entry wins; a slot the session does not bind
falls back to the device-wide map. This is explicit configuration for every
session on the device, not an implicit identity.

Lane slots are non-empty single-line strings of the form
`vendor:workflow-kind`, where `vendor` names a driver with a `lane` section in
`drivers.json`. Resolvers look up the
explicit key (for example `driver-a:implementer`) and
require the bound role's account provider to match `vendor`. For workflow kinds
`reviewer`, `pr-reviewer-quality`, `pr-reviewer-logic`, and `check`, the bound
role must use `access: read-only`; a writable binding is refused. The `check`
kind is required at `<first>:check` when a coordinator worker enables orders.

Unknown top-level, account, role, or session fields are rejected. Dangling
account or role references are rejected. Credentials and API tokens must not
appear in this manifest; they belong only inside each `config_dir`. Error text
from the loader does not echo credential contents.

## Process isolation prefix

`AIRole.env_prefix(drivers)` returns an `env` argv prefix for child processes.
It removes every ambient variable that any configured driver lists in
`credential_env`, the network and CA overrides below, and every configured
driver's `home_env`, in configuration order, then sets the role driver's
`home_env` to the role's account `config_dir` (unless `home_default_dir`
applies, see [Required fields](#required-fields)). It does not mutate the parent `os.environ`. Unrelated environment
variables are left for the child to inherit. This is process configuration
isolation, not a sandbox and not a claim that the provider CLI is already
authenticated for that profile.

Every lane and every interactive pane also removes the union of all configured
`credential_env` variables and the upper- and lower-case `HTTP_PROXY`,
`HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` variables and the CA overrides
`NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`
and `CURL_CA_BUNDLE`. The network and CA list is built in; the credential
variables come only from `drivers.json`, so removing a driver from that file
also stops scrubbing its variables. A lane therefore never inherits an ambient proxy or CA
trust that could read its provider credential. There is no opt-in for this
stripping. A usage adapter with `proxy_from_environment: false` builds its own
HTTP client with `trust_env=False` for the same reason. A windows adapter's
`trust_env` follows its `proxy_from_environment` key as well.

This lane and pane list equals the role prefix's list without the `home_env`
variables, and the role prefix runs before it, so it never changes the child's
environment.
Earlier versions put only part of the credential variables into the lane and
pane list and left the others to the role prefix alone. With the same
variables configured, a lane or pane argv, and so the output of
`agent lane run --dry-run`, now carries one more `-u` pair for each of those
others; the child's environment is unchanged.

## Interactive selection

`AIAccounts.for_session(session_id)` resolves `sessions[session_id].interactive`
and fails when that binding is absent or null. The loader may represent roles of
any configured driver in the manifest. An interactive start needs the role
driver's `interactive` section in `drivers.json`; a driver without one, or one
that `drivers.json` does not configure, is refused with `provider must be
<drivers with an interactive section> (see drivers.json,
docs/ai-accounts.md#drivers)`. `agent session start --id ID --provider <driver>` and the
control payload `{provider: "<driver>"}` require that driver to be the one of the
session's interactive role (`configured interactive role does not match the
requested provider`); the dashboard's "Start AI session" button sends
`{interactive: true}` and starts whatever driver that role names. `--model`
requires `--provider`. The configured model is required,
and a supplied `--model` must agree with it. A read-only role appends the
driver's `interactive.read_only_args`, and every start appends its
`interactive.hardening_args`. A resumed conversation is pinned to
the configured role, account, model, access and configuration-directory identity;
a changed or missing binding requires a new session. Existing unbound interactive session IDs are not silently adopted under a newly
configured account. Interactive provider starts run only on a declared lane
host. Agent does not provide its own remote lane transport or workspace
mirroring. Interactive tmux sessions may still use
`$AGENT_HOME/runtime-targets.json` after the `models` and `lane_execution` checks
pass.

A new start creates a UUID, stores it as `interactive_session_id` in the
session's `runtime` together with `interactive_start_pending` until the tmux
session is attached, and prints `started <sid> tmux=<name> interactive=<uuid>`.
If the tmux session already exists, the start fails with `tmux session already
exists; the interactive driver was not started`. Runtimes written by an earlier
version stored these two values under keys named after the driver
(`<driver>_session_id` and `<driver>_start_pending`). For every configured
driver with an `interactive` section those keys are read as the neutral keys,
and the next session write stores the neutral keys.

`agent allow` without `--session` reads the session id from the first
configured `interactive.session_env` variable that is set, in configuration
order. A variable exported with an empty value counts as set and yields the
empty session id, which matches no task. Without `drivers.json` it cannot know
those variables, so every action without `--session` fails with `drivers is
not configured: ...` instead of deciding without a session. The same holds when
`drivers.json` exists but no driver configures `interactive.session_env`: every
action without `--session` fails with `no driver in drivers.json configures
interactive.session_env; pass --session (see drivers.json,
docs/ai-accounts.md#drivers)`. When variables are configured but none is set,
the action runs as if no session were given; `task-done` then uses the task's
own session.

The script classifies an interactive pane (knock, keep-working, supervise) with
the pane markers of the driver of the session's interactive role. A session
without an interactive role, or whose role's driver has no `interactive`
section, is classified with the markers of `pane_driver`. Without either, the
call fails with `session <sid> has no interactive driver and no pane_driver is
configured; cannot classify its pane (see drivers.json,
docs/ai-accounts.md#drivers)`, and a knock stays queued.

## Drivers

`$AGENT_HOME/drivers.json` is the operator's description of every driver. Installation
creates no such file, and the repository ships no driver table, no driver
default and no fallback. The file is a JSON object with at most three top-level
keys:

- `drivers`: an object of driver id → driver entry, or null.
- `recorded_slots`: optional; the driver ids an earlier version wrote into
  stored names (see [Recorded slot names](#recorded-slot-names)).
- `pane_driver`: optional; a driver with an `interactive` section whose pane
  markers classify a pane that no interactive driver owns.

`ai-accounts.json` keeps its own keys. Its accounts, `models`, `implementer`,
lane slots and usage selections name drivers by id, and the loader checks every
reference against `drivers.json` through the same configuration error. Unknown
keys are rejected at every level of `drivers.json`, so a typo fails instead of
reading as absent.

### Rollout

Versions before drivers existed ignore `drivers.json`. Place the file on every
device before upgrading; the upgrade then finds it in place. A device that is
upgraded without the file fails as soon as anything names a driver, and
`agent allow` without `--session` fails the same way:

```text
drivers is not configured: drivers.json must describe every driver that ai-accounts.json names in accounts, models, implementer, lanes and usage selections (docs/ai-accounts.md#drivers)
```

Use the driver ids the device already uses as keys of `drivers`. Those ids are
stored in task pins, agent and gate rows, lane slot keys, session runtimes and
usage snapshots, and nothing rewrites them. Keep every driver that stored rows
name: removing one also removes its credential variables from the scrub lists
and its display name from review records. Configure `recorded_slots` when stored
rows may still hold names an earlier version wrote.

### Example

The following placeholder document describes five drivers. It is an
illustration of the schema, not a working configuration for any tool.

```json
{
  "drivers": {
    "driver-a": {
      "display_name": "Vendor A",
      "home_env": "TOOL_A_HOME",
      "credential_env": ["TOOL_A_API_KEY"],
      "lane": {
        "spec": "file-arg",
        "output": "event-stream",
        "may_run_commands": false,
        "audit_stderr_when_empty": false,
        "argv": {
          "read": ["tool-a", "--prompt-file", "{spec_file}", "-m", "{model}", "--read-only", "--cwd", "{cwd}"],
          "write": ["tool-a", "--prompt-file", "{spec_file}", "-m", "{model}", "--edit", "--cwd", "{cwd}"],
          "judgment": ["tool-a", "--prompt-file", "{spec_file}", "-m", "{model}", "--no-tools", "--cwd", "{cwd}"]
        },
        "write_hardening": [["--no-shell"], ["--deny", "Shell"]]
      },
      "interactive": {
        "argv_new": ["tool-a", "--session-id", "{session_id}", "--model", "{model}"],
        "argv_resume": ["tool-a", "--resume", "{session_id}", "--model", "{model}"],
        "session_id": "uuid",
        "read_only_args": ["--deny", "Edit"],
        "hardening_args": ["--deny", "Shell"],
        "session_env": "TOOL_A_SESSION_ID",
        "pane": {
          "working": ["(?i)working\\.\\.\\.", "\\[stop\\]"],
          "queued": [["queued", "send now"]],
          "idle": ["> $"],
          "permission": ["(?i)approve\\?"],
          "permission_confirm": ["Tab:next"],
          "chrome": ["Status bar"]
        }
      },
      "usage": {
        "snapshot": "period",
        "credential": {
          "file": "auth.json",
          "format": "json-entry",
          "entry_prefix": "https://auth.example.test::",
          "token": "key",
          "expires_at": "expires_at",
          "account": "email"
        },
        "proxy_from_environment": false,
        "requests": [
          {"name": "billing", "url": "https://usage.example.test/v1/billing", "headers": {"x-client": "agent"}},
          {"name": "plan", "url": "https://usage.example.test/v1/plan"}
        ],
        "fields": {
          "tier": "/plan/tier",
          "used_percent": "/billing/used",
          "period_type": "/billing/period/type",
          "period_start": "/billing/period/start",
          "period_end": "/billing/period/end",
          "products": {"list": "/billing/products", "product": "/name", "used_percent": "/used"},
          "prepaid_val": "/billing/prepaid/val",
          "on_demand_cap_val": "/billing/cap/val",
          "on_demand_used_val": "/billing/on_demand/val"
        }
      }
    },
    "driver-b": {
      "display_name": "Vendor B",
      "home_env": "TOOL_B_HOME",
      "credential_env": ["TOOL_B_API_KEY"],
      "lane": {
        "spec": "stdin",
        "output": "last-message-file",
        "may_run_commands": true,
        "audit_stderr_when_empty": false,
        "argv": {
          "read": ["tool-b", "exec", "--model", "{model}", "--sandbox", "read-only", "--cd", "{cwd}", "--last-message", "{output_file}", "-"],
          "write": ["tool-b", "exec", "--model", "{model}", "--sandbox", "workspace-write", "--cd", "{cwd}", "--last-message", "{output_file}", "-"]
        }
      }
    },
    "driver-c": {
      "display_name": "Vendor C",
      "home_env": "TOOL_C_CONFIG",
      "home_default_dir": "~/.tool-c",
      "credential_env": ["TOOL_C_API_KEY", "TOOL_C_TOKEN"],
      "lane": {
        "spec": "stdin",
        "output": "event-stream",
        "may_run_commands": false,
        "audit_stderr_when_empty": true,
        "argv": {
          "read": ["tool-c", "--print", "--model", "{model}", "--tools", "Read", "--mode", "default"],
          "write": ["tool-c", "--print", "--model", "{model}", "--tools", "Read,Edit", "--mode", "edit"],
          "judgment": ["tool-c", "--print", "--model", "{model}", "--tools", "", "--mode", "default"]
        },
        "read_dir_args": ["--add-dir", "{dir}"]
      }
    },
    "driver-d": {
      "display_name": "Vendor D",
      "home_env": "TOOL_C_CONFIG",
      "credential_env": ["TOOL_D_API_KEY", "TOOL_C_API_KEY", "TOOL_C_TOKEN"],
      "lane": {
        "spec": "stdin",
        "output": "event-stream",
        "may_run_commands": false,
        "audit_stderr_when_empty": true,
        "argv": {
          "read": ["tool-c", "--print", "--model", "{model}", "--tools", "Read", "--mode", "default"],
          "write": ["tool-c", "--print", "--model", "{model}", "--tools", "Read,Edit", "--mode", "edit"]
        },
        "read_dir_args": ["--add-dir", "{dir}"],
        "env": {"TOOL_C_BASE_URL": "https://gateway.example.test/api", "TOOL_C_API_KEY": ""},
        "credential_file": {"file": "tool-d-key", "env": "TOOL_C_TOKEN"}
      },
      "usage": {
        "snapshot": "spend",
        "credential": {"file": "tool-d-key", "format": "text"},
        "proxy_from_environment": false,
        "requests": [{"name": "key", "url": "https://gateway.example.test/api/v1/key"}],
        "fields": {
          "usage_usd": "/key/data/usage",
          "usage_daily_usd": "/key/data/usage_daily",
          "usage_weekly_usd": "/key/data/usage_weekly",
          "usage_monthly_usd": "/key/data/usage_monthly",
          "limit_usd": "/key/data/limit",
          "limit_remaining_usd": "/key/data/limit_remaining",
          "limit_reset": "/key/data/limit_reset"
        }
      }
    },
    "driver-e": {}
  },
  "recorded_slots": {"first": "driver-a", "second": "driver-b"},
  "pane_driver": "driver-a"
}
```

Not every driver needs every section. `driver-e` is record-only: it may be the
`implementer` value and be recorded by `agent agent start --role implementer`,
and every lane launcher refuses it like an unknown `--vendor`: `vendor must be
driver-a|driver-b|driver-c|driver-d (see drivers.json,
docs/ai-accounts.md#drivers)`. `driver-d` runs the same CLI as `driver-c` against another
endpoint and shares its configuration-directory variable.

### Driver entry

A driver id starts with a letter or digit, continues with letters, digits, `-`
or `_`, has at most 32 characters and is neither `first` nor `second`.

| Key | Meaning |
|---|---|
| `display_name` | Provider name in A38 review records and in the dashboard usage overview. Required for a driver with a `lane` section. |
| `home_env` | Environment variable that selects the account's `config_dir`. Required for a driver that an account names. |
| `home_default_dir` | Optional `~/...` or absolute path of the CLI's own default profile; for that `config_dir` the role prefix leaves `home_env` unset. |
| `credential_env` | Optional list of credential variables that every lane, pane and role prefix clears. It must not name any driver's `home_env`. |
| `lane` | Optional; how a headless lane runs. Without it the driver cannot fill a model slot or run a lane. |
| `interactive` | Optional; how an interactive terminal session runs and how its pane is read. |
| `usage` | Optional; the usage adapter for `usage_session` (`period`) or `usage_accounts` (`spend` or `windows`). |
| `login` | Optional; how `agent account-login` drives the tool's own sign-in (see [Login section](#login-section)). Requires `home_env`. |

### Lane section

| Key | Meaning |
|---|---|
| `spec` | Required. `stdin` passes the spec text on standard input; `file-arg` passes the spec file through `{spec_file}`. |
| `output` | Required. `event-stream` reads a JSON event stream; `last-message-file` reads the final message from `{output_file}`. |
| `may_run_commands` | Required boolean. `true` marks a driver whose write lane cannot be kept from running commands. |
| `audit_stderr_when_empty` | Required boolean. `true` records the lane's stderr as its audit final text when stdout is empty. |
| `argv.read`, `argv.write` | Required argv templates of a read-only and a write lane. |
| `argv.judgment` | Optional argv template of a check lane without tools and file access. |
| `read_dir_args` | Optional template appended once per extra read directory, with `{dir}` exactly once. |
| `write_hardening` | Optional list of argv groups that every coordinator implementer lane and every conflict worker lane must carry. |
| `env` | Optional object of fixed variables set for the child after the scrub list; an empty value is allowed. A key must not name any driver's `home_env`. |
| `credential_file` | Optional `{file, env}`: a key file in the account's `config_dir`, exported as `env` after launch. Requires `home_env` and `output: event-stream`; `env` must not name any driver's `home_env`. |

Template rules: an argv template is a non-empty list of at most 200 single-line
strings; an empty string is allowed. An element that is exactly one placeholder
is substituted; every other element is literal, and a `{` or `}` in any other
element is a validation error. The placeholders are `{model}` (required in every
lane template), `{cwd}` (optional), `{spec_file}` (required for `file-arg`),
`{output_file}` (required for `last-message-file` and refused otherwise) and,
only in `read_dir_args`, `{dir}`.

The lane child runs `env`, which clears the scrub list, then sets `lane.env`,
then runs the credential wrapper when `credential_file` is set, then the
expanded template. A lane that needs an output file without getting one fails
with `driver <driver> requires an output file`. The event stream is read from
standard output, or from the output file when the lane runs in a tmux pane or
uses `credential_file`.

A lane held in a tmux pane follows four rules, derived from `spec`, `output`
and `credential_file`:

1. `credential_file` set: the wrapper writes the stream to the output file. A
   `stdin` spec is redirected from the spec file.
2. `event-stream` with `stdin`: the spec file is redirected to standard input
   and the stream to the output file.
3. `event-stream` with `file-arg`: the stream is redirected to the output file.
4. `last-message-file`: no redirect; the driver writes its own output file, and
   a `stdin` spec is typed into the pane.

Capabilities that follow from the lane section:

- A driver with `may_run_commands: true` is refused by the issue coordinator as
  the effective implementer (the task's `implementer` pin when present,
  otherwise `models.first`), and by the conflict worker as implementer
  (`<driver> is refused as implementer provider because it can run commands`).
  `models.first` may name such a driver when a separate implementer is
  configured, because slot `first` only runs read-only lanes. `agent lane run`
  and `agent run` still start it.
- A driver without `argv.judgment` cannot run a check lane: `driver <driver> has
  no lane.argv.judgment; it cannot run a check lane`.
- A driver without `read_dir_args` receives no extra read directory; one with
  it refuses extra directories on a write lane.
- `write_hardening` appends each group that the write argv does not already
  contain as a contiguous run. It is process argv hardening, not a sandbox.
- A review record names the driver recorded on a stage by its `display_name`;
  a recorded driver without one stops the comment with `driver <driver>
  recorded on <stage> has no display_name (see drivers.json,
  docs/ai-accounts.md#drivers)`.
- A driver with `credential_file` refuses a worktree or read directory that
  overlaps the account's `config_dir`, for example `<driver> worktree overlaps
  account config_dir`.

### Interactive section

| Key | Meaning |
|---|---|
| `argv_new` | Required template of a new session; uses `{session_id}` and `{model}`. |
| `argv_resume` | Required template that resumes a session; uses `{session_id}` and `{model}`. |
| `session_id` | Required; `uuid` is the only format. |
| `read_only_args` | Required list appended for a read-only role (may be empty). |
| `hardening_args` | Required list appended to every start (may be empty). |
| `session_env` | Optional variable through which the driver exports its session id to hooks; `agent allow` reads it. `agent allow` without `--session` fails while no driver configures one. |
| `pane` | Required pane markers, below. |

Pane markers are read from the pane text after terminal sanitization:

| Key | Meaning |
|---|---|
| `working` | Required regular expressions; one match means the driver is in a turn. |
| `queued` | Optional groups of substrings; a group whose substrings are all present (case-insensitive) also means working. |
| `idle` | Required regular expressions; one match means the composer waits for a prompt, unless the pane is working or shows a permission prompt. |
| `permission` | Required regular expressions of a tool-approval modal. |
| `permission_confirm` | Required substrings (case-insensitive); one of them must be present together with a `permission` match, so quoted text alone is not a modal. The script confirms such a modal with Enter. |
| `chrome` | Required substrings of interface lines that `supervise` skips when it reads a closed answer. |

Each regular expression has at most 500 characters and must compile; each list
has at most 20 expressions or 50 substrings.

### Usage adapters

A `usage` section turns HTTPS responses into a `usage.snapshot`. The code keeps
the request semantics: a bearer token in `Authorization`, `Accept:
application/json`, an 8 second timeout and no redirects. A configured header
cannot replace either of the two fixed headers (`requests[i].headers.<name> is
set by the usage adapter itself`).

| Key | Meaning |
|---|---|
| `snapshot` | `period` (subscription period usage of the `usage_session` account), `spend` (USD key spend of each `usage_accounts` account) or `windows` (five-hour, weekly and per-model weekly windows of each `usage_accounts` account). |
| `credential` | `period` needs `{"format": "json-entry", file, entry_prefix, token, expires_at, account}`: the first top-level key of the JSON file that starts with `entry_prefix` holds the entry, whose `token`, `expires_at` (ISO-8601 with offset) and `account` fields are read. `spend` needs `{"format": "text", file}`: the whole file is the key. `windows` needs `{"format": "oauth-file", file, label, object, token, token_name, expires_at, expires_unit, subscription, login_name, renew}`: the file holds a login object with the token, expiry and subscription fields, and `renew` names the operator's login command. An optional `multiplier` is `{plan, pattern, field}`; an optional `keychain` is `{argv, service, user_env, user_pattern, fallback_user, missing_status, platform}` plus optional `service_suffix: nfc-sha256-8`. The file lies in the account's `config_dir`. |
| `proxy_from_environment` | Required boolean. `true` lets the requests honour proxy and CA variables from the environment; `false` builds an owned client with `trust_env=False`. |
| `requests` | One to ten `{name, url, headers}` objects; `url` must be `https://`, `headers` is optional. Every request is sent in order. |
| `fields` | JSON pointers (RFC 6901) whose first segment names a request. `period`: `tier`, `used_percent`, `period_type`, `period_start`, `period_end`, `products` (`list` pointer plus `product` and `used_percent` pointers inside each item), `prepaid_val`, `on_demand_cap_val`, `on_demand_used_val`. `spend`: `usage_usd`, `usage_daily_usd`, `usage_weekly_usd`, `usage_monthly_usd`, `limit_usd`, `limit_remaining_usd`, `limit_reset`. `windows`: `five_hour`, `weekly`, `limits`. |
| `label` | Required by `windows`; a non-empty single-line display name used in its error texts. |
| `poll_seconds` | Required by `windows`; an integer from 1 to 86400, the minimum interval between reads of one account under the daemon. |
| `account_file` | Required by `windows`; `{file, object, field}` names the JSON file that holds the login identity. Optional `default_name` reads that file from the home directory for the driver's `home_default_dir`; `default_name` requires `home_default_dir`. |
| `window` | Required by `windows`; `{used_percent, resets_at, round}` names the response fields of one window. `round` must be `minute`. |
| `limits` | Required by `windows`; `{kind, weekly_all, weekly_model, used_percent, resets_at, scope, model, model_names}` names the limits list and its item fields. `model_names` is a list of display-name keys. |
| `keepalive` | Optional, `windows` only; `{argv}` is the command the [token keep-alive](#token-keep-alive) runs to make the tool renew its login (see [Usage keep-alive](#usage-keep-alive)). Requires `home_env`. |

For a `windows` adapter, `label`, `window.used_percent`, `window.resets_at`,
`limits.kind`, `limits.weekly_all`, `limits.weekly_model`,
`limits.used_percent`, `limits.resets_at`, `limits.scope`, `limits.model` and
each `limits.model_names` entry are non-empty single-line strings.
`poll_seconds` is an integer from 1 to 86400. `proxy_from_environment` is a
boolean. `account_file.file`, `credential.file` and optional
`account_file.default_name` are plain file names. `account_file.object` and
`account_file.field` are non-empty single-line strings. The required
`credential` keys `label`, `object`, `token`, `token_name`, `expires_at`,
`subscription`, `login_name` and `renew` are non-empty single-line strings;
`multiplier.plan` and `multiplier.field` are too. `multiplier.pattern` is a
non-empty string of at most 500 characters with exactly one group. The
`keychain` keys `service`, `user_env`, `fallback_user` and `platform` are
non-empty single-line strings, `user_env` is an environment variable name,
`user_pattern` is a valid regular expression, and `argv` is a non-empty list of
at most 200 strings that uses `{service}` and `{user}`. `keychain.argv` is the
program and arguments that read one secret-store item. An optional
`service_suffix` is `nfc-sha256-8`. `window.round` is `minute`;
`credential.format` is `oauth-file` and `credential.expires_unit` is
`epoch-ms`. `limits.model_names` is a list of 1 to 100 entries. `fields` has
exactly the `five_hour`, `weekly` and `limits` pointers; `window` has exactly
`used_percent`, `resets_at` and `round`; `limits` has exactly its eight keys.
`credential.keychain.missing_status` is an integer from 0 to 255. A missing
key fails as `<path>.<key> is required`; a wrong value fails with the matching
type error, such as `must be a non-empty single-line string`, `must be a plain
file name`, `must be one of minute`, `must be an integer from 1 to 86400`, or
`must be a JSON pointer`.

Every field is required. Text fields must be non-empty strings, percentages and
amounts finite numbers, and the three `*_val` fields integers. In a `spend`
snapshot, `limit_usd` and `limit_remaining_usd` may be null, and those two and
`limit_reset` read as null when an existing object lacks their last segment.
In a `windows` snapshot, `five_hour`, `weekly` and each limit may be null when
the response has no value or its configured `used_percent` field is absent.
Nothing else is defaulted. The errors name the driver, the field and its
pointer, for example `<driver> usage field <name> at <pointer> is missing` or
`... must be a number`. A request fails with `<driver> usage request <name>
returned HTTP <code>`, `... failed: <ExceptionClass>`, `... returned invalid
JSON` or `... response is not an object`; no message contains the token.
Unknown keys are rejected at every level of the usage object (`<path> accepts
only ...`), and a missing or wrong value fails as `<path>.<key> is required`,
`... must be a non-empty single-line string`, `... must be true or false`,
`... must be an integer from 1 to 86400`, `... must be one of minute`, or
`... must be a JSON pointer`.

On the `usage_session` path, a missing credential file, a missing entry
(`<driver> credential file has no entry that starts with <prefix>`) or an
expired token (`<driver> credential expired; log in again`) is not a scan
failure: `agent watch session-usage` prints `usage.snapshot skipped`. An invalid
file or entry field fails (`<driver> credential entry field <field> is missing
or invalid`). On the `usage_accounts` path, a missing or empty key file is an
error of that account (`<driver> key missing`); `agent watch account-usage`
records the other accounts, prints `usage.snapshot error: ...` per failed
account and exits 1. On the `windows` path, `agent watch window-usage` records
the other accounts, prints `usage.snapshot skipped <account>: <reason>` for a
missing or expired login, prints `usage.snapshot error: ...` per failed account,
and exits 1 on an error. The lock keys are `usage.snapshot:<driver>` and
`usage.snapshot:<driver>:<account>`. The dashboard tells the snapshot shapes
apart by their fields: a spend snapshot names its `account`, and a windows
snapshot names `account_email` and `windows`.

### Usage keep-alive

The optional `keepalive` key of a `windows` usage adapter names the command
that the [token keep-alive](#token-keep-alive) runs for a profile whose login is
close to expiry. A `spend` or `period` adapter refuses the key. The following
placeholder fragment shows only the new key; the rest of the `windows` adapter
is unchanged and omitted here:

```json
{
  "drivers": {
    "driver-c": {
      "home_env": "TOOL_C_CONFIG",
      "usage": {
        "snapshot": "windows",
        "keepalive": {
          "argv": ["tool-c", "--print", "Reply with the single word: ok", "--model", "small-model"]
        }
      }
    }
  }
}
```

| Key | Meaning |
|---|---|
| `argv` | Required non-empty list of at most 200 non-empty single-line strings. The first element is the executable, looked up on `PATH`; there are no placeholders. |

A driver with `usage.keepalive` must configure `home_env`, which the run sets
to the account's `config_dir` (unless it is `home_default_dir`, whose profile is
never kept alive).

### Login section

The optional `login` section describes the tool's own sign-in for
`agent account-login` (see [Signing in a subscription](#signing-in-a-subscription)).
Placeholder example:

```json
{
  "drivers": {
    "driver-c": {
      "home_env": "TOOL_C_CONFIG",
      "login": {
        "argv": ["tool-c", "auth", "login", "--email", "{email}"],
        "status_argv": ["tool-c", "auth", "status", "--json"],
        "email_field": "email",
        "logged_in_field": "loggedIn",
        "subscription_field": "plan",
        "logout_url": "https://sign-in.example.test/logout",
        "authorize_url_marker": "redirect_uri=http%3A%2F%2Flocalhost",
        "magic_link_pattern": "https://sign-in\\.example\\.test/magic-link#[A-Za-z0-9_:=+/-]+",
        "mail_from": "sign-in.example.test"
      }
    }
  }
}
```

| Key | Meaning |
|---|---|
| `argv` | Required argv template of the login command; uses `{email}` (the only placeholder) and has no empty element. |
| `status_argv` | Required non-empty list of non-empty strings without placeholders; the command prints the login status as one JSON object. |
| `email_field` | Required key of the status object that holds the signed-in address. |
| `logged_in_field` | Required key of the status object that is `true` for a login. |
| `subscription_field` | Optional key of the status object whose text the success line shows. |
| `logout_url` | Required `https://` URL that signs the browser out before the authorize page opens. |
| `authorize_url_marker` | Required text that the captured authorize URL must contain, for example its local redirect. |
| `magic_link_pattern` | Required regular expression (at most 500 characters) of the sign-in link in the e-mail; only an `https://` match counts. |
| `mail_from` | Required IMAP `FROM` search text; no double quote or backslash. |

A driver with a `login` section must configure `home_env`.

### Recorded slot names

Earlier versions named the first and second review stage after the driver that
ran it and wrote those names into stored rows: gate and agent stages
`<driver>-pr`, checklist keys `<driver>_pr_quality` and `<driver>_pr_logic`, and
coordinator phases `pr_gates_<driver>`. History is immutable and replicated, so
these names stay readable permanently through `recorded_slots`:

```json
{"recorded_slots": {"first": "driver-a", "second": "driver-b"}}
```

With this entry, `driver-a-pr` reads as `review-1`, `driver-b_pr_logic` as
`review_2_logic` and `pr_gates_driver-a` as `pr_gates_first`. CLI input
(`agent agent start --stage`, `gate record`, `checklist set`, `close-step`)
accepts the old names with a deprecation warning. The coordinator rewrites the
old phase names of a task (`phase`, `resume_phase`, the retry and gate-route
phases, and the order checkpoint) when it next advances that task. A task
created before tasks pinned model slots reads its slots as `first`, then
`second` of this entry, only to read or display its recorded rows.

The two ids must differ and follow the driver id rule; they only name history,
so they need not be configured drivers. Every command that opens the store reads
only this entry, so an error elsewhere in `drivers.json` does not hide stored
rows; an invalid entry or an unreadable file stops the command.

Without the entry, the old names are unknown names. Each refusal listed below
then ends with the hint `(written by an earlier version? configure
recorded_slots in drivers.json, docs/ai-accounts.md#drivers)`:

- CLI input of an old name fails with the command's usual refusal plus the
  hint: `stage must be review-1|review-2` for `agent agent start --stage` and
  `gate record`, `checklist <key> for task <id> not found` for `checklist set`,
  and `unknown key <key>` for `close-step`.
- Stored gate and checklist rows with an old name read as absent, so a gate or
  checklist key that only such a row holds counts as missing; such a row never
  counts as an approval. `agent allow --action task-done|claim-done` and
  `agent task state <id> done` add the hint to their refusal when the task
  holds such a row. Other readers, such as `agent next`, treat the gate or key
  as missing without the hint.
- The coordinator does not advance a task whose checklist holds a key of an
  old name. When the tick would visit that task, it reports `blocked: task <id>
  holds unknown checklist key <key>` plus the hint instead. It also adds no
  checklist row for that task, so the recorded values read again once the
  entry is configured.
- An old phase name fails coordinator dispatch with `unknown coordinator phase
  <phase>` plus the hint.

An unknown name of any other shape fails with the same refusal without the
hint.

A task without a model pin cannot be read without the entry either (`task <id>
predates model pins; configure recorded_slots in drivers.json to read it
(docs/ai-accounts.md#drivers)`). The coordinator reports that error for such a
task instead of routing its second review, and refuses a second review waiver
with it.

### Validation and errors

`drivers.json` is validated completely whenever the AI configuration is loaded.
Every message names the failing path and ends with `(see drivers.json,
docs/ai-accounts.md#drivers)`, for example:

- `drivers.json accepts only drivers, recorded_slots, pane_driver`
- `drivers.<id>.lane accepts only spec, output, may_run_commands, ...`
- `drivers.<id>.lane.argv.write is required`
- `drivers.<id>.lane.argv.read[4] uses unknown placeholder {x}`
- `drivers.<id>.lane.argv.read must use {spec_file}`
- `drivers.<id>.display_name is required for a driver with a lane section`
- `drivers.<id>.lane.credential_file requires home_env`
- `drivers.<id>.credential_env[0] names home_env <VAR> of driver <id2>`, and the
  same for a `lane.env` key or `lane.credential_file.env`: the role prefix sets
  the home variable first, so a later scrub or fixed value would run the driver
  on another directory than the account's
- `drivers.<id>.home_env <VAR> is a network override that every lane clears`
- `drivers.<id>.interactive.pane.working[0] is not a valid regular expression`
- `drivers.<id>.usage.fields.tier must start with the name of a configured request`
- `drivers.<id>.usage.keepalive.argv must be a list of 1 to 200 entries` and
  `drivers.<id>.usage.keepalive requires home_env`
- `drivers.<id>.login.argv must use {email}`, `drivers.<id>.login.logout_url
  must be an https:// URL`, `drivers.<id>.login.magic_link_pattern is not a
  valid regular expression` and `drivers.<id>.login requires home_env`
- `pane_driver <id> must name a driver with an interactive section`
- `recorded_slots.first and recorded_slots.second must differ`

References from `ai-accounts.json` fail with errors such as `Unsupported
provider for account <name>: drivers.json configures no such driver
(docs/ai-accounts.md#drivers)`, `Lane slot <slot> must name a driver with a lane
section in drivers.json (docs/ai-accounts.md#drivers)`, `usage_session uses
driver <driver>, which has no period usage section in drivers.json
(docs/ai-accounts.md#drivers)` and, at launch, the refusal of an unknown
`--vendor`, `vendor must be <configured drivers> (see drivers.json,
docs/ai-accounts.md#drivers)`; a task pin of a driver that is no longer
configured fails the same way. Without `drivers.json` the refusal is
`drivers is not configured: ...`. An unreadable file
fails with `Cannot read drivers.json (docs/ai-accounts.md#drivers)`.

## Migration from implicit defaults

Before this manifest, lane launchers and the interactive runtime used fixed
vendor/role lists and built-in model choices in code. After adopting
`ai-accounts.json`:

1. Describe every driver in `drivers.json` (see [Drivers](#drivers)) and place
   that file before upgrading.
2. Create one account entry per provider CLI profile directory you intend to use.
3. Define roles with explicit `account`, `model`, and `access` (no omitted
   fields).
4. Choose the two model slots in `models` and bind the lanes, device-wide in
   the top-level `lanes` map or per session: keys such as `driver-a:implementer`
   and, when needed, `interactive`.
5. On the external lane host, set `lane_execution` to
   `{"mode":"this-device-is-lane-host"}` before enabling launch paths. The
   local harness's lane starter is responsible for starting work on that host.
6. Sessions without a binding of their own use the device-wide `lanes` map.
   An empty or missing file does not authorize a fallback identity.

Configure the lane bindings (device-wide `lanes` or per session) before
enabling launch paths after upgrading.
For example, the static script invokes `agent lane run --session chosen-session
--role implementer --vendor driver-a --spec-file task.md --no-tmux`; it resolves the
`driver-a:implementer` slot to the operator-defined role. `agent run` resolves slots
using its task session. Interactive starts on the lane host use `agent session
start --id chosen-session --provider driver-a`. Raw terminal
`--cmd` remains an explicitly
supplied script command, outside provider-profile launch enforcement; it is not
a sandboxed model interface. No model lane may invoke these execution commands.
