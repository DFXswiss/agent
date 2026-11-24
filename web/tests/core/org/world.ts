// Test world of the org-management flows (U-WEB-ui-members/workgroups core part): the simulated org of
// tests/core/log/world.ts behind a small hub that applies the hub's own batch rules (`checkBatch` with receipt time,
// platform access and decertified devices) to every `POST /v1/orgs/{org}/log`, stores wraps, invitations and events,
// and answers catch-up from the stored wraps. The browser side is a store holding the actor's verified log, keys and
// the verified platform log; `refreshLog` re-reads the hub's log like the log job.
import { type Mock, vi } from 'vitest';
import { formatTime } from '../../../src/core/encoding/time';
import { CryptoError } from '../../../src/core/crypto/errors';
import { buildEntries, type Entry, entryHash, type LogFields } from '../../../src/core/log/entries';
import { missingWraps } from '../../../src/core/log/catchup';
import { verifyPlatformLog, type VerifiedPlatformLog } from '../../../src/core/log/platform';
import { checkBatch } from '../../../src/core/log/rotation';
import { epochKey } from '../../../src/core/log/state';
import { extendLog } from '../../../src/core/log/verify';
import { slotJson, type WrapRecord } from '../../../src/core/log/wraps';
import { HubError } from '../../../src/core/net/signedFetch';
import type { OrgContext } from '../../../src/core/org/batch';
import { createStore, type Store } from '../../../src/core/store';
import { setIdentity, setSession } from '../../../src/core/store/slices/me';
import { addKeys, ensureOrg, setLog, setPlatform } from '../../../src/core/store/slices/orgs';
import type { StoredEvent } from '../../../src/core/net/sse';
import { type Actor, actors, platformWith, T0, World } from '../log/world';
import { FakeClient } from '../tasks/world';

export type ActorName = 'ada' | 'bob' | 'cy' | 'dev1' | 'dev2' | 'pdev';

export interface StoredInvite {
  invite_id: string;
  kind: 'user' | 'device';
  state: 'open' | 'used' | 'revoked' | 'expired';
  log_seq: number;
  created_by: string;
  created_at: string;
  expires_at: string;
  registered: boolean;
  used_at: null;
  used_by: null;
  revoked_at: string | null;
  /** First lookup (hubs since KOR-36). */
  opened_at?: string | null;
  package_ct: string;
}

/** The hub of the org: rules of `POST /v1/orgs/{org}/log` and the stores the flows touch. */
export class OrgHub {
  readonly stored: (WrapRecord & { wrapped_by: string })[] = [];
  readonly invites = new Map<string, StoredInvite>();
  readonly events: unknown[] = [];
  /** Entries a concurrent admin appends right before the next N log writes (`seq_conflict`). */
  readonly before: (() => Promise<void>)[] = [];
  now = T0;
  platformAccess = false;

  constructor(
    readonly world: World,
    public platform: VerifiedPlatformLog | null,
  ) {}

  get org(): string {
    return this.world.org;
  }

  record(author: string, wraps: readonly WrapRecord[]): void {
    for (const wrap of wraps) this.stored.push({ ...wrap, wrapped_by: author });
  }

  /** Runs a queued concurrent write; true if one ran (the caller then answers `seq_conflict`). */
  async race(): Promise<boolean> {
    const next = this.before.shift();
    if (next === undefined) return false;
    await next();
    return true;
  }

  async append(
    author: string,
    body: { expected_seq: number; entries: Entry[]; wraps: WrapRecord[]; events: unknown[] },
  ) {
    if (await this.race()) throw new HubError('seq_conflict', 409);
    if (body.expected_seq !== this.world.log!.head.seq + 1) throw new HubError('seq_conflict', 409);
    const platform = this.platform?.state ?? null;
    let result;
    try {
      result = await checkBatch(this.world.log, body.entries, body.wraps, author, {
        platform,
        now: this.now,
        platformAccess: this.platformAccess,
        excluded: platform === null ? [] : platform.excluded(this.world.log!.state),
      });
    } catch (error) {
      if (error instanceof CryptoError) throw new HubError(error.code, 400);
      throw error;
    }
    this.world.log = result.log;
    await this.world.store(body.wraps);
    this.record(author, body.wraps);
    this.events.push(...body.events);
    return { head: result.log.head };
  }

  /** An entry appended by another actor (no wraps), e.g. to provoke `seq_conflict`. */
  async foreign(author: Actor, fields: LogFields): Promise<void> {
    const signer = { id: author.id, key: author.identity.sig.privateKey };
    const entries = await buildEntries({ org: this.org, head: this.world.log!.head }, signer, [fields], formatTime(T0));
    this.world.log = await extendLog(this.world.log!, entries, this.platform?.state ?? null);
  }
}

export interface OrgWorld {
  readonly world: World;
  readonly hub: OrgHub;
  readonly me: Actor;
  readonly others: Readonly<Record<ActorName, Actor>>;
  readonly store: Store;
  readonly client: FakeClient;
  readonly context: OrgContext & {
    refreshLog: Mock<() => Promise<boolean>>;
    ingest: Mock<(org: string, scope: string, items: readonly StoredEvent[]) => Promise<void>>;
  };
  readonly ingested: { org: string; scope: string; items: readonly StoredEvent[] }[];
  readonly org: string;
  /** Puts the hub's current log and the actor's keys into the store (what the log and wrap jobs do). */
  sync(): Promise<void>;
  /** The platform log the hub serves (`GET /v1/platform/log`); tests may append to `entries`. */
  readonly platform: { readonly root: string; readonly entries: Entry[] };
}

export interface OrgWorldOptions {
  /** Act as this actor (default `ada`, the founder and admin). */
  readonly as?: 'ada' | 'bob' | 'cy';
  readonly private?: boolean;
  /** Org setup after genesis, before the store is filled. */
  readonly setup?: (world: World, actors: Readonly<Record<ActorName, Actor>>) => Promise<void>;
  /** Platform devices certified in the platform log. */
  readonly platformDevices?: readonly string[];
  readonly noPlatform?: boolean;
}

/** ada (founder, admin), bob, cy (users), dev1, dev2 (devices). */
export async function orgWorld(options: OrgWorldOptions = {}): Promise<OrgWorld> {
  const [ada, bob, cy, dev1, dev2, pdev] = await actors('ada', 'bob', 'cy', 'dev1', 'dev2', 'pdev');
  const all = { ada, bob, cy, dev1, dev2, pdev };
  const certified = (options.platformDevices ?? []).map((name) => all[name as keyof typeof all]);
  const platformLog = await platformWith(certified);
  const world = await World.create(ada, { private: options.private ?? false });
  world.platform = platformLog.state;
  const verified = options.noPlatform ? null : await verifyPlatformLog(platformLog.entries, platformLog.root);
  const hub = new OrgHub(world, verified);
  hub.record(ada.id, world.wraps);
  const original = world.batch.bind(world);
  world.batch = async (author, entries, extra) => {
    const built = await original(author, entries, extra);
    hub.record(author.id, built.body.wraps);
    return built;
  };
  await options.setup?.(world, all);
  const me = all[options.as ?? 'ada'];
  const store = createStore();
  setSession(store, me.id, { keyId: 'session.x', expiresAt: T0 + 3600_000 });
  setIdentity(store, me.identity);
  ensureOrg(store, world.org);
  if (verified !== null) setPlatform(store, verified);
  const sync = async () => {
    setLog(store, world.org, world.log!);
    addKeys(store, world.org, world.held.get(me.id) ?? new Map());
  };
  await sync();
  const client = new FakeClient();
  const ingested: OrgWorld['ingested'] = [];
  const context = {
    store,
    client,
    now: () => hub.now,
    refreshLog: vi.fn(async () => {
      await sync();
      return true;
    }),
    ingest: vi.fn(async (org: string, scope: string, items: readonly StoredEvent[]) => {
      ingested.push({ org, scope, items });
    }),
  };
  const org = world.org;
  client.on(`POST /v1/orgs/${org}/log`, (body) => hub.append(me.id, body as never));
  client.on(`POST /v1/orgs/${org}/invites`, async (raw) => {
    const body = raw as { entry: Entry; package_ct: string };
    if (await hub.race()) throw new HubError('seq_conflict', 409);
    const result = await hub.append(me.id, {
      expected_seq: body.entry.body.seq as number,
      entries: [body.entry],
      wraps: [],
      events: [],
    });
    const entry = body.entry.body;
    hub.invites.set(entry.invite_id as string, {
      invite_id: entry.invite_id as string,
      kind: entry.kind as 'user',
      state: 'open',
      log_seq: entry.seq as number,
      created_by: me.id,
      created_at: formatTime(hub.now),
      expires_at: entry.expires as string,
      registered: false,
      used_at: null,
      used_by: null,
      revoked_at: null,
      package_ct: body.package_ct,
    });
    return { invite_id: entry.invite_id, seq: result.head.seq };
  });
  client.on(`GET /v1/orgs/${org}/invites`, () => ({
    invites: [...hub.invites.values()].map(({ package_ct: _p, ...row }) => row),
  }));
  client.on(`GET /v1/orgs/${org}/catch-up`, () => {
    const existing = hub.stored.map((w) => ({
      scope: w.scope,
      scope_id: w.scope_id,
      epoch: w.epoch,
      recipient: w.recipient_id,
    }));
    const platform = hub.platform?.state ?? null;
    const excluded = platform === null ? [] : platform.excluded(world.log!.state);
    return { missing: missingWraps(world.log!, me.id, existing, excluded).map(slotJson) };
  });
  client.on('POST /v1/wraps', async (raw) => {
    const body = raw as { wraps: WrapRecord[] };
    await world.store(body.wraps);
    hub.record(me.id, body.wraps);
    return { stored: body.wraps.length, ignored: 0 };
  });
  client.on('GET /v1/wraps', (_body, target) => {
    const query = new URL(target, 'https://hub.example').searchParams;
    return {
      wraps: hub.stored.filter(
        (w) =>
          w.recipient_id === me.id &&
          (query.get('scope') === null || w.scope === query.get('scope')) &&
          (query.get('scope_id') === null || w.scope_id === query.get('scope_id')),
      ),
    };
  });
  client.on('POST /v1/events', (raw) => {
    const body = raw as { events: { event: unknown }[] };
    hub.events.push(...body.events.map((item) => item.event));
    return { results: body.events.map((_, i) => ({ id: 'x', scope_id: org, scope_seq: 100 + i, replayed: false })) };
  });
  const platformEntries = [...platformLog.entries];
  client.on('GET /v1/platform/log', async (_body, target) => {
    const after = Number(new URL(target, 'https://hub.example').searchParams.get('after') ?? -1);
    const last = platformEntries[platformEntries.length - 1]!;
    return {
      entries: platformEntries.map((entry, seq) => ({ seq, entry })).filter((item) => item.seq > after),
      head: { seq: platformEntries.length - 1, hash: await entryHash(last) },
      next_after: null,
    };
  });
  client.on(`GET /v1/orgs/${org}/log`, async (_body, target) => {
    const after = Number(new URL(target, 'https://hub.example').searchParams.get('after') ?? -1);
    return {
      entries: world.log!.entries.map((entry, seq) => ({ seq, entry })).filter((item) => item.seq > after),
      head: world.log!.head,
      next_after: null,
    };
  });
  return {
    world,
    hub,
    me,
    others: all,
    store,
    client,
    context,
    ingested,
    org,
    sync,
    platform: { root: platformLog.root, entries: platformEntries },
  };
}

/** The key `actor` holds for the current epoch of a scope in the world (null if none). */
export function holds(w: OrgWorld, actor: Actor, scope: string, id: string): boolean {
  const epoch = w.world.state.epoch(scope, id);
  return w.world.held.get(actor.id)?.has(epochKey(scope, id, epoch)) ?? false;
}
