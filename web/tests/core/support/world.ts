// The support side of the release tests (ADR-096): the org world of tests/core/org/world.ts with sam and tom as persons
// of DFX support, workgroup `one` released by the admin through the real flow, and a second browser – sam's – with its
// own store (identity, verified platform log) and a client that serves what the hub serves a support person: the org's
// log, its own wraps, the releases, the events of the workgroup (with filters) and the prompts it posts.
import { bodyJson } from '../../../src/core/encoding/json';
import { formatTime } from '../../../src/core/encoding/time';
import { newHeader, sealEvent } from '../../../src/core/crypto/event';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import { epochKey, WG } from '../../../src/core/log/state';
import { createStore, type Store } from '../../../src/core/store';
import { setIdentity, setSession } from '../../../src/core/store/slices/me';
import { setPlatform } from '../../../src/core/store/slices/orgs';
import type { Access } from '../../../src/core/store/slices/support';
import { grantRelease } from '../../../src/core/support/release';
import { EMPTY_DIRECTORY } from '../../../src/core/tasks/directory';
import { type Actor, idOf, T0 } from '../log/world';
import { type OrgWorld, orgWorld } from '../org/world';
import { FakeClient } from '../tasks/world';

export const TASK = '0190f000-0000-7000-8000-0000000000f1';

export interface StoredItem {
  readonly scope_seq: number;
  readonly received_at: string | null;
  readonly event: { readonly header: Record<string, unknown> };
}

export interface SupportWorld {
  readonly w: OrgWorld;
  readonly a: OrgWorld['others'];
  readonly one: string;
  /** sam's browser. */
  readonly store: Store;
  readonly client: FakeClient;
  readonly context: { store: Store; client: FakeClient; now: () => number };
  /** The stored events of workgroup `one` (as `GET /v1/scopes/{one}/events` serves them). */
  readonly events: StoredItem[];
  /** Stores an event as it is (e.g. a malformed one). */
  put(event: StoredItem['event']): StoredItem;
  /** Seals an event of workgroup `one` as `author` (current epoch, `lh` = the head) and stores it; a byte body is
   *  sealed as it is. */
  write(author: Actor, kind: string, body: unknown, extra?: Record<string, unknown>): Promise<StoredItem>;
}

export async function supportWorld(
  options: { access?: Access | null; platform?: boolean; former?: boolean } = {},
): Promise<SupportWorld> {
  const one = await idOf('wg one');
  const w = await orgWorld({
    support: options.former ? ['tom'] : ['sam', 'tom'],
    ...(options.former && { formerSupport: ['sam'] }),
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.admitUser(a.ada, a.bob, 'member', { [one]: 'write' });
    },
  });
  const a = w.others;
  const access = options.access === undefined ? 'read' : options.access;
  if (access !== null) {
    const names = { ...EMPTY_DIRECTORY, orgName: 'DFX AG', members: new Map([[a.bob.id, 'Bob']]) };
    await grantRelease(w.context, w.org, one, access, { ...names, workgroups: new Map([[one, 'Team']]) });
  }
  const store = createStore();
  setSession(store, a.sam.id, { keyId: 'session.sam', expiresAt: T0 + 3600_000 });
  setIdentity(store, a.sam.identity);
  if (options.platform !== false) setPlatform(store, await verifyPlatformLog(w.platform.entries, w.platform.root));
  const client = new FakeClient();
  const events: StoredItem[] = [];
  let seq = 0;
  const put = (event: StoredItem['event']): StoredItem => {
    seq += 1;
    const item = { scope_seq: seq, received_at: formatTime(T0), event };
    events.push(item);
    return item;
  };
  for (const event of w.hub.events as StoredItem['event'][]) if (event.header.scope_id === one) put(event);
  client.on(`GET /v1/orgs/${w.org}/log`, (_body, target) => {
    const after = Number(new URL(target, 'https://hub.example').searchParams.get('after') ?? -1);
    return {
      entries: w.world.log!.entries.map((entry, index) => ({ seq: index, entry })).filter((item) => item.seq > after),
      head: w.world.log!.head,
      next_after: null,
    };
  });
  client.on('GET /v1/wraps', () => ({
    wraps: w.hub.stored
      .filter((wrap) => wrap.recipient_id === a.sam.id)
      .map((wrap) => ({ ...wrap, created_at: formatTime(T0) })),
  }));
  client.on(`GET /v1/scopes/${one}/events`, (_body, target) => {
    const query = new URL(target, 'https://hub.example').searchParams;
    const kinds = query.get('kinds')?.split(',') ?? null;
    const task = query.get('task');
    const shown = events.filter(
      (item) =>
        // a malformed event without header passes every filter (the reader refuses it)
        item.event.header === undefined ||
        ((kinds === null || kinds.includes(item.event.header.kind as string)) &&
          (task === null || item.event.header.task === task)),
    );
    return { events: [...shown].reverse(), next_before: null, head: seq };
  });
  client.on(`POST /v1/tasks/${TASK}/prompts`, (body) => {
    const item = put((body as { event: StoredItem['event'] }).event);
    return { scope_seq: item.scope_seq, session_id: 'x' };
  });
  const write = async (author: Actor, kind: string, body: unknown, extra: Record<string, unknown> = {}) => {
    const state = w.world.state;
    const epoch = state.epoch(WG, one);
    const key = w.world.held.get(author.id)!.get(epochKey(WG, one, epoch))!;
    const header = newHeader({
      id: `0190f000-0000-7000-8000-${String(100000000000 + seq).padStart(12, '0')}`,
      org: w.org,
      scope: WG,
      scope_id: one,
      epoch,
      author: author.id,
      kind,
      ts: formatTime(T0),
      lh: w.world.log!.head.seq,
      wg: one,
      task: TASK,
      ...extra,
    });
    const plaintext = body instanceof Uint8Array ? body : bodyJson(body);
    return put((await sealEvent(key, header, plaintext, author.identity.sig.privateKey)) as StoredItem['event']);
  };
  return { w, a, one, store, client, context: { store, client, now: () => T0 }, events, put, write };
}
