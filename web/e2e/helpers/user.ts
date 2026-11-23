// The signed-in user of a page, acting through the API where the UI of later packages is missing (workgroups:
// WEB-06) or where a test needs many objects (paging). The page signs every request with its own session key
// (non-extractable, from IndexedDB, agent-request-v1 exactly like `core/net`); Node holds the identity, opened from
// the recovery slot with the recovery code the onboarding showed, and builds log entries, wraps and events with the
// web core. Answers are not response-checked (the app does that; this is the test's own side).
import type { Page } from '@playwright/test';
import { bodyJson } from '../../src/core/encoding/json';
import { uuid7 } from '../../src/core/encoding/ids';
import { formatTime } from '../../src/core/encoding/time';
import { unlockWithRecoveryCode } from '../../src/core/account/login';
import { newHeader, sealEvent } from '../../src/core/crypto/event';
import type { Identity } from '../../src/core/crypto/keys';
import type { Secret } from '../../src/core/crypto/secret';
import { unwrapKey } from '../../src/core/crypto/wrap';
import { buildBatch } from '../../src/core/log/rotation';
import { DEVICE, epochKey, ORG, WG } from '../../src/core/log/state';
import type { LogFields } from '../../src/core/log/entries';
import { type PlatformState, verifyPlatformLog } from '../../src/core/log/platform';
import { verifyLog, type VerifiedLog } from '../../src/core/log/verify';
import { compiledRootKey } from '../../build/manifest.ts';
import { deriveTitle } from '../../src/core/tasks/title';

interface PageAnswer {
  readonly status: number;
  readonly text: string;
}

/** One signed request from inside the page (the session key never leaves it). */
function signedInPage(page: Page, method: string, target: string, body: string | null): Promise<PageAnswer> {
  return page.evaluate(
    async ({ method, target, body }) => {
      const db = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open('agent-platform');
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      const session = await new Promise<{ keyId: string; privateKey: CryptoKey }>((resolve, reject) => {
        const request = db.transaction('session').objectStore('session').get('current');
        request.onsuccess = () => resolve(request.result as { keyId: string; privateKey: CryptoKey });
        request.onerror = () => reject(request.error);
      });
      db.close();
      const b64u = (bytes: Uint8Array) =>
        btoa(String.fromCharCode(...bytes))
          .replace(/\+/g, '-')
          .replace(/\//g, '_')
          .replace(/=+$/, '');
      const raw = new TextEncoder().encode(body ?? '');
      const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', raw));
      const hash = [...digest].map((byte) => byte.toString(16).padStart(2, '0')).join('');
      const time = new Date().toISOString().slice(0, 19) + 'Z';
      const nonce = b64u(crypto.getRandomValues(new Uint8Array(16)));
      const text = ['agent-request-v1', session.keyId, method, location.host, target, time, nonce, hash].join('\n');
      const signature = new Uint8Array(
        await crypto.subtle.sign(
          { name: 'ECDSA', hash: 'SHA-256' },
          session.privateKey,
          new TextEncoder().encode(text),
        ),
      );
      const headers: Record<string, string> = {
        'agent-key': session.keyId,
        'agent-time': time,
        'agent-nonce': nonce,
        'agent-signature': b64u(signature),
      };
      if (body !== null) headers['content-type'] = 'application/json';
      const response = await fetch(target, { method, headers, ...(body !== null && { body: raw }) });
      return { status: response.status, text: await response.text() };
    },
    { method, target, body },
  );
}

export class PageUser {
  private constructor(
    readonly page: Page,
    readonly userId: string,
    readonly identity: Identity,
  ) {}

  /** The page's user with the identity from the recovery slot. */
  static async open(page: Page, recoveryCode: string): Promise<PageUser> {
    const probe = new PageUserRequests(page);
    const me = (await probe.request('GET', '/v1/me')) as { user_id: string };
    const bundle = (await probe.request('GET', '/v1/me/bundles/recovery')) as { bundle_ct: string };
    const identity = await unlockWithRecoveryCode(me.user_id, recoveryCode, bundle.bundle_ct);
    return new PageUser(page, me.user_id, identity);
  }

  request(method: string, target: string, body?: unknown): Promise<unknown> {
    return new PageUserRequests(this.page).request(method, target, body);
  }

  /** The org's log, verified with the user as founder (the user's private org) and the verified platform log. */
  async log(org: string): Promise<VerifiedLog> {
    const page = (await this.request('GET', `/v1/orgs/${org}/log`)) as { entries: { entry: unknown }[] };
    return verifyLog(
      page.entries.map((item) => item.entry),
      this.identity.public().sig,
      await this.platform(),
    );
  }

  /** The platform state of the verified platform log (certified and decertified platform devices); reads every page
   *  (`next_after`), as the web client does, so a long log of a long suite run is complete. */
  async platform(): Promise<PlatformState> {
    const entries: unknown[] = [];
    let after: number | null = null;
    for (;;) {
      const page = (await this.request('GET', `/v1/platform/log${after === null ? '' : `?after=${after}`}`)) as {
        entries: { entry: unknown }[];
        next_after: number | null;
      };
      entries.push(...page.entries.map((item) => item.entry));
      if (page.next_after === null) break;
      after = page.next_after;
    }
    const verified = await verifyPlatformLog(entries, compiledRootKey());
    return verified.state;
  }

  /** The `org`/`wg` keys of the org wrapped for the user. */
  async keys(org: string, log: VerifiedLog): Promise<Map<string, Secret>> {
    const body = (await this.request('GET', '/v1/wraps')) as {
      wraps: { scope: string; scope_id: string; epoch: number; wrap: string }[];
    };
    const keys = new Map<string, Secret>();
    for (const wrap of body.wraps) {
      const ours =
        (wrap.scope === ORG && wrap.scope_id === org) || (wrap.scope === WG && log.state.workgroups.has(wrap.scope_id));
      if (!ours) continue;
      const slot = { scope: wrap.scope, scope_id: wrap.scope_id, epoch: wrap.epoch, recipient: this.userId };
      keys.set(epochKey(wrap.scope, wrap.scope_id, wrap.epoch), await unwrapKey(this.identity.kem, slot, wrap.wrap));
    }
    return keys;
  }

  /**
   * A new workgroup (`wg_create` + its `wg.profile` event): the creating admin has `write`; with `access: 'read'` the
   * admin grants itself read access only (a visible `grant`), which makes it a reader of the workgroup. `readers` get
   * a read `grant`; `assignDevices` assigns every active device of the org (so tasks can be created there).
   */
  async createWorkgroup(
    org: string,
    name: string,
    access: 'write' | 'read' = 'write',
    options: { readonly readers?: readonly string[]; readonly assignDevices?: boolean } = {},
  ): Promise<string> {
    const log = await this.log(org);
    const keys = await this.keys(org, log);
    const wg = uuid7();
    const ts = formatTime(Date.now());
    const orgEpoch = log.state.epoch(ORG, org);
    const profileHeader = newHeader({
      id: uuid7(),
      org,
      scope: ORG,
      scope_id: org,
      epoch: orgEpoch,
      author: this.userId,
      kind: 'wg.profile',
      ts,
      lh: log.head.seq + 1,
      wg,
    });
    const profile = await sealEvent(
      keys.get(epochKey(ORG, org, orgEpoch))!,
      profileHeader,
      bodyJson({ name }),
      this.identity.sig.privateKey,
    );
    const devices = options.assignDevices
      ? [...log.state.principals.values()].filter((p) => p.kind === DEVICE && p.removedSeq === null)
      : [];
    const entries: LogFields[] = [
      { type: 'wg_create', wg, epoch: 1 },
      ...(access === 'read' ? [{ type: 'grant' as const, subject: this.userId, wg, access: 'read' as const }] : []),
      ...(options.readers ?? []).map((subject) => ({ type: 'grant' as const, subject, wg, access: 'read' as const })),
      ...devices.map((device) =>
        device.platformSeq === null
          ? { type: 'device_assign' as const, device: device.id, wg }
          : { type: 'device_assign' as const, device: device.id, wg, platform_seq: device.platformSeq },
      ),
    ];
    const built = await buildBatch({
      log,
      author: { id: this.userId, identity: this.identity },
      // an org with a platform device needs the platform state to choose the recipients of the new keys
      platform: await this.platform(),
      entries,
      keys: (scope, scopeId, epoch) => keys.get(epochKey(scope, scopeId, epoch)) ?? null,
      events: [profile],
      ts,
    });
    await this.request('POST', `/v1/orgs/${org}/log`, built.body);
    return wg;
  }

  /** Creates tasks through the API (the workgroup must have a device); returns their IDs, oldest first. */
  async createTasks(org: string, wg: string, prompts: readonly string[]): Promise<string[]> {
    const log = await this.log(org);
    const keys = await this.keys(org, log);
    const epoch = log.state.epoch(WG, wg);
    const key = keys.get(epochKey(WG, wg, epoch))!;
    const ids: string[] = [];
    for (const text of prompts) {
      const task = uuid7();
      const seal = async (kind: string, body: unknown) =>
        sealEvent(
          key,
          newHeader({
            id: uuid7(),
            org,
            scope: WG,
            scope_id: wg,
            epoch,
            author: this.userId,
            kind,
            ts: formatTime(Date.now()),
            lh: log.head.seq,
            wg,
            task,
          }),
          bodyJson(body),
          this.identity.sig.privateKey,
        );
      const events = [
        await seal('task.meta', { title: deriveTitle(text) }),
        await seal('prompt', { text, attachments: [] }),
      ];
      await this.request('POST', '/v1/tasks', { task_id: task, wg_id: wg, events });
      ids.push(task);
    }
    return ids;
  }
}

class PageUserRequests {
  constructor(private readonly page: Page) {}

  async request(method: string, target: string, body?: unknown): Promise<unknown> {
    const answer = await signedInPage(this.page, method, target, body === undefined ? null : JSON.stringify(body));
    if (answer.status !== 200) throw new Error(`${method} ${target}: ${answer.status} ${answer.text}`);
    return JSON.parse(answer.text) as unknown;
  }
}
