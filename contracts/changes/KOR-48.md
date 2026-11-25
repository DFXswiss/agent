# KOR-48 – contract changes

ADR-027 section 3 (additive). Decision: ADR-084 (pull requests at a glance, "Fix conflicts" with a merge commit).

## API (`api/INDEX.json`, `api/objects.json`)
| Endpoint | Right | Purpose |
|---|---|---|
| `GET /v1/workgroups/{wg}/pr-links?task=&cursor=&limit=` (`workgroup-pr-links`) | `wg:read` | links of the workgroup (or of one task), newest first, `limit` 1..200 (default 100), opaque `cursor`; `{items: [pr_link_row], next_cursor}`; read resource `workgroup.pr_links` |
| `POST /v1/workgroups/{wg}/pr-links` (`workgroup-pr-links-post`) | `wg:write` | `{epoch, tag, sealed, task_id}` – link a pull request to a task by its address; current epoch only (older epoch `400 epoch_stale`); `201 {created:true, link}`, a tag linked already under that epoch `200 {created:false, link}` |
| `DELETE /v1/workgroups/{wg}/pr-links/{epoch}/{tag}` (`workgroup-pr-link-delete`) | `wg:write` | remove a person's link; a device link `409 id_conflict`; `{deleted:true}` |

- New object `pr_link_row {epoch, tag, sealed: string|null, task_id, session_id: uuid|null, linked_by: uuid|null,
  created_at}` (exactly one of `session_id`, `linked_by` is set).
- `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}` answers `pr_link_row` without `epoch`/`tag` changes: it adds
  `sealed`, `linked_by`, and `session_id` may be null.
- `POST /v1/device/pr-links` takes optional `sealed`.
- `sealed`: base64url without padding of `nonce(12) ‖ ciphertext(256) ‖ tag(16)` (04-crypto.md §4.11a).

## Device request `github.pull_status` (`requests/`, `common.json#/$defs/request_kind`)
- Request: `{org_id, wg_id, task_id?, pulls: [{repo, number}] (1..50, unique)}`.
- Response (one, final): `{step:"done", pulls: [pull_state]}` or `{step:"failed", code}`.
- `pull_state`: `{repo, number, found:true, title, state: "open"|"closed", draft, merged, mergeable: boolean|null,
  mergeable_state, head_ref, head_sha, head_repo: string|null, base_ref, url, updated_at}` or `{repo, number,
  found:false, code: "not_found"|"repo_not_accessible"|"repo_not_allowed"|"upstream_error"}`.
- `failure_code` adds `github_not_connected` (no identity usable by the requester) and `forbidden` (the device does
  not serve the workgroup or the requester cannot read it).

## Operations (`ops/`)
- New type `github.merge_base` (no dedupe): params `{repo, number, discard?: boolean, github_identity?}`; result
  `{repo, number, branch, base, head_sha, base_sha, merge_base_sha, up_to_date, conflicts: [{path, kind}] (≤ 1000),
  conflicts_total, dir, files, bytes}`, `kind` ∈ `content`, `binary`, `modify_delete`, `type`, `mode`, `protected`.
- `github.commit` params add `merge: true` and `decisions: [{path, choice: "head"|"base"|"deleted"|"edited"}]`
  (≤ 1000, only with `merge`; `paths` with `merge` is `params_invalid`); result adds `merge_parent` (the second
  parent, only for a merge commit).
- `github.get_pull_request` result adds `mergeable: boolean|null` and `mergeable_state`.
- Operation error codes add `conflict_markers`, `conflicts_unresolved`, `merge_outdated`, `merge_pending`,
  `merge_unsupported`, `workspace_dirty`.
- `events/objects.json` `body_operation_params` variant for `github.merge_base`.

## Audit
Actions `pr.linked` (details `{epoch, replaced}`), `pr.unlinked` (`{epoch}`), `read.workgroup.pr_links`.
