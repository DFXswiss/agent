# Bounded model lanes

`agent lane run`, the lane steps in `agent run`, and the optional issue
coordinator use the same static source executor. The model returns one strict
JSON request at a time: list, read, write, replace, delete, or finish. The
script supplies a source snapshot, validates each request, and applies proposed
edits only after `STATUS: complete` and `RESULT: done`. Review roles cannot
propose edits. Questions, blockers, partial results and invalid requests leave
the source proposal unapplied.

Git/GitHub operations, account selection, lane starts, tests, review ordering,
monitoring and waits remain script responsibilities. There is no protocol
action for any of them. When useful source work ends, the model returns its
result; only the script decides whether another lane should start.

## Explicit configuration

Accounts, roles, session selections and workers remain unconfigured at
installation. Each account used for a lane additionally needs `lane_runtime`:

```json
{
  "binary": "/operator/selected/native-cli",
  "sha256": "<actual lowercase SHA-256 of that executable>"
}
```

This is an illustrative fragment inside an account in `ai-accounts.json`, not
an installed default or a valid placeholder credential. The binary must be an
absolute path to a native executable; shell and Node launchers are rejected.
The script verifies its digest before each invocation. Missing/null runtime
configuration refuses lane execution without selecting another account,
provider, model or binary. Existing account/role counts remain unrestricted.

`for_lane` validates fixed workflow review kinds (`reviewer`,
`pr-reviewer-quality`, `pr-reviewer-logic`) as read-only before launch.
Configured role names in `ai-accounts.json` are arbitrary operator labels, not
those workflow kinds. The SourceSession then enforces the capability the
trusted static caller supplies (`read-only` or `workspace-write`). Existing
generic and coordinator callers already reject writable bindings for those
review workflow kinds; this boundary does not invent further role-name
restrictions or treat a trusted writable builder binding as a reviewer.

The adapters recognize Grok 1.0.5/1.0.13 and Codex 0.147.0/0.153.4. Upgrading a
CLI requires an explicit pin and adapter validation; an unknown version is
refused. A configured profile must contain private regular `auth.json` login
data. Only that authentication file is copied into the temporary profile.
Authentication refreshed by the CLI is persisted under a lock only when the
original selected file has not changed concurrently. Other credential storage
schemes are unsupported by this adapter; no fallback login is attempted.

## Execution and limits

The native CLI receives a separate temporary home, profile and working
directory, a minimal environment, and structured text. Source files are
provided as data through the script. Provider plugins, hooks, MCP settings and
project configuration are not copied from the original profile. The Python
process bridge also uses isolated startup and the minimal environment.

Grok disables subagents/web and removes its native work tools using the
adapter's explicit tool settings. `--verbatim` preserves long task input as
text. The complete Grok work input is a JSON-encoded string with the file
mention delimiter escaped: raw `@/path` otherwise causes the CLI itself to
read host files before model execution. The model decodes source data; the
CLI receives no raw mention delimiter. Codex disables its discovered feature switches and uses a read-only
sandbox with approval policy `never`. This is not a claim that every native
handler is absent: adversarial probes exercise recognized Codex patch calls
that are rejected by the read-only sandbox, and code execution calls whose
code-mode host is disabled. The selected executable, its installed runtime
and the host are trusted; this is not an OS isolation guarantee against a
malicious CLI binary. Native provider metadata requests can still occur.

Task text reaches the model directly as `TASK DATA` text, not nested inside
metadata JSON. TextCLI still JSON-encodes the entire Grok prompt and escapes
raw file-mention delimiters so the CLI cannot treat repository paths as host
file mentions. The source executor appends a static `SCRIPT WORK BUDGET` object
with `remaining_requests` and `remaining_seconds` to every model work prompt.
That budget is script-owned feedback only; the model must not start a timer,
poll, or monitor, and must finish when useful source work ends. The executor
allows at most 200 source requests within the script's lane deadline, snapshots
at most 20,000 paths/50 MB, and accepts text files up to 1 MB. Reads are
paginated to 200 lines and bounded result size; exact replace requires one
occurrence and the current source digest. It rejects host/Git
paths, known control/credential paths, links, binary source and ambiguous path
collisions. These exclusions do not detect every possible secret in ordinary
repository text; the selected repository remains the authorized source scope.

Touched files are checked against the snapshot before application. Individual
writes are atomic; a multi-file proposal is not a filesystem transaction.
The coordinator owns the worktree and treats interrupted application as
uncertain. Source code and model output never become executable commands.

## Migration and verification

Configure each lane account's runtime explicitly before starting lanes after
upgrade. Existing CLI login profiles can be reused subject to the authentication
file requirement above. A local source inventory needs no GitHub account;
GitHub operations still require their own explicit account binding.

Dry runs report the selected bounded executor plan and start neither Git nor
a model. The legacy arbitrary-argv runner is rejected. Coordinator dependency
injection now accepts the bounded source executor contract (`role`, `cwd`,
`manifest`, `spec`, `timeout`), not native CLI argv. Production records no
fabricated native command in `LaneResult.argv`. The legacy `--no-tmux` flag is
accepted for compatibility; lanes no longer start a tmux pane.

Unit tests cover protocol denial, guarded edits, result handling and process
environment isolation. Optional native CLI probes use fake local providers
and dummy accounts, including denied process/subagent/monitor requests and
positive execution controls. The test-only `AGENT_TEST_NATIVE_LANES` manifest
explicitly selects native binaries and hashes; without it those probes skip.
An absent tool inventory or an absent sentinel alone does not establish denial.

Interactive provider sessions are a separate existing execution path; this
lane boundary does not turn them into bounded coordinator lanes. Installing
this change does not configure or activate a worker or demonstrate a live
issue-to-PR deployment.
