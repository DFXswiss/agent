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
adapter's explicit tool settings. For the pinned Grok versions above,
`--tools Read` is the CLI allow-list alias while the canonical native tool
name is `read_file`; `--disallowed-tools read_file` removes that canonical
tool. Measured native fake-provider probes already show an empty work-tool
inventory and injected `read_file` yields `Tool not found`, alongside
positive execution controls. This documents the measured alias/canonical
combination for those pins, not a new bypass claim. `--verbatim` preserves
long task input as text. The complete Grok work input is a JSON-encoded
string with the file mention delimiter escaped: raw `@/path` otherwise
causes the CLI itself to read host files before model execution. The model
decodes source data; the CLI receives no raw mention delimiter. Codex
disables its discovered feature switches and uses a read-only sandbox with
approval policy `never`. This is not a claim that every native handler is
absent: adversarial probes exercise recognized Codex patch calls that are
rejected by the read-only sandbox, and code execution calls whose code-mode
host is disabled. The selected executable, its installed runtime and the
host are trusted; this is not an OS isolation guarantee against a malicious
CLI binary. Native provider metadata requests can still occur.

Source application and `Completed` return happen only after the transport
context exits successfully. If `__exit__` fails while persisting refreshed
auth or cleaning temporary data, that failure propagates and the lane fails
closed without applying proposed edits. Readonly finish outcomes are also
unavailable on teardown failure; they are never treated as false approval.

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

Touched files are checked against the snapshot before application. Before any
capture, the script creates a private same-filesystem recovery directory
outside the repository (mode `0700`) and fsyncs that directory's parent so the
new recovery entry is requested durable on supporting filesystems. It then
writes and fsyncs a private recovery index (`recovery-index.json`) that maps
each source-relative path and action (replace/delete) plus snapshot mode to
the captured basename, and records the source root for operator-only recovery;
the recovery directory itself is fsynced after the index file. That index is
local operator data: it is never model source and is never published into
prompts or messages. The index alone does not make a later capture durable.

Existing targets are then renamed into that recovery directory. After each
capture rename the script fsyncs the destination recovery directory first,
then the source parent directory, before treating capture as ready for
validated publication or deletion success. Captured regular-file data is also
fsynced after the usual nofollow/type/link/size checks. Newly created nested
source parents are likewise fsynced in their parent directory before descent.
Replacements or new files are published with atomic no-clobber link of a fully
written and fsynced exclusive temporary file; after that link the target parent
is fsynced, and after the temporary name is unlinked the target parent is
fsynced again. Concurrent destinations are never unlinked. Any directory or
data sync failure fails the operation rather than reporting complete success;
after mutation the outcome can be uncertain while recovery data remains.
EINVAL and EIO from these barriers are not hidden, and unsupported filesystems
are not pretended to have passed.

Captured originals remain in recovery even after successful publication or
deletion so late writers through existing open descriptors are retained rather
than destroyed; originals are never erased automatically. An earlier fsync of
a captured inode does not make later writes through another open descriptor
durable—those writers must sync their own later data. No-clobber publication
of each replacement is atomic, but capture makes an existing path briefly
absent, so proposals must not depend on intermediate ordering. This is not
filesystem compare-and-swap, not portable CAS, not arbitrary-writer exclusion,
and not a multi-file transaction: paths may be briefly absent during capture;
noncooperating live writers may still produce an uncertain outcome, but
displaced originals are kept for operator recovery instead of being destroyed.
Directory fsync barriers are requested on supporting filesystems; hardware and
filesystem durability guarantees, and any claim of an actual power-cut test,
remain outside scope.

On captured mismatch, publish conflict, sync failure after mutation, or
publication I/O failure the script attempts restoration by writing a fresh
independent inode (exclusive temp, fsync, atomic no-clobber link into the
absent destination, with the same target-parent fsync ordering) while retaining
the original captured inode in private recovery so late open-descriptor writes
stay indexed there. When restoration succeeds, the worktree path is a distinct
`nlink == 1` inode usable by later snapshots; the recovery original remains.
When the destination already exists (this script's own prior publication after
a later sync failure, or another writer), both that existing destination and
the recovery original are preserved; the reported status says existing
destination rather than inventing concurrent provenance. No-clobber protection
against concurrent writers remains. When restoration fails for other I/O or
permission reasons with no destination observed, recovery data is retained and
the error uses neutral retained-in-recovery wording rather than implying an
occupied destination. A `ProtocolError` reports the recovery basename without
leaking private absolute host paths. Concurrent destinations are never blindly
unlinked during cleanup or rollback. Descriptors used on failure paths are
closed exactly once; recovery data and index are kept with no automatic unsafe
cleanup. The root/recovery device preflight only compares the source root and
recovery parent (and refuses a filesystem root); nested mount mismatches among
touched targets can still surface later as retained recovery or uncertainty
rather than an all-files device guarantee. Recovery storage is an explicit
tradeoff and is kept outside Git-controlled paths so it does not clutter the
tracked worktree. The coordinator owns the worktree and treats interrupted
application as uncertain. Source code and model output never become executable
commands.

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
