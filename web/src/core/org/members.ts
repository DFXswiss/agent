// Members and rights of an org (06-web.md section 7.1; 04-crypto.md sections 4.7, 5.10): the rows of the member list
// from the verified log (display names from `member.profile`, never from the hub), what the signed-in user may do, and
// the planners of every change. A planner re-checks the change against the log it is given (after `seq_conflict` the
// re-read log): a change another admin already made is "nothing to do" (null), a change that is no longer allowed is
// refused with the code the log rules would give.
//
// Rights mirror the log rules (04-crypto.md section 4.7): role, grant, ungrant and remove of others need an admin;
// the last admin can neither be demoted nor removed nor leave (`last_admin`); the default workgroup's write access is
// not reducible (ADR-024 addendum: no `ungrant`, no `grant` on it).
import { ADMIN, MEMBER, type OrgState, READ, USER, WRITE } from '../log/state';
import { type Directory, selectDirectory } from '../tasks/directory';
import type { StoreState } from '../store/store';
import { type Planner, refuse } from './batch';

export type Access = typeof READ | typeof WRITE;
export type Role = typeof ADMIN | typeof MEMBER;

/** A workgroup a member has access to. */
export interface MemberAccess {
  readonly wg: string;
  readonly access: Access;
  readonly isDefault: boolean;
}

/** One row of the member list. */
export interface MemberRow {
  readonly id: string;
  /** Display name from the newest verified `member.profile` of the member, null if none was read. */
  readonly name: string | null;
  readonly role: Role;
  /** Workgroups with access (default workgroup first, then by ID), archived ones included. */
  readonly access: readonly MemberAccess[];
  readonly isMe: boolean;
  /** The only admin: the role control and removal are hidden (the hub would answer `last_admin`). */
  readonly lastAdmin: boolean;
  readonly joinedSeq: number;
}

/** The only remaining admin. */
export function isLastAdmin(state: OrgState, subject: string): boolean {
  return state.isAdmin(subject) && state.admins().length === 1;
}

/** Workgroups ordered: the default workgroup first, then by ID. */
export function workgroupOrder(state: OrgState): string[] {
  return [...state.workgroups.keys()].sort(
    (a, b) => Number(b === state.defaultWg) - Number(a === state.defaultWg) || Number(a > b) - Number(a < b),
  );
}

/** The active users of the org, admins first, then by name (unnamed last) and ID. */
export function memberRows(state: OrgState, me: string, directory: Directory): MemberRow[] {
  const order = workgroupOrder(state);
  const rows = [...state.principals.values()]
    .filter((p) => p.kind === USER && p.removedSeq === null)
    .map((p): MemberRow => ({
      id: p.id,
      name: directory.members.get(p.id) ?? null,
      role: p.role as Role,
      access: order.flatMap((wg) => {
        const access = state.wgAccess(p.id, wg) as Access | null;
        return access === null ? [] : [{ wg, access, isDefault: wg === state.defaultWg }];
      }),
      isMe: p.id === me,
      lastAdmin: isLastAdmin(state, p.id),
      joinedSeq: p.joinedSeq,
    }));
  const rank = (row: MemberRow) => Number(row.role !== ADMIN);
  const name = (row: MemberRow) => row.name ?? '￿';
  return rows.sort(
    (a, b) => rank(a) - rank(b) || name(a).localeCompare(name(b)) || Number(a.id > b.id) - Number(a.id < b.id),
  );
}

/** Admin controls (invite, role, grant, ungrant, remove) are rendered only for admins (06-web.md section 1.2). */
export function canManage(state: OrgState, me: string): boolean {
  return state.isAdmin(me);
}

/** Workgroups `subject` can still be granted (not the default one, not archived) with the access it has now. */
export function grantable(state: OrgState, subject: string): { wg: string; access: Access | null }[] {
  return workgroupOrder(state)
    .filter((wg) => wg !== state.defaultWg && state.writable(wg))
    .map((wg) => ({ wg, access: state.wgAccess(subject, wg) as Access | null }));
}

function requireAdmin(state: OrgState, me: string): void {
  if (!state.isAdmin(me)) refuse('forbidden');
}

function requireUser(state: OrgState, subject: string): void {
  if (state.member(subject)?.kind !== USER) refuse('not_found');
}

/** `role`: null once the subject has the role; `last_admin` for demoting the only admin. */
export function rolePlan(subject: string, role: Role): Planner {
  return (log, me) => {
    const state = log.state;
    requireAdmin(state, me);
    requireUser(state, subject);
    if (state.member(subject)!.role === role) return null;
    if (isLastAdmin(state, subject)) refuse('last_admin');
    return { entries: [{ type: 'role', subject, role }] };
  };
}

/** `grant` of `access` on `wg` (optionally with the older epochs); null once the subject has exactly that access. */
export function grantPlan(subject: string, wg: string, access: Access, history = false): Planner {
  return (log, me) => {
    const state = log.state;
    requireAdmin(state, me);
    requireUser(state, subject);
    if (wg === state.defaultWg || !state.writable(wg)) refuse('forbidden');
    if (state.wgAccess(subject, wg) === access) return null;
    return { entries: [{ type: 'grant', subject, wg, access }], history };
  };
}

/** `ungrant` (+ R: the workgroup's rotation); null once the subject has no access. */
export function ungrantPlan(subject: string, wg: string): Planner {
  return (log, me) => {
    const state = log.state;
    requireAdmin(state, me);
    if (wg === state.defaultWg) refuse('forbidden');
    if (state.member(subject) === null || state.wgAccess(subject, wg) === null) return null;
    return { entries: [{ type: 'ungrant', subject, wg }] };
  };
}

/** `remove` of another member (+ R: the org and every workgroup it read); null once it is gone. */
export function removePlan(subject: string): Planner {
  return (log, me) => {
    const state = log.state;
    requireAdmin(state, me);
    if (subject === me) refuse('forbidden');
    // (another admin remains: the actor is one, and removing oneself is leaving)
    if (state.member(subject) === null) return null;
    return { entries: [{ type: 'remove', subject }] };
  };
}

/**
 * Leaving the org (a self `remove`): the rotation of what the user could read is owed by the remaining admins and
 * writers (`rotation_pending`, the pending-rotation job), so the batch is the entry alone.
 */
export function leavePlan(): Planner {
  return (log, me) => {
    const state = log.state;
    if (state.member(me) === null) return null;
    if (isLastAdmin(state, me)) refuse('last_admin');
    return { entries: [{ type: 'remove', subject: me }] };
  };
}

/** The user's display name in `org` (newest verified `member.profile`), '' if unknown (no org, not read yet). */
export const selectOwnName =
  (org: string | null, userId: string | null) =>
  (state: StoreState): string =>
    org === null || userId === null ? '' : (selectDirectory(org)(state).members.get(userId) ?? '');
