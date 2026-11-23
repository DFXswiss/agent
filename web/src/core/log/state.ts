// The rights state of one organization as derived from its verified membership log (04-crypto.md section 4.7).
// TypeScript twin of `agent_hub.log.state`.
//
// `OrgState` is immutable; `verify.apply` returns a new state per entry, so a reader keeps the state at every `seq`
// (E5 checks rights "at `lh`"). Derived rights (section 4.7, ADR-024 addendum):
// - an active user member reads the org scope and writes the default workgroup (not reducible in v1);
// - an admin writes the org scope but has no implicit workgroup access, except the creating admin of a workgroup
//   (`wg_create`) who gets `write` on it;
// - workgroup access of users comes from `invite.wgs` (applied at `accept`), `grant` and `ungrant`;
// - devices read the workgroups they are assigned to and the org scope while they have at least one assignment
//   (ADR-007);
// - an archived workgroup stays readable but is no longer writable (decision CRY-02).
// `remove` deletes every access and assignment of the subject; a former member who accepts a new invitation starts
// again from that invitation (rejoin).

export const USER = 'user';
export const DEVICE = 'device';
export const ADMIN = 'admin';
export const MEMBER = 'member';
export const READ = 'read';
export const WRITE = 'write';
export const ORG = 'org';
export const WG = 'wg';

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

/** A user or device of the org. `role` is `admin`/`member` for users and `device` for devices. */
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
}

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
  /** `pairKey(device, wg) -> seq` of the assignment. */
  readonly assignments: ReadonlyMap<string, number>;
  /** `pairKey(scope, scope_id) -> epoch`. */
  readonly epochs: ReadonlyMap<string, number>;
  /** `"scope scope_id epoch" -> principal` that created the epoch (genesis, `wg_create`, `rotate`). */
  readonly epochAuthors: ReadonlyMap<string, string>;
  /** `pairKey(scope, scope_id)` of scopes whose rotation is still owed (a member left by itself, decision CRY-02). */
  readonly pending: ReadonlySet<string>;
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
  declare readonly epochs: ReadonlyMap<string, number>;
  declare readonly epochAuthors: ReadonlyMap<string, string>;
  declare readonly pending: ReadonlySet<string>;

  constructor(fields: OrgStateFields) {
    Object.assign(this, fields);
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

  /** The active principal `principal`, else null. */
  member(principal: string): Principal | null {
    const found = this.principals.get(principal);
    return found !== undefined && found.removedSeq === null ? found : null;
  }

  /** Active admin. */
  isAdmin(principal: string): boolean {
    return this.member(principal)?.role === ADMIN;
  }

  /** Active admins, sorted. */
  admins(): string[] {
    return [...this.principals.values()]
      .filter((p) => p.removedSeq === null && p.role === ADMIN)
      .map((p) => p.id)
      .sort();
  }

  // --- workgroups ---

  /** `read`/`write` of an active user on a workgroup (default workgroup: `write`), else null. */
  wgAccess(principal: string, wg: string): string | null {
    const found = this.member(principal);
    if (found === null || found.kind !== USER || !this.workgroups.has(wg)) return null;
    if (wg === this.defaultWg) return WRITE;
    return this.access.get(pairKey(principal, wg)) ?? null;
  }

  /** An active device assigned to the workgroup. */
  assigned(device: string, wg: string): boolean {
    return this.member(device) !== null && this.assignments.has(pairKey(device, wg));
  }

  /** Workgroups an active device is assigned to, sorted. */
  deviceWgs(device: string): string[] {
    if (this.member(device) === null) return [];
    return [...this.assignments.keys()]
      .map(splitKey)
      .filter(([dev]) => dev === device)
      .map(([, wg]) => wg)
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
    if (scope === ORG) return scopeId === this.org && (found.kind === USER || this.deviceWgs(principal).length > 0);
    if (found.kind === USER) return this.wgAccess(principal, scopeId) !== null;
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
    };
  }
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
