// The GitHub connection of the signed-in user (06-web.md section 8.1, 03-api.md sections 4.8 and 4.10, ADR-015).
//
// - `loadGithub`: `GET /v1/me/credentials` lists the user's credential kinds; with a `github` record, the record
//   (`GET /v1/me/credentials/github`) is verified and opened in the browser: its owner is the user, its author the
//   user or a device entitled to the user's `CK` by the verified logs (the device that ran the device flow,
//   04-crypto.md section 4.4), its signature checks with that author's key, and it opens with the user's `CK` of the
//   record's epoch (the user's own self-wrap). Only the GitHub login, the scopes, the source (device flow or a stored
//   token; records without it came from the device flow), the creation time and whether the token is one the platform
//   minted (`revocable`: a token of a device flow - the GitHub App's `ghu_` or the OAuth App's `gho_`, KOR-65 - which
//   the devices revoke at GitHub on disconnect; KOR-40)
//   leave this module – the token is never kept or shown. The result goes to the `github` slice (settings section,
//   composer chip).
// - `disconnectGithub`: `DELETE /v1/me/credentials/github` (a missing record, `404`, counts as done); a revocable token
//   is revoked at GitHub by the devices; any other (the operator's account, a transferred token) stays valid there and
//   the UI says so and links the settings page.
// - `connectDevices`: the devices a `github.connect` request may go to. If `GET /v1/me/devices` is empty (a fresh
//   private org), the device auto-assignment of the first task (`ensureDevice`, 03-api.md section 4.6.1 step 1) runs
//   for the default workgroup of the active org first. Devices the verified logs do not confirm are left out; online
//   devices of the default workgroup come first and the first online one is preselected. Each choice says whether the
//   device runs a GitHub sign-in (`deviceFlow`, KOR-65: it announced `github.device_flow` or `github.operator`, or it
//   is an older runner that announces no GitHub ways at all).
// - `githubWays`: whether any eligible device runs a GitHub sign-in (null while unknown: no device yet) – "Connect
//   GitHub" is offered only then; otherwise only the existing token (no dead button, rule E-05).
// - `startConnect`: the sealed `github.connect` request (`core/requests`); with a token (the second way of KOR-25,
//   ADR-065) the body is `{token}`, sealed with HPKE to the device that checks it – the token is not kept anywhere
//   else in the browser. `connectProgress` reads the flow's state from the opened responses: waiting → device code
//   (user code, verification link, expiry from the response's time and `expires_in`) → done (login, scopes, source)
//   or failed (code), or expired when the hub ended the request without an answer. With `ConnectOptions` (KOR-47,
//   ADR-078) the request names the record to reconnect, the organization of a company account (`org_id`, also in the
//   plaintext of `POST /v1/device-requests` for the hub's admin check) and asks for a signing key; `done` then names
//   the identity and the signing outcome (the body of a company account is handled by `github/identities.ts`).
// - `tokenInput`: the pasted token trimmed (a copied line ends in a newline), or null when its form cannot be one.
import { b64uDecode } from '../encoding/b64u';
import { parseJson } from '../encoding/json';
import { parseTime } from '../encoding/time';
import { openCredential, verifyCredential } from '../crypto/credential';
import type { Fields } from '../crypto/fields';
import type { Identity } from '../crypto/keys';
import type { Secret } from '../crypto/secret';
import { unwrapKey, verifyWrap } from '../crypto/wrap';
import { CRED } from '../log/wraps';
import { HubError } from '../net/signedFetch';
import { isRecord } from '../registry';
import { setGithub } from '../store/slices/github';
import type { Store } from '../store/store';
import { ensureDevice } from '../tasks/send';
import type { TasksContext } from '../tasks/pull';
import { type App, isGithubBody, REQUEST_FAILED, type SigningOutcome, type Source, TOKEN_RE } from '../requests/bodies';
import {
  deviceKeys,
  type EligibleDevice,
  eligibleDevices,
  prepareRequest,
  type PreparedRequest,
  RequestError,
  type RequestsContext,
  type RequestView,
  submitRequest,
} from '../requests/client';

export const KIND = 'github';
/** Tokens the platform mints through a device flow (GitHub App user tokens, OAuth App tokens; KOR-65): the devices
 *  revoke them on disconnect. A token stored by its owner (`source: "token"`) is never revoked. */
export const REVOCABLE_PREFIXES = ['ghu_', 'gho_'] as const;
/** A classic token reaching every repository of the account, pre-filled at GitHub (scopes `repo`, `workflow`). */
export const TOKEN_URL = 'https://github.com/settings/tokens/new?scopes=repo,workflow&description=agent.dfx.swiss';
/** Feature tokens of `GET /v1/me/devices` (KOR-65). */
export const WAYS_FEATURE = 'github.ways';
export const SIGN_IN_FEATURES: readonly string[] = ['github.device_flow', 'github.operator'];
/** The device runs the OAuth App's sign-in, whose token reaches every repository of the account. */
export const OAUTH_FEATURE = 'github.oauth_app';
/** Where the user revokes the app authorization (06-web.md section 8.1). */
export const GITHUB_SETTINGS_URL = 'https://github.com/settings/applications';

export type GithubConnection =
  | {
      readonly status: 'connected';
      readonly login: string;
      readonly scopes: readonly string[];
      readonly source: Source;
      readonly createdAt: string;
      readonly version: number;
      readonly revocable: boolean;
    }
  | { readonly status: 'disconnected' }
  | { readonly status: 'unverifiable'; readonly code: string };

function refuse(code: string): never {
  throw new RequestError(code);
}

/** The user's `CK` of `epoch` from the own self-wrap (signature checked with the own identity), or null. */
export async function credentialKey(
  context: RequestsContext,
  userId: string,
  identity: Identity,
  epoch: number,
): Promise<Secret | null> {
  const query = `/v1/wraps?scope=cred&scope_id=${encodeURIComponent(userId)}&epoch=${epoch}`;
  const body = await context.client.request('GET', query);
  const wraps = isRecord(body) && Array.isArray(body.wraps) ? body.wraps.filter(isRecord) : [];
  const slot = { scope: CRED, scope_id: userId, epoch, recipient: userId };
  for (const found of wraps) {
    const own =
      found.scope === CRED &&
      found.scope_id === userId &&
      found.epoch === epoch &&
      found.recipient_id === userId &&
      found.wrapped_by === userId &&
      typeof found.wrap === 'string';
    if (!own) continue;
    try {
      await verifyWrap(identity.sigPublic(), slot, found.wrap as string, found.sig);
      return await unwrapKey(identity.kem, slot, found.wrap as string);
    } catch {
      // the next one may open
    }
  }
  return null;
}

/** The signing key of a record's author: the user, or a device the verified logs entitle to the user's `CK`. */
export function authorKey(store: Store, userId: string, identity: Identity, author: unknown): Uint8Array | null {
  if (author === userId) return identity.sigPublic();
  const device = typeof author === 'string' ? deviceKeys(store, author) : null;
  return device === null ? null : b64uDecode(device.sig);
}

/** The body exactly as `contracts/credentials/github.json` allows it (`requests/bodies.ts`). */
export { isGithubBody };

/** Verifies and opens a `github` record (module comment). */
export async function openGithubRecord(context: RequestsContext, record: unknown): Promise<GithubConnection> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  const header = isRecord(record) && isRecord(record.header) ? (record.header as Fields) : {};
  if (header.owner !== userId || header.kind !== KIND) refuse('header_invalid');
  const key = authorKey(context.store, userId, identity, header.author) ?? refuse('author_not_writer');
  const checked = await verifyCredential(record, key);
  const ck = (await credentialKey(context, userId, identity, checked.epoch as number)) ?? refuse('epoch_stale');
  const body = parseJson(await openCredential(ck, record, key));
  if (!isGithubBody(body)) refuse('params_invalid');
  parseTime(body.created_at);
  return {
    status: 'connected',
    login: body.login,
    scopes: [...body.scopes],
    source: body.source ?? 'device_flow',
    createdAt: body.created_at,
    version: checked.version as number,
    revocable: isRevocable(body),
  };
}

/** A token of a device flow the devices revoke at GitHub on disconnect (module comment; also for `github/identities.ts`). */
export function isRevocable(body: { readonly token: string; readonly source?: Source }): boolean {
  return (
    (body.source ?? 'device_flow') === 'device_flow' &&
    REVOCABLE_PREFIXES.some((prefix) => body.token.startsWith(prefix))
  );
}

const listed = (body: unknown): boolean =>
  isRecord(body) &&
  Array.isArray(body.credentials) &&
  body.credentials.some((item) => isRecord(item) && item.kind === KIND);

/** Loads, checks and stores the connection state (module comment); unverifiable records keep their code. */
export async function loadGithub(context: RequestsContext): Promise<GithubConnection> {
  const generation = context.store.generation;
  let connection: GithubConnection;
  const list = await context.client.request('GET', '/v1/me/credentials');
  if (!listed(list)) {
    connection = { status: 'disconnected' };
  } else {
    try {
      const answer = await context.client.request('GET', `/v1/me/credentials/${KIND}`);
      connection = await openGithubRecord(context, isRecord(answer) ? answer.record : null);
    } catch (error) {
      if (error instanceof HubError && error.code === 'not_found') connection = { status: 'disconnected' };
      else if (error instanceof HubError) throw error;
      else connection = { status: 'unverifiable', code: (error as { code?: string }).code ?? 'unavailable' };
    }
  }
  if (context.store.generation === generation) setGithub(context.store, connection);
  return connection;
}

/** `DELETE /v1/me/credentials/github` (`404` counts as done). */
export async function disconnectGithub(context: RequestsContext): Promise<void> {
  const generation = context.store.generation;
  try {
    await context.client.request('DELETE', `/v1/me/credentials/${KIND}`);
  } catch (error) {
    if (!(error instanceof HubError) || error.code !== 'not_found') throw error;
  }
  if (context.store.generation === generation) setGithub(context.store, { status: 'disconnected' });
}

// --- connect ---

export interface DeviceChoice {
  readonly device: string;
  readonly kind: string;
  readonly online: boolean;
  /** Assigned to the default workgroup of the active org. */
  readonly defaultWg: boolean;
  /** The device runs a GitHub sign-in (module comment). */
  readonly deviceFlow: boolean;
  /** The device runs the OAuth App's sign-in (`github.oauth_app`, KOR-65). */
  readonly oauthApp: boolean;
}

/** Whether a device item announces a GitHub sign-in, or announces no GitHub ways at all (an older runner). */
export function offersSignIn(item: EligibleDevice): boolean {
  const features = item.features ?? [];
  return !features.includes(WAYS_FEATURE) || features.some((feature) => SIGN_IN_FEATURES.includes(feature));
}

/** Whether any eligible device runs a GitHub sign-in; null while no device is known (module comment). */
export async function githubWays(context: RequestsContext): Promise<boolean | null> {
  const listed = await eligibleDevices(context);
  return listed.length === 0 ? null : listed.some(offersSignIn);
}

/** The active org and its default workgroup (from `GET /v1/me`). */
export function defaultWorkgroup(store: Store): { readonly org: string; readonly wg: string } | null {
  const { session, me } = store.getState();
  const orgs = isRecord(me.profile) && Array.isArray(me.profile.orgs) ? me.profile.orgs.filter(isRecord) : [];
  const found = orgs.find((org) => org.org_id === session.activeOrg) ?? orgs[0];
  return found !== undefined && typeof found.org_id === 'string' && typeof found.default_wg === 'string'
    ? { org: found.org_id, wg: found.default_wg }
    : null;
}

/**
 * The devices for a request and the preselected one (module comment); with `org` (a company account, KOR-47) only the
 * devices of that organization, without the automatic assignment.
 */
export async function connectDevices(
  context: TasksContext,
  org?: string,
): Promise<{ readonly devices: readonly DeviceChoice[]; readonly preselected: string | null }> {
  let listedDevices = (await eligibleDevices(context)).filter((item) => org === undefined || item.org_id === org);
  const home = defaultWorkgroup(context.store);
  if (listedDevices.length === 0 && home !== null && org === undefined) {
    await ensureDevice(context, home.org, home.wg);
    listedDevices = await eligibleDevices(context);
  }
  const byDevice = new Map<string, DeviceChoice>();
  for (const item of listedDevices) {
    if (deviceKeys(context.store, item.device_id) === null) continue;
    const known = byDevice.get(item.device_id);
    const inDefault = home !== null && item.org_id === home.org && item.wg_ids.includes(home.wg);
    byDevice.set(item.device_id, {
      device: item.device_id,
      kind: item.kind,
      online: item.online || (known?.online ?? false),
      defaultWg: inDefault || (known?.defaultWg ?? false),
      deviceFlow: offersSignIn(item) || (known?.deviceFlow ?? false),
      oauthApp: (item.features ?? []).includes(OAUTH_FEATURE) || (known?.oauthApp ?? false),
    });
  }
  const devices = [...byDevice.values()].sort(
    (a, b) =>
      Number(b.online) - Number(a.online) ||
      Number(b.defaultWg) - Number(a.defaultWg) ||
      Number(a.device > b.device) - Number(a.device < b.device),
  );
  return { devices, preselected: devices.find((item) => item.online)?.device ?? null };
}

/** The pasted token without surrounding white space, or null when it cannot be a token (module comment). */
export function tokenInput(text: string): string | null {
  const token = text.trim();
  return TOKEN_RE.test(token) ? token : null;
}

/** What a `github.connect` request asks for besides the token (KOR-47, `contracts/requests/github.connect.request.json`). */
export interface ConnectOptions {
  /** The device flow of this app (`app`, KOR-65); never together with a token. */
  readonly app?: App | undefined;
  /** Reconnect into this own identity record (same ID, version + 1). */
  readonly identityId?: string;
  /** A company account of this organization (admins; the answer carries the body, `github/identities.ts`). */
  readonly orgId?: string;
  /** Also create and register an own signing key. */
  readonly signingKey?: boolean;
}

/**
 * Seals and posts the `github.connect` request to `device` (with `token`: store that token, module comment); `options`
 * add the device flow's app (KOR-65), the identity to reconnect, the organization of a company account (also in
 * plaintext for the hub) and the signing key.
 */
export async function startConnect(
  context: RequestsContext,
  device: string,
  token?: string,
  options: ConnectOptions = {},
): Promise<PreparedRequest> {
  const body = {
    ...(token !== undefined && { token }),
    ...(token === undefined && options.app !== undefined && { app: options.app }),
    ...(options.orgId !== undefined && { org_id: options.orgId }),
    ...(options.identityId !== undefined && { identity_id: options.identityId }),
    ...(options.signingKey === true && { signing_key: true }),
  };
  const prepared = await prepareRequest(context, {
    device,
    kind: 'github.connect',
    body,
    ...(options.orgId !== undefined && { orgId: options.orgId }),
  });
  await submitRequest(context, prepared);
  return prepared;
}

export type ConnectProgress =
  | { readonly step: 'waiting' }
  | {
      readonly step: 'device_code';
      readonly userCode: string;
      readonly verificationUri: string;
      /** Unix ms at which the code expires (device time of the response + `expires_in`). */
      readonly expiresAt: number;
    }
  | {
      readonly step: 'done';
      readonly login: string;
      readonly scopes: readonly string[] | null;
      readonly source: Source | null;
      /** The record ID of the identity (KOR-47), null when the device named none. */
      readonly identityId: string | null;
      /** The outcome of `signing_key`, null when none was asked for. */
      readonly signing: SigningOutcome | null;
    }
  | { readonly step: 'failed'; readonly code: string };

/** The state of a connect flow from its request view (module comment). */
export function connectProgress(view: RequestView): ConnectProgress {
  const verified = view.responses.filter((item) => item.status === 'verified');
  const last = verified.at(-1);
  const body = last?.body;
  if (body?.step === 'done')
    return {
      step: 'done',
      login: body.login!,
      scopes: body.scopes ?? null,
      source: body.source ?? null,
      identityId: body.identity_id ?? null,
      signing: body.signing ?? null,
    };
  if (body?.step === 'failed') return { step: 'failed', code: body.code };
  if (view.state === 'expired') return { step: 'failed', code: 'timeout' };
  // the hub fails a request whose device lost its eligibility or that a newer one of the same kind replaced
  if (view.state === 'failed') return { step: 'failed', code: REQUEST_FAILED };
  if (body?.step === 'device_code')
    return {
      step: 'device_code',
      userCode: body.user_code,
      verificationUri: body.verification_uri,
      expiresAt: last!.at + body.expires_in * 1000,
    };
  return { step: 'waiting' };
}
