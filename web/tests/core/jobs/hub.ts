// A simulated hub for the job and reader tests: the org of tests/core/log/world.ts plus a platform log, served page by
// page through `request` (the jobs take a `Pick<SignedClient, 'request'>`), with knobs to tamper with what it serves.
import { IDBFactory } from 'fake-indexeddb';
import { entryHash, type Entry } from '../../../src/core/log/entries';
import type { JobContext } from '../../../src/core/jobs';
import { HubError } from '../../../src/core/net/signedFetch';
import { Persist } from '../../../src/core/persist';
import { createStore, type Store } from '../../../src/core/store';
import { setIdentity, setSession } from '../../../src/core/store/slices/me';
import type { StoredWrap } from '../../../src/core/jobs/wrap-verify';
import { type Actor, platformWith, World } from '../log/world';

export interface Served {
  entries: unknown[];
  head: { seq: number; hash: string } | null;
}

export class JobHub {
  readonly wraps: StoredWrap[] = [];
  readonly requests: string[] = [];
  pageSize = 2;
  /** Platform log seq of each certified platform device. */
  seqs: ReadonlyMap<string, number> = new Map();
  /** Replace what a log path serves (`org` or `platform`). */
  serve: {
    org?: (served: Served) => Served | Promise<Served>;
    platform?: (served: Served) => Served | Promise<Served>;
  } = {};
  fail: Map<string, HubError | (() => unknown)> = new Map();

  private constructor(
    readonly world: World,
    readonly platform: { root: string; entries: Entry[] },
  ) {
    // every batch of the world records its wraps with the batch author as `wrapped_by` (as the hub does)
    const original = world.batch.bind(world);
    world.batch = async (author, entries, extra) => {
      const built = await original(author, entries, extra);
      this.record(author, built.body.wraps);
      return built;
    };
  }

  /** The org of `founder`; `platformDevices` are certified in the platform log (and known to the world's builder). */
  static async create(founder: Actor, platformDevices: readonly Actor[] = []): Promise<JobHub> {
    const platform = await platformWith(platformDevices);
    const hub = new JobHub(await World.create(founder), platform);
    hub.world.platform = platform.state;
    hub.seqs = platform.seqs;
    hub.record(founder, hub.world.wraps);
    return hub;
  }

  record(
    author: Actor,
    wraps: readonly {
      scope: string;
      scope_id: string;
      epoch: number;
      recipient_id: string;
      wrap: string;
      sig: string;
    }[],
  ): void {
    for (const wrap of wraps) this.wraps.push({ ...wrap, wrapped_by: author.id });
  }

  private async page(path: string, all: Served, after: number | null) {
    const start = after === null ? 0 : after + 1;
    const entries = all.entries.slice(start, start + this.pageSize).map((entry, i) => ({ seq: start + i, entry }));
    const last = start + entries.length - 1;
    const more = start + this.pageSize < all.entries.length;
    return { entries, head: all.head, next_after: more ? last : null, path };
  }

  async served(kind: 'org' | 'platform'): Promise<Served> {
    const entries = kind === 'org' ? [...this.world.log!.entries] : [...this.platform.entries];
    const last = entries[entries.length - 1];
    const base: Served = {
      entries,
      head: last === undefined ? null : { seq: entries.length - 1, hash: await entryHash(last) },
    };
    const change = this.serve[kind];
    return change === undefined ? base : change(base);
  }

  request = async (method: string, target: string): Promise<unknown> => {
    this.requests.push(`${method} ${target}`);
    const url = new URL(target, 'https://hub.example');
    const failure = this.fail.get(url.pathname);
    if (failure instanceof HubError) throw failure;
    if (failure !== undefined) return failure();
    const after = url.searchParams.has('after') ? Number(url.searchParams.get('after')) : null;
    if (url.pathname === '/v1/platform/log') return this.page(url.pathname, await this.served('platform'), after);
    if (url.pathname === `/v1/orgs/${this.world.org}/log`)
      return this.page(url.pathname, await this.served('org'), after);
    if (url.pathname === '/v1/wraps')
      return { wraps: this.wraps.map((wrap) => ({ ...wrap, created_at: '2026-11-21T12:00:00.000Z' })) };
    throw new HubError('not_found', 404);
  };
}

export interface Setup {
  readonly store: Store;
  readonly persist: Persist;
  readonly context: JobContext;
  readonly changed: string[];
}

/** A signed-in, unlocked store for `actor` and a job context against `hub`. */
export async function signedIn(hub: JobHub, actor: Actor, factory: IDBFactory = new IDBFactory()): Promise<Setup> {
  const store = createStore();
  setSession(store, actor.id, { keyId: 'session.x', expiresAt: 0 });
  setIdentity(store, actor.identity);
  const persist = await Persist.open(factory);
  const changed: string[] = [];
  const context: JobContext = {
    store,
    client: hub,
    persist,
    rootKey: hub.platform.root,
    now: () => 1000,
    changed: (org) => {
      changed.push(org);
    },
  };
  return { store, persist, context, changed };
}
