// The device-request client (06-web.md sections 8.1 and 8.2, 03-api.md section 4.10, 04-crypto.md section 4.5): a
// user asks one execution device to act for them (GitHub connect, Claude login, credential checks). The hub only
// relays opaque envelopes; everything is checked here.
//
// - `eligibleDevices`: `GET /v1/me/devices` – the devices the hub offers (assigned to a workgroup in which the user is
//   a member), with presence.
// - `deviceKeys`: the device's X25519 and Ed25519 keys come from the **verified** membership logs (for platform devices
//   the keys of their platform certification, kept in the log state), never from the hub: the device must be an
//   active device assigned to a workgroup the user can access (the `cred` readers of the user, 04-crypto.md §5.2a) and
//   its certification must not have ended (`excluded`). Anything else is `device_not_assigned`.
// - `prepareRequest`: a fresh request ID, the header (`follows` = `""` or the original request), the body checked
//   against the kind (`bodies.ts`), sealed with HPKE to the device and signed with the user identity. The prepared
//   request is kept by the caller: a retry posts the identical envelope (the hub answers `replayed`).
// - `submitRequest`: `POST /v1/device-requests {envelope}` (plus `org_id` for a company account's `github.connect`,
//   KOR-47, which the hub checks against the organization's admins).
// - `readRequest`: `GET /v1/device-requests/{id}` – state and responses. Every response must be signed by the device
//   of the request, name this request (`in_reply_to`), this user, device and kind, carry the `seq` it is listed under,
//   open with the user's X25519 key and hold a body that the contracts allow for the kind. A response that fails any
//   of this is kept as `unverifiable` with its code (never shown as content); the flow treats it as no answer.
//
// WEB-10 builds the Claude login and the credential checks on the same functions.
import { b64uDecode } from '../encoding/b64u';
import { uuid7 } from '../encoding/ids';
import { bodyJson, parseJson } from '../encoding/json';
import { formatTime, parseTime } from '../encoding/time';
import { CryptoError } from '../crypto/errors';
import type { Fields } from '../crypto/fields';
import { ENC_ALG, type Envelope, openResponse, sealRequest, verifyResponse } from '../crypto/request';
import { DEVICE } from '../log/state';
import { credReaders } from '../log/wraps';
import { HubError, type SignedClient } from '../net/signedFetch';
import { isRecord } from '../registry';
import type { Store } from '../store/store';
import { checkResponseBody, type RequestKind, type ResponseBody, validRequestBody } from './bodies';

export interface RequestsContext {
  readonly store: Store;
  readonly client: Pick<SignedClient, 'request'>;
  readonly now: () => number;
}

/** A refused request flow step with a catalog code. */
export class RequestError extends Error {
  readonly code: string;

  constructor(code: string) {
    super(code);
    this.name = 'RequestError';
    this.code = code;
  }
}

/** The catalog code of any failure of a request step. */
export function requestErrorCode(error: unknown): string {
  if (error instanceof RequestError || error instanceof HubError || error instanceof CryptoError) return error.code;
  return 'unavailable';
}

function refuse(code: string): never {
  throw new RequestError(code);
}

// --- devices ---

/** An item of `GET /v1/me/devices` (one per device and org). */
export interface EligibleDevice {
  readonly device_id: string;
  readonly kind: string;
  readonly org_id: string;
  readonly wg_ids: readonly string[];
  readonly online: boolean;
  readonly last_seen_at: string | null;
  /** What the device announced on its live channel (KOR-65: the GitHub ways it runs); absent from older hubs. */
  readonly features?: readonly string[];
}

const isEligible = (value: unknown): value is EligibleDevice =>
  isRecord(value) &&
  typeof value.device_id === 'string' &&
  typeof value.kind === 'string' &&
  typeof value.org_id === 'string' &&
  Array.isArray(value.wg_ids) &&
  value.wg_ids.every((wg) => typeof wg === 'string') &&
  typeof value.online === 'boolean' &&
  (value.last_seen_at === null || typeof value.last_seen_at === 'string') &&
  (value.features === undefined ||
    (Array.isArray(value.features) && value.features.every((item) => typeof item === 'string')));

/** `GET /v1/me/devices`; a malformed answer is `unavailable`. */
export async function eligibleDevices(context: RequestsContext): Promise<EligibleDevice[]> {
  const body = await context.client.request('GET', '/v1/me/devices');
  if (!isRecord(body) || !Array.isArray(body.devices) || !body.devices.every(isEligible)) {
    throw new HubError('unavailable', 200);
  }
  return body.devices;
}

export interface DeviceKeys {
  readonly device: string;
  /** b64u X25519 public key (requests are sealed to it). */
  readonly kem: string;
  /** b64u Ed25519 public key (responses are signed with it). */
  readonly sig: string;
}

/** The keys of `device` from the verified logs, or null if the user may not address it (module comment). */
export function deviceKeys(store: Store, device: string): DeviceKeys | null {
  const { userId } = store.getState().me;
  const { orgs } = store.getState();
  if (userId === null) return null;
  const platform = orgs.platform?.state ?? null;
  for (const entry of Object.values(orgs.byId)) {
    const state = entry.log?.state;
    if (state === undefined || entry.blocked !== null) continue;
    const found = state.member(device);
    if (found === null || found.kind !== DEVICE) continue;
    const excluded = platform === null ? [] : platform.excluded(state);
    if (found.platformSeq !== null && platform === null) continue;
    if (credReaders([state], userId, excluded).has(device)) return { device, kem: found.kem, sig: found.sig };
  }
  return null;
}

// --- requests ---

export interface PreparedRequest {
  readonly id: string;
  readonly kind: RequestKind;
  readonly follows: string;
  readonly device: DeviceKeys;
  readonly envelope: Envelope;
  /** The organization of a company account's `github.connect`, posted in plaintext (KOR-47). */
  readonly orgId?: string;
}

export interface RequestInput {
  readonly device: string;
  readonly kind: RequestKind;
  readonly body: unknown;
  /** The original request of a follow-up message (same kind). */
  readonly follows?: PreparedRequest;
  /** `org_id` of `POST /v1/device-requests` (a company account's `github.connect`, KOR-47). */
  readonly orgId?: string;
}

/** Seals a request (module comment); `params_invalid` for a body the kind does not allow. */
export async function prepareRequest(context: RequestsContext, input: RequestInput): Promise<PreparedRequest> {
  const { userId, identity } = context.store.getState().me;
  if (userId === null || identity === null) refuse('unauthorized');
  if (
    input.follows !== undefined &&
    (input.follows.kind !== input.kind || input.follows.device.device !== input.device)
  )
    refuse('request_invalid');
  if (!validRequestBody(input.kind, input.body, input.follows !== undefined)) refuse('params_invalid');
  const device = deviceKeys(context.store, input.device) ?? refuse('device_not_assigned');
  const now = context.now();
  const header = {
    v: 1,
    id: uuid7(now),
    user: userId,
    device: input.device,
    kind: input.kind,
    follows: input.follows?.id ?? '',
    ts: formatTime(now),
    enc_alg: ENC_ALG,
  };
  const envelope = await sealRequest(header, b64uDecode(device.kem), bodyJson(input.body), identity.sig.privateKey);
  const prepared = { id: header.id, kind: input.kind, follows: header.follows, device, envelope };
  return input.orgId === undefined ? prepared : { ...prepared, orgId: input.orgId };
}

/** `POST /v1/device-requests` (with `org_id` when the request names one); returns the request state. */
export async function submitRequest(context: RequestsContext, prepared: PreparedRequest): Promise<string> {
  const body = {
    envelope: prepared.envelope,
    ...(prepared.orgId !== undefined && { org_id: prepared.orgId }),
  };
  const answer = await context.client.request('POST', '/v1/device-requests', { body });
  if (!isRecord(answer) || answer.request_id !== prepared.id || typeof answer.state !== 'string') {
    throw new HubError('unavailable', 200);
  }
  return answer.state;
}

export type OpenedResponse =
  | {
      readonly seq: number;
      readonly status: 'verified';
      readonly final: boolean;
      /** The device's time of the response (`header.ts`, Unix ms). */
      readonly at: number;
      readonly body: ResponseBody;
    }
  | { readonly seq: number; readonly status: 'unverifiable'; readonly code: string };

export interface RequestView {
  readonly id: string;
  /** `pending`, `delivered`, `answered`, `failed` or `expired` (hub state). */
  readonly state: string;
  /** Unix ms of the hub's expiry of the request. */
  readonly expiresAt: number;
  /** Ascending by `seq`. */
  readonly responses: readonly OpenedResponse[];
}

function checkResponseHeader(header: Fields, prepared: PreparedRequest, userId: string, seq: number): void {
  const expected = {
    in_reply_to: prepared.id,
    user: userId,
    device: prepared.device.device,
    kind: prepared.kind,
    seq,
  };
  for (const [field, value] of Object.entries(expected)) if (header[field] !== value) refuse('header_invalid');
}

async function openOne(context: RequestsContext, prepared: PreparedRequest, item: unknown): Promise<OpenedResponse> {
  const seq = isRecord(item) && Number.isSafeInteger(item.seq) ? (item.seq as number) : 0;
  const { userId, identity } = context.store.getState().me;
  try {
    if (seq < 1 || userId === null || identity === null) refuse('header_invalid');
    const envelope = (item as Record<string, unknown>).envelope;
    const sig = b64uDecode(prepared.device.sig);
    const header = await verifyResponse(envelope, sig);
    checkResponseHeader(header, prepared, userId, seq);
    const plaintext = await openResponse(identity.kem, envelope, sig);
    const body = checkResponseBody(prepared.kind, parseJson(plaintext)) ?? refuse('params_invalid');
    return { seq, status: 'verified', final: header.final === true, at: parseTime(header.ts), body };
  } catch (error) {
    return { seq, status: 'unverifiable', code: requestErrorCode(error) };
  }
}

/** `GET /v1/device-requests/{id}` with every response checked and opened (module comment). */
export async function readRequest(context: RequestsContext, prepared: PreparedRequest): Promise<RequestView> {
  const view = await context.client.request('GET', `/v1/device-requests/${encodeURIComponent(prepared.id)}`);
  if (
    !isRecord(view) ||
    view.request_id !== prepared.id ||
    view.kind !== prepared.kind ||
    view.device_id !== prepared.device.device ||
    typeof view.state !== 'string' ||
    !Array.isArray(view.responses)
  ) {
    throw new HubError('unavailable', 200);
  }
  let expiresAt: number;
  try {
    expiresAt = parseTime(view.expires_at);
  } catch {
    throw new HubError('unavailable', 200);
  }
  const responses = await Promise.all(view.responses.map((item) => openOne(context, prepared, item)));
  return { id: prepared.id, state: view.state, expiresAt, responses: responses.sort((a, b) => a.seq - b.seq) };
}

/** The newest verified response body, and whether it ended the flow. */
export function latestResponse(view: RequestView): Extract<OpenedResponse, { status: 'verified' }> | null {
  const verified = view.responses.filter((item) => item.status === 'verified');
  return verified.at(-1) ?? null;
}
