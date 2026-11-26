// The runner side of an org device enrollment for the web suite (04-crypto.md section 5.8; `agent-runner enroll --hub
// <url> --code <code>` of RUN-06 does the same on a real device): a fresh device identity, the invitation lookup, the
// package opened with the code, the log of the lookup verified against the package's founder, and the `accept` entry
// signed by the device and by `sk_inv`, sent as `POST /v1/device/enroll` signed with the new device key
// (`agent-request-v1`, key ID `device.<32 hex>`). Presence comes from the device double (`setOnline`). Once enrolled, the
// device announces the current runner's features once, as a runner does on its live channel (`GET /v1/device/ws` with
// the header `Agent-Features`, ADR-060): the hub refuses log entries that an older runner could not verify while any
// active device has not announced them (`peer_outdated`), and the specs run in parallel on one hub.
import { randomBytes } from 'node:crypto';
import { request as httpRequest } from 'node:http';
import { request as httpsRequest } from 'node:https';
import { b64uEncode } from '../../src/core/encoding/b64u';
import { utf8 } from '../../src/core/encoding/bytes';
import { uuid7 } from '../../src/core/encoding/ids';
import { formatSeconds, formatTime } from '../../src/core/encoding/time';
import { inviteSecrets, openPackage, parseCode } from '../../src/core/crypto/invite';
import { Identity } from '../../src/core/crypto/keys';
import { random } from '../../src/core/crypto/subtle';
import { buildEntries } from '../../src/core/log/entries';
import type { PlatformState } from '../../src/core/log/platform';
import { verifyLog } from '../../src/core/log/verify';
import { requestText, sha256Hex } from '../../src/core/net/responseCheck';

export interface EnrolledDevice {
  readonly deviceId: string;
  readonly identity: Identity;
  readonly org: string;
}

const CAPACITY = {
  vm_memory_budget_mib: 65536,
  vm_memory_used_mib: 8192,
  vm_slots_total: 8,
  vm_slots_free: 7,
  pool_ready: 1,
};

/** What the current runner announces (`FEATURES` of src/agent_runner/sync/ws.py). */
export const RUNNER_FEATURES = [
  'log.admit',
  'log.org_assign',
  'log.policy',
  'log.support',
  'vm.choice',
  'github.ways',
  'github.identities',
  'prompt.withdraw',
  'subscriptions',
] as const;

const WS_TARGET = '/v1/device/ws';

/** The `agent-request-v1` headers of a request signed by the device key. */
async function signedHeaders(
  origin: string,
  deviceId: string,
  identity: Identity,
  method: string,
  target: string,
  content: Uint8Array,
): Promise<Record<string, string>> {
  const keyId = `device.${deviceId.replace(/-/g, '')}`;
  const time = formatSeconds(Math.floor(Date.now() / 1000));
  const nonce = b64uEncode(random(16));
  const text = requestText(keyId, method, new URL(origin).host, target, time, nonce, await sha256Hex(content));
  return {
    'agent-key': keyId,
    'agent-time': time,
    'agent-nonce': nonce,
    'agent-signature': b64uEncode(await identity.sign(text)),
  };
}

/** A request signed by the device key (`body` undefined: a `GET`). */
async function deviceRequest(origin: string, deviceId: string, identity: Identity, target: string, body?: unknown) {
  const method = body === undefined ? 'GET' : 'POST';
  const content = utf8(body === undefined ? '' : JSON.stringify(body));
  const headers = await signedHeaders(origin, deviceId, identity, method, target, content);
  if (body !== undefined) headers['content-type'] = 'application/json';
  return fetch(new URL(target, origin), { method, headers, ...(body !== undefined && { body: content }) });
}

/**
 * Opens the device's live channel once with the runner's features (the signed upgrade over an empty body, as the
 * runner sends it) and closes it with a close frame: the hub stores the features with the connection, before it
 * accepts the upgrade, and keeps them after the close; the device is offline again afterwards.
 */
export async function announceFeatures(origin: string, deviceId: string, identity: Identity): Promise<void> {
  const url = new URL(WS_TARGET, origin);
  const headers = {
    ...(await signedHeaders(origin, deviceId, identity, 'GET', WS_TARGET, new Uint8Array(0))),
    connection: 'Upgrade',
    upgrade: 'websocket',
    'sec-websocket-version': '13',
    'sec-websocket-key': randomBytes(16).toString('base64'),
    'agent-features': RUNNER_FEATURES.join(','),
  };
  await new Promise<void>((resolve, reject) => {
    const request = (url.protocol === 'https:' ? httpsRequest : httpRequest)(url, { headers });
    request.on('upgrade', (_response, socket) => {
      socket.on('error', reject);
      socket.on('close', () => resolve());
      socket.resume(); // reads (and drops) the hub's frames: the socket ends only once its data was read
      // a masked close frame with code 1000 (RFC 6455 section 5.5.1); the hub answers it and ends the connection
      const mask = randomBytes(4);
      const code = Buffer.from([0x03, 0xe8]).map((byte, index) => byte ^ mask[index % 4]!);
      socket.end(Buffer.concat([Buffer.from([0x88, 0x80 | code.length]), mask, code]));
    });
    request.on('response', (response) => {
      response.resume();
      reject(new Error(`device ws: ${response.statusCode}`));
    });
    request.on('error', reject);
    request.end();
  });
}

/** Enrolls a new org device with the invitation `code`; `platform` is needed once the org has platform devices. */
export async function enrollOrgDevice(
  origin: string,
  code: string,
  options: { readonly platform?: PlatformState | null; readonly capacity?: typeof CAPACITY } = {},
): Promise<EnrolledDevice> {
  const secrets = await inviteSecrets(parseCode(code));
  const lookup = await fetch(new URL('/v1/invites/lookup', origin), {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ invite_id: secrets.inviteId }),
  });
  if (lookup.status !== 200) throw new Error(`lookup: ${lookup.status} ${await lookup.text()}`);
  const found = (await lookup.json()) as { org_id: string; package_ct: string; log: unknown[] };
  const pkg = (await openPackage(secrets, found.package_ct)) as { founder: string };
  const log = await verifyLog(found.log, pkg.founder, options.platform ?? null);
  const identity = await Identity.generate();
  const deviceId = uuid7();
  const [entry] = await buildEntries(
    { org: found.org_id, head: log.head },
    { id: deviceId, key: identity.sig.privateKey, invite: secrets.signing.privateKey },
    [{ type: 'accept', invite_id: secrets.inviteId, subject: deviceId, kind: 'device', pub: identity.public() }],
    formatTime(Date.now()),
  );
  const body = {
    device_id: deviceId,
    kind: 'org',
    invite_id: secrets.inviteId,
    pub: identity.public(),
    agent_version: '0.1.0',
    os: 'darwin',
    arch: 'arm64',
    capacity: options.capacity ?? CAPACITY,
    entry,
  };
  const answer = await deviceRequest(origin, deviceId, identity, '/v1/device/enroll', body);
  if (answer.status !== 200) throw new Error(`enroll: ${answer.status} ${await answer.text()}`);
  await announceFeatures(origin, deviceId, identity);
  return { deviceId, identity, org: found.org_id };
}

/** The enrollment refusal code for `code` (used, revoked or unknown invitations: `invite_unknown`). */
export async function enrollRefusal(origin: string, code: string): Promise<string> {
  try {
    await enrollOrgDevice(origin, code);
  } catch (error) {
    const match = /"error":"([a-z_]+)"/.exec(String(error));
    return match?.[1] ?? String(error);
  }
  return 'enrolled';
}

/** The HTTP status of the device's `GET /v1/device/state` (200 while it is a device of the platform). */
export async function deviceStateStatus(origin: string, device: EnrolledDevice): Promise<number> {
  return (await deviceRequest(origin, device.deviceId, device.identity, '/v1/device/state')).status;
}
