// Test world of the credential flows (WEB-10): the task world (an org with a verified log, the user's keys and CK
// self-wrap) plus an org device assigned to the default workgroup, and a small credential store that answers like the
// hub (HUB-13: list, get, put with `expected_version` and the stored ID, delete, CK rotation with every record).
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { bodyJson } from '../../../src/core/encoding/json';
import { newHeader, sealCredential } from '../../../src/core/crypto/credential';
import type { Secret } from '../../../src/core/crypto/secret';
import type { Sealed } from '../../../src/core/crypto/signed';
import { signWrap, wrapKey } from '../../../src/core/crypto/wrap';
import { HubError } from '../../../src/core/net/signedFetch';
import { setProfile } from '../../../src/core/store/slices/me';
import { setLog } from '../../../src/core/store/slices/orgs';
import { actor, type Actor, TS } from '../log/world';
import { taskWorld, type TaskWorld, type WorldOptions } from '../tasks/world';

export type CredWorld = TaskWorld & { device: Actor; hub: CredHub };

interface Stored {
  record: Sealed;
  version: number;
  epoch: number;
  updated_at: string;
}

/** The credential part of the hub (HUB-13), in memory. */
export class CredHub {
  readonly records = new Map<string, Stored>();
  readonly wraps: Record<string, unknown>[] = [];
  epoch = 1;
  rotations: { epoch: number; wraps: Record<string, unknown>[]; records: Sealed[] }[] = [];

  constructor(readonly owner: string) {}

  list() {
    return {
      credentials: [...this.records.entries()].map(([kind, item]) => ({
        kind,
        version: item.version,
        epoch: item.epoch,
        updated_at: item.updated_at,
      })),
    };
  }

  put(kind: string, body: { record: Sealed; expected_version: number }) {
    const known = this.records.get(kind);
    const header = body.record.header as Record<string, unknown>;
    if (header.epoch !== this.epoch) throw new HubError('epoch_stale', 400);
    if ((known?.version ?? 0) !== body.expected_version) throw new HubError('version_conflict', 409);
    if (known !== undefined && (known.record.header as Record<string, unknown>).id !== header.id) {
      throw new HubError('request_invalid', 400);
    }
    const version = header.version as number;
    this.records.set(kind, { record: body.record, version, epoch: this.epoch, updated_at: TS });
    return { version };
  }

  rotate(body: { epoch: number; wraps: Record<string, unknown>[]; records: Sealed[] }) {
    if (body.epoch !== this.epoch + 1) throw new HubError('version_conflict', 409);
    if (body.records.length !== this.records.size) throw new HubError('version_conflict', 409);
    for (const record of body.records) {
      const header = record.header as Record<string, unknown>;
      const known = this.records.get(header.kind as string);
      if (known === undefined || header.version !== known.version + 1) throw new HubError('version_conflict', 409);
    }
    this.epoch = body.epoch;
    for (const record of body.records) {
      const header = record.header as Record<string, unknown>;
      this.records.set(header.kind as string, {
        record,
        version: header.version as number,
        epoch: body.epoch,
        updated_at: TS,
      });
    }
    for (const wrap of body.wraps)
      this.wraps.push({ ...wrap, wrapped_by: this.owner, created_at: '2026-11-21T13:00:00.000Z' });
    this.rotations.push(body);
    return {};
  }
}

/** A world whose default workgroup has an org device (`device`) and whose hub keeps credentials. */
export async function credWorld(options: WorldOptions = {}): Promise<CredWorld> {
  const t = await taskWorld(options);
  const device = await actor('cred-device');
  await t.world.admitDevice(options.member ? t.ada : t.me, device, [t.wg]);
  setLog(t.store, t.org, t.world.log!);
  setProfile(t.store, {
    orgs: [{ org_id: t.org, role: 'admin', private: true, default_wg: t.wg }],
    settings: { model_access: 'auto', model_id: null, locale: 'en' },
  });
  const hub = new CredHub(t.me.id);
  hub.wraps.push({ ...t.credWrap, created_at: '2026-11-21T11:00:00.000Z' });
  t.client.on('GET /v1/wraps', () => ({ wraps: hub.wraps }));
  t.client.on('GET /v1/me', () => ({ user_id: t.me.id, orgs: [{ org_id: t.org }] }));
  t.client.on('GET /v1/me/credentials', () => hub.list());
  for (const kind of ['github', 'anthropic_api_key', 'claude_subscription', 'mailbox']) {
    t.client.on(`GET /v1/me/credentials/${kind}`, () => {
      const found = hub.records.get(kind);
      if (found === undefined) throw new HubError('not_found', 404);
      return { record: found.record };
    });
    t.client.on(`PUT /v1/me/credentials/${kind}`, (body) => hub.put(kind, body as never));
    t.client.on(`DELETE /v1/me/credentials/${kind}`, () => {
      if (!hub.records.delete(kind)) throw new HubError('not_found', 404);
      return {};
    });
  }
  t.client.on('POST /v1/me/credentials/rotate', (body) => hub.rotate(body as never));
  return { ...t, device, hub };
}

/** A record of `kind` sealed by `author` under `key` (as a device or another browser would store it). */
export async function storedRecord(
  t: CredWorld,
  kind: string,
  body: unknown,
  options: { author?: Actor; key?: Secret; epoch?: number; version?: number; ts?: string; id?: string } = {},
): Promise<Sealed> {
  const author = options.author ?? t.me;
  const header = newHeader({
    id: options.id ?? `0190f000-0000-7000-8000-00000000${String(kind.length).padStart(4, '0')}`,
    owner: t.me.id,
    kind,
    version: options.version ?? 1,
    epoch: options.epoch ?? 1,
    author: author.id,
    ts: options.ts ?? TS,
  });
  return sealCredential(options.key ?? t.credKey, header, bodyJson(body), author.identity.sig.privateKey);
}

/** Puts a record straight into the hub's store. */
export async function seed(
  t: CredWorld,
  kind: string,
  body: unknown,
  options: Parameters<typeof storedRecord>[3] = {},
) {
  const record = await storedRecord(t, kind, body, options);
  t.hub.records.set(kind, {
    record,
    version: options.version ?? 1,
    epoch: options.epoch ?? 1,
    updated_at: TS,
  });
  return record;
}

/** A self-wrap of the user's CK of `epoch` with the hub's `created_at`. */
export async function selfWrap(t: CredWorld, epoch: number, key: Secret, createdAt: string | null) {
  const slot = { scope: 'cred', scope_id: t.me.id, epoch, recipient: t.me.id };
  const wrap = await wrapKey(b64uDecode(t.me.identity.public().kem), slot, key);
  return {
    scope: 'cred',
    scope_id: t.me.id,
    epoch,
    recipient_id: t.me.id,
    wrap,
    sig: await signWrap(t.me.identity.sig.privateKey, slot, wrap),
    wrapped_by: t.me.id,
    ...(createdAt !== null && { created_at: createdAt }),
  };
}

export const API_KEY = { key: 'sk-ant-not-a-real-key-0001' };
export const MAILBOX = {
  imap_host: 'imap.example.com',
  imap_port: 993,
  imap_user: 'claude@example.com',
  imap_password: 'not a real password',
  email: 'claude@example.com',
};
export const SUBSCRIPTION = { token: 'sk-ant-oat01-not-real', email: 'claude@example.com', kind: 'setup_token' };
export const GITHUB = { token: 'gho_x', login: 'octo-cat', user_id: 1, scopes: [], created_at: TS };
