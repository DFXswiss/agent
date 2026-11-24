// The rights state of one organization as derived from its verified membership log (04-crypto.md section 4.7).
// TypeScript twin of `agent_hub.log.state`.
//
// `OrgState` is immutable; `verify.apply` returns a new state per entry, so a reader keeps the state at every `seq`
// (E5 checks rights "at `lh`"). Derived rights (section 4.7, ADR-024 addendum):
// - an active user member reads the org scope and writes the default workgroup (not reducible in v1);
// - an admin writes the org scope and reads every workgroup of the org (inherited read, ADR-058); the creating admin
//   of a workgroup (`wg_create`) gets explicit `write` on it;
// - explicit workgroup access of users comes from `invite.wgs` (applied at `accept`), `grant` and `ungrant`; the
//   effective access (`wgAccess`) is the explicit access if there is one, else `read` for an active admin; the
//   credential scope uses the explicit access only (`explicitAccess`, ADR-058 section 1);
// - devices read the workgroups they are assigned to and the org scope while they have at least one assignment
//   (ADR-007); a device assigned to the organization (`device_assign_org` until `device_unassign_org` or `remove`,
//   ADR-075) is assigned to every workgroup of the org, including later ones;
// - an archived workgroup stays readable but is no longer writable (decision CRY-02);
// - `admit` (ADR-060) makes a user a member like `accept` of a user invitation with its role and workgroups, but
//   without the user's consent (`Principal.consented` false until it leaves and joins again by an own `accept`).
// `remove` deletes every access and assignment of the subject; a former member who accepts a new invitation starts
// again from that invitation (rejoin).
//
// Agents (ADR-082 sections 1 and 9): an agent is a sub-principal of its user (`parent`), added by `agent_add` or the
// `agent` member of `genesis`/`accept`. It has no role, access or assignment of its own: every right resolves
// through `acting` (the parent of an agent, else the principal itself), so the active agent of a user reads exactly
// what its user reads. An agent is active while it is not removed and its parent is an active member; `remove` of a
// user removes its active agent at the same `seq` (`removedWithUser`); only such an agent may come back with its user.
// Agents never count as admins (`admins`).

export const USER = 'user';
export const DEVICE = 'device';
export const AGENT = 'agent';
export const ADMIN = 'admin';
export const MEMBER = 'member';
export const READ = 'read';
export const WRITE = 'write';
export const ORG = 'org';
export const WG = 'wg';

// --- maps shared between states ---
//
// Immutable maps that share their contents with the map they were derived from (KOR-35). A verified log keeps the
// state after every entry (`VerifiedLog.states`), and an entry that changes one invitation, principal, access or
// assignment used to copy the whole map, so verifying a log cost time and memory quadratic in its length (an org with
// a few thousand entries did not finish its first sign-in). `withEntry` and `withoutEntry` return a map that reads
// exactly like the copy `new Map(map).set(key, value)` or `.delete(key)` – the same `get`, `has`, `size` and the same
// iteration order (first insertion order; a replaced value stays in place, a deleted and added again key moves to the
// end). All maps of one lineage share an append-only store of every write with its version; a map is a version of it
// and reads the latest write of a key at or before its version. A write from the newest version appends (O(1)
// amortized); a write from an older version (a second child of one state) copies the map into a new store first.
// `changedKeys` names the keys written between two versions of a store, so the readers of the rights questions over
// every state of a log only look at the principals an entry touched (`changedPrincipals`). A store copies the map it
// is built from, and `OrgState` turns every plain map it is given into such a copy: what a state answers (and every
// index built for it) can never change because a caller still holds and changes the plain map.

/** The mark of a deleted key in the store. */
const DELETED: unique symbol = Symbol('deleted');

/** The writes of one key: ascending versions, their values, and the versions at which it was (again) inserted. */
interface Slot<V> {
  readonly versions: number[];
  readonly values: (V | typeof DELETED)[];
  readonly inserted: number[];
}

/** The index of the last element `<= version` of an ascending list, -1 if none. */
function lastAtMost(list: readonly number[], version: number): number {
  let low = 0;
  let high = list.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (list[middle]! <= version) low = middle + 1;
    else high = middle - 1;
  }
  return high;
}

class Store<K, V> {
  /** The newest version; writes are numbered from 1. */
  latest = 0;
  readonly slots = new Map<K, Slot<V>>();
  /** `[version, key]` of every insertion, in version order (the iteration order of every version). */
  readonly inserts: [number, K][] = [];
  /** `writes[version - 1]`: the key written at `version`. */
  readonly writes: K[] = [];

  /** A store holding a copy of `contents` (its newest version). */
  constructor(contents: Iterable<readonly [K, V]>) {
    for (const [key, value] of contents) this.write(key, value, true);
  }

  write(key: K, value: V | typeof DELETED, insert: boolean): number {
    const version = ++this.latest;
    let slot = this.slots.get(key);
    if (slot === undefined) {
      slot = { versions: [], values: [], inserted: [] };
      this.slots.set(key, slot);
    }
    slot.versions.push(version);
    slot.values.push(value);
    if (insert) {
      slot.inserted.push(version);
      this.inserts.push([version, key]);
    }
    this.writes.push(key);
    return version;
  }

  /** The value of `key` at `version`; `DELETED` also for a key not written by then. */
  at(key: K, version: number): V | typeof DELETED {
    const slot = this.slots.get(key);
    if (slot === undefined) return DELETED;
    const index = lastAtMost(slot.versions, version);
    return index < 0 ? DELETED : slot.values[index]!;
  }
}

class VersionedMap<K, V> implements ReadonlyMap<K, V> {
  constructor(
    readonly store: Store<K, V>,
    readonly version: number,
    readonly size: number,
  ) {}

  get(key: K): V | undefined {
    const value = this.store.at(key, this.version);
    return value === DELETED ? undefined : value;
  }

  has(key: K): boolean {
    return this.store.at(key, this.version) !== DELETED;
  }

  *entries(): MapIterator<[K, V]> {
    const { store, version } = this;
    for (const [inserted, key] of store.inserts) {
      if (inserted > version) return;
      // a key counts at its last insertion up to this version (a deleted and added again key moves to the end)
      const slot = store.slots.get(key)!;
      if (slot.inserted[lastAtMost(slot.inserted, version)] !== inserted) continue;
      const value = store.at(key, version);
      if (value !== DELETED) yield [key, value];
    }
  }

  *keys(): MapIterator<K> {
    for (const [key] of this.entries()) yield key;
  }

  *values(): MapIterator<V> {
    for (const [, value] of this.entries()) yield value;
  }

  [Symbol.iterator](): MapIterator<[K, V]> {
    return this.entries();
  }

  forEach(callback: (value: V, key: K, map: ReadonlyMap<K, V>) => void, thisArg?: unknown): void {
    for (const [key, value] of this.entries()) callback.call(thisArg, value, key, this);
  }
}

/** The newest version of a store holding `map` (a new store, a copy, for a plain map or an older version). */
function writable<K, V>(map: ReadonlyMap<K, V>): VersionedMap<K, V> {
  if (map instanceof VersionedMap && map.version === map.store.latest) return map as VersionedMap<K, V>;
  const store = new Store<K, V>(map);
  return new VersionedMap(store, store.latest, map.size);
}

/** `map` as an immutable shared map: itself if it is one, else a copy (section comment). */
export function sharedMap<K, V>(map: ReadonlyMap<K, V>): ReadonlyMap<K, V> {
  if (map instanceof VersionedMap) return map;
  const store = new Store<K, V>(map);
  return new VersionedMap(store, store.latest, map.size);
}

/** `new Map(map).set(key, value)` as an immutable map that shares `map`'s contents (section comment). */
export function withEntry<K, V>(map: ReadonlyMap<K, V>, key: K, value: V): ReadonlyMap<K, V> {
  const from = writable(map);
  const present = from.has(key);
  const version = from.store.write(key, value, !present);
  return new VersionedMap(from.store, version, present ? from.size : from.size + 1);
}

/** `new Map(map)` without `key`, as an immutable map that shares `map`'s contents (section comment). */
export function withoutEntry<K, V>(map: ReadonlyMap<K, V>, key: K): ReadonlyMap<K, V> {
  if (!map.has(key)) return map;
  const from = writable(map);
  return new VersionedMap(from.store, from.store.write(key, DELETED, false), from.size - 1);
}

/**
 * Keys whose value (or presence) may differ between `a` and `b` – none for the same map, the keys written between
 * their versions for two versions of one store – or null if the maps share nothing known (a plain map, two stores;
 * the caller then compares everything).
 */
export function changedKeys<K, V>(a: ReadonlyMap<K, V>, b: ReadonlyMap<K, V>): Iterable<K> | null {
  if (a === b) return [];
  if (!(a instanceof VersionedMap && b instanceof VersionedMap && a.store === b.store)) return null;
  const [first, second] = [a.version, b.version];
  return new Set((a.store as Store<K, V>).writes.slice(Math.min(first, second), Math.max(first, second)));
}

/** `[scope, scope_id]` with scope `org` or `wg`. */
export type Scope = readonly [scope: string, scopeId: string];

/** The map key of a pair of IDs (UUIDs and scope names never contain a space). */
export const pairKey = (a: string, b: string): string => `${a} ${b}`;

/** The pair of a `pairKey`. */
export const splitKey = (value: string): Scope => {
  const [a, b] = value.split(' ');
  return [a!, b!];
};

/** Orders two texts by code unit (equal to code point order for the ASCII IDs of the logs). */
export const compareText = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

/** Orders two scopes (`org` before `wg`, then by ID). */
export const compareScopes = (a: Scope, b: Scope): number => compareText(pairKey(...a), pairKey(...b));

/** A user, device or agent of the org. `role` is `admin`/`member` for users, `device` for devices and `agent` for
 *  agents. */
export interface Principal {
  readonly id: string;
  readonly kind: string;
  readonly role: string;
  readonly kem: string;
  readonly sig: string;
  readonly joinedSeq: number;
  readonly removedSeq: number | null;
  /** Platform devices: the platform-log `seq` of their `device_certify` (keys come from there). */
  readonly platformSeq: number | null;
  /**
   * A user that joined by its own signed entry (`genesis`, `accept`); false after `admit` (ADR-060): the devices of
   * this org are then no recipients of the user's credential key.
   */
  readonly consented: boolean;
  /** Agents: the user the agent acts for; null for users and devices. */
  readonly parent: string | null;
  /** Agents: the generation of the certificate (ADR-082 section 9.1); null for users and devices. */
  readonly gen: number | null;
  /** Agents: removed together with its user (it may come back when the user rejoins, section 9.2). */
  readonly removedWithUser: boolean;
  /** Agents: removed by its user, an agent of its user or itself (a revocation in every log, unlike a removal by an
   *  admin, which only ends its membership in this org). */
  readonly removedByOwner: boolean;
  /** Users: the agent that signed the user's `genesis`/`accept` (its revocation withdraws the consent). */
  readonly joinAgent: string | null;
}

/** The fields of a new principal that only agents and agent-signed joins set. */
export const PLAIN: Pick<Principal, 'parent' | 'gen' | 'removedWithUser' | 'removedByOwner' | 'joinAgent'> = {
  parent: null,
  gen: null,
  removedWithUser: false,
  removedByOwner: false,
  joinAgent: null,
};

/** An `invite` entry and what happened to it. */
export interface Invite {
  readonly inviteId: string;
  readonly pkInv: string;
  readonly kind: string;
  readonly role: string | null;
  readonly wgs: Readonly<Record<string, string>>;
  readonly expires: string;
  readonly seq: number;
  readonly revokedSeq: number | null;
  readonly acceptedSeq: number | null;
}

/** A workgroup of the org. */
export interface Workgroup {
  readonly id: string;
  readonly isDefault: boolean;
  readonly createdSeq: number;
  readonly archivedSeq: number | null;
}

export interface OrgStateFields {
  readonly org: string;
  /** The pinned founder signing key (b64u) of `genesis` (L3). */
  readonly founder: string;
  readonly defaultWg: string;
  readonly private: boolean;
  readonly seq: number;
  readonly head: string;
  readonly principals: ReadonlyMap<string, Principal>;
  readonly invites: ReadonlyMap<string, Invite>;
  readonly workgroups: ReadonlyMap<string, Workgroup>;
  /** Explicit `pairKey(user, wg) -> read|write`; the default workgroup is derived and never stored here. */
  readonly access: ReadonlyMap<string, string>;
  /** `pairKey(device, wg) -> seq` of the explicit assignment. */
  readonly assignments: ReadonlyMap<string, number>;
  /** `device -> seq` of its assignment to the whole organization (ADR-075). */
  readonly orgAssignments: ReadonlyMap<string, number>;
  /** `pairKey(scope, scope_id) -> epoch`. */
  readonly epochs: ReadonlyMap<string, number>;
  /** `"scope scope_id epoch" -> principal` that created the epoch (genesis, `wg_create`, `rotate`). */
  readonly epochAuthors: ReadonlyMap<string, string>;
  /** `pairKey(scope, scope_id)` of scopes whose rotation is still owed (a member left by itself, decision CRY-02, or an
   *  admin demoted itself, ADR-058 section 3). */
  readonly pending: ReadonlySet<string>;
  /** `pairKey(scope, scope_id) -> user`: the rotation of a scope its agent read, owed by the user that removed or
   *  replaced that agent (ADR-082 section 4); the user or its active agent may rotate it without admin or write right. */
  readonly agentOwed: ReadonlyMap<string, string>;
}

/** `device -> workgroups` of every key a store of assignments ever inserted, extended as the store grows. */
const storeIndexes = new WeakMap<Store<string, number>, { seen: number; index: Map<string, string[]> }>();

function storeIndex(store: Store<string, number>): ReadonlyMap<string, readonly string[]> {
  let found = storeIndexes.get(store);
  if (found === undefined) {
    found = { seen: 0, index: new Map() };
    storeIndexes.set(store, found);
  }
  for (; found.seen < store.inserts.length; found.seen += 1) {
    const [device, wg] = splitKey(store.inserts[found.seen]![1]);
    const wgs = found.index.get(device);
    if (wgs === undefined) found.index.set(device, [wg]);
    else if (!wgs.includes(wg)) wgs.push(wg);
  }
  return found.index;
}

/** `user -> agents` of every principal a store of principals ever wrote with a `parent`, extended as the store grows. */
const agentIndexes = new WeakMap<Store<string, Principal>, { seen: number; index: Map<string, string[]> }>();

function agentIndex(principals: ReadonlyMap<string, Principal>): ReadonlyMap<string, readonly string[]> {
  // the constructor keeps every map as a shared map
  const store = (principals as VersionedMap<string, Principal>).store;
  let found = agentIndexes.get(store);
  if (found === undefined) {
    found = { seen: 0, index: new Map() };
    agentIndexes.set(store, found);
  }
  for (; found.seen < store.writes.length; found.seen += 1) {
    const id = store.writes[found.seen]!;
    // the principals of a log are never deleted; a user or device has the parent null
    const parent = (store.at(id, found.seen + 1) as Partial<Principal>).parent;
    if (typeof parent !== 'string') continue;
    const agents = found.index.get(parent);
    if (agents === undefined) found.index.set(parent, [id]);
    else if (!agents.includes(id)) agents.push(id);
  }
  return found.index;
}

/** The key of an epoch author. */
export const epochKey = (scope: string, scopeId: string, epoch: number): string => `${scope} ${scopeId} ${epoch}`;

/** Rights after the entry `seq` (whose hash is `head`). */
export class OrgState implements OrgStateFields {
  declare readonly org: string;
  declare readonly founder: string;
  declare readonly defaultWg: string;
  declare readonly private: boolean;
  declare readonly seq: number;
  declare readonly head: string;
  declare readonly principals: ReadonlyMap<string, Principal>;
  declare readonly invites: ReadonlyMap<string, Invite>;
  declare readonly workgroups: ReadonlyMap<string, Workgroup>;
  declare readonly access: ReadonlyMap<string, string>;
  declare readonly assignments: ReadonlyMap<string, number>;
  declare readonly orgAssignments: ReadonlyMap<string, number>;
  declare readonly epochs: ReadonlyMap<string, number>;
  declare readonly epochAuthors: ReadonlyMap<string, string>;
  declare readonly pending: ReadonlySet<string>;
  declare readonly agentOwed: ReadonlyMap<string, string>;

  /** Every map of `fields` is kept as an immutable shared map (a plain map is copied, section "maps shared"). */
  constructor(fields: OrgStateFields) {
    Object.assign(this, fields, {
      principals: sharedMap(fields.principals),
      invites: sharedMap(fields.invites),
      workgroups: sharedMap(fields.workgroups),
      access: sharedMap(fields.access),
      assignments: sharedMap(fields.assignments),
      orgAssignments: sharedMap(fields.orgAssignments),
      epochs: sharedMap(fields.epochs),
      epochAuthors: sharedMap(fields.epochAuthors),
      agentOwed: sharedMap(fields.agentOwed),
    });
    Object.freeze(this);
  }

  /** A copy with some fields replaced (the state transitions). */
  with(changes: Partial<OrgStateFields>): OrgState {
    return new OrgState({ ...this.fields(), ...changes });
  }

  private fields(): OrgStateFields {
    return { ...this };
  }

  // --- principals and roles ---

  /** The active principal `principal`, else null (an agent is removed with its user, so its parent is active). */
  member(principal: string): Principal | null {
    const found = this.principals.get(principal);
    return found !== undefined && found.removedSeq === null ? found : null;
  }

  /** The principal whose rights `principal` uses: the parent of an agent, else the principal itself. */
  acting(principal: string): string {
    return this.principals.get(principal)?.parent ?? principal;
  }

  /** The active agent of `user`, else null (a user has at most one: a new agent replaces the active one). */
  activeAgent(user: string): Principal | null {
    return this.agentsOf(user).find((p) => this.member(p.id) !== null) ?? null;
  }

  /** Every agent (active or not) of `user`, in the order they were added (an index of the store of principals, which
   *  also knows agents of later states, not a scan of every principal). */
  agentsOf(user: string): Principal[] {
    return (agentIndex(this.principals).get(user) ?? [])
      .map((id) => this.principals.get(id))
      .filter((p): p is Principal => p?.parent === user);
  }

  /** Active admin, or an active agent of one. */
  isAdmin(principal: string): boolean {
    return this.member(principal) !== null && this.member(this.acting(principal))?.role === ADMIN;
  }

  /** Active admins, sorted. */
  admins(): string[] {
    return [...this.principals.values()]
      .filter((p) => p.removedSeq === null && p.role === ADMIN)
      .map((p) => p.id)
      .sort();
  }

  // --- workgroups ---

  /** `read`/`write` of an active user on a workgroup from membership alone (default workgroup: `write`), else null;
   *  the admin role adds nothing here (credential scope, ADR-058 section 1). */
  explicitAccess(principal: string, wg: string): string | null {
    if (this.member(principal) === null) return null;
    const user = this.acting(principal);
    if (this.member(user)?.kind !== USER || !this.workgroups.has(wg)) return null;
    if (wg === this.defaultWg) return WRITE;
    return this.access.get(pairKey(user, wg)) ?? null;
  }

  /** The effective `read`/`write` of an active user on a workgroup: the explicit access, else `read` for an active
   *  admin of the org (ADR-058), else null. */
  wgAccess(principal: string, wg: string): string | null {
    const explicit = this.explicitAccess(principal, wg);
    if (explicit === null && this.workgroups.has(wg) && this.isAdmin(principal)) return READ;
    return explicit;
  }

  /** The user reads the workgroup only because it is an admin (no explicit access). */
  inherited(principal: string, wg: string): boolean {
    return this.explicitAccess(principal, wg) === null && this.wgAccess(principal, wg) !== null;
  }

  /** An active device assigned to the whole organization (ADR-075). */
  orgAssigned(device: string): boolean {
    return this.member(device) !== null && this.orgAssignments.has(device);
  }

  /** An active device assigned to the workgroup, explicitly or through the organization. */
  assigned(device: string, wg: string): boolean {
    if (this.member(device) === null) return false;
    return this.assignments.has(pairKey(device, wg)) || (this.orgAssignments.has(device) && this.workgroups.has(wg));
  }

  /** Workgroups an active device is assigned to, sorted. */
  deviceWgs(device: string): string[] {
    if (this.member(device) === null) return [];
    if (this.orgAssignments.has(device)) return [...this.workgroups.keys()].sort();
    // every workgroup the store ever assigned the device to that is assigned in this version (the constructor keeps
    // every map as a shared map)
    const assignments = this.assignments as VersionedMap<string, number>;
    return (storeIndex(assignments.store as Store<string, number>).get(device) ?? [])
      .filter((wg) => assignments.has(pairKey(device, wg)))
      .sort();
  }

  /** An existing workgroup that is not archived. */
  writable(wg: string): boolean {
    const found = this.workgroups.get(wg);
    return found !== undefined && found.archivedSeq === null;
  }

  /** `wg:write` of a user: write access and the workgroup not archived. */
  canWriteWg(principal: string, wg: string): boolean {
    return this.wgAccess(principal, wg) === WRITE && this.writable(wg);
  }

  // --- scopes ---

  /** Read right on an `org` or `wg` scope (the entitlement to its key). */
  canRead(principal: string, scope: string, scopeId: string): boolean {
    const found = this.member(principal);
    if (found === null) return false;
    const device = found.kind === DEVICE;
    if (scope === ORG) return scopeId === this.org && (!device || this.deviceWgs(principal).length > 0);
    if (!device) return this.wgAccess(principal, scopeId) !== null;
    return this.assigned(principal, scopeId);
  }

  /** Principals entitled to the scope key, sorted; `excluded` drops e.g. decertified platform devices. */
  readers(scope: string, scopeId: string, excluded: Iterable<string> = []): string[] {
    const skip = new Set(excluded);
    return [...this.principals.keys()].filter((p) => !skip.has(p) && this.canRead(p, scope, scopeId)).sort();
  }

  /** Every `org`/`wg` scope of the org, sorted (org first). */
  scopes(): Scope[] {
    return [...this.epochs.keys()].map(splitKey).sort(compareScopes);
  }

  /** Current epoch of a scope (0 for an unknown scope). */
  epoch(scope: string, scopeId: string): number {
    return this.epochs.get(pairKey(scope, scopeId)) ?? 0;
  }

  /** Every scope the principal may read, sorted. */
  readableScopes(principal: string): Scope[] {
    return this.scopes().filter(([scope, id]) => this.canRead(principal, scope, id));
  }

  /** Scopes whose rotation is owed, sorted. */
  pendingScopes(): Scope[] {
    return [...this.pending].map(splitKey).sort(compareScopes);
  }

  /** A JSON view of the state (vectors and the hub projections of 02-data-model.md section 2.2). */
  projection(): Record<string, unknown> {
    const ids = [...this.principals.keys()].sort();
    const byId = <T>(items: Iterable<T>, id: (item: T) => string) =>
      [...items].sort((a, b) => compareText(id(a), id(b)));
    return {
      org: this.org,
      default_wg: this.defaultWg,
      private: this.private,
      head: { seq: this.seq, hash: this.head },
      members: byId(this.principals.values(), (p) => p.id).map((p) => ({
        principal_id: p.id,
        kind: p.kind,
        role: p.role,
        joined_seq: p.joinedSeq,
        removed_seq: p.removedSeq,
        platform_seq: p.platformSeq,
        consented: p.consented,
        ...(p.joinAgent === null ? {} : { join_agent: p.joinAgent }),
        ...(p.parent === null
          ? {}
          : {
              parent: p.parent,
              gen: p.gen,
              removed_with_user: p.removedWithUser,
              removed_by_owner: p.removedByOwner,
            }),
        ...(this.orgAssignments.has(p.id) ? { org_assigned_seq: this.orgAssignments.get(p.id) } : {}),
      })),
      workgroups: byId(this.workgroups.values(), (wg) => wg.id).map((wg) => ({
        wg_id: wg.id,
        is_default: wg.isDefault,
        created_seq: wg.createdSeq,
        archived_seq: wg.archivedSeq,
        epoch: this.epoch(WG, wg.id),
        access: Object.fromEntries(
          ids.flatMap((p) => {
            const level = this.wgAccess(p, wg.id);
            return level === null ? [] : [[p, level]];
          }),
        ),
        inherited: ids.filter((p) => this.inherited(p, wg.id)),
        devices: ids.filter((p) => this.assigned(p, wg.id)),
      })),
      org_epoch: this.epoch(ORG, this.org),
      invites: byId(this.invites.values(), (inv) => inv.inviteId).map((inv) => ({
        invite_id: inv.inviteId,
        kind: inv.kind,
        seq: inv.seq,
        revoked_seq: inv.revokedSeq,
        accepted_seq: inv.acceptedSeq,
      })),
      pending_rotation: this.pendingScopes().map(([scope, id]) => ({ scope, scope_id: id })),
      ...(this.agentOwed.size === 0
        ? {}
        : {
            agent_owed: [...this.agentOwed]
              .sort(([a], [b]) => compareScopes(splitKey(a), splitKey(b)))
              .map(([key, user]) => ({ scope: splitKey(key)[0], scope_id: splitKey(key)[1], user })),
          }),
    };
  }
}

/**
 * The principals whose rights may differ between two states of one log, or null for "any". Every right of a
 * principal – `member`, `acting`, `isAdmin`, `admins`, `explicitAccess`, `wgAccess`, `inherited`, `orgAssigned`,
 * `assigned`, `deviceWgs`, `writable`, `canWriteWg`, `canRead`, `readers`, and `credDevice` of `wraps.ts` – is derived
 * from exactly these fields: the org and the default workgroup (fixed by `genesis`), the workgroups (created,
 * archived), the principal's own entry in `principals` (kind, role, `removedSeq`, `platformSeq`, `consented`, and for
 * an agent `parent`), its own keys in `access` and `assignments` (`pairKey(principal, …)`) and its own key in
 * `orgAssignments` (`device_assign_org` and `device_unassign_org` change nothing else). An agent (a sub-principal) has
 * no role, access or assignment of its own: its rights resolve through its parent (`acting`), so every agent of a
 * named principal is named as well (an agent added, replaced or revoked writes its own entry; a parent whose role,
 * access or membership changes names all its agents). The user that owes the rotation of a scope (`agentOwed`) and its
 * agents are named when that changes (the right to rotate without admin or write right). The other fields
 * (`invites`, `epochs`, `epochAuthors`, `pending`) grant no right; the epochs of a scope are read per state by their
 * users (`epochHolders`), decertified platform devices are passed as exclusions per call. Which agent of a user is
 * revoked (`wraps.revokedAgents`) and whether the user's devices count for its credential key depend on the user and
 * all its agents: a caller asking that (`credentials/rotation.ts`) asks everything again when the user or one of its
 * agents is named. So a principal that is not named here has the same rights in both states. Another org or default
 * workgroup, a change of the workgroups, or maps that share nothing known give null (the caller asks every principal
 * again). A new field that rights depend on must be added here.
 */
export function changedPrincipals(a: OrgState, b: OrgState): Set<string> | null {
  if (a.org !== b.org || a.defaultWg !== b.defaultWg || a.workgroups !== b.workgroups) return null;
  const principals = changedKeys(a.principals, b.principals);
  const access = changedKeys(a.access, b.access);
  const assignments = changedKeys(a.assignments, b.assignments);
  const orgAssignments = changedKeys(a.orgAssignments, b.orgAssignments);
  const owed = changedKeys(a.agentOwed, b.agentOwed);
  if (principals === null || access === null || assignments === null || orgAssignments === null || owed === null)
    return null;
  const found = new Set([...principals, ...orgAssignments]);
  for (const key of [...access, ...assignments]) found.add(splitKey(key)[0]);
  for (const key of owed) {
    for (const user of [a.agentOwed.get(key), b.agentOwed.get(key)]) if (user !== undefined) found.add(user);
  }
  // the sub-principals of every named principal (both states share the store of principals, so one index)
  const agents = agentIndex(b.principals);
  for (const principal of [...found]) for (const agent of agents.get(principal) ?? []) found.add(agent);
  return found;
}

/**
 * The rotation obligation R of 04-crypto.md section 4.7: `R(before, after, subject)` = { (`wg`, w) | subject could
 * read workgroup w before and cannot after } ∪ { (`org`, org) | subject could read the org scope before and cannot
 * after }. The `cred` scope is never in R. As `pairKey(scope, scope_id)` values.
 */
export function requiredSet(before: OrgState, after: OrgState, subject: string): Set<string> {
  const lost = new Set<string>();
  for (const wg of before.workgroups.keys()) {
    if (before.canRead(subject, WG, wg) && !after.canRead(subject, WG, wg)) lost.add(pairKey(WG, wg));
  }
  if (before.canRead(subject, ORG, before.org) && !after.canRead(subject, ORG, after.org)) {
    lost.add(pairKey(ORG, before.org));
  }
  return lost;
}

/** The principal an entry is about: `subject`, else `device`, else its author `by`. */
export function entrySubject(body: Readonly<Record<string, unknown>>): string {
  if (Object.hasOwn(body, 'subject')) return String(body.subject);
  if (Object.hasOwn(body, 'device')) return String(body.device);
  return String(body.by);
}

/**
 * The principals that may lose access by an entry (ADR-082): its subject and the active agent (in `before`) of the
 * subject, or for `agent_add` of the agent's user (that agent is replaced).
 */
export function losers(before: OrgState, body: Readonly<Record<string, unknown>>): Set<string> {
  const subject = entrySubject(body);
  const agent = before.activeAgent(String(body.user ?? subject));
  return new Set(agent === null ? [subject] : [subject, agent.id]);
}

/** The union of R over `losers`. */
export function lost(before: OrgState, after: OrgState, body: Readonly<Record<string, unknown>>): Set<string> {
  return new Set([...losers(before, body)].flatMap((loser) => [...requiredSet(before, after, loser)]));
}

/** Entry types whose R is owed by the others when the subject is the author itself (or the user of the author). */
export const SELF_OWED: ReadonlySet<string> = new Set(['remove', 'role']);

/**
 * R is owed by the others (`OrgState.pending`): `remove`/`role` whose subject is the author or the author's user
 * (a member leaving or an admin demoting itself, also through its agent).
 */
export function selfOwed(before: OrgState, body: Readonly<Record<string, unknown>>): boolean {
  const by = String(body.by);
  return SELF_OWED.has(body.type as string) && (body.subject === by || body.subject === before.acting(by));
}
