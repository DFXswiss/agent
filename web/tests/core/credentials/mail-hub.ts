// The mailbox part of the hub (03-api.md section 4.8, KOR-74, ADR-099) in memory, behind the credential world's client,
// with the records, forms and list items the core and UI tests of mailboxes share.
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { parseJson } from '../../../src/core/encoding/json';
import { openCredential } from '../../../src/core/crypto/credential';
import type { Secret } from '../../../src/core/crypto/secret';
import type { Sealed } from '../../../src/core/crypto/signed';
import type { Mailbox, MailboxForm } from '../../../src/core/credentials/mailboxes';
import { HubError } from '../../../src/core/net/signedFetch';
import type { Actor } from '../log/world';
import { sealed } from '../github/identity-hub';
import { credWorld, type CredWorld, MAILBOX } from './world';

export const A = '0190f000-0000-7000-8000-0000000000a1';
export const B = '0190f000-0000-7000-8000-0000000000a2';
export const C = '0190f000-0000-7000-8000-0000000000a3';
export const D = '0190f000-0000-7000-8000-0000000000a4';
export const WG = '0190f000-0000-7000-8000-0000000000b1';
export const WG2 = '0190f000-0000-7000-8000-0000000000b2';
export const PASSWORD = MAILBOX.imap_password;

export interface Row {
  primary: boolean;
  record: unknown;
  version: number;
  use: { wgs: string[] } | null;
  owner?: string;
}

/** The mailbox part of the hub (03-api.md section 4.8, ADR-099) in memory, behind the credential world's client. */
export class MailHub {
  readonly rows = new Map<string, Row>();
  readonly puts: { id: string; body: Record<string, unknown> }[] = [];
  failPut: HubError[] = [];
  failDelete: HubError | null = null;
  failGet: HubError | null = null;

  constructor(readonly t: CredWorld) {
    const fallback = t.client.request.bind(t.client);
    t.client.request = async (method: string, target: string, options: { body?: unknown } = {}) => {
      const match = /^\/v1\/mailboxes\/([^/]+)$/.exec(target);
      if (match === null) return fallback(method, target, options);
      return this.handle(method, decodeURIComponent(match[1]!), options.body as Record<string, unknown>);
    };
    t.client.on('GET /v1/me/mailboxes', () => ({
      mailboxes: [...this.rows.entries()].map(([id, row]) => ({
        id,
        owner_id: t.me.id,
        primary: row.primary,
        version: row.version,
        epoch: 1,
        created_at: '2026-11-21T12:00:00.000Z',
        updated_at: '2026-11-21T12:00:00.000Z',
        use: row.use,
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
      if (this.failGet !== null) throw this.failGet;
      if (row === undefined) throw new HubError('not_found', 404);
      return { owner_id: row.owner ?? this.t.me.id, primary: row.primary, record: row.record };
    }
    this.puts.push({ id, body });
    const failure = this.failPut.shift();
    if (failure !== undefined) throw failure;
    if ((row?.version ?? 0) !== body.expected_version) throw new HubError('version_conflict', 409);
    const header = (body.record as Sealed).header as Record<string, unknown>;
    const use = body.use as { wgs: string[] };
    this.rows.set(id, {
      primary: row?.primary ?? this.rows.size === 0,
      record: body.record,
      version: header.version as number,
      use: use.wgs.length === 0 ? null : use,
    });
    return { version: header.version };
  }

  async add(id: string, content: unknown, extra: Partial<Row> & { author?: Actor; key?: Secret; epoch?: number } = {}) {
    const { author, key, epoch, ...row } = extra;
    const options: Parameters<typeof sealed>[1] = { id, kind: 'mailbox', body: content };
    if (author !== undefined) options.author = author;
    if (key !== undefined) options.key = key;
    if (epoch !== undefined) options.epoch = epoch;
    this.rows.set(id, { primary: false, record: await sealed(this.t, options), version: 1, use: null, ...row });
  }

  async opened(id: string): Promise<Record<string, unknown>> {
    const record = this.rows.get(id)!.record;
    const key = b64uDecode(this.t.me.identity.public().sig);
    return parseJson(await openCredential(this.t.credKey, record, key)) as Record<string, unknown>;
  }
}

export async function world() {
  const t = await credWorld();
  return { t, hub: new MailHub(t) };
}

export const body = (extra: Record<string, unknown> = {}) => ({ ...MAILBOX, ...extra });
export const form = (extra: Partial<MailboxForm> = {}): MailboxForm => ({
  name: 'signin',
  purpose: 'sign-in mails',
  email: 'claude@example.com',
  imap_host: 'imap.example.com',
  imap_port: '993',
  imap_user: 'claude@example.com',
  imap_password: PASSWORD,
  ...extra,
});
export const item = (id: string, extra: Partial<Mailbox> = {}): Mailbox => ({
  id,
  primary: false,
  status: 'verified',
  code: null,
  name: null,
  purpose: null,
  email: null,
  host: null,
  port: null,
  user: null,
  wgs: [],
  ...extra,
});
