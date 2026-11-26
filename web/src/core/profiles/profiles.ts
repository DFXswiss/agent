// Rights profiles of sessions (ADR-094; 03-api.md section 4.13): a voluntary restriction - without one a session may
// do what its human may (A1, E-11).
//
// - `profileOf(value)`: a well-formed `agent_profile` snapshot of a task or session row (kept as received), else null.
// - `sessionRole(sessions, session, taskProfile)`: the name of the profile a session works with - its own (a child
//   started with one), else the nearest parent's, else its task's; null without any (every right).
// - `listProfiles` (`GET /v1/workgroups/{wg}/agent-profiles`): the built-in roles and the own profiles usable in the
//   workgroup; `saveProfile` / `deleteProfile` (`PUT`/`DELETE …/agent-profiles/{name}` of a workgroup or an
//   organization); `setTaskProfile` (`PUT /v1/tasks/{task}/agent-profile`; the task row in the store follows).
import type { RequestsContext } from '../requests/client';
import { isRecord } from '../registry';
import { putTasks, type SessionEntry } from '../store/slices/tasks';

export const NAME_RE = /^[a-z][a-z0-9_-]{0,39}$/;
export const REACHES = ['task', 'workgroup', 'all'] as const;
export type Reach = (typeof REACHES)[number];
/** The built-in roles (derived from the operation classes by the hub; `analysis`: ADR-095 section 2). */
export const ROLES = ['read_only', 'development', 'security', 'review', 'merge', 'analysis'] as const;

export interface AgentProfile {
  readonly name: string;
  readonly operations: readonly string[];
  readonly reads: Reach;
}

export interface ListedProfile extends AgentProfile {
  readonly scope: 'builtin' | 'org' | 'wg';
  readonly updated_at: string | null;
}

export interface ProfileList {
  readonly profiles: readonly ListedProfile[];
  readonly classes: Readonly<Record<string, string>>;
}

const isReach = (value: unknown): value is Reach => REACHES.includes(value as Reach);

/** A well-formed snapshot, else null. */
export function profileOf(value: unknown): AgentProfile | null {
  if (!isRecord(value)) return null;
  const { name, operations, reads } = value;
  if (typeof name !== 'string' || !NAME_RE.test(name) || !isReach(reads)) return null;
  if (!Array.isArray(operations) || !operations.every((item) => typeof item === 'string')) return null;
  return { name, operations, reads };
}

/** The text of a profile: its name, else `none` (every right, E-11). */
export function profileLabel(profile: AgentProfile | null, none: string): string {
  return profile === null ? none : profile.name;
}

/** The profile name a session works with (module comment). */
export function sessionRole(
  sessions: Readonly<Record<string, SessionEntry>>,
  session: string,
  taskProfile: unknown,
): string | null {
  const seen = new Set<string>();
  let current: string | null = session;
  while (current !== null && !seen.has(current)) {
    seen.add(current);
    const row: Readonly<Record<string, unknown>> | undefined = sessions[current]?.row as
      Readonly<Record<string, unknown>> | undefined;
    if (row === undefined) break;
    const own = profileOf(row.agent_profile);
    if (own !== null) return own.name;
    current = typeof row.parent_session_id === 'string' ? row.parent_session_id : null;
  }
  return profileOf(taskProfile)?.name ?? null;
}

function listed(value: unknown): ListedProfile | null {
  const profile = profileOf(
    isRecord(value) ? { name: value.name, operations: value.operations, reads: value.reads } : null,
  );
  if (profile === null || !isRecord(value)) return null;
  const { scope, updated_at: updated } = value;
  if (scope !== 'builtin' && scope !== 'org' && scope !== 'wg') return null;
  return { ...profile, scope, updated_at: typeof updated === 'string' ? updated : null };
}

/** `GET /v1/workgroups/{wg}/agent-profiles` (entries that do not fit the contract are left out). */
export async function listProfiles(context: RequestsContext, wg: string): Promise<ProfileList> {
  const answer = await context.client.request('GET', `/v1/workgroups/${encodeURIComponent(wg)}/agent-profiles`);
  const profiles = isRecord(answer) && Array.isArray(answer.profiles) ? answer.profiles : [];
  const classes = isRecord(answer) && isRecord(answer.classes) ? answer.classes : {};
  return {
    profiles: profiles.map(listed).filter((item): item is ListedProfile => item !== null),
    classes: Object.fromEntries(
      Object.entries(classes).filter((entry): entry is [string, string] => typeof entry[1] === 'string'),
    ),
  };
}

/** `PUT …/agent-profiles/{name}` of a workgroup (writers) or an organization (admins). */
export async function saveProfile(
  context: RequestsContext,
  scope: 'org' | 'wg',
  id: string,
  profile: AgentProfile,
): Promise<void> {
  const body = { body: { operations: [...new Set(profile.operations)].sort(), reads: profile.reads } };
  const [owner, name] = [encodeURIComponent(id), encodeURIComponent(profile.name)];
  if (scope === 'org') await context.client.request('PUT', `/v1/orgs/${owner}/agent-profiles/${name}`, body);
  else await context.client.request('PUT', `/v1/workgroups/${owner}/agent-profiles/${name}`, body);
}

/** `DELETE …/agent-profiles/{name}`; assignments keep their snapshot. */
export async function deleteProfile(context: RequestsContext, scope: 'org' | 'wg', id: string, name: string) {
  const [owner, profile] = [encodeURIComponent(id), encodeURIComponent(name)];
  if (scope === 'org') await context.client.request('DELETE', `/v1/orgs/${owner}/agent-profiles/${profile}`);
  else await context.client.request('DELETE', `/v1/workgroups/${owner}/agent-profiles/${profile}`);
}

/** `PUT /v1/tasks/{task}/agent-profile` (null clears it); the task row in the store follows. */
export async function setTaskProfile(context: RequestsContext, task: string, name: string | null): Promise<void> {
  const generation = context.store.generation;
  const answer = await context.client.request('PUT', `/v1/tasks/${encodeURIComponent(task)}/agent-profile`, {
    body: { profile: name },
  });
  const row = context.store.getState().tasks.tasks[task];
  if (row !== undefined && context.store.generation === generation) {
    putTasks(context.store, [{ ...row, agent_profile: isRecord(answer) ? profileOf(answer.profile) : null }]);
  }
}
