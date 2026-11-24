// The identity part of the hub (03-api.md section 4.8, ADR-078) in memory behind a test world's fake client, and
// sealed own records (shared by the core and UI tests of GitHub identities).
import { bodyJson } from '../../../src/core/encoding/json';
import { newHeader, sealCredential } from '../../../src/core/crypto/credential';
import type { Secret } from '../../../src/core/crypto/secret';
import type { Sealed } from '../../../src/core/crypto/signed';
import { HubError } from '../../../src/core/net/signedFetch';
import { type Actor, TS } from '../log/world';
import { type CredWorld, credWorld } from '../credentials/world';

export const BODY = {
  token: 'gho_not_real',
  login: 'octo-cat',
  user_id: 42,
  scopes: ['repo'],
  created_at: '2026-11-21T12:00:00.000Z',
};
export const OWN = '0190f000-0000-7000-8000-0000000000e1';
export const SECOND = '0190f000-0000-7000-8000-0000000000e2';
export const COMPANY = '0190f000-0000-7000-8000-0000000000e3';

export interface Stored {
  owner_id: string;
  owner_kind: 'user' | 'org';
  primary: boolean;
  record: unknown;
  version: number;
  epoch: number;
}

/** The identity part of the hub (03-api.md section 4.8), in memory, behind the world's fake client. */
export class IdentityHub {
  readonly records = new Map<string, Stored>();
  defaults: unknown[] = [];
  readonly puts: { id: string; body: Record<string, unknown> }[] = [];
  failPut: HubError[] = [];
  answerVersion: number | null = null;

  constructor(readonly t: CredWorld) {
    const fallback = t.client.request.bind(t.client);
    t.client.request = async (method: string, target: string, options: { body?: unknown } = {}) => {
      const match = /^\/v1\/github-identities\/([^/]+)$/.exec(target);
      if (match === null) return fallback(method, target, options);
      t.client.calls.push({ method, target, body: options.body });
      return this.handle(method, decodeURIComponent(match[1]!), options.body as Record<string, unknown>);
    };
    t.client.on('GET /v1/me/github-identities', () => this.list());
  }

  list() {
    return {
      identities: [...this.records.entries()].map(([id, item]) => ({
        id,
        owner_id: item.owner_id,
        owner_kind: item.owner_kind,
        primary: item.primary,
        version: item.version,
        epoch: item.epoch,
        created_at: TS,
        updated_at: TS,
      })),
      defaults: this.defaults,
    };
  }

  handle(method: string, id: string, body: Record<string, unknown>) {
    const found = this.records.get(id);
    if (method === 'GET') {
      if (found === undefined) throw new HubError('not_found', 404);
      return { owner_id: found.owner_id, owner_kind: found.owner_kind, primary: found.primary, record: found.record };
    }
    this.puts.push({ id, body });
    const failure = this.failPut.shift();
    if (failure !== undefined) throw failure;
    if ((found?.version ?? 0) !== body.expected_version) throw new HubError('version_conflict', 409);
    const header = (body.record as Sealed).header as Record<string, unknown>;
    const org = body.owner_id !== this.t.me.id;
    this.records.set(id, {
      owner_id: body.owner_id as string,
      owner_kind: org ? 'org' : 'user',
      primary: found?.primary ?? false,
      record: body.record,
      version: header.version as number,
      epoch: header.epoch as number,
    });
    return { version: this.answerVersion ?? header.version };
  }
}

export async function sealed(
  t: CredWorld,
  options: {
    id?: string;
    owner?: string;
    author?: Actor;
    epoch?: number;
    version?: number;
    key?: Secret;
    body?: unknown;
    kind?: string;
  } = {},
): Promise<Sealed> {
  const header = newHeader({
    id: options.id ?? OWN,
    owner: options.owner ?? t.me.id,
    kind: options.kind ?? 'github',
    version: options.version ?? 1,
    epoch: options.epoch ?? 1,
    author: (options.author ?? t.me).id,
    ts: TS,
  });
  return sealCredential(
    options.key ?? t.credKey,
    header,
    bodyJson(options.body ?? BODY),
    (options.author ?? t.me).identity.sig.privateKey,
  );
}

export async function world(options: { member?: boolean } = {}) {
  const t = await credWorld(options);
  const hub = new IdentityHub(t);
  return { t, hub };
}

export function own(hub: IdentityHub, id: string, record: unknown, extra: Partial<Stored> = {}) {
  hub.records.set(id, {
    owner_id: hub.t.me.id,
    owner_kind: 'user',
    primary: false,
    record,
    version: 1,
    epoch: 1,
    ...extra,
  });
}

export function company(hub: IdentityHub, id: string, extra: Partial<Stored> = {}) {
  hub.records.set(id, {
    owner_id: hub.t.org,
    owner_kind: 'org',
    primary: false,
    record: { header: {} },
    version: 1,
    epoch: 1,
    ...extra,
  });
}
