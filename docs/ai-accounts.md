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

This device-local manifest is used by `agent lane run`, the lane steps of
`agent run`, interactive Grok session starts, optional Grok and OpenRouter usage reads,
and, for its `models` and `implementer` keys only, by every task creation (`agent task create`,
the issue coordinator, error-fix tasks) and `agent models`; an unreadable file, an
unknown top-level key or a malformed model selection therefore stops task
creation. `agent task create` and error-fix tasks ignore errors inside
`accounts`, `roles`, `sessions`, `lanes` or `usage_accounts`; the issue
coordinator validates the whole manifest, including `lane_execution`, before it
accepts a task.
It does not implement the complete issue-to-PR coordinator or a sandbox.

The following is an **operator-supplied example**, never an installed default.
It configures model slot `first` = Claude and model slot `second` = Codex and
binds the lanes device-wide:

```json
{
  "lane_execution": {
    "mode": "this-device-is-lane-host"
  },
  "models": {
    "first": "claude",
    "second": "codex"
  },
  "accounts": {
    "claude-profile": {
      "provider": "claude",
      "config_dir": "/operator/claude-config"
    },
    "codex-profile": {
      "provider": "codex",
      "config_dir": "/operator/codex-config"
    }
  },
  "roles": {
    "first-builder": {
      "account": "claude-profile",
      "model": "operator-selected-model",
      "access": "workspace-write"
    },
    "first-reader": {
      "account": "claude-profile",
      "model": "operator-selected-model",
      "access": "read-only"
    },
    "second-reader": {
      "account": "codex-profile",
      "model": "operator-selected-model",
      "access": "read-only"
    }
  },
  "lanes": {
    "claude:implementer": "first-builder",
    "claude:reviewer": "first-reader",
    "claude:pr-reviewer-quality": "first-reader",
    "claude:pr-reviewer-logic": "first-reader",
    "codex:pr-reviewer-quality": "second-reader",
    "codex:pr-reviewer-logic": "second-reader"
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
`reviewer`, `pr-reviewer-quality`, `pr-reviewer-logic`, `check`) and from the supported
provider adapters (`grok`, `codex`, `claude`, `openrouter`). The `check` kind is
read-only and is required for the provider of model slot `first` when a worker
enables orders, through lane slot `<first>:check`. The implementer and first
review roles of that worker must name one model. Those workflow kinds and adapters
are protocol capabilities used when resolving a lane slot, not preinstalled user
roles.

## Model slots

The lane chain has two logical review slots. Model slot `first` reviews the
inner loop and runs the first PR review pair; model slot `second` runs the
second PR review pair. The required top-level `models` block names the provider
of each review slot:

- `first`: `grok`, `codex`, `claude` or `openrouter`
- `second`: `grok`, `codex`, `claude` or `openrouter`, different from `first`,
  or JSON `null` when an A38 coordinator worker obtains the second review
  externally. An A38 worker with `a38_second_review: "model"` sets `second` to
  a provider.

The optional top-level `implementer` names the implementation provider. It may
be `grok`, `codex`, `claude`, `openrouter`, or `deepseek`. It must differ from
`models.second`; matching `models.first` is allowed and means there is no
separate implementation provider. When the key is absent, the implementer is
`models.first`. `deepseek` is an implementer value only and remains invalid in
either `models` slot. For example, a device may configure:

```json
{
  "models": {"first": "codex", "second": "grok"},
  "implementer": "deepseek"
}
```

Which provider a person works with is this device-local choice; no provider
name is fixed in the chain. Gate stages are `review-1` and `review-2`, with
checklist prefixes `review_1_` and `review_2_`. The old stages `grok-pr` and
`codex-pr` and old keys `grok_pr_quality`, `grok_pr_logic`, `codex_pr_quality`,
and `codex_pr_logic` are legacy aliases: CLI input accepts and warns about them,
and stored rows remain readable without a database rewrite. Coordinator phases `pr_gates_grok` and
`pr_gates_codex` retain their internal persisted names. A device without a
`models` block has no slot assignment; task creation, device model display, and
launch configuration fail loudly.

Tasks recorded before model slots existed may use the historical `grok`, then
`codex` mapping only to read or display their existing agent and gate rows; they
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
values, so changing `models` or `implementer` affects only tasks created
afterwards, whether by the CLI or error-fix. The issue coordinator is the exception: it refuses a
separate implementer at preflight, so setting one stops every coordinator tick, including for
tasks it has already accepted, until `implementer` is removed or set to `models.first`. It hashes the device's `models` and `lane_execution` into each accepted
task's execution binding: a later change stops the task with "task execution
configuration changed" until the accepted configuration is restored. A task
accepted before the current hash formula accepts the matching legacy formula
once and is immediately repinned; this compatibility covers only
the newly added `lane_execution` field and the former omission of the historical
`grok`/`codex` `models` value. Other changes still stop the task. For the PR reviewer roles, `agent agent start --stage review-1|review-2` requires the
provider pinned to that stage. A second-stage agent cannot start until both
first-stage dimensions are approved. `gate record` repeats the slot check and
verifies the agent's recorded stage. Older tasks without an `implementer` field use their
pinned `models.first`. Tasks recorded before model slots existed and therefore
missing `models` remain readable with the historical `grok`, then `codex`
mapping for existing agent and gate rows. They cannot start a new lane or write
a new agent or gate row; recreate them after the local harness has written the
device's `models` selection. For a nonterminal legacy error-fix task, run
`agent task state <id> superseded`; the next `agent watch error-fix` scan ignores
the terminal task and creates a newly pinned task. The watcher performs no
staging or Git operation while that explicit transition is still required.
When `agent run --spec-file` resolves an implementer without a local lane
adapter, it deliberately uses the existing handoff path, names the pinned
provider as the implementer, and creates no agent row.

The issue coordinator refuses `models.first = codex`: its implementer must not
execute software, and the codex lane (`codex exec --sandbox workspace-write`)
cannot be kept from running commands. It also refuses a configured implementer
that differs from `models.first`; separate implementers are not supported by
the coordinator yet. `agent lane run` and `agent run` accept `models.first = codex`.
Every codex lane also passes `-c model_reasoning_effort="high"`, so the reasoning
effort of the account's own codex configuration does not apply to lanes.

The `claude` and `openrouter` adapters run Claude Code with `--restricted`,
`--strict-mcp-config`, `--disable-slash-commands`, and an explicit file-tool
list. Restricted mode ignores user, project and local settings. A write lane
gets only `Read,Grep,Glob,Edit,Write` in its worktree and cannot receive an
extra directory. A read-only lane gets only `Read,Grep,Glob`; a static script
may add an explicit read directory for control artifacts.

The `openrouter` adapter points Claude Code at OpenRouter's Anthropic-compatible
endpoint. Its model is the operator-supplied OpenRouter model id; there is no
default. Put the key in `<config_dir>/openrouter-api-key`. A shell wrapper reads
it only after launch and exports it as `ANTHROPIC_AUTH_TOKEN`; the key never
appears in argv. Keep every OpenRouter `config_dir` outside every worktree and
every added read directory. Launch is refused when those paths overlap. Claude
Code's reported token counts and tool calls are stored in `lane.run`; its
Anthropic-derived `total_cost_usd`/`costUSD` fields are ignored because they are
not OpenRouter prices.
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
build the three views, one at a time: a control-stripped view
removes complete or unterminated controls and their payloads, a payload-kept
view removes only introducers, terminators, and CSI parameter bytes, and a
key-join view uses the assignment scanner's own classifier to retain only
control-sequence bytes that the scanner may join into a key. Those bytes include
a key-character CSI/ESC final and a `-` intermediate; string-control payloads
and all other control bytes are dropped. If any view contains a shared secret
keyword, case-insensitively, the raw logical line is replaced from the start of that
keyword's mapped whitespace token
through logical-line end. Standalone removable C0 bytes are removed from every
view without activating the rule by themselves. Removable control bytes inside
retained string-control payloads are omitted from every structural view, so
they cannot split a keyword in the payload-kept view. String-control spans are
indexed on the complete raw input first, so their payload newlines do not split
logical lines; a mapped start inside one moves to the introducer and the span is
never split by two replacement stages. An unterminated string control extends
through the end of the text and swallows every following physical line into its payload.
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
such text costs time quadratic in the number of those controls. Control-tolerant URL schemes, terminal escape
parameters, and removable-control interleaves have fixed matching windows so
long separatorless text remains linear to process. For bounded lane fields, the
controlled-line structural rule first sees the complete raw source. Its result
is then cut to the limit plus a small inspection margin before the remaining
matching; a token split by that cut is redacted in full before the ordinary
truncation marker is added.
The known-secret raw pass
allows a CSI/ESC final byte to be a
token character, so sanitization cannot assemble a credential that evades the
matcher. Seven-bit DCS, PM, APC, and SOS strings accept
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

For a task pinned with `second: null`, `gate record --stage review-2` is refused;
the external result is recorded in the two `review_2_*` checklist keys with
`external second review by ...` evidence. That evidence counts only on a
coordinator A38 task whose stored second review for the evidence head is
posted and whose newest review does not block the merge.

## Required fields

Each account requires:

- `provider`: `grok`, `codex`, `claude` or `openrouter`
- `config_dir`: absolute path to that profile's provider CLI configuration
  directory (no NUL, newline, CR, or parent traversal); for `claude` this is
  the directory the `CLAUDE_CONFIG_DIR` variable points to. The default
  profile is `~/.claude`: Claude Code keeps its login outside that directory,
  so for exactly this path the lane leaves `CLAUDE_CONFIG_DIR` unset instead of
  pointing it there. Any other directory is a named profile the operator logged
  in with `CLAUDE_CONFIG_DIR=<dir> claude auth login`. An `openrouter` account
  always sets `CLAUDE_CONFIG_DIR`, including when the directory is named
  `~/.claude`.

Each role requires:

- `account`: name of a configured account
- `model`: explicit model id string (no default)
- `access`: `read-only` or `workspace-write` (no default)

The optional top-level `usage_session` selects an explicitly configured
interactive Grok session for automatic billing reads. Missing/null disables
those reads; it never chooses the host profile or the first available account.
The selected session must exist locally, be owned and active.

The optional top-level `usage_accounts` is a list of explicitly named
`openrouter` accounts. Missing/null/`[]` disables OpenRouter polling. Each
configured account is read from `<config_dir>/openrouter-api-key`; unknown names,
accounts of another provider and duplicate names are rejected. `agent watch openrouter-usage`
records changed USD key-usage values from OpenRouter's key endpoint as
`usage.snapshot`, without storing the key, label, or provider ids. Its request
uses an owned client with `trust_env=False`, so ambient proxy and CA
environment variables are ignored and the bearer key cannot be routed through
an intercepting proxy.

Each session may include:

- `interactive`: role name for the interactive runner, or omit/null when none
- `lanes`: map of slot → role name, or omit/null/`{}` when none

The optional top-level `lanes` map holds device-wide bindings with the same
keys. A session's own `lanes` entry wins; a slot the session does not bind
falls back to the device-wide map. This is explicit configuration for every
session on the device, not an implicit identity.

Lane slots are arbitrary non-empty single-line strings. Resolvers look up the
explicit key `vendor:workflow-kind` (for example `claude:implementer`) and
require the bound role's account provider to match `vendor`. For workflow kinds
`reviewer`, `pr-reviewer-quality`, `pr-reviewer-logic`, and `check`, the bound
role must use `access: read-only`; a writable binding is refused. The `check`
kind is required at `<first>:check` when a coordinator worker enables orders.

Unknown top-level, account, role, or session fields are rejected. Dangling
account or role references are rejected. Credentials and API tokens must not
appear in this manifest; they belong only inside each `config_dir`. Error text
from the loader does not echo credential contents.

## Process isolation prefix

`AIRole.env_prefix()` returns an `env` argv prefix for child processes. It
removes ambient `XAI_API_KEY`, `GROK_API_KEY`, `OPENAI_API_KEY`, `CODEX_API_KEY`,
`OPENROUTER_API_KEY`,
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`,
`AWS_BEARER_TOKEN_BEDROCK`, `CLAUDE_CODE_USE_FOUNDRY`, `ANTHROPIC_FOUNDRY_API_KEY`,
`ANTHROPIC_FOUNDRY_BASE_URL`, `ANTHROPIC_FOUNDRY_RESOURCE`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `GROK_HOME`,
`CODEX_HOME`, and `CLAUDE_CONFIG_DIR`, then sets `GROK_HOME`, `CODEX_HOME` or
`CLAUDE_CONFIG_DIR` to the role's account `config_dir`. It does not mutate the parent `os.environ`. Unrelated environment
variables are left for the child to inherit. This is process configuration
isolation, not a sandbox and not a claim that the provider CLI is already
authenticated for that profile.

Every lane provider also removes the upper- and lower-case `HTTP_PROXY`,
`HTTPS_PROXY`, `ALL_PROXY` and `NO_PROXY` variables and the CA overrides
`NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`
and `CURL_CA_BUNDLE`. A lane therefore never inherits an ambient proxy or CA
trust that could read its provider credential. There is no opt-in for this
stripping. The script-owned OpenRouter usage watcher builds its own HTTP client
with `trust_env=False` for the same reason.

## Interactive selection

`AIAccounts.for_session(session_id)` resolves `sessions[session_id].interactive`
and fails when that binding is absent or null. The loader may represent Codex
and Claude/OpenRouter roles in the manifest. Interactive runtime support remains Grok-only;
unsupported interactive providers are refused. The `claude` adapter is a
headless lane (`claude --print`, spec on stdin, `Read,Grep,Glob` for a
read-only lane, plus `Edit,Write` in permission mode `acceptEdits` for a write
lane, edits inside the worktree only; never Bash, subagents or the web). The OpenRouter adapter uses the same restricted file-tool
surface. The configured model is required,
and a supplied `--model` must agree with it. A resumed conversation is pinned to
the configured role, account, model, access and configuration-directory identity;
a changed or missing binding requires a new session. Existing unbound Grok conversation IDs are not silently adopted under a newly
configured account. Interactive provider starts run only on a declared lane
host. Agent does not provide its own remote lane transport or workspace
mirroring. Interactive tmux sessions may still use
`$AGENT_HOME/runtime-targets.json` after the `models` and `lane_execution` checks
pass.

## Migration from implicit defaults

Before this manifest, lane launchers and the interactive runtime used fixed
vendor/role lists and built-in model choices in code. After adopting
`ai-accounts.json`:

1. Create one account entry per provider CLI profile directory you intend to use.
2. Define roles with explicit `account`, `model`, and `access` (no omitted
   fields).
3. Choose the two model slots in `models` and bind the lanes, device-wide in
   the top-level `lanes` map or per session: keys such as `claude:implementer`
   and, when needed, `interactive`.
4. On the external lane host, set `lane_execution` to
   `{"mode":"this-device-is-lane-host"}` before enabling launch paths. The
   local harness's lane starter is responsible for starting work on that host.
5. Sessions without a binding of their own use the device-wide `lanes` map.
   An empty or missing file does not authorize a fallback identity.

Configure the lane bindings (device-wide `lanes` or per session) before
enabling launch paths after upgrading.
For example, the static script invokes `agent lane run --session chosen-session
--role implementer --vendor claude --spec-file task.md --no-tmux`; it resolves the
`claude:implementer` slot to the operator-defined role. `agent run` resolves slots
using its task session. Interactive starts on the lane host use `agent session
start --id chosen-session --provider grok`. Raw terminal
`--cmd` remains an explicitly
supplied script command, outside provider-profile launch enforcement; it is not
a sandboxed model interface. No model lane may invoke these execution commands.
