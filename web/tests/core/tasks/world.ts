// Test world of the task flows (U-WEB-ui-tasks core part): an org with a verified log built through `buildBatch`
// (tests/core/log/world.ts), the user's keys, a verified platform log with two certified devices, the user's own CK
// self-wrap, a store holding all of it, and a scriptable signed client (`METHOD path` → handler).
import { vi } from 'vitest';
import { b64uDecode } from '../../../src/core/encoding/b64u';
import { Secret } from '../../../src/core/crypto/secret';
import { signWrap, wrapKey } from '../../../src/core/crypto/wrap';
import { verifyPlatformLog } from '../../../src/core/log/platform';
import { HubError } from '../../../src/core/net/signedFetch';
import type { StoredEvent } from '../../../src/core/net/sse';
import { createStore, type Store } from '../../../src/core/store';
import { setIdentity, setSession } from '../../../src/core/store/slices/me';
import { addKeys, ensureOrg, setLog, setPlatform } from '../../../src/core/store/slices/orgs';
import type { TasksContext } from '../../../src/core/tasks/pull';
import { type Actor, actors, platformWith, T0, World } from '../log/world';

export type Handler = (body: unknown, target: string) => unknown;

export interface Call {
  readonly method: string;
  readonly target: string;
  readonly body: unknown;
}

/** A signed client double: answers from `routes`, records every call; unknown routes are `404 not_found`. */
export class FakeClient {
  readonly routes = new Map<string, Handler>();
  readonly calls: Call[] = [];

  on(route: string, handler: Handler): this {
    this.routes.set(route, handler);
    return this;
  }

  async request(method: string, target: string, options: { body?: unknown } = {}): Promise<unknown> {
    this.calls.push({ method, target, body: options.body });
    const handler = this.routes.get(`${method} ${target.split('?')[0]}`);
    if (handler === undefined) throw new HubError('not_found', 404);
    return handler(options.body, target);
  }

  /** The calls of one route. */
  of(route: string): Call[] {
    return this.calls.filter((call) => `${call.method} ${call.target.split('?')[0]}` === route);
  }
}

export interface TaskWorld {
  readonly world: World;
  readonly me: Actor;
  readonly ada: Actor;
  readonly devices: readonly Actor[];
  readonly platform: Awaited<ReturnType<typeof platformWith>>;
  readonly store: Store;
  readonly client: FakeClient;
  readonly context: TasksContext & { refreshLog: ReturnType<typeof vi.fn>; ingest: ReturnType<typeof vi.fn> };
  readonly ingested: { org: string; scope: string; items: readonly StoredEvent[] }[];
  readonly clock: { now: number };
  /** The user's own CK self-wrap (as `GET /v1/wraps` lists it). */
  readonly credWrap: Record<string, unknown>;
  readonly credKey: Secret;
  readonly org: string;
  readonly wg: string;
}

export interface WorldOptions {
  readonly private?: boolean;
  /** Act as a plain member (not admin) of the org. */
  readonly member?: boolean;
  /** Leave the platform log out of the store. */
  readonly noPlatform?: boolean;
}

export async function taskWorld(options: WorldOptions = {}): Promise<TaskWorld> {
  const [ada, bob, dev1, dev2] = await actors('ada', 'bob', 'dev1', 'dev2');
  const platform = await platformWith([dev1, dev2]);
  const world = await World.create(ada, { private: options.private ?? true });
  world.platform = platform.state;
  if (options.member) await world.admitUser(ada, bob, 'member');
  const me = options.member ? bob : ada;
  const store = createStore();
  setSession(store, me.id, { keyId: 'session.x', expiresAt: T0 + 3600_000 });
  setIdentity(store, me.identity);
  ensureOrg(store, world.org);
  setLog(store, world.org, world.log!);
  addKeys(store, world.org, world.held.get(me.id)!);
  if (!options.noPlatform) setPlatform(store, await verifyPlatformLog(platform.entries, platform.root));
  const credKey = Secret.of(new Uint8Array(32).fill(7));
  const slot = { scope: 'cred', scope_id: me.id, epoch: 1, recipient: me.id };
  const wrap = await wrapKey(b64uDecode(me.identity.public().kem), slot, credKey);
  const credWrap = {
    scope: 'cred',
    scope_id: me.id,
    epoch: 1,
    recipient_id: me.id,
    wrap,
    sig: await signWrap(me.identity.sig.privateKey, slot, wrap),
    wrapped_by: me.id,
  };
  const client = new FakeClient();
  const ingested: TaskWorld['ingested'] = [];
  const clock = { now: T0 };
  const context = {
    store,
    client,
    now: () => clock.now,
    refreshLog: vi.fn(async () => true),
    ingest: vi.fn(async (org: string, scope: string, items: readonly StoredEvent[]) => {
      ingested.push({ org, scope, items });
    }),
  };
  return {
    world,
    me,
    ada,
    devices: [dev1, dev2],
    platform,
    store,
    client,
    context,
    ingested,
    clock,
    credWrap,
    credKey,
    org: world.org,
    wg: world.defaultWg,
  };
}

/** A candidate as `GET /v1/workgroups/{wg}/device-candidates` lists it. */
export function candidate(
  t: TaskWorld,
  index: number,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  const device = t.devices[index]!;
  return {
    device_id: device.id,
    platform_seq: t.platform.seqs.get(device.id),
    pub: device.identity.public(),
    online: true,
    capacity: { vm_memory_budget_mib: 1, vm_memory_used_mib: 0, vm_slots_total: 1, vm_slots_free: 1, pool_ready: 0 },
    capacity_low: false,
    ...overrides,
  };
}

/** A task row. */
export function taskRow(t: { org: string; wg: string; me: Actor }, overrides: Record<string, unknown> = {}) {
  return {
    task_id: '0190f000-0000-7000-8000-000000000001',
    org_id: t.org,
    wg_id: t.wg,
    owner_id: t.me.id,
    state: 'starting',
    state_reason: null,
    created_at: '2026-11-21T12:00:00.000Z',
    state_at: '2026-11-21T12:00:00.000Z',
    last_activity_at: '2026-11-21T12:00:00.000Z',
    closed_at: null,
    main_session_id: '0190f000-0000-7000-8000-0000000000a1',
    title_event_id: null,
    ...overrides,
  };
}

/** A session row. */
export function sessionRow(t: { org: string; wg: string; me: Actor }, overrides: Record<string, unknown> = {}) {
  return {
    session_id: '0190f000-0000-7000-8000-0000000000a1',
    task_id: '0190f000-0000-7000-8000-000000000001',
    org_id: t.org,
    wg_id: t.wg,
    owner_id: t.me.id,
    device_id: '0190f000-0000-7000-8000-0000000000d1',
    vm_id: null,
    parent_session_id: null,
    origin_operation_id: null,
    depth: 0,
    state: 'requested',
    state_reason: null,
    model_access: null,
    model_id: null,
    agent_kind: 'claude-code',
    agent_version: null,
    estimate_ms: 3000,
    created_at: '2026-11-21T12:00:00.000Z',
    ready_at: null,
    first_working_at: null,
    ended_at: null,
    ...overrides,
  };
}
