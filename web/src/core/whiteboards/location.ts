// Where a whiteboard lives (KONZEPT section 12, 04-crypto.md section 4.3, 03-api.md section 4.9): exactly one board on
// each of the three levels – the organization (events in the org scope, `task` and `wg` empty), a workgroup (the
// workgroup scope, `task` empty) and a task (the scope of the task's workgroup, `task` set). There is no project level.
//
// Rights, as the hub enforces them and as the UI mirrors them (06-web.md section 1: controls without the right are not
// rendered): reading the org board needs org membership, reading a workgroup or task board read access to the
// workgroup; writing (create, edit, delete) needs `org:admin` on the org level and `wg:write` below (an archived
// workgroup is not writable).
import { ORG, type OrgState, WG } from '../log/state';

export type Level = 'org' | 'workgroup' | 'task';

export const LEVELS: readonly Level[] = ['org', 'workgroup', 'task'];

export interface BoardLocation {
  readonly level: Level;
  readonly org: string;
  /** The workgroup (workgroup and task level), null on the org level. */
  readonly wg: string | null;
  /** The task (task level), null otherwise. */
  readonly task: string | null;
}

export const orgBoard = (org: string): BoardLocation => ({ level: 'org', org, wg: null, task: null });
export const wgBoard = (org: string, wg: string): BoardLocation => ({ level: 'workgroup', org, wg, task: null });
export const taskBoard = (org: string, wg: string, task: string): BoardLocation => ({ level: 'task', org, wg, task });

/** A stable key of a location (store maps). */
export function boardKey(location: BoardLocation): string {
  return [location.level, location.org, location.wg ?? '', location.task ?? ''].join(' ');
}

/** The event scope of a board: `org` with the org ID, else `wg` with the workgroup ID. */
export function boardScope(location: BoardLocation): { readonly scope: string; readonly scopeId: string } {
  return location.level === 'org' ? { scope: ORG, scopeId: location.org } : { scope: WG, scopeId: location.wg! };
}

/** The level of a whiteboard event by its header (the hub derives it the same way). */
export function levelOfHeader(header: { readonly scope?: unknown; readonly task?: unknown }): Level {
  if (header.scope === ORG) return 'org';
  return header.task === '' ? 'workgroup' : 'task';
}

/** Whether an event header (checked or not) belongs to the board at `location`. */
export function onBoard(header: Readonly<Record<string, unknown>>, location: BoardLocation): boolean {
  const { scope, scopeId } = boardScope(location);
  return (
    header.org === location.org &&
    header.scope === scope &&
    header.scope_id === scopeId &&
    levelOfHeader(header) === location.level &&
    (location.level !== 'task' || header.task === location.task)
  );
}

/** Whether `user` may read the board (null state: the log is not verified yet → no). */
export function canReadBoard(state: OrgState | null, user: string | null, location: BoardLocation): boolean {
  if (state === null || user === null || state.org !== location.org) return false;
  if (location.level === 'org') return state.canRead(user, ORG, location.org);
  return state.canRead(user, WG, location.wg!);
}

/** Whether `user` may create, edit and delete entries of the board. */
export function canWriteBoard(state: OrgState | null, user: string | null, location: BoardLocation): boolean {
  if (state === null || user === null || state.org !== location.org) return false;
  if (location.level === 'org') return state.isAdmin(user);
  return state.canWriteWg(user, location.wg!);
}

/** The route of a board's screen (the task board is a tab of the task page). */
export function boardPath(location: BoardLocation): string {
  const org = `/o/${encodeURIComponent(location.org)}`;
  if (location.level === 'org') return `${org}/whiteboard`;
  if (location.level === 'workgroup') return `${org}/w/${encodeURIComponent(location.wg!)}/whiteboard`;
  return `${org}/t/${encodeURIComponent(location.task!)}?tab=whiteboard`;
}
