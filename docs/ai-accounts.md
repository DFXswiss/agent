# AI accounts and roles

Static scripts that load this manifest select AI provider accounts and
user-defined roles from `$AGENT_HOME/ai-accounts.json`. Installation creates no
accounts, roles, or session selections. A missing file, `{}`, or null/empty
`accounts`, `roles`, and `sessions` leaves the configuration empty. There is no
implicit provider profile, model, or role from the host environment, hub
pairing, or built-in defaults.

This device-local manifest is used by `agent lane run`, the lane steps of
`agent run`, interactive Grok session starts, and optional Grok usage polling.
It does not implement the complete issue-to-PR coordinator or a sandbox.

The following is an **operator-supplied example**, never an installed default:

```json
{
  "accounts": {
    "provider-profile": {
      "provider": "grok",
      "config_dir": "/operator/path",
      "lane_runtime": {
        "binary": "/operator/path/to/grok-1.0.13",
        "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
      }
    }
  },
  "roles": {
    "my-builder": {
      "account": "provider-profile",
      "model": "operator-selected-model",
      "access": "workspace-write"
    }
  },
  "sessions": {
    "chosen-session": {
      "interactive": "my-builder",
      "lanes": {
        "grok:implementer": "my-builder"
      }
    }
  }
}
```

The `lane_runtime.binary` and `lane_runtime.sha256` values above are placeholders only,
never a verified binary or hash and never installed defaults. Before lane execution,
the operator must select a supported actual native binary and replace the digest with
the actual SHA256 measured by the static script. Accounts may leave `lane_runtime`
null/unconfigured for interactive-only use; lane execution still requires a configured
runtime.

Add as many named accounts, roles, and session bindings as needed. No fixed
account list, role list, or count is built in. Configurable role names are
chosen by the operator; they are distinct from the fixed workflow kinds
(`implementer`, `reviewer`, `pr-reviewer-quality`, `pr-reviewer-logic`) and from
the supported provider adapters (`grok`, `codex`). Those workflow kinds and
adapters are protocol capabilities used when resolving a lane slot, not
preinstalled user roles.

## Required fields

Each account requires:

- `provider`: `grok` or `codex`
- `config_dir`: absolute path to that profile's provider CLI configuration
  directory (no NUL, newline, CR, or parent traversal)

`lane_runtime` is optional/null for stored accounts, but mandatory for lane
execution: an absolute native `binary` path and its lowercase `sha256` digest.
See [bounded model lanes](lane-boundary.md) for supported adapters, selected
login-file handling, migration and verification limits. Installation supplies
no runtime selection.

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

Lane slots are arbitrary non-empty single-line strings. Resolvers look up the
explicit key `vendor:workflow-kind` (for example `grok:implementer`) and require
the bound role's account provider to match `vendor`. For workflow kinds
`reviewer`, `pr-reviewer-quality`, and `pr-reviewer-logic`, the bound role must
use `access: read-only`; a writable binding is refused.

Unknown top-level, account, role, or session fields are rejected. Dangling
account or role references are rejected. Credentials and API tokens must not
appear in this manifest; they belong only inside each `config_dir`. Error text
from the loader does not echo credential contents.

## Interactive process isolation prefix

`AIRole.env_prefix()` returns an `env` argv prefix for child processes. It
removes ambient `XAI_API_KEY`, `GROK_API_KEY`, `OPENAI_API_KEY`, `CODEX_API_KEY`,
`ANTHROPIC_API_KEY`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `GROK_HOME`, and
`CODEX_HOME`, then sets `GROK_HOME` or `CODEX_HOME` to the role's account
`config_dir`. It does not mutate the parent `os.environ`. Unrelated environment
variables are left for the child to inherit. This is process configuration
isolation, not a sandbox and not a claim that the provider CLI is already
authenticated for that profile.

Bounded lanes use their separate minimal environment and isolated temporary
profile instead of this interactive prefix; see [lane-boundary.md](lane-boundary.md).

## Interactive selection

`AIAccounts.for_session(session_id)` resolves `sessions[session_id].interactive`
and fails when that binding is absent or null. The loader may represent Codex
roles in the manifest. Interactive runtime support remains Grok-only initially;
unsupported interactive providers are refused. The configured model is required,
and a supplied `--model` must agree with it. A resumed conversation is pinned to
the configured role, account, model, access and configuration-directory identity;
a changed or missing binding requires a new session. Existing unbound Grok
conversation IDs are not silently adopted under a newly configured account.

## Migration from implicit defaults

Before this manifest, lane launchers and the interactive runtime used fixed
vendor/role lists and built-in model choices in code. After adopting
`ai-accounts.json`:

1. Create one account entry per provider CLI profile directory you intend to use.
2. For accounts used by lanes, select a supported native binary and its SHA256
   digest for `lane_runtime` (see [lane-boundary.md](lane-boundary.md) for
   supported adapter limitations). Interactive-only accounts may leave
   `lane_runtime` null/unconfigured.
3. Define roles with explicit `account`, `model`, and `access` (no omitted
   fields).
4. Bind each session that should run lanes or an interactive runner: set
   `lanes` keys such as `grok:implementer` and, when needed, `interactive`.
5. Existing sessions are unconfigured until those bindings are added. An empty
   or missing file does not authorize a fallback identity.

Configure sessions explicitly before enabling launch paths after upgrading.
For example, the static script invokes `agent lane run --session chosen-session
--role implementer --vendor grok --spec-file task.md --no-tmux`; it resolves the
`grok:implementer` slot to the operator-defined role. `agent run` resolves slots
using its task session. Interactive starts use `agent session start --id
chosen-session --provider grok`. Raw terminal `--cmd` remains an explicitly
supplied script command, outside provider-profile launch enforcement; it is not
a sandboxed model interface. No model lane may invoke these execution commands.
