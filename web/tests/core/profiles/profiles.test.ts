// Rights profiles in the web core (ADR-090): snapshots, the role of a session along its parents and its task, the
// profile list, own profiles of a workgroup or an organization and the profile of a task.
import { describe, expect, it } from 'vitest';
import {
  deleteProfile,
  listProfiles,
  profileOf,
  saveProfile,
  sessionRole,
  setTaskProfile,
} from '../../../src/core/profiles/profiles';
import { putTasks, type SessionEntry, type TaskRow } from '../../../src/core/store/slices/tasks';
import { taskRow, taskWorld } from '../tasks/world';

const REVIEW = { name: 'review', operations: ['review.verdict'], reads: 'all' };

const entry = (id: string, parent: string | null, profile: unknown = null): SessionEntry =>
  ({
    row: { session_id: id, parent_session_id: parent, agent_profile: profile },
    transitions: [],
    estimatedAt: null,
  }) as unknown as SessionEntry;

describe('profiles', () => {
  it('reads well-formed snapshots only', () => {
    expect(profileOf(REVIEW)).toEqual(REVIEW);
    for (const bad of [
      null,
      'review',
      { ...REVIEW, name: 'Bad' },
      { ...REVIEW, reads: 'some' },
      { ...REVIEW, operations: 'x' },
      { ...REVIEW, operations: [1] },
    ]) {
      expect(profileOf(bad)).toBeNull();
    }
  });

  it('names the role of a session from itself, its parents or its task', () => {
    const sessions = {
      main: entry('main', null),
      child: entry('child', 'main', { ...REVIEW, name: 'merge' }),
      grandchild: entry('grandchild', 'child'),
      loop: entry('loop', 'loop'),
      orphan: entry('orphan', 'gone'),
    };
    expect(sessionRole(sessions, 'grandchild', null)).toBe('merge');
    expect(sessionRole(sessions, 'main', REVIEW)).toBe('review');
    expect(sessionRole(sessions, 'main', null)).toBeNull();
    expect(sessionRole(sessions, 'loop', REVIEW)).toBe('review');
    expect(sessionRole(sessions, 'orphan', null)).toBeNull();
    expect(sessionRole(sessions, 'unknown', 'broken')).toBeNull();
  });

  it('lists, saves and deletes profiles', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}/agent-profiles`, () => ({
      profiles: [
        { ...REVIEW, scope: 'builtin', updated_at: null },
        { ...REVIEW, name: 'triage', scope: 'wg', updated_at: '2026-11-25T10:00:00.000Z' },
        { ...REVIEW, scope: 'elsewhere', updated_at: null },
        { name: 'Bad', operations: [], reads: 'all', scope: 'wg' },
        'junk',
      ],
      classes: { 'review.verdict': 'review', bad: 1 },
    }));
    const found = await listProfiles(t.context, t.wg);
    expect(found.profiles.map((item) => [item.name, item.scope, item.updated_at])).toEqual([
      ['review', 'builtin', null],
      ['triage', 'wg', '2026-11-25T10:00:00.000Z'],
    ]);
    expect(found.classes).toEqual({ 'review.verdict': 'review' });
    t.client.on(`GET /v1/workgroups/${t.org}/agent-profiles`, () => 'nothing');
    expect(await listProfiles(t.context, t.org)).toEqual({ profiles: [], classes: {} });
    t.client.on(`PUT /v1/workgroups/${t.wg}/agent-profiles/triage`, () => ({}));
    t.client.on(`PUT /v1/orgs/${t.org}/agent-profiles/auditor`, () => ({}));
    t.client.on(`DELETE /v1/orgs/${t.org}/agent-profiles/auditor`, () => ({}));
    await saveProfile(t.context, 'wg', t.wg, { name: 'triage', operations: ['b', 'a', 'b'], reads: 'task' });
    expect(t.client.of(`PUT /v1/workgroups/${t.wg}/agent-profiles/triage`)[0]!.body).toEqual({
      operations: ['a', 'b'],
      reads: 'task',
    });
    await saveProfile(t.context, 'org', t.org, { name: 'auditor', operations: [], reads: 'all' });
    await deleteProfile(t.context, 'org', t.org, 'auditor');
    expect(t.client.of(`DELETE /v1/orgs/${t.org}/agent-profiles/auditor`)).toHaveLength(1);
  });

  it('assigns the profile of a task and keeps the row in step', async () => {
    const t = await taskWorld();
    const task = taskRow(t) as TaskRow;
    putTasks(t.store, [task]);
    t.client.on(`PUT /v1/tasks/${task.task_id}/agent-profile`, (body) =>
      (body as { profile: string | null }).profile === null ? { profile: null } : { profile: REVIEW },
    );
    await setTaskProfile(t.context, task.task_id, 'review');
    expect(t.store.getState().tasks.tasks[task.task_id]!.agent_profile).toEqual(REVIEW);
    await setTaskProfile(t.context, task.task_id, null);
    expect(t.store.getState().tasks.tasks[task.task_id]!.agent_profile).toBeNull();
    t.client.on(`PUT /v1/tasks/unknown/agent-profile`, () => ({}));
    await setTaskProfile(t.context, 'unknown', 'review');
    expect(t.store.getState().tasks.tasks.unknown).toBeUndefined();
    t.client.on(`PUT /v1/tasks/${task.task_id}/agent-profile`, () => 'odd');
    await setTaskProfile(t.context, task.task_id, 'review');
    expect(t.store.getState().tasks.tasks[task.task_id]!.agent_profile).toBeNull();
    // a sign-out while the request ran: nothing is written afterwards
    putTasks(t.store, [task]);
    t.client.on(`PUT /v1/tasks/${task.task_id}/agent-profile`, () => {
      t.store.reset();
      putTasks(t.store, [task]);
      return { profile: REVIEW };
    });
    await setTaskProfile(t.context, task.task_id, 'review');
    expect(t.store.getState().tasks.tasks[task.task_id]!.agent_profile).toBeUndefined();
  });
});
