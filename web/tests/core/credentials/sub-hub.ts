// The subscription part of the hub (03-api.md section 4.8, KOR-69) in memory, behind the credential world's client.
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { parseJson } from '../../../src/core/encoding/json';
import { openCredential } from '../../../src/core/crypto/credential';
import type { Secret } from '../../../src/core/crypto/secret';
import type { Sealed } from '../../../src/core/crypto/signed';
import { HubError } from '../../../src/core/net/signedFetch';
import { type actor, TS } from '../log/world';
import { sealed } from '../github/identity-hub';
import { credWorld, type CredWorld } from './world';

export const A = '0190f000-0000-7000-8000-0000000000c1';
export const B = '0190f000-0000-7000-8000-0000000000c2';
export const C = '0190f000-0000-7000-8000-0000000000c3';
export const ORG = '0190f000-0000-7000-8000-0000000000d1';
export const TOKEN = 'double-subscription-token-not-real';
export const body = (extra: Record<string, unknown> = {}) => ({
  token: TOKEN,
  email: 'team@example.com',
  kind: 'setup_token',
  ...extra,
});
export const reading = (week: number | null, unreadable = false) => ({
  fetched_at: TS,
  device_id: ORG,
  unreadable,
  windows: week === null ? [] : [{ window: 'seven_day', used_percent: week, resets_at: TS, severity: null }, 'x'],
});

export interface Row {
  primary: boolean;
  record: unknown;
  version: number;
  reading: unknown;
  owner?: string;
}

export class SubHub {
  readonly rows = new Map<string, Row>();
  readonly puts: { id: string; body: Record<string, unknown> }[] = [];
  failPut: HubError[] = [];
  failDelete: HubError | null = null;

  constructor(readonly t: CredWorld) {
    const fallback = t.client.request.bind(t.client);
    t.client.request = async (method: string, target: string, options: { body?: unknown } = {}) => {
      const match = /^\/v1\/subscriptions\/([^/]+)$/.exec(target);
      if (match === null) return fallback(method, target, options);
      return this.handle(method, decodeURIComponent(match[1]!), options.body as Record<string, unknown>);
    };
    t.client.on('GET /v1/me/subscriptions', () => ({
      subscriptions: [...this.rows.entries()].map(([id, row]) => ({
        id,
        owner_id: t.me.id,
        primary: row.primary,
        version: row.version,
        epoch: 1,
        created_at: TS,
        updated_at: TS,
        reading: row.reading,
      })),
    }));
  }

  handle(method: string, id: string, body: Record<string, unknown>) {
    const row = this.rows.get(id);
    if (method === 'DELETE') {
      if (this.failDelete !== null) throw this.failDelete;
      if (!this.rows.delete(id)) throw new HubError('not_found', 404);
      return {};
    }
    if (method === 'GET') {
      if (row === undefined) throw new HubError('not_found', 404);
      return { owner_id: row.owner ?? this.t.me.id, primary: row.primary, record: row.record };
    }
    this.puts.push({ id, body });
    const failure = this.failPut.shift();
    if (failure !== undefined) throw failure;
    const header = (body.record as Sealed).header as Record<string, unknown>;
    this.rows.set(id, { primary: false, record: body.record, version: header.version as number, reading: null });
    return { version: header.version };
  }

  async add(
    id: string,
    content: unknown,
    extra: Partial<Row> & { author?: Awaited<ReturnType<typeof actor>>; key?: Secret } = {},
  ) {
    const options: Parameters<typeof sealed>[1] = { id, kind: 'claude_subscription', body: content };
    if (extra.author !== undefined) options.author = extra.author;
    if (extra.key !== undefined) options.key = extra.key;
    const record = await sealed(this.t, options);
    this.rows.set(id, { primary: false, record, version: 1, reading: null, ...extra });
  }

  async opened(id: string): Promise<Record<string, unknown>> {
    const record = this.rows.get(id)!.record;
    const key = b64uDecode(this.t.me.identity.public().sig);
    return parseJson(await openCredential(this.t.credKey, record, key)) as Record<string, unknown>;
  }
}

export async function world() {
  const t = await credWorld();
  return { t, hub: new SubHub(t) };
}
