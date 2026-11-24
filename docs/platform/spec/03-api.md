# 03 API contracts

> As built: where the implementation refines or deviates from this document, see the register
> [`spec-deviations.md`](../spec-deviations.md#03-api) (DOC-01).

Status: binding. Every request and response body, WebSocket frame, guest-control message, MCP tool and event
body of this document has a JSON Schema in `contracts/` (`contracts/api/<name>.{request,response}.json`,
`contracts/ws/`, `contracts/guest/`, `contracts/mcp/`, `contracts/ops/`, `contracts/events/`). The schemas are
written in M0 from this document; afterwards the schema files are authoritative for field-level details and
this document for semantics. Both implementations validate inbound messages against the schemas in tests
(contract tests, `07-testing.md` §4).

## 1. Conventions

| Topic | Rule |
|---|---|
| Base path | `/v1/`. JSON UTF-8, `Content-Type: application/json`. Unknown fields in requests → `400 request_invalid` (`additionalProperties: false` everywhere). |
| Bytes | b64u (no padding). IDs UUIDv7 text. Times ISO-8601 UTC with milliseconds. |
| Errors | HTTP status + compact body `{"error":"<code>"}`; optional `"detail"` with non-confidential structured data (e.g. `{"pointers":["/params/repo"]}`, `{"expected_seq":17}`). Codes: §11. Never exception or database text. |
| Signed requests | `agent-request-v1` exactly as in `agent_core.signing.request_text`: lines `agent-request-v1`, key ID, METHOD, host, target (path + `?query` as sent), time (`YYYY-MM-DDTHH:MM:SSZ`), nonce (b64u 16 B), `sha256_hex(body)`. Headers `Agent-Key`, `Agent-Time`, `Agent-Nonce`, `Agent-Signature`. Window 120 s, nonce replay store 600 s, body ≤ 1 MiB unless stated (event batches ≤ 4 MiB). |
| Key IDs | browser: `session.<32 hex>` (P-256 raw `r‖s` signature, `agent_core.passkey.encoding.verify_session_signature`); device: `device.<32 hex>` = device UUID without dashes (Ed25519); agent: `agent.<32 hex>` = agent ID without dashes (Ed25519; acts for its user within the scope of `11-agent-access.md` §6). |
| Signed responses | Hub signs every non-streaming response to a signed request with `agent-response-v1` (`Agent-Hub-Key`, `Agent-Hub-Time`, `Agent-Hub-Signature`). Runners MUST verify (pinned hub key); browsers SHOULD. Streaming responses (SSE `/v1/stream`, the WebSocket upgrade `/v1/device/ws` and its frames) are **not** response-signed: they carry hints or data that is itself signed (events, log entries, wraps). Long polls are ordinary signed responses. |
| Root-signed requests | Operator CLI requests (`POST /v1/platform/log`, `GET /v1/platform/enrollments`) use `agent-request-v1` with key ID `platform-root`, signed by the platform root key (Ed25519); the hub verifies against `AGENT_HUB_PLATFORM_ROOT_KEY`. |
| Idempotency | Every create request carries a client-generated ID (`task_id`, `event.id`, `operation_id`, `request_id`). Repeating a request with identical content returns the original result with `200` and `"replayed":true`; different content with the same ID → `409 id_conflict`. |
| Connections | HTTP/1.1 keep-alive. The hub keeps an idle connection for 120 s (`agent-hub serve`); every client reuses an idle connection for a shorter time than any server or proxy on its route keeps it (runner: 2 s), so the client, not the server, closes an idle connection. A reverse proxy in front reuses its upstream connections for less than 120 s. A runner request that fails on a reused connection before any byte of the answer arrived is sent once more (exactly once; the broken connection is dropped), newly signed with the same body; byte-identical repeats are harmless for every device endpoint (Idempotency, `origin_seq`, `seq`, stored credential bytes). The same holds for the copy of a late outbox push, which may reach the hub while the first request is still being processed (the device's stream row serialises them, ADR-050). Answers that do not verify are never repeated (KOR-18, `05-runner.md` §2.3). |
| Pagination | Logs and events: ascending, `?after=<seq>&limit=<n>` (default 200, max 1000), response `next_after`. Lists: newest first, `?before=<opaque>&limit=` (default 50, max 200), response `next_before`. |
| Rights | Every endpoint lists its right (§3). Missing right → `403 forbidden` (never reveals existence of foreign objects: unknown and forbidden IDs both return `404 not_found` for reads, `403 forbidden` for writes on known scopes). |
| Read log | Every authenticated `GET` appends an `audit` row `read.<resource>` before its answer leaves the hub (a read that cannot be recorded is `503 unavailable`); repeats of the same read key (actor, target, scope IDs, result, query) within 300 s are counted in one `audit.repeat` row at the end of the window; `GET /v1/stream` writes `read.stream` and `read.stream.terminal` per watched session (ADR-063 §3). |
| Write audit | Every `POST`/`PUT`/`PATCH`/`DELETE` that reached its route leaves a committed audit row: the route's own, else `request.write` (generic routes, unchanged repeats); a refusal without an own refusal row gets `request.refused` in a transaction of its own, after the rollback; refusals before a principal is known and `429` are not audited per request (ADR-063 §1). |
| Rate limits | Anonymous endpoints: 30/min per source address; recovery as in `04-crypto.md` §5.5; signed endpoints: 600/min per browser session key and 6000/min per device key (runners start an outbox push at most every 100 ms and send a late push once more at most every 2 s; the outbox paces each device on its own, §6.1, ADR-048, ADR-050). `429 rate_limited` with `Retry-After`. |
| Security headers | `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self' http://127.0.0.1:*; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`, `Permissions-Policy: publickey-credentials-get=(self), publickey-credentials-create=(self)`. |

## 2. Configuration of the hub (`AGENT_HUB_*`, fail-closed like `agent_core.config`)

`PUBLIC_URL` (origin; host = WebAuthn RP ID), `DATABASE` (writer DSN), `OWNER_DATABASE` (migrate only; forbidden
in the hub process), `SIGNING_KEY` (Ed25519 seed hex), `HUB_KEY_ID`, `PLATFORM_ROOT_KEY` (b64u), `HOST`, `PORT`
(default 8788), `STATIC_DIR` (default package `static/`), `DEVICE_ONLINE_SECONDS` (30), `PLATFORM_BUDGET_USD_PER_ORG_DAY`
(empty = unlimited), `MODEL_CATALOG` (path of the model catalog file, same format as the runner's, source of `/v1/config.models`),
`CONTEXT_MAX_OBJECT_BYTES` (8 GiB per context checkpoint), `CONTEXT_KEEP` (3 complete checkpoints per task, 1–1000),
`CONTEXT_ORG_BYTES` (200 GiB of checkpoints per organisation; all three ADR-072 §6, 1 to 2^53−1 bytes). `HUB_KEY_ID` MUST match `[a-z0-9-]{1,64}` (required by the reused `HubSigner`). Tests: `AGENT_HUB_TEST_POSTGRES`.
`TRUSTED_PROXIES` (comma-separated addresses or networks; empty = none) and `FORWARDED_HEADER` (`x-forwarded-for`
default, or `forwarded`; refused without trusted proxies): behind a trusted reverse proxy the source address of the
per-source limits is the rightmost untrusted address of that one header (KOR-06). `DOWNLOADS_DIR` (absolute path of an
existing directory; no default, unset = no downloads): the download area of an own device (ADR-074 §7, below). Any
other `AGENT_HUB_*` name is refused (`AGENT_HUB_TEST_*` belongs to the test suites).

**Runner downloads (ADR-074 §7).** `GET /downloads/runner/{name}` (anon, outside `/v1/` like `/agents.md` and
`/build-manifest.json`, not in `contracts/api/INDEX.json`) serves the files of `<DOWNLOADS_DIR>/runner/` unchanged; the
hub never signs or changes them. `latest.json` (`application/json`) and `latest.json.sig`
(`text/plain; charset=us-ascii`) with `Cache-Control: no-cache`; `DFX-Agent-Runner-<YYYY.MM.DD.HHMM>.pkg`
(`application/octet-stream`) with `public, max-age=31536000, immutable`. Any other name, a missing file, a symbolic
link, a path resolving outside `runner/` or an unset `DOWNLOADS_DIR` → `404 not_found`.

**One hub process per database.** Rate limits, the recovery limit, the live streams (SSE/WebSocket fan-out),
reminders and the `metrics` frames are held per process; several processes on one database would split the limits
and send reminders and frames twice (HUB-15, HUB-16; AUD-01 D-03). The hub is operated as exactly one process
(`agent-hub serve`, single uvicorn worker); runbook `../runbooks/hub-operation.md`.

## 3. Rights vocabulary

| Right | Meaning |
|---|---|
| `anon` | no authentication (rate-limited) |
| `user` | valid browser session |
| `org:member`, `org:admin` | active member / admin of the org in the path or body |
| `wg:read`, `wg:write` | access to the workgroup (default workgroup: every member has write) |
| `task:read`, `task:write` | `wg:read` / `wg:write` on the task's workgroup |
| `platform:inviter` | platform role from the platform log |
| `community:moderator` | community moderator, granted by the operator at the hub (`14-community.md` §4.4, ADR-069) |
| `device:any` | valid device signature, device `active` or `pending_certification` (only `GET /v1/device/state`) |
| `device` | valid device signature, device `active` |
| `device:wg` | device assigned to the workgroup |
| `device:session` | session runs on this device |

## 4. Browser API (signed by the browser session key unless `anon`)

### 4.1 Configuration and identity

| Endpoint | Right | Request → Response |
|---|---|---|
| `GET /v1/health` | anon | → `{"ok":true,"version":…}` |
| `GET /v1/config` | anon | → `{"rp_id","origin","platform_root_key","hub_key":{"id","public_key"},"version","limits":{…},"models":{"platform":[ids],"api_key":[ids],"subscription":[ids]},"defaults":{"platform":"deepseek/deepseek-v4.1-flash-1017"}}` (model allow-lists per access kind, from the hub config file `AGENT_HUB_MODEL_CATALOG`; runners use the same catalog file) |
| `POST /v1/invites/lookup` | anon | `{invite_id}` → `{kind,scope,org_id?,expires_at,registered:bool,package_ct?,log_head?,log?:[entries]}` (org invitations return the full membership log up to the head so that the invitee – user or device – can verify it before it is a member); unknown/used/revoked/expired → `404 invite_unknown` (one code for all, no oracle) |
| `POST /v1/passkeys/register/begin` | anon (+invite) or user (`add:true`) | `{invite_id}` (refused with `invite_unknown` if the invitation is used, revoked, expired, already has a `registered_user` or has no known `pk_inv`; the binding happens at `finish` with `inv_sig`, ADR-043) or `{add:true}` → `{ceremony_id,user_id,options}` (`PublicKeyCredentialCreationOptions` JSON: rp, user id = `user_id` bytes, challenge, `pubKeyCredParams` `[{type:"public-key",alg:-7}]` only (ES256/P-256, the only algorithm the reused `agent_core.passkey` verification accepts), `authenticatorSelection{residentKey:"required",userVerification:"required"}`, `extensions.prf.eval.first`, timeout 300000) |
| `POST /v1/passkeys/register/finish` | anon / user | `{ceremony_id,credential:{id,clientDataJSON,attestationObject,transports},identity:{kem_pub,sig_pub}` (first passkey only)`,recovery:{recovery_id,bundle_ct}` (first passkey only)`,cred_wrap:{epoch:1,wrap,sig}` (first passkey only: self-wrap of the user's `CK_1`, signed by the new identity)`,inv_sig` (first passkey only: proof of the invitation code, `04-crypto.md` §5.1, ADR-043; wrong → `401 signature_invalid`, the ceremony is consumed)`}` → `{user_id,credential_id}`. Verification by `agent_core.passkey.webauthn.verify_registration` + `check_client_data`; the stored public key is the 65-byte uncompressed P-256 point. First passkey: the hub creates `scope_epoch(cred,user,1)` + the wrap and sets `invite.registered_user`; a registration that is not followed by `accept` (org invitation) or `POST /v1/orgs {private:true}` (platform invitation) within 24 h is deleted by a hub job and the invitation becomes usable again (until it expires). |
| `POST /v1/passkeys/login/begin` | anon | `{pairing_id?}` → `{ceremony_id,hub_value,options}` (allowCredentials empty; UV required; `pairing_id`: the login confirms that agent pairing, `11-agent-access.md` §5) |
| `POST /v1/passkeys/login/finish` | anon | `{ceremony_id,certificate:{statement,credential_id,authenticator_data,client_data_json,signature}}` → `{user_id,key_id,expires_at,bundle_ct?,prf_expected:bool}`; verification by `certificate.verify_certificate` + `check_counter` |
| `POST /v1/recovery/begin` | anon | `{recovery_id}` → `{user_id,bundle_ct,challenge}` |
| `POST /v1/recovery/finish` | anon | `{user_id,challenge,proof}` → `{ceremony_id,options}` (registration of a new passkey; continue with `register/finish`, then login). A `recovery` ceremony carrying `hub_value` (the proof ceremony of `recovery/begin`) is refused by `register/finish` with `400 ceremony_invalid` before it is consumed (KOR-01, A-14). With `agent` (an account without a passkey, KOR-62): `proof` over the recover text and the new agent becomes the main orchestrator → `{user_id,agent_id}` (`11-agent-access.md` §4, §5.5) |
| `POST /v1/session/end` | user | `{}` → `{}` |
| `GET /v1/me` | user | → `{user_id,identity:{kem_pub,sig_pub},settings,orgs:[{org_id,role,private,default_wg,log_head:{seq,hash}}],platform_roles,unread_notifications,passkeys:[{credential_id,created_at,last_used_at,prf_seen}],cred_holders:{epoch,devices}}` (`cred_holders`: the devices holding a wrap of the user's current `CK` epoch, KOR-04; IDs only) |
| `GET /v1/me/bundles/recovery` | user | → `{bundle_ct}`; unlock without PRF (unlocking does not rotate the code; replacing it: `PUT /v1/me/bundles/recovery`) |
| `POST /v1/me/recovery/begin` | user | `{}` → `{ceremony_id,challenge,options}` (ceremony `recovery_rotate`, 300 s, one attempt; `options` as for login; KOR-01) |
| `PUT /v1/me/bundles/{slot}` | user | `{bundle_ct}` → `{}`; slot must be `passkey:<own credential>` or `recovery` (recovery replace requires `{new_recovery_id}` and either `assertion{ceremony_id,credential_id,authenticator_data,client_data_json,signature}` – an active own passkey over a `recovery_rotate` challenge, checked like a login, counter included – or, without it, once after a finished recovery whose registration added a passkey (else `403 forbidden`); a foreign, used or expired ceremony is `400 ceremony_invalid`, a credential that is no active own passkey `credential_invalid`; audited `auth.recovery {step:"rotate"}`; KOR-01, `04-crypto.md` §5.5) |
| `DELETE /v1/passkeys/{credential_id}` | user | → `{}`; refuses last passkey (`last_passkey`) |
| `DELETE /v1/me/registration` | user | → `{}`; withdraws the signed-in user's **unfinished** registration (invitation unused, no org membership, no platform role, no task): deletes everything the user stored, as the 24 h orphan job does, and frees the invitation at once; otherwise `403 forbidden`. Audited `invite.release` (actor `user`). Used when the new passkey gives no PRF output (KOR-22, ADR-055) |
| `PUT /v1/me/settings` | user | `{model_access,model_id,locale}` → settings |
| `GET /v1/me/devices` | user | → devices eligible for device requests of this user (assigned to a wg in which the user is a member), with presence |
| `GET /v1/me/quotas` | user | → `quota_snapshot` rows of the user; no rows = "not available" (never polled, or the polling device reported `unavailable`) |
| `GET /v1/users/{user}/identity` **(ADR-060)** | user | → `user_identity {user_id,pub,platform_seq,id_sig,account_code}`: the public identity of an active account with its anchor in the platform log; clients verify it (`04-crypto.md` §4.8, §5.13); `404 identity_unproven` for an unknown, disabled or unanchored account |
| `GET /v1/users/lookup` **(ADR-060)** | user | `?code=` (account code as typed; `400 account_code_invalid`) → `user_identity` of the account with this code, else `404 identity_unproven`; no search by name |
| `GET /v1/me/invitees` **(ADR-060)** | user | → `{invitees:[{user_id,invite_id,registered_at,identity:user_identity\|null}]}`: accounts registered through the caller's own platform invitations, newest first |

### 4.2 Invitations

| Endpoint | Right | Request → Response |
|---|---|---|
| `POST /v1/orgs/{org}/invites` | org:admin | `{entry` (log `invite` entry)`,package_ct}` → `{invite_id,seq}` (atomic with the log append) |
| `GET /v1/orgs/{org}/invites` | org:admin | → open and recent invites (state `open|used|revoked|expired`, `registered`, `opened_at`: first answered lookup from the audit or null, KOR-36; `registered_user`: the account that registered with it or null, ADR-073) |
| `POST /v1/orgs/{org}/invites/{id}/revoke` | org:admin | `{entry` (`invite_revoke`)`}` → `{seq}` |
| `POST /v1/platform/invites` | platform:inviter | `{invite_id,kind:"user",expires_at,pk_inv}` → `{}` (code never sent; `pk_inv` not an Ed25519 public key → `400 params_invalid`; the same ID with other values → `409 id_conflict`; ADR-043) |
| `GET /v1/platform/invites` / `POST /v1/platform/invites/{id}/revoke` | platform:inviter | list / revoke |

### 4.3 Organizations, membership log, wraps

| Endpoint | Right | Request → Response |
|---|---|---|
| `POST /v1/orgs` | user | `{genesis` entry`,wraps[],events[]` (`org.profile`, `wg.profile`, `member.profile`)`,platform_invite_id?}` → `{org_id,default_wg}`. `genesis.private:true` REQUIRES `platform_invite_id` = an unused platform invitation whose `registered_user` is the caller (`platform_invite_required`); it is consumed in the same transaction and the org gets `platform_access=true` (one private org per platform invitation). Non-private orgs may be created by any user; they get `platform_access=false` until a root-signed `org_platform_access` entry (ADR-026). |
| `GET /v1/orgs/{org}` | org:member | → `{org_id,private,default_wg,log_head,workgroups:[{wg_id,is_default,archived,my_access,devices:[device_id],epoch}],members:[{principal_id,kind,role,joined_seq}],rotation_pending}` |
| `GET /v1/orgs/{org}/log` | org:member | `?after=` → `{entries:[{seq,entry}],head}` |
| `POST /v1/orgs/{org}/log` | per entry type (`04-crypto.md` §4.7) | `{expected_seq,entries[],wraps[],events[]}` (`expected_seq` = the `seq` of the first entry in the batch = current head + 1) → `{head}`; atomic; hub runs L1–L8, W3, E1–E9, the rotation obligation (`rotation_required` if the batch does not contain exactly the rotate set R of `04-crypto.md` §4.7) and, for `device_assign`/`device_assign_org` with `platform_seq`, the platform-access rule (`platform_access_denied` unless `org.platform_access`); an added `device_assign_org` (or an added device in an org that uses one) only when every active device member of the org announced `log.org_assign` (`409 peer_outdated`, ADR-075 §5) |
| `GET /v1/wraps` | user | `?scope=&scope_id=&epoch=` → wraps where recipient = me |
| `POST /v1/wraps` | user | `{wraps:[{scope,scope_id,epoch,recipient_id,wrap,sig}]}` → `{stored,ignored}` |
| `GET /v1/orgs/{org}/catch-up` | org:member | → list of `{scope,scope_id,epoch,recipient_id}` without wrap that this caller may create (W2) |
| `GET /v1/platform/log` | user | `?after=` → platform log entries |
| `POST /v1/platform/log` | entry signed by the platform root (operator CLI `agent-hub platform …`; no session needed) | `{expected_seq,entries[]}` → `{head}`; types of `04-crypto.md` §4.8 incl. `device_decertify` (hub then refuses the device, sets `decertified`, marks affected scopes `rotation_pending`, sends `decertified` hint) |
| `GET /v1/platform/enrollments` | root-signed request (§1) | → pending platform device enrollments with verified `inv_sig` (for `certify-pending`) |
| `GET /v1/platform/users/{user}` **(ADR-060)** | root-signed request (§1) | → `{user_id,pub,account_code,created_at,platform_invite_id,registered_at,certified_seq}` of an active user (for `certify-user`), else `404 not_found` |
| `GET /v1/platform/waiting` | root-signed request (§1) | → `{waiting:[{org_id,wg_id,reason,tasks,oldest_at,platform_access}]}`: workgroups whose tasks wait for a device the operator must provide (`platform_access_required`, or `no_device` without an assignable device), oldest first (operator CLI `agent-hub platform waiting`, ADR-075 §7) |

### 4.4 Events

| Endpoint | Right | Request → Response |
|---|---|---|
| `POST /v1/events` | per kind (`04-crypto.md` §4.3) | `{events:[{event,notify?:bool}]}` (≤ 100) → `{results:[{id,scope_id,scope_seq,replayed}]}`; all-or-nothing |
| `GET /v1/scopes/{scope_id}/events` | org:member (org scope) / wg:read (wg scope) | `?after=&limit=&kinds=a,b&task=&session=&ref=` → `{events:[{scope_seq,received_at,event}],next_after,head}`; `session.terminal` only when listed in `kinds` |

### 4.5 Workgroups and devices

| Endpoint | Right | Request → Response |
|---|---|---|
| `GET /v1/workgroups/{wg}` | wg:read | → `{wg_id,org_id,is_default,epoch,devices:[{device_id,kind,online,capacity,assigned_via:"workgroup"|"org",vms:[{vm_id,state,sessions}]}],members:[{user_id,access}],placement:{device_id,vm_id}|null}` (`vms`: the workgroup's live VMs on the device with their occupying sessions, ADR-075) – `members` and `my_access` (of `GET /v1/orgs/{org}`) carry the effective access, so every admin appears with `read` unless it has explicit access (ADR-058); no marker, a client derives the source from the verified log |
| `GET /v1/workgroups/{wg}/placement` / `PUT …/placement` | wg:read / wg:write | → / `{device_id:uuid|null,vm_id:string|null}` → `{placement:{device_id,vm_id}|null}`: the workgroup's default device and VM for new tasks (`null` device = automatic; audit `wg.placement`); the device must be a candidate of the workgroup (`403 placement_forbidden`), the VM a live VM of the workgroup on it (`409 placement_invalid`) (ADR-075 §6) |
| `GET /v1/workgroups/{wg}/device-candidates` | wg:write | → platform devices `{device_id,platform_seq,pub,online,capacity}` ordered by free capacity (§4.6.2) |
| `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}` | wg:read | → `{task_id,session_id,created_at}` of the pull request whose opaque tag (`04-crypto.md` §4.11) a runner registered for an epoch of the workgroup; `404 not_found` for an unknown or malformed epoch or tag (ADR-072 §8; the button on the pull request is a later package) |
| `GET /v1/orgs/{org}/devices` | org:member | → devices of the org and platform devices assigned to its workgroups: `{device_id,kind,state,online,last_seen_at,agent_version,capacity,assigned_wgs,assigned_org,vms:[{vm_id,wg_id,state,sessions}]}` (`assigned_org`: assigned to the whole organization, ADR-075 §5; `vms` only of workgroups the caller may read) |

### 4.6 Tasks and sessions

| Endpoint | Right | Request → Response |
|---|---|---|
| `POST /v1/tasks` | wg:write | `{task_id,wg_id,events:[task.meta,prompt],placement?:{device_id,vm_id?}}` → `{task,session:{session_id,state,device_id,estimate_ms},events:[…]}`; the task is always created – without a device it waits `scheduling` with a reason of §4.6.2 (ADR-075 §7; `409 no_device` is no longer answered). `placement` (else the workgroup default, else automatic): `403 placement_forbidden` for a device that is not a candidate of the workgroup, `409 placement_invalid` for a VM that is not a live VM of the workgroup on that device |
| `GET /v1/workgroups/{wg}/tasks` | wg:read | `?state=&before=&order=` → tasks newest first (`order=created`, default) or by latest activity (`order=activity`, KOR-32) `{task_id,owner_id,state,created_at,last_activity_at,main_session_id,title_event_id}` |
| `GET /v1/tasks/{task}` | task:read | → task + sessions (tree) + usage per session + latest transitions |
| `POST /v1/tasks/{task}/prompts` | task:write | `{event}` (kind `prompt`) → `{scope_seq,session_id}` (resumes or restarts the main session, §4.6.3) |
| `POST /v1/tasks/{task}/close` | task:write | `{}` → task (`closed`; all sessions `stopping`) |
| `PUT /v1/tasks/{task}/placement` | task:write | `{device_id:uuid|null,vm_id:string|null}` → task: changes the chosen device/VM (`null` = automatic) for the waiting and later main sessions; same checks as `POST /v1/tasks`; audit `task.placement` (ADR-075 §6) |
| `POST /v1/sessions/{session}/control` | task:write | `{action:"interrupt"|"stop"}` → `{}` |
| `GET /v1/sessions/{session}` | task:read | → session incl. transitions, usage, device, vm, parent, children |
| `GET /v1/tasks/{task}/contexts` | task:read | → `{contexts:[{object_id,session_id,author_id,epoch,chunks,size,digest,created_at,completed_at}]}`: the complete context checkpoints of the task (metadata only, newest first as in §4.6.3, at most 1000); uploads in progress are never listed (ADR-072 §5) |

Session rows (`GET /v1/tasks/{task}`, `GET /v1/sessions/{session}`, the `session` frame) carry `context_restore`
(`restored`, `fallback` or null) and `context_reason` (closed list `common.json#/$defs/context_reason`): what the
runner reported for a main session that continued an earlier one (§4.6.3). The user's agent reads all of this through
the same endpoints (`11-agent-access.md`, KOR-24 parity); continuing is `POST /v1/tasks/{task}/prompts`.

#### 4.6.1 First-task flow (normative sequence)

1. Browser: `GET /v1/workgroups/{wg}`. If `devices` is empty: `GET …/device-candidates`; pick the first
   candidate; verify its certification in the platform log; append `device_assign` + wraps of `WK`, `OK` and the
   own `CK` for the device in one `POST /v1/orgs/{org}/log` batch (admins only – in a private org the single member is the admin – and only if the org has platform access, ADR-026). If no candidate exists or the user is no admin, the task is created anyway and waits (`no_device` / `platform_access_required`, §4.6.2) with the follow-up in the task view.
2. Browser: `POST /v1/tasks` with `task.meta` (title derived per `06-web.md` §4.2) and `prompt` events.
3. Hub (one transaction): checks, stores events, creates `task(scheduling)` and `session(requested)`, runs the
   scheduler, sets `session.device_id`, `estimate_ms`, task `starting`, writes audit; after commit: NOTIFY,
   WebSocket hint `session.start` to the device, SSE frames to browsers.
4. Runner: reconciles (`GET /v1/device/state`), starts the session (`05-runner.md` §6), pushes transitions.
5. Acceptance metric: `first_working_at − task.created_at` (both hub clock; `first_working_at` = hub receive
   time of the `working` transition) **< 10 s** (`07-testing.md` §5.3).

#### 4.6.2 Scheduler (deterministic)

Candidates = devices assigned to the workgroup, `active`, online. Order: (1) device with a VM of this workgroup
in state `running`; (2) device with a `stopped` VM of this workgroup; (3) highest `capacity.vm_slots_free`, then
highest free VM memory budget; (4) smallest device ID. A device with `vm_slots_free = 0` and no VM of the
workgroup is skipped. Estimates: (1) 2500 ms, (2) 6500 ms, (3) with `pool_ready > 0` 3000 ms else 8000 ms.
No candidate: task stays `scheduling` with reason `device_offline` or `capacity_exhausted`; the hub re-runs the
scheduler when a device of the workgroup connects or reports capacity, and the browser shows the real reason.
**Several VMs per workgroup (ADR-075 §3):** candidates include devices assigned through the organization
(`device_assign_org`). A device counts at level (1) only while a running VM of the workgroup has room
(`capacity.sessions_per_vm` minus its occupying sessions – states `requested` … `stopping` without `suspended` –
after the workgroup's sessions placed there without a VM yet), at level (2) only with a stopped VM with room, and
otherwise at level (3) a new VM: a ready pool VM (`pool_ready > 0`) or `vm_slots_free > 0`, and fewer than `AGENT_HUB_VMS_PER_WG` (default 100, 0 = no limit)
live VMs of the workgroup on all devices; else it is skipped. Without `sessions_per_vm` (0 or not reported) every
running VM has room. Each placement of a re-scheduling round is booked on its device (a pending session; at level (3)
also a slot and a VM of the workgroup). A task with a chosen device (`placement`) considers only that device and,
with a VM, only that VM's room; when the device is no longer a candidate the choice is ignored (automatic). Reasons
while waiting and their category for the situation view: `capacity_exhausted`, `wg_vm_limit` (the workgroup is
at `AGENT_HUB_VMS_PER_WG` while a device has a free slot) and `placement_full` (the chosen device or VM is full) →
`waiting_for_capacity`; `device_offline`, `placement_offline` (the chosen device is offline), `keys_pending` (the
devices with room are assigned only through the organization and lack the wrap of the workgroup's current key),
`no_device` (no active device assigned) and `platform_access_required` (no device assigned, the org has no device of
its own and no platform access) → `waiting_for_device`; `credit_exhausted` (KOR-31: the org's credit is used up and
only platform devices could take it) → `waiting_for_credit`. A new VM (level 3) also needs, when the device reports
`disk_free_mib`, at least 20480 MiB free. The device state names per session whether the runner may create a VM for
it (`vm_new_allowed`). The hub re-runs the scheduler also after a placement change. Tasks waiting
with `platform_access_required`, or with `no_device` while no device could be assigned, are listed for the operator
(`GET /v1/platform/waiting`).
**Refusal for lack of capacity** (`05-runner.md` §3.4, ADR-049): when the device fails the newest main session of an
open task with `capacity_exhausted` and prompts are pending there, the hub creates the next main session (an
*attempt*: its first transition `requested` carries the reason `capacity_exhausted`) in the same transaction, without
showing the task `failed` in between; the attempt takes over the undelivered prompts (§4.6.3) and is placed by this
scheduler. A device that refused counts with `vm_slots_free = 0` until its next heartbeat, and for the refused
workgroup a stopped VM no longer exempts it from needing a free slot until it reports one (a running VM still does),
so the attempt goes to another device with room or waits `scheduling` (`capacity_exhausted`). Not after the user
stopped the session. Refusals in a row for the same prompts (sessions refused before they ever worked) up to
`AGENT_HUB_CAPACITY_REFUSALS` (default 3) are placed at once; from then on the next attempt waits `scheduling`
(`capacity_exhausted`) and is not placed before `AGENT_HUB_REFUSAL_HOLD_SECONDS` (default 60, `session.not_before`)
– the task never fails for want of capacity (ADR-075 §7). The attempt resumes from the last session that
ran, on whichever device (§4.6.3; a refused session never ran an agent).
Capacity warning: when `vm_slots_free / vm_slots_total < 0.1` the device carries `capacity_low:true` in all
device lists (UI badge) and in metrics (alert rule, `08-monitoring.md`).

#### 4.6.3 Follow-up prompts

The prompt is routed to the main session. Main session `idle` → device delivers it; `suspended` → device
resumes (`claude --resume`) then delivers (when the session's VM is gone, the runner restores it from its newest
checkpoint on another VM first); `stopping`/`stopped`/`failed`, or a closed task (reopened) → the hub creates a new
main session (depth 0, same task) on the scheduler's device, **on any device of the workgroup**. That session
**continues the same conversation** (ADR-072): its device state names `resume_from` = the task's previous main
session once it had ended (a session refused for lack of capacity before it worked is skipped) and `context
{object_id,session_id,created_at,size}` = the task's newest complete context checkpoint. Newest means ordered by the
creation of the checkpoint's session at the hub, then by the object ID (UUIDv7) - never by a device clock or the time
of the commit, so a late upload of an earlier session never supersedes its successor's. When `context.session_id` is
not `resume_from`, the final checkpoint of that session has not arrived yet; the runner waits for it a bounded time
(same device: its local copy; another device: it reads the device state again) and otherwise restores the newest one
and says so (`stale`). The runner restores the transcript and the workspace into the new sandbox and starts `claude
--resume` with the **same** Claude session ID; the session is told in one sentence what changed (VM or device,
checkpoint time, workspace restored fully, partly or not, a work VM to be requested again) - as `restore.note`,
which the guest attaches with the current session facts once to the first hook answer of the resumed conversation;
Claude Code ignores a new harness file on `--resume` (05-runner.md §8). It reports the outcome
as the outbox audit `session.context_restored {object_id,workspace,stale}`; the hub stores `context_restore =
restored` on the session row, with `context_reason` `workspace_partial`/`workspace_omitted` when the workspace came
back only partly or not at all (too large). **Only** when no checkpoint can be used does the session start a new
conversation whose harness tells it to read the chat history with `get_task_messages` - the reported emergency path:
audit `session.context_fallback {reason}` (`context_missing`, `context_invalid`, `context_unavailable`,
`version_incompatible`, `restore_failed`), `context_restore = fallback` with that reason, the log metric
`agent_hub_session_context_total{result,reason}` and a notice on the task (`06-web.md` §4.4). Never silent.

Prompts a main session did not deliver before it ended `stopping`/`stopped` are carried over to the next main
session of the task and delivered first, in event order; a `failed` session keeps them ("Retry" sends the newest
prompt again), except that the hub's attempt after a refusal for lack of capacity (§4.6.2) takes them over. When
the device itself ends the newest main session of an open task `stopped` after it delivered a prompt and prompts are
still pending there, the hub creates the next main session at once, placed like a
follow-up's, without a new prompt; after a user's "Stop" or on a closed task they wait for the next follow-up
(KOR-11).

### 4.7 Operations (read-only for browsers)

`GET /v1/tasks/{task}/operations` (task:read) → newest first; `GET /v1/operations/{id}` (task:read) → operation
with transitions and the IDs of the `operation.params` / `operation.result` events.

### 4.8 Credentials

| Endpoint | Right | Request → Response |
|---|---|---|
| `GET /v1/me/credentials` | user | → `[{kind,version,epoch,updated_at}]` |
| `GET /v1/me/credentials/{kind}` | user | → record (`04-crypto.md` §4.4) |
| `PUT /v1/me/credentials/{kind}` | user | `{record,expected_version}` (0 = create) → `{version}`; `409 version_conflict` |
| `DELETE /v1/me/credentials/{kind}` | user | → `{}`; hard delete; hint `credentials` to all eligible devices (they purge caches) |
| `POST /v1/me/credentials/rotate` | user | `{epoch,wraps[],records[]}` → `{}` (CK rotation, all records re-encrypted atomically) |

### 4.9 Whiteboards and notifications

| Endpoint | Right | Request → Response |
|---|---|---|
| (write) `POST /v1/events` with `whiteboard.*` | see `04-crypto.md` §4.3 | `notify:true` allowed only on `whiteboard.entry` |
| `GET /v1/whiteboards` | org:member (org level) / wg:read | `?org=&wg=&task=&level=` → entries `{entry_id,author_id,created_at,updated_at,deleted,latest_event:{scope_id,scope_seq}}` newest first |
| `GET /v1/whiteboards/{entry}/history` | same | → all events of the entry ascending |
| `GET /v1/notifications` | user | `?before=` → newest first |
| `POST /v1/notifications/read` | user | `{ids:[…]}` or `{all:true}` → `{}` |

Notification fan-out on `whiteboard.entry` with `notify:true`: recipients = all users with read right on the
level (org level: org members; wg/task level: wg readers) except the author; each gets a `notification` row and
an SSE frame. Active sessions in scope (org level: all non-stopped sessions of the org; wg level: of the wg;
task level: of the task) get a content-free ping through their device (`ping` hint, §6.2).

Other notification rules: `task.idle` to the task owner when the main session goes `working → idle` after at least 10 s of `working`; `task.failed` to the owner when the task becomes `failed`; `invite.accepted` to the inviting admin. `rotation.required` is no longer created (KOR-34, ADR-058: owed rotations run silently in the admins' browsers); existing rows stay valid and render neutrally.

### 4.10 Device requests

| Endpoint | Right | Request → Response |
|---|---|---|
| `POST /v1/device-requests` | user (device must be in `GET /v1/me/devices`) | `{envelope}` (`04-crypto.md` §4.5) → `{request_id,state}` |
| `GET /v1/device-requests/{id}` | user (owner) | → state + responses |

If `GET /v1/me/devices` is empty (fresh private org), the web client first runs the device auto-assign batch of
§4.6.1 step 1 for the user's default workgroup, then sends the request. The hub sets the request `answered` when
a response with header `final:true` arrives (success or error – the outcome is inside the ciphertext);
`expired` after 15 min without a final response. Follow-up requests
(`follows` set) are accepted only while the original is `pending` or `delivered`.

### 4.11 Monitoring and audit

| Endpoint | Right | Request → Response |
|---|---|---|
| `GET /v1/monitoring/overview` | org:member | `?org=` → `{devices:[{device_id,online,capacity,latest}],vms:[{vm_id,device_id,wg_id,state,latest}] (only own wgs),sessions:[{session_id,task_id,wg_id,state,latest,usage}] (wg:read)}` |
| `GET /v1/monitoring/series` | as overview for the entity | `?entity_kind=&entity_id=&metric=&from=&to=&step=` (`step` ≥ 15 s raw within 48 h, ≥ 300 s from rollups) → `{points:[[ts,value]]}` |
| `GET /v1/orgs/{org}/audit` | org:admin (all), org:member (rows of wgs with wg:read and own actions) | `?before=&action=&actor=&task=&session=&wg=` → audit rows newest first |
| `GET /v1/orgs/{org}/audit/verify` | org:admin | → `{ok,head:{seq,hash},checkpoint}` |

Usage billing and prepaid credit (KOR-31) are specified in `12-billing.md`: the endpoints `GET /v1/orgs/{org}/billing`
(org:member), `GET /v1/workgroups/{wg}/charges` (wg:read) and `GET /v1/tasks/{task}/charges` (task:read) (section
6), the `billing` object of `GET /v1/device/state` (section 5) and the outbox item `runtime` (section 4).
### 4.12 Community

The endpoints of the community (`/v1/community…`), its error codes and the audience `all` with the frame `community` are
specified in `14-community.md` §4 and §5 (KOR-26, ADR-069).

## 5. Browser live channel

`GET /v1/stream` (signed, read with `fetch` streaming; `Accept: text/event-stream`). Query: `org=<id>` (one
active org), optional `terminal=<session_id>` (repeatable, max 3), `cursors=<scope_id>:<seq>,…`, `monitoring=1` (adds `metrics` frames). Response frames
`event: <type>\ndata: <json>\n\n`; heartbeat comment every 15 s.

| Frame | Data |
|---|---|
| `hello` | `{stream_id,heads:{scope_id:seq},hub_time}` |
| `event` | `{scope_id,scope_seq,event}` – every event in readable scopes of the org except `session.terminal` |
| `terminal` | `{scope_id,scope_seq,event}` – `session.terminal` of watched sessions |
| `task` | task row after a change |
| `session` | session row + newest transition |
| `operation` | operation row after a change |
| `log` | `{org_id,head}` |
| `wraps` | `{}` (new wraps for me; pull `GET /v1/wraps`) |
| `notification` | notification row |
| `device_response` | `{request_id,seq}` |
| `metrics` | latest samples of visible entities (every 15 s, only while `monitoring=1` query flag is set) |
| `resync` | `{reason}` – client MUST re-pull with its cursors (sent after a buffer overflow or hub restart) |

Delivery: at-least-once; the client de-duplicates by `(scope_id, scope_seq)`. On connect the hub replays events
after the given cursors (max 1000 per scope; beyond that `resync`). Gap detection: a frame with `scope_seq >
cursor + 1` makes the client pull the gap with `GET /v1/scopes/{scope_id}/events`. Latency target hub
receive → browser frame: p95 < 300 ms on LAN.

## 6. Device API (signed by the device key)

### 6.1 Endpoints

| Endpoint | Right | Request → Response |
|---|---|---|
| `POST /v1/device/enroll` | signature by the key in the body (proof of possession) + invite proof | `{device_id,kind:"org"|"platform",invite_id,pub:{kem,sig},entry?` (org: `accept` entry with `sig`+`inv_sig`)`,enroll_sig?,inv_sig?` (platform)`,agent_version,os,arch,capacity}` → `{state:"active"|"pending_certification",org_id?}` |
| `GET /v1/device/state` | device:any | pending device → `{state:"pending_certification",hub_time}`; active device → desired state: `{state:"active",last_origin_seq,orgs:[{org_id,log_head}],assigned:[{org_id,wg_id}],sessions:[{session_id,task_id,org_id,wg_id,state,parent_session_id,depth,owner_id,model_access_pref,model_id_pref,resume_from?,context?:{object_id,session_id,created_at,size},vm_id_pref?,vm_new_allowed?}],pending_prompts:[{task_id,session_id,scope_id,scope_seq,event_id}],delivered_prompt_event_ids:{session_id:[event_id]}` (last 100 per active session)`,requests:[{request_id}],pings:[{ping_id,session_id,kind,level,ref,created_at}]` (undelivered rows of `session_ping`)`,credentials_changed:[owner_id],rotation_pending:[…],quota_poll_owners:[owner_id],platform_budget:{usd_per_org_day,spent_today_by_org}|null,hub_time}`; `quota_poll_owners` = subscription owners whose quota this device polls (the eligible online device with the smallest ID). After a local store wipe the runner continues its outbox at `last_origin_seq + 1` and never re-delivers the prompts in `delivered_prompt_event_ids`. `resume_from` and `context` only on main sessions (§4.6.3). |
| `POST /v1/device/outbox` | device (+ item rights) | `{items:[{origin_seq,item_kind,body}]}` (≤ 500 items, ≤ 4 MiB; pushes paced per device by a token bucket of one push per 100 ms with a reserve of 10 pushes, beyond it `429 rate_limited` with `Retry-After: 1`, ADR-048) → `{acked_through,results:[{origin_seq,status:"ok"|"replayed"|"rejected"|"reseal",error?}]}` |
| `GET /v1/device/pull` | device:wg / org | `?cursor=<scope_id>:<seq>` (repeatable) `&kinds=` `&limit=` → every event kind except `session.terminal` in the wg scopes of assigned workgroups, plus `org.profile`, `wg.profile`, `member.profile`, `device.profile` and org-level `whiteboard.*` in the org scope (needed for `get_task_messages`, restarted sessions and duplicate results) |
| `GET /v1/device/workgroups/{wg}/tasks` | device:wg | `?state=&before=&limit=` → task metadata as `GET /v1/workgroups/{wg}/tasks` |
| `GET /v1/device/tasks/{task}` | device:wg | → task, sessions (tree, states), operations (`id,type,state,error_code,result_event_id`) metadata |
| `GET /v1/device/logs` | device | `?org=&after=` → membership-log entries; `GET /v1/device/platform-log?after=` |
| `GET /v1/device/wraps` / `POST /v1/device/wraps` | device | as §4.3 for the device as recipient / wrapper (W2) |
| `POST /v1/device/operations` | device:session | §8.3 → decision |
| `GET /v1/device/credentials/{owner}/{kind}` | device eligible for owner's CK | → record |
| `PUT /v1/device/credentials/{owner}/{kind}` | same, only while answering a device request of `owner` | `{record,expected_version,request_id}` → `{version}` |
| `GET /v1/device/requests/{id}` / `POST /v1/device/requests/{id}/responses` | device addressed | → envelope / `{envelope}` → `{}` |
| `PUT /v1/device/contexts/{object}/chunks/{index}` | device:session | `{session_id,ct}` (`ct` = one sealed chunk, 29 B – 2 MiB + 28 B, `04-crypto.md` §4.11; the body may use the 4 MiB batch limit) → `{replayed}`. The session must be a main session (`400 request_invalid` at `/session_id`) that runs or ran on the calling device in a workgroup it is assigned to (`403 forbidden`); the first chunk creates the object (`uploading`) for that session and device, another session or device under the same ID is `409 id_conflict`; identical bytes again `replayed: true`, other bytes at a stored index or a new chunk after the commit `409 id_conflict`. Quotas (`413 quota_exceeded`, `detail.quota {scope:"object"|"org",limit,used}`): `AGENT_HUB_CONTEXT_MAX_OBJECT_BYTES` per object, `AGENT_HUB_CONTEXT_ORG_BYTES` for every object of the organisation (serialised per organisation); each refusal is audited as `session.context_refused` |
| `POST /v1/device/contexts/{object}/commit` | device:session | `{manifest,sig}` → `{completed_at,replayed}`: only the author device of an assigned workgroup (`403`); the manifest (`04-crypto.md` §4.11) names exactly the object's IDs and an epoch of the workgroup (`400 request_invalid` at the field), its signature verifies with the device key (`401 signature_invalid`), `chunks`/`size`/`digest` equal the stored chunks `0..chunks-1` (`400` at the field). Atomically `complete`; the same manifest and signature again `replayed`, anything else afterwards `409 id_conflict`. In the same transaction (one commit per task at a time) the task's complete checkpoints beyond the newest `AGENT_HUB_CONTEXT_KEEP` (order §4.6.3; never the one just committed) are deleted; audit `session.context_saved {chunks,size,pruned}` |
| `GET /v1/device/contexts/{object}` / `GET /v1/device/contexts/{object}/chunks/{index}` | device:wg | → `{manifest,sig,created_at,completed_at}` / `{ct}` of a **complete** object of a workgroup the device is assigned to (a restore may run on any device of the workgroup); an upload in progress, an object of another workgroup or a missing chunk is `404 not_found` |
| `POST /v1/device/pr-links` | device:session | `{session_id,epoch,tag}` → `{replayed}`: the opaque tag of a pull request the main session created (ADR-072 §8), rights as for a chunk; the epoch must be one of the workgroup (`400` at `/epoch`); the same tag for the same task again `replayed`, for another task `409 id_conflict` (the first task keeps it) |

Outbox item kinds and their effect at the hub (closed list `contracts/enums/outbox-item-kinds.json`; credentials, operation registrations and device responses use their own endpoints, never the outbox):

| `item_kind` | Body | Effect |
|---|---|---|
| `event` | `{event}` (device-authored kinds) | append (E1–E9) |
| `session_transition` | `{session_id,state,reason,at,agent_version?,model_access?,model_id?,claude_session_id_present?,vm_id?}` | session row + transition + task derivation + SSE; `vm_id` (from `vm_ready` on, ADR-075) sets `session.vm_id` |
| `vm_state` | `{vm_id,wg_id,state,golden_version,cpus,memory_mib,disk_gib,at}` | `vm` row; for a device that did not announce `vm.choice` (one VM per wg and device): `assigning`/`running` with a wg sets `session.vm_id` of the device's open sessions of that wg (a `session_transition` of an open session without VM takes it too) |
| `operation_transition` | `{operation_id,state:"executing"|"succeeded"|"failed"|"expired",error_code?,retryable?,result_event_id?,at}` | operation row + transition + dedupe slot release on failure |
| `usage` | `{session_id,delta:{requests,input_tokens,…,cost_micro_usd},cost_basis,context_used_tokens,context_window_tokens,last_provider,model?,access_origin?,at?}` | `usage_counter` += delta; token charge (`12-billing.md` §4) |
| `quota` | `{owner_id,windows:[{window,used_percent,resets_at,severity}],fetched_at}` or `{owner_id,unavailable:true,fetched_at}` (the usage endpoint refused the token, KOR-05) | `quota_snapshot` (an `unavailable` report deletes the owner's rows with `fetched_at` ≤ its own) |
| `metrics` | `{samples:[metric sample]}` | `metric_sample` |
| `audit` | `{action,result,task_id?,session_id?,target_kind,target_id,detail}` (closed list `contracts/enums/outbox-audit-actions.json`: `hook.denied`, `read.session.<tool>`, `ping.delivered` (for a hub ping, `whiteboard.entry`, also marks the `session_ping` row delivered; the runner's own `subagent.finished`/`operation.finished` pings are audited without a hub row, KOR-07), `session.prompt_refused`, `model.provider_outside_allowlist`, `runner.inbound_rejected`, `runner.log_rollback`, `runner.log_fork`, `runner.alert`, `session.context_restored`, `session.context_fallback` (both also set `context_restore`/`context_reason` on the main session and send its `session` frame, §4.6.3)) | audit row |
| `wrap` | `{wrap}` | as `POST /v1/device/wraps` |
| `runtime` | `{session_id,vm_id,vm_kind:"session"|"work",size,from,to,runtime_ms}` | runtime charge of a platform device (`12-billing.md` §4) |

Processing is strictly in `origin_seq` order. Rule set (pattern of the legacy `/sync/push`): `origin_seq` must
be `last+1` (`409 origin_seq_gap` with `detail.expected`; a runner that no longer holds `detail.expected` – it
prunes acknowledged items after 1 h, the hub was restored from an older export – confirms the hub's position with
`GET /v1/device/state`, renumbers its remaining items to follow it and reports the lost ones as `runner.alert`
`outbox_items_lost`, `05-runner.md` §2.3, KOR-10); an already stored `origin_seq` with identical body →
`replayed`; different body → `409 origin_seq_conflict` (fatal for the runner: stops syncing, raises an alert),
except that an item never stored before may be resent with new bytes after `reseal`. An `event` item whose only
failure is E6 (`epoch_stale`) gets `status:"reseal"`; processing of the batch stops there (`acked_through` =
previous item) and the runner re-seals it with the current epoch.
An item rejected for content reasons (e.g. `kind_not_allowed`) is acknowledged with `status:"rejected"` and
logged; the sequence continues (a poison item must not block the stream). The runner never sends an event it
cannot seal: such an event and the later items of its scope wait in a runner-side scope queue and are numbered only
once they can be sealed, so `origin_seq` stays gapless and other scopes are not held back (`05-runner.md` §2.3,
ADR-047).

### 6.2 Device WebSocket (`GET /v1/device/ws`, signed upgrade request)

The upgrade request may carry the header `Agent-Features: <feature>[,<feature>…]` (not signed, not a query parameter:
an older hub ignores unknown headers but refuses unknown query parameters). The hub replaces `device.features` with it
on every connection. `log.admit` (ADR-060): the runner verifies `admit` and `user_certify`/`user_decertify`; an
`admit` batch is refused with `409 peer_outdated` (`detail.devices`) while an active device of the org lacks it.

Hints from hub to device (JSON frames `{"type":…}`): `hello {hub_time}`, `reconcile {}` (pull
`/v1/device/state`), `session.start {session_id}`, `session.control {session_id,action}`, `prompt {session_id,
scope_id,scope_seq}`, `events {scope_id,scope_seq}`, `log {org_id,seq}`, `wraps {}`, `credentials {owner_id}`,
`request {request_id}`, `ping {org_id,wg_id?,task_id?,level,entry_id}`, `decertified {}`.
Frames from device: `heartbeat {capacity,sessions:[{session_id,state}],vms:[{vm_id,state}]}` every 15 s, and once more 250 ms after a VM state change (capacity, e.g. `pool_ready`, for the start estimate; changes in between coalesce).
A hint carries no content; the runner always fetches the data by signed HTTP. On (re)connect the runner first
reconciles, then pushes its outbox, then pulls all cursors. Reconnect backoff: 1 s doubling to 30 s with ±20 %
jitter, reset after 30 s of stable connection (legacy `cmd_sync` rule). Hub-side: a device is online while
heartbeats arrive (30 s); the hub never queues hints for offline devices (reconcile covers them).

## 7. Guest control protocol (runner host ↔ guest agent)

Transport: HTTP/1.1 over the unix socket `/run/agent-guest/control.sock` in the VM, forwarded by Lima to
`<lima instance dir>/sock/agent-guest.sock` on the host (`portForwards` with `guestSocket`/`hostSocket` in the
golden template). Only the host connects. Header `Authorization: Bearer <control token>` (token passed by the
runner as the Lima parameter `AGENT_GUEST_CONTROL_TOKEN` of `limactl start` before the first boot and stored by
`agent-guest-harden.service` in `/etc/agent-guest/control-token`, ADR-009; compared constant-time).
JSON bodies except tar streams.

### 7.1 Endpoints

| Endpoint | Request → Response |
|---|---|
| `GET /v1/health` | → `{guest_version,claude_version,state:"unassigned"|"assigned"|"broken",org_id?,wg_id?,sessions:[{session_id,state}],landlock_abi,boot_id}` |
| `POST /v1/assign` | `{vm_id,org_id,wg_id}` → `{}`; refused if already assigned to another wg (`already_assigned`) |
| `POST /v1/sessions` | `{session_id,task_id,parent_session_id,depth,harness:{context_markdown},model:{access,model_id,env:{…}},resume_claude_session_id?,restore?:{claude_session_id,object_id,note?},initial_prompt?:{prompt_id,text},limits:{memory_max,cpu_quota,tasks_max,runtime_max_sec}}` → `{state:"sandbox_ready"}`; further transitions via journal. `restore` (ADR-072 §7, not with `resume_claude_session_id`) creates the sandbox from the context staged for this session ID and resumes its conversation (`note`: the sentence about what changed, delivered as the session notice of 05-runner.md §8); without a matching staging `409 state_conflict` |
| `PUT /v1/sessions/{id}/credential` | `{upstream_base_url,auth_header,auth_value,extra_headers:{…},provider_allowlist?:[…]}` → `{}`; memory only, replaced on refresh; a model request that finds no credential is held until the next provisioning (`05-runner.md` §10.2) |
| `POST /v1/sessions/{id}/prompts` | `{prompt_id,text}` → `{state:"registered"}`; delivery result via journal (`session.state` with `prompt_accepted`/`prompt_refused`) |
| `POST /v1/sessions/{id}/control` | `{action:"interrupt"|"stop"|"suspend"}` → `{}` |
| `POST /v1/sessions/{id}/files/import` | `?dir=<relative dir>&replace=true|false`, body `application/x-tar` (regular files and directories only; no links, devices, absolute or `..` paths; ≤ 512 MiB) → `{files,bytes}` |
| `POST /v1/sessions/{id}/files/export` | `{dir,paths?:[…]}` → `application/x-tar` + header `X-Manifest-Sha256`; regular files only, symlinks are reported in the manifest as skipped |
| `POST /v1/sessions/{id}/context/export` | `{workspace_max_bytes,transcript_max_bytes}` → `application/x-tar`: context archive v1 (ADR-072 §2: `context.json`, `transcript/…`, `work/…`); any state with directories; workspace over its limit left out (`omitted_too_large`), transcript over its limit `413 size_exceeded`, no conversation or no complete turn yet (no `user` and `assistant` record, after a bounded wait for a quiet transcript) `409 state_conflict` |
| `POST /v1/contexts/{id}/stage` | body `application/x-tar` (a context archive, ≤ 16 GiB; every transcript `.jsonl` line complete JSON) → `{files,bytes}`; root-only staging for the session `{id}` that `POST /v1/sessions` with `restore` creates; an existing session that is not `suspended` `409 state_conflict` |
| `POST /v1/sessions/{id}/pings` | `{ping_id,kind:"whiteboard.entry"|"subagent.finished"|"operation.finished",level?,ref}` → `{}` |
| `GET /v1/journal` | `?after=<seq>&limit=<n>&wait=<s ≤ 25>` → `{records:[{seq,ts,session_id,kind,body}],head}` (long poll) |
| `POST /v1/journal/ack` | `{through}` → `{}` |
| `POST /v1/pending/{id}/answer` | `{status:"ok"|"error",result?,error?}` → `{}` (answers `operation.request` and `platform.query` records) |
| `GET /v1/metrics` | → `{vm:{cpu_usage_usec,mem_used_bytes,mem_total_bytes,disk_used_bytes,disk_total_bytes,net_rx_bytes,net_tx_bytes,uptime_seconds,load1},sessions:[{session_id,cpu_usage_usec,mem_current_bytes,mem_max_bytes,tasks_current,io_read_bytes,io_write_bytes,workspace_bytes,state}]}` |

### 7.2 Journal record bodies (`kind` → `body`)

| Kind | Body |
|---|---|
| `session.state` | `{state,reason?,claude_session_id?,at}` incl. `prompt_accepted {prompt_id}`, `prompt_refused {prompt_id,reason}` |
| `session.output` | `{message_id,role:"assistant",text,turn,stop_reason?}` (from the transcript) |
| `session.tool` | `{tool_use_id,name,input_summary,input,result_summary,is_error,duration_ms}` (input/result truncated to 64 KiB each) |
| `session.hook` | `{event,tool?,decision:"allow"|"deny"|"block"|"none",reason?,path?}` |
| `session.file` | `{path,op:"write"|"edit"|"delete",sha256,size,patch?}` (patch ≤ 64 KiB); or a summary of many files `{path,op:"summary",files,deleted,bytes,paths,reason:"bulk"|"limit"}` (no patches, `paths` ≤ 20; HOT-06, 05-runner.md §6.4) |
| `session.terminal` | `{offset_ms_start,frames:[[t_ms,"o",data]],cols,rows}` (asciicast v2 output events, ≤ 16 KiB per record, flushed every 100 ms) |
| `session.tools_ready` | `{tools,at}`: the session's agent listed the platform tools (`tools/list` on `/mcp`) for the first time in this run of the agent; the first prompt is delivered only after it (HOT-09, 05-runner.md §6.1 step 6); the runner measures the start step `tools_ready` |
| `usage` | `{request_id,model,provider?,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,thinking_tokens,cost_usd?,context_window_tokens,request_bytes,response_bytes,status}` |
| `operation.request` | `{pending_id,operation_id,type,params,idempotency_key?,requested_at}` |
| `platform.query` | `{pending_id,tool,arguments}` |
| `ping.delivered` | `{ping_id,via:"hook"|"tool"}` |
| `platform.read` | `{tool:"get_notifications",result_count}` (a read the guest answers itself; the runner records event and audit as for a forwarded read, KOR-07) |
| `platform.refused` | `{code,tool,method?}`: an MCP request refused before it was forwarded or answered; `code` ∈ `parse_error`, `invalid_request`, `method_not_found`, `unknown_tool`, `size_exceeded` (`tool` null), `params_invalid` (a tool name or null), `not_found` (`get_subagent_result`), `rate_limited` (a read tool); never arguments (KOR-07); `method` only with `method_not_found`: the refused method by name when it is a request method of the protocol (`server/discover`, `resources/read`, `prompts/get`, `completion/complete`, …), else `other` (HOT-09) |
| `guest.error` | `{code}` |

## 8. Session platform API (MCP)

Endpoint `http://127.0.0.1:7700/mcp` inside the VM; server name `platform`; JSON-RPC 2.0 over HTTP POST with
JSON responses (no SSE). Methods: `initialize` (echo protocol version, default `2025-06-18`; `serverInfo
{name:"agent-dfx-swiss-platform",version}`), `notifications/initialized` (202), `tools/list`, `tools/call`,
`ping`; `resources/list`, `resources/templates/list`, `prompts/list` answer an empty list and `logging/setLevel`
`{}` (HOT-09: every method a client asks while it connects is answered); others → JSON-RPC error `-32601`, among them
the probe `server/discover` of a client of a newer protocol generation, which then connects with `initialize`. The
first successful `tools/list` of a run of the agent marks the tools as arrived (journal `session.tools_ready`). Authentication: `Authorization: Bearer ${PLATFORM_SESSION_TOKEN}`
(constant-time comparison against the stored hash; 401 otherwise). Every call is journaled, except refusals beyond
the per-session limits below; every journaled read is also an audit row (`read.session.<tool>`), also a read refused
before it was forwarded or answered (`rejected`). A read whose arguments the tool refused (`params_invalid`) or that
failed inside the tool is audited but sends no `platform.read` event (its body would carry unchecked arguments);
rights refusals keep their event (KOR-03). Reads (every tool but `request_operation`) are limited per session
by a token bucket in the guest (burst 120, 2 per second); a read over the limit is the tool error `rate_limited`.
Refusals are journaled under a second bucket of the same size per session: the first refusal of a stretch over it is
journaled, the rest of the stretch is not, so a session cannot grow its records faster than the limits. The body
limit of 1 MiB is checked after the authentication, so an oversized request of a session is journaled too
(`size_exceeded`); an unauthenticated request is `401` and journals nothing. Results: `content:[{type:"text",text:<JSON>}]`, `isError` true for errors
with JSON `{"error":<code>,"message":<English sentence>}`.

### 8.1 Tools

All argument objects have `additionalProperties:false`. Integers reject booleans.

| Tool | Arguments | Result |
|---|---|---|
| `get_context` | `{}` | `{org:{id,name},workgroup:{id,name,is_default},task:{id,title,state,owner:{id,display_name}},session:{id,parent_session_id,depth,model_id},rights:{workgroup:"read"|"write"},limits:{subagent_max_depth,subagent_max_parallel,subagent_max_total},api_version:1}` |
| `list_workgroups` | `{}` | `[{id,name,is_default,my_access}]` – names of all org workgroups; `my_access` from the task owner's rights |
| `list_members` | `{scope:"org"|"workgroup"}` | `[{id,kind,display_name,role|access}]` |
| `list_tasks` | `{state?,limit?≤100,before?}` | tasks of the own workgroup `[{id,title,state,owner_id,created_at,last_activity_at}]` |
| `get_task` | `{task_id}` | `{id,title,state,owner_id,created_at,sessions:[{id,state,depth,parent_session_id}],operations:[{id,type,state}]}` (own workgroup only) |
| `get_task_messages` | `{task_id,after?,limit?≤200}` | chat items `[{seq,kind:"prompt"|"output",author_id,ts,text}]` |
| `get_whiteboard` | `{level:"org"|"workgroup"|"task",task_id?}` | `[{entry_id,author_id,created_at,updated_at,text}]` |
| `get_notifications` | `{}` | content-free pending pings `[{ping_id,kind,level?,ref,at}]`; marks them delivered |
| `request_operation` | `{type,params,idempotency_key?:string≤128}` | `{operation_id,status:"succeeded"|"failed"|"rejected"|"duplicate"|"denied"|"pending",result?,error?,duplicate_of?}`; waits up to 20 s for a final state, else `pending` |
| `get_operation` | `{operation_id,wait_seconds?≤30}` | as above |
| `list_operations` | `{limit?≤100}` | operations of the own task |
| `get_subagent_result` | `{session_id,wait_seconds?≤30}` | `{session_id,state,final_text?,finished_at?}` – only for child sessions of the calling session; `final_text` = last assistant message of the child (≤ 64 KiB) once the child is `stopped`; empty when read an hour or more after the child ended (watch window and outbox retention, `05-runner.md` §2.3) |

Read scope (ADR-024): own org (workgroup names, members, org whiteboard) and own workgroup (tasks, messages,
whiteboards, operations). Other workgroups: names only. Rights are evaluated against the task owner at call
time; a task owner who lost write access turns `request_operation` into `denied`.

### 8.2 Hooks contract (guest side; behaviour in `05-runner.md` §5)

`POST http://127.0.0.1:7700/hooks/<Event>` with the Claude Code hook JSON body. Responses:
PreToolUse → `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow"|"deny","permissionDecisionReason":…}}`;
UserPromptSubmit → `{}` (accepted) or `{"decision":"block","reason":"Prompts are accepted only from agent.dfx.swiss."}`;
PreToolUse / PostToolUse / UserPromptSubmit with pending pings (never `Stop`) → additionally
`{"hookSpecificOutput":{"hookEventName":<event>,"additionalContext":"agent.dfx.swiss notice: <n> new item(s) available (<kinds>). Use mcp__platform__get_notifications to see them."}}`;
the first of these answers after a resume or restore carries the session notice once (`additionalContext` starting
with `agent.dfx.swiss session notice: `, the sentence about what changed and the session facts the conversation does
not know; a ping notice follows after a blank line; KOR-33, 05-runner.md §§5.5, 8);
all others → `{}`.

### 8.3 Operation registration (runner → hub)

`POST /v1/device/operations`:
`{operation_id,session_id,task_id,wg_id,type,requested_at,local_check:"passed"|"rejected",error_code?,dedupe:{epoch,tag}|null,params_event}`
→ `{state:"accepted"|"duplicate"|"denied"|"rejected",error_code?,duplicate_of?,original:{state,result_event_id?}?,child_session_id?}`.
Hub checks in this order: H1 session belongs to device and is not stopped; H2 actor (task owner) has `wg:write`
now; H3 type known and enabled; H4 type-specific limits (subagents: depth, parallel, total; budget); H5 duplicate:
`dedupe_slot` (`wg_id`,`epoch`,`tag`) held by an operation in `accepted|executing|succeeded` whose slot has not
expired → `duplicate`; H6 accept: take the slot with `expires_at = now + window`. `local_check:"rejected"` is
stored as `rejected` without H2–H6 (still audited). Every outcome writes `operation` + transition + audit.

## 9. Operation types (formal rules)

General: `params` schema in `contracts/ops/<type>.json`; violations → `params_invalid` with JSON pointers.
`repo` = `owner/name` matching `^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$`; branch names follow
`git check-ref-format --branch` rules (implemented without git: no `..`, no ASCII control chars, no `~^:?*[\`,
no leading `-` or `/`, no trailing `/` or `.lock`, no `@{`, ≤ 200 chars). Workgroup settings (`settings.workgroup`
event): `repositories` allow-list (exact `owner/name`, lowercased; optional: empty, the default, allows every
repository the acting user's GitHub connection reaches, ADR-054), `protected_branches` patterns (default
`["main","master","develop","release/*"]`), `pr_draft_default` (true). Repository-level checks: `repo` in the
allow-list when the list is not empty (`repo_not_allowed`) and accessible with the GitHub connection of the acting
user - the person whose instruction started the session's turn, never the task owner as such (HOT-14, ADR-070,
`05-runner.md` §12 step 3; `github_not_connected`, `repo_not_accessible`). `github.list_repositories` marks every entry `allowed` when the
list is empty. Retry: retryable failures (network, GitHub 5xx, 429, secondary rate limit) are retried
by the runner inside the same operation: 3 attempts, delays 2 s, 8 s, 30 s (respecting `Retry-After`); then
`failed` with `retryable:true`, which releases the dedupe slot.
Idempotency key (KOR-28): the dedupe fields of a type apply whether or not the session passes a key, so a new key
never repeats an operation. The key only marks a repetition as the same request: the same key with the same content
is a duplicate by the fields; for a type with dedupe fields, the same key for other content of the same task and
type, while an earlier operation with that key holds its slot (`accepted`, `executing` or `succeeded`, same epoch,
within the type's window), is rejected by the runner with `params_invalid` (pointer `/idempotency_key`) and
registered as `rejected`. A type without dedupe fields has a slot only with a key (`{idempotency_key, task}`, 24 h);
any repetition with that key is a duplicate. There is no
way to repeat an operation on purpose inside its window; a repetition is possible once the window ended or the
original released its slot (`failed`, `expired`), or with other content (e.g. another comment text).

| Type | Params | Formal checks (runner) | Dedupe fields | Window | Result |
|---|---|---|---|---|---|
| `github.list_repositories` | `{}` | connection present | – | – | `{repositories:[{repo,default_branch,private,allowed}]}` |
| `github.checkout` | `{repo,ref?}` | allow-list (when set); `ref` branch/tag/sha syntax; archive ≤ 512 MiB | repo, ref (resolved sha), session | 10 min | `{repo,commit_sha,dir:"repos/<owner>/<name>",files,bytes}` (`dir` in lower case: GitHub names are case-insensitive); tarball via REST, extracted into the session workspace (regular files only) |
| `github.get_pull_request` | `{repo,number}` | allow-list (when set); number ≥ 1 | – | – | `{number,title,body,state,draft,head:{ref,sha},base:{ref,sha},url,files:[{path,status,additions,deletions,patch?}],comments:[{id,author,body,created_at}]}` (patches ≤ 1 MiB total, flagged `truncated`) |
| `github.create_branch` | `{repo,branch,from}` | allow-list (when set); branch syntax; not protected | repo, branch | 24 h | `{branch,sha}` (409, or 422 "Reference already exists", at GitHub ⇒ `branch_exists`, non-retryable; a retry that finds the branch at the resolved commit succeeds) |
| `github.commit` | `{repo,branch,message,paths?}` | allow-list (when set); branch not protected; message 1–4096 chars; paths relative, no `..`, not under `.git/`; files ≤ 10 MiB each, ≤ 500 files, ≤ 50 MiB total; deterministic secret patterns (PEM private keys, `ghp_`/`gho_`/`ghu_`/`github_pat_`, `sk-ant-`, `sk-or-`, `AKIA[0-9A-Z]{16}`) ⇒ `secret_detected` | repo, branch, parent sha, local tree sha | 24 h | `{commit_sha,parent_sha,tree_sha,files_changed}`; **one** workspace export snapshot of `repos/<owner>/<name>` is taken into a runner scratch directory before registration; parent = current head of `branch` at GitHub at check time, or the checkout commit if the branch does not exist yet; blob and tree SHA-1 are computed locally in git object format (no git binary) from the snapshot and the parent tree listing; the secret scan runs on the snapshot; the executor commits exactly that snapshot via the Git Data API and verifies that GitHub's tree SHA equals the local one (`tree_mismatch` otherwise); a snapshot without a change against the parent (`files_changed` 0) is rejected at the check with `no_changes` (not retryable; registered as locally rejected without a dedupe slot, nothing reaches GitHub); **no ref update** |
| `github.push` | `{repo,branch,commit_sha}` | allow-list (when set); branch not protected; commit created by an earlier `github.commit` of this task; fast-forward only (`non_fast_forward`) | repo, branch, commit sha | 24 h | `{branch,sha,url}` (ref update without force; creates the branch ref if missing) |
| `github.create_pull_request` | `{repo,head,base,title,body?,draft?}` | allow-list (when set); title 1–256; body ≤ 65536; head not protected; `draft` default from settings | repo, head, base | 7 days; additionally, if the executor finds an open PR with the same head/base at GitHub, the operation ends `succeeded` with that PR and `result.existing:true`, and MCP reports `status:"duplicate"` | `{number,url,id,draft}` |
| `github.comment` | `{repo,number,body}` | allow-list (when set); body 1–65536 | repo, number, sha256(body) | 24 h | `{comment_id,url}`; `number` is an issue **or** a pull request (GitHub's issue comment endpoint serves both; KOR-25) |
| `github.list_issues` | `{repo,state?,labels?,sort?,direction?,per_page?,page?}` | allow-list (when set); `state` `open`(default)/`closed`/`all`; `labels` ≤ 20 names (1–100 chars, no comma, no control character; only issues carrying all of them); `sort` `created`(default)/`updated`/`comments`; `direction` `desc`(default, newest first)/`asc`; `per_page` 1–100 (30); `page` 1–1000 (1) | – | – | `{issues:[{number,title,state,state_reason,author,labels,assignees,comments,created_at,updated_at,closed_at,url}],next_page,truncated,note?}`: one page of **issues** - `per_page` and `page` count issues, pull requests (which GitHub lists among the issues) are left out and do not count (HOT-09); the runner reads GitHub's list from its start in pages of 100 until the page is filled and one more issue is seen or the list ends, so a page is empty only when there are no further issues and paging lists every issue once; `next_page` = `page`+1 while another issue follows (`null` on the last page); flood limit 10 GitHub pages per operation: a page not filled by then is answered with what was found, `truncated: true`, `next_page: null` and a `note` on how to go on (labels, state, `direction`); a page cut by the result budget gets a `note` too (smaller `per_page`); entries that break the result contract are left out (they keep their place in the count) and titles cut to 1024 chars (`truncated`) |
| `github.get_issue` | `{repo,number,comments_page?,comments_per_page?}` | allow-list (when set); number ≥ 1; `comments_page` 1–1000 (1); `comments_per_page` 1–100 (100) | – | – | `{number,title,body,state,state_reason,author,labels,assignees,created_at,updated_at,closed_at,url,pull_request,comments_total,comments:[{id,author,body,created_at}],comments_next_page,truncated}`: one page of comments, oldest first; the result stays within 200 KiB of JSON (the newest comments of the page, then the body are cut first, `truncated`); `pull_request:true` when the number is a pull request (read it with `github.get_pull_request`) |
| `github.create_issue` | `{repo,title,body?,labels?}` | allow-list (when set); title 1–256; body ≤ 65536; labels as above (GitHub sets labels only for accounts with push access and drops them silently otherwise) | repo, sha256(title), sha256(body, empty when left out) | 24 h | `{number,url,id}` |
| `github.update_issue` | `{repo,number,state?,state_reason?,title?,body?,labels?}` | allow-list (when set); number ≥ 1; at least one of `state`, `title`, `body`, `labels` (`params_invalid`); `state` `open`/`closed`; `state_reason` only together with a fitting `state` (`reopened` with `open`, `completed`/`not_planned` with `closed`); title 1–256; body ≤ 65536; `labels` replaces every label of the issue (`[]` removes all) | – (setting values is idempotent) | – | `{number,url,state,state_reason,title,labels}` |
| `github.list_pull_requests` | `{repo,state?,base?,sort?,direction?,per_page?,page?}` | allow-list (when set); `state` `open`(default)/`closed`/`all`; `base` branch syntax; `sort` `created`(default)/`updated`; `direction`, `per_page`, `page` as for `github.list_issues` | – | – | `{pull_requests:[{number,title,state,draft,author,head,base,created_at,updated_at,url}],next_page,truncated}` |
| `github.request_connection` | `{}` | none; the acting user's GitHub connection is the subject (held while missing, below) | – | – | `{login}` of the connected account |
| `subagent.request` | `{purpose,prompt,model_id?}` | purpose 1–200, prompt 1–32768 chars; `model_id` from the allowed list of the parent session's access kind (checked by the runner against the model catalog ⇒ `model_not_allowed`) | parent session, sha256(purpose‖prompt) | 10 min | `{session_id}` (hub creates the child session, H4 limits: depth ≤ 2, ≤ 3 running children per task, ≤ 10 children per task) |
| `task.set_title` | `{title}` | 1–120 chars, single line | – | – | `{}` (runner writes a device-authored `task.meta` event) |

GitHub answers of the issue and list types (KOR-25): `401` → `github_not_connected`; `404` → `not_found`; `410`
(issues are disabled in the repository) → `not_found` with the message "Issues are disabled in this repository.";
`403` with the header `x-github-sso: required…` → `upstream_error` with a message that an organisation requires SSO
authorisation of the token (the authorisation URL of the header is never passed on); rate limits as above
(`upstream_error`, `retryable:true`). There is no new error code. Texts from GitHub (titles, bodies, comments,
labels, logins) are untrusted data: the session reads them as data, the web renders them only as text and links
only `https:` URLs.

Held operations (KOR-25, ADR-066, `05-runner.md` §12): when the formal check or the pre-check of a `github.*` type
fails with `github_not_connected` (a repository outside a workgroup's list stays `repo_not_allowed` and is rejected at
once), the runner does not reject the operation but holds it for up to
15 minutes and asks the acting user in the task's chat (`session.status` `waiting` with `for_user`, §10; without an
acting user it is rejected at once and uses nobody's connection, HOT-14); the MCP call answers
`pending` after its 20 s and the result arrives with `operation.finished`. After 15 minutes the operation is rejected
with `github_not_connected` ("The person who gave this instruction did not connect GitHub within 15 minutes."). At most 3 held operations
per session (more are rejected at once) and at most one `waiting` event per session per 60 s. `github.request_connection` lets a session ask for the connection without another type: held
while the acting user is not connected, else `{login}` at once.

Compatibility while the parts are rolled out one by one (KOR-25): a type the hub does not know is `rejected` with
`unknown_type` (H3) and a type a guest does not know is forwarded with `local_check:"rejected"` and `unknown_type`;
neither is ever executed in part. A new type is therefore rolled out hub first, then runners, then goldens: a new
runner beside an old hub would only see its new types refused, an old runner or guest beside a new hub never sends
them. Old runners never write `session.status` `waiting`; their checks reject as before. A `github.connect` with `{token}` sent to an
old runner is not understood there (it runs the device flow or fails without `GITHUB_CLIENT_ID`; the token is never
stored): runners are therefore updated right after the hub. Web tabs still open with the previous bundle do not know
the new `source`/`scopes` fields and show such a connection as unverifiable until they are reloaded.

Result delivery: the runner writes `operation.result` (encrypted) via the outbox, then the
`operation_transition`, then answers the pending MCP call. For `duplicate` the runner returns the original
operation's result (decrypted from its `operation.result` event) with `status:"duplicate"`. If the original is not yet final, the MCP call waits for it within its wait time; otherwise it returns `status:"pending"` with `duplicate_of`, and `get_operation` on the original yields the result later.

## 10. Event bodies (inside the ciphertext)

| Kind | Body |
|---|---|
| `org.profile` | `{name,description?}` |
| `wg.profile` | `{name,description?}` |
| `member.profile` | `{display_name}` |
| `device.profile` | `{label}` |
| `member.name` | `{display_name}`; header `ref` = the user or the invitation the name is for (ADR-073) |
| `task.meta` | `{title,description?}`; header `session=""`, `ref=""` |
| `prompt` | `{text,attachments:[]}` (attachments reserved, empty in v1); header `session=""`, `ref=""` (prompts address the task) |
| `whiteboard.entry` | `{level,text}` (Markdown, ≤ 64 KiB) |
| `whiteboard.edit` | `{text,base_event_id}` |
| `whiteboard.delete` | `{base_event_id}` |
| `settings.workgroup` | `{repositories,protected_branches,pr_draft_default}` (latest wins) |
| `session.output` | journal body of `session.output` |
| `session.tool` / `session.hook` / `session.file` / `session.terminal` / `session.status` | journal bodies (§7.2); `session.status` = `{state,reason,details?}` for errors and states, or `{state:"waiting",waiting_for,operation_id,operation_type,repo?,for_user?,until}` while the runner holds an operation of the session for the person who gave the instruction (§9, KOR-25, HOT-14): `waiting_for` `github_connection` (that person has no usable GitHub connection); `for_user` = that person, the acting user (05-runner.md §12 step 3; a body without it, from an older runner, means the task owner); `repo` is left out for types without one; `until` = receipt + 15 min; only platform-checked values, never text of the session |
| `operation.params` | `{operation_id,type,params,idempotency_key?}` |
| `operation.result` | `{operation_id,status,connection_of?,result?,error?}`; `connection_of` = the user whose own personal connection (GitHub) the executed operation used - the acting user, never anybody else (HOT-14, ADR-070) |
| `platform.read` | `{tool,arguments,result_count}` |

## 11. Error codes (HTTP status)

400: `request_invalid`, `header_invalid`, `params_invalid`, `non_ascii`, `size_exceeded`, `kind_not_allowed`,
`epoch_stale`, `log_*` (see `04-crypto.md` §8), `invite_code_invalid`, `ceremony_invalid`, plus the passkey codes
of `agent_core.passkey.errors` (`client_data_invalid`, `challenge_mismatch`, `origin_mismatch`,
`attestation_invalid`, `authenticator_data_invalid`, `rp_id_mismatch`, `user_presence_missing`,
`user_verification_missing`, `credential_invalid`, `statement_invalid`, `certificate_expiry_invalid`,
`counter_regressed`). 401: `signature_missing`, `key_unknown`, `key_revoked`, `time_skew`, `signature_invalid`,
`nonce_replayed`, `session_expired`, `unauthorized`. 403: `forbidden`, `last_admin`, `last_passkey`,
`device_not_assigned`. 404: `not_found`, `invite_unknown`. 409: `id_conflict`, `event_conflict`, `seq_conflict`,
`version_conflict`, `origin_seq_gap`, `origin_seq_conflict`, `no_device` (no longer answered, ADR-075), `already_assigned`, `rotation_required`, `placement_invalid`. 403 also: `placement_forbidden`.
410: `invite_expired`. 413: `size_exceeded`, `quota_exceeded` (a context checkpoint over its quota, `detail.quota`, ADR-072 §6). 429: `rate_limited`. 503: `unavailable`. Additional: `platform_invite_required` (403), `platform_access_denied` (403), `no_device_available` (409), `invite_registered` (409, invitation already bound to another user), and every code of `04-crypto.md` §8 (e.g. `author_not_writer`, `invite_not_in_log`) with status 400 unless listed above. Delegated agent access (`11-agent-access.md` §4): `pairing_unknown` (404), `pairing_gesture_required` (403), `claim_invalid` (401); pairing from another computer (§5.4 there, ADR-072): `handover_required` (400), `handover_unexpected` (400). Client-only codes (never sent by the hub): `login_automation_failed`, `prf_missing`, `log_rollback`, `log_fork`, `platform_root_mismatch`, `pairing_mismatch`.
All enumerations of this section are closed lists in `contracts/enums/` (`error-codes.json`, `operation-error-codes.json`, `session-failure-reasons.json`); every document and implementation references them.
Operation codes (in operation results): `unknown_type`, `params_invalid`, `repo_not_allowed`,
`repo_not_accessible`, `branch_protected`, `branch_exists`, `non_fast_forward`, `path_invalid`, `secret_detected`,
`size_exceeded`, `github_not_connected`, `not_found`, `limit_exceeded`, `budget_exceeded`, `rights_denied`,
`hub_unreachable`, `upstream_error`, `timeout`, `model_not_allowed`, `credit_exhausted` (`12-billing.md` §7). Session failure reasons (in `session.state_reason`):
`model_access_unavailable`, `vm_start_failed`, `guest_unreachable`, `sandbox_failed`, `agent_start_failed`,
`agent_exited`, `prompt_refused`, `delivery_failed`, `device_offline`, `device_shutdown`, `capacity_exhausted`,
`runtime_exceeded`, `budget_exceeded`, `credit_exhausted` (also a task's `scheduling` reason, `12-billing.md` §7), `oom`, `unassigned`. Additional operation codes: `tree_mismatch`, `no_changes`.

## 12. Limits and timeouts (defaults; configurable only where marked)

| Item | Value |
|---|---|
| Ceremony TTL | 300 s |
| Session certificate | ≤ 12 h (43080 s issued) |
| Invitation expiry | user 72 h, device 24 h, max 30 d (UI-selectable) |
| Device request expiry | 15 min |
| Event ciphertext | ≤ 256 KiB; terminal ≤ 64 KiB |
| SSE heartbeat / WS heartbeat | 15 s / 15 s; device online window 30 s |
| MCP `request_operation` wait | 20 s; `get_operation` ≤ 30 s |
| MCP reads per session (guest) | token bucket: burst 120, 2/s (`rate_limited`); journaled refusals per session: the same bucket |
| MCP request body | ≤ 1 MiB, checked after the authentication (`413 size_exceeded`, journaled) |
| Operation expiry | 10 min in `received` (hub unreachable) ⇒ `expired` (`hub_unreachable`); 30 min in `accepted` without `executing` ⇒ `expired` |
| Guest journal long poll | ≤ 25 s |
| Session idle → suspend | 30 min (config `AGENT_RUNNER_SESSION_IDLE_SECONDS`) |
| VM idle → stop | 30 min (config `AGENT_RUNNER_VM_IDLE_SECONDS`) |
| Subagents | depth ≤ 2, running per task ≤ 3, total per task ≤ 10 (ADR-017) |
| Context checkpoints (ADR-072 §6) | chunk ≤ 2 MiB of compressed plaintext (sealed + 28 B); per checkpoint 8 GiB, kept 3 per task, 200 GiB per organisation (config `AGENT_HUB_CONTEXT_*`); an upload without a new chunk for 6 h is removed (job `contexts.expire`, every 10 min) |
