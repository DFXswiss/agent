# AI accounts and roles

Static scripts that load this manifest select AI provider accounts and
user-defined roles from `$AGENT_HOME/ai-accounts.json`. Installation creates no
accounts, roles, or session selections. A missing file, `{}`, or null/empty
`accounts`, `roles`, `sessions` and `lanes` leaves the configuration empty. An
absent `models` block pins the historical order `grok`, then `codex` on new tasks
and is how tasks without a pin are read; it authorizes no launch without lane
bindings. There is no
implicit provider profile, model, or role from the host environment, hub
pairing, or built-in defaults.

This device-local manifest is used by `agent lane run`, the lane steps of
`agent run`, interactive Grok session starts, optional Grok usage polling,
and, for its `models` block only, by every task creation (`agent task create`,
the issue coordinator, error-fix tasks) and `agent models`; an unreadable file, an
unknown top-level key or a malformed `models` block therefore stops task
creation. `agent task create` and error-fix tasks ignore errors inside
`accounts`, `roles`, `sessions` or `lanes`; the issue coordinator validates the
whole manifest before it accepts a task.
It does not implement the complete issue-to-PR coordinator or a sandbox.

The following is an **operator-supplied example**, never an installed default.
It configures model slot `first` = Claude and model slot `second` = Codex and
binds the lanes device-wide:

```json
{
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

Add as many named accounts, roles, and bindings as needed. No fixed account
list, role list, or count is built in. Configurable role names are chosen by
the operator; they are distinct from the fixed workflow kinds (`implementer`,
`reviewer`, `pr-reviewer-quality`, `pr-reviewer-logic`) and from the supported
provider adapters (`grok`, `codex`, `claude`). Those workflow kinds and adapters
are protocol capabilities used when resolving a lane slot, not preinstalled user
roles.

## Model slots

The lane chain has two logical slots. Model slot `first` implements, reviews the
inner loop and runs the first PR review pair; model slot `second` runs the
second PR review pair. The optional top-level `models` block names the provider
of each slot:

- `first`: `grok`, `codex` or `claude`
- `second`: `grok`, `codex` or `claude`, different from `first`

Which provider a person works with is this device-local choice; no provider
name is fixed in the chain. The stored ledger identifiers keep their historical
names and now mean the slot, not a provider: gate stages `grok-pr` (slot
`first`) and `codex-pr` (slot `second`), checklist keys `grok_pr_*` and
`codex_pr_*`, coordinator phases `pr_gates_grok` and `pr_gates_codex`. A device
without a `models` block keeps the historical order `grok`, then `codex`.

A task pins the device's `models` when it is created (`task create`, the issue
coordinator, error-fix tasks). Every later check for that task (`agent agent start` for `reviewer` and
`implementer`,
`gate record`, `allow`, `next`, `run`, the coordinator gates) uses the pinned
value, so changing `models` affects new CLI and error-fix tasks only. The issue
coordinator also hashes the device's non-default `models` into each accepted
task's execution binding: after a change, tasks it already accepted stop with
"task execution configuration changed" until the configuration is restored. For the PR reviewer roles,
`agent agent start` accepts any supported provider and `gate record` enforces the
pinned slot provider. Tasks without a pin keep
`grok`, then `codex`.

The issue coordinator refuses `models.first = codex`: its implementer must not
execute software, and the codex lane (`codex exec --sandbox workspace-write`)
cannot be kept from running commands. `agent lane run` and `agent run` accept it.

The `claude` adapter runs with `--setting-sources user` and `--strict-mcp-config`,
so a worktree's `.claude/settings.json` hooks and `.mcp.json` never reach a lane.

`agent agent start --role reviewer --vendor X` requires `X` to be `models.first`
pinned on the task;
`--role implementer` accepts the provider of either slot (the chain leaves the
implementer unpinned, the coordinator uses `models.first`); `agent gate record
--stage S --vendor X` requires `X` to be the provider of the slot `S` stands for.

## Required fields

Each account requires:

- `provider`: `grok`, `codex` or `claude`
- `config_dir`: absolute path to that profile's provider CLI configuration
  directory (no NUL, newline, CR, or parent traversal); for `claude` this is
  the directory the `CLAUDE_CONFIG_DIR` variable points to. The default
  profile is `~/.claude`: Claude Code keeps its login outside that directory,
  so for exactly this path the lane leaves `CLAUDE_CONFIG_DIR` unset instead of
  pointing it there. Any other directory is a named profile the operator logged
  in with `CLAUDE_CONFIG_DIR=<dir> claude auth login`.

Each role requires:

- `account`: name of a configured account
- `model`: explicit model id string (no default)
- `access`: `read-only` or `workspace-write` (no default)

The optional top-level `usage_session` selects an explicitly configured
interactive Grok session for automatic billing reads. Missing/null disables
those reads; it never chooses the host profile or the first available account.
The selected session must exist locally, be owned and active.

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
`reviewer`, `pr-reviewer-quality`, and `pr-reviewer-logic`, the bound role must
use `access: read-only`; a writable binding is refused.

Unknown top-level, account, role, or session fields are rejected. Dangling
account or role references are rejected. Credentials and API tokens must not
appear in this manifest; they belong only inside each `config_dir`. Error text
from the loader does not echo credential contents.

## Process isolation prefix

`AIRole.env_prefix()` returns an `env` argv prefix for child processes. It
removes ambient `XAI_API_KEY`, `GROK_API_KEY`, `OPENAI_API_KEY`, `CODEX_API_KEY`,
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `GROK_HOME`,
`CODEX_HOME`, and `CLAUDE_CONFIG_DIR`, then sets `GROK_HOME`, `CODEX_HOME` or
`CLAUDE_CONFIG_DIR` to the role's account `config_dir`. It does not mutate the parent `os.environ`. Unrelated environment
variables are left for the child to inherit. This is process configuration
isolation, not a sandbox and not a claim that the provider CLI is already
authenticated for that profile.

## Interactive selection

`AIAccounts.for_session(session_id)` resolves `sessions[session_id].interactive`
and fails when that binding is absent or null. The loader may represent Codex
and Claude roles in the manifest. Interactive runtime support remains Grok-only;
unsupported interactive providers are refused. The `claude` adapter is a
headless lane (`claude --print`, spec on stdin, read-only or edit-only tool
sets, never Bash, subagents or the web). The configured model is required,
and a supplied `--model` must agree with it. A resumed conversation is pinned to
the configured role, account, model, access and configuration-directory identity;
a changed or missing binding requires a new session. Existing unbound Grok
conversation IDs are not silently adopted under a newly configured account.

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
4. Sessions without a binding of their own use the device-wide `lanes` map.
   An empty or missing file does not authorize a fallback identity.

Configure the lane bindings (device-wide `lanes` or per session) before
enabling launch paths after upgrading.
For example, the static script invokes `agent lane run --session chosen-session
--role implementer --vendor claude --spec-file task.md --no-tmux`; it resolves the
`claude:implementer` slot to the operator-defined role. `agent run` resolves slots
using its task session. Interactive starts use `agent session start --id
chosen-session --provider grok`. Raw terminal `--cmd` remains an explicitly
supplied script command, outside provider-profile launch enforcement; it is not
a sandboxed model interface. No model lane may invoke these execution commands.
