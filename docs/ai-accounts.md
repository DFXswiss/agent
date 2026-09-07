# AI accounts and roles

Static scripts that load this manifest select AI provider accounts and
user-defined roles from `$AGENT_HOME/ai-accounts.json`. Installation creates no
accounts, roles, or session selections. A missing file, `{}`, or null/empty
`accounts`, `roles`, and `sessions` leaves the configuration empty. There is no
implicit provider profile, model, or role from the host environment, hub
pairing, or built-in defaults.

This loader is device-local configuration only. Wiring launch sites to call it
is separate work in the same draft and is not claimed here.

The following is an **operator-supplied example**, never an installed default:

```json
{
  "accounts": {
    "provider-profile": {
      "provider": "grok",
      "config_dir": "/operator/path"
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

Each role requires:

- `account`: name of a configured account
- `model`: explicit model id string (no default)
- `access`: `read-only` or `workspace-write` (no default)

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

## Process isolation prefix

`AIRole.env_prefix()` returns an `env` argv prefix for child processes. It
removes ambient `XAI_API_KEY`, `GROK_API_KEY`, `OPENAI_API_KEY`, `CODEX_API_KEY`,
`ANTHROPIC_API_KEY`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `GROK_HOME`, and
`CODEX_HOME`, then sets `GROK_HOME` or `CODEX_HOME` to the role's account
`config_dir`. It does not mutate the parent `os.environ`. Unrelated environment
variables are left for the child to inherit. This is process configuration
isolation, not a sandbox and not a claim that the provider CLI is already
authenticated for that profile.

## Interactive selection

`AIAccounts.for_session(session_id)` resolves `sessions[session_id].interactive`
and fails when that binding is absent or null. The loader may represent Codex
roles in the manifest. Interactive runtime support remains Grok-only initially;
the runtime that starts an interactive session must refuse an unsupported
interactive provider. This document does not invent Codex interactive support.

## Migration from implicit defaults

Before this manifest, lane launchers and the interactive runtime used fixed
vendor/role lists and built-in model choices in code. After adopting
`ai-accounts.json`:

1. Create one account entry per provider CLI profile directory you intend to use.
2. Define roles with explicit `account`, `model`, and `access` (no omitted
   fields).
3. Bind each session that should run lanes or an interactive runner: set
   `lanes` keys such as `grok:implementer` and, when needed, `interactive`.
4. Existing sessions are unconfigured until those bindings are added. An empty
   or missing file does not authorize a fallback identity.

Configure sessions explicitly before enabling covered launch paths after
upgrading. Covered launch-site callers are integrated separately; absence of
that wiring here is intentional for this document.
