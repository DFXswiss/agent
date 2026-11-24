// The runner side of an org device enrollment for the web suite (04-crypto.md section 5.8; `agent-runner enroll --hub
// <url> --code <code>` of RUN-06 does the same on a real device): a fresh device identity, the invitation lookup, the
// package opened with the code, the log of the lookup verified against the package's founder, and the `accept` entry
// signed by the device and by `sk_inv`, sent as `POST /v1/device/enroll` signed with the new device key
// (`agent-request-v1`, key ID `device.<32 hex>`). Presence comes from the device double (`setOnline`).
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

/** A request signed by the device key (`body` undefined: a `GET`). */
async function deviceRequest(origin: string, deviceId: string, identity: Identity, target: string, body?: unknown) {
  const keyId = `device.${deviceId.replace(/-/g, '')}`;
  const method = body === undefined ? 'GET' : 'POST';
  const content = utf8(body === undefined ? '' : JSON.stringify(body));
  const time = formatSeconds(Math.floor(Date.now() / 1000));
  const nonce = b64uEncode(random(16));
  const text = requestText(keyId, method, new URL(origin).host, target, time, nonce, await sha256Hex(content));
  const headers: Record<string, string> = {
    'agent-key': keyId,
    'agent-time': time,
    'agent-nonce': nonce,
    'agent-signature': b64uEncode(await identity.sign(text)),
  };
  if (body !== undefined) headers['content-type'] = 'application/json';
  return fetch(new URL(target, origin), { method, headers, ...(body !== undefined && { body: content }) });
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
