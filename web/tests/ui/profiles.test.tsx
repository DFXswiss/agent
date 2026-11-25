// Rights profiles in the web client (ADR-091 sections 2, 5-7): the profiles section and the merge rules of the
// workgroup settings, the profile of a task and the role of every session; nothing is restricted by default.
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { putSession, putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { RoleBadge } from '../../src/ui/profiles/RoleBadge';
import { TaskProfile } from '../../src/ui/screens/task/TaskProfile';
import { routes } from '../../src/ui/screens/wg-settings/routes';
import { sessionRow, taskRow, taskWorld, type TaskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK } from './chat-world';

const REVIEW = { name: 'review', operations: ['review.verdict'], reads: 'all' };
const PROFILES = {
  profiles: [
    { ...REVIEW, scope: 'builtin', updated_at: null },
    { name: 'triage', operations: ['github.get_issue'], reads: 'task', scope: 'wg', updated_at: null },
    { name: 'auditor', operations: [], reads: 'workgroup', scope: 'org', updated_at: null },
  ],
  classes: { 'github.get_issue': 'read', 'review.verdict': 'review' },
};

function serve(t: TaskWorld) {
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null }));
  t.client.on(`GET /v1/workgroups/${t.wg}/agent-profiles`, () => PROFILES);
  let seq = 40;
  t.client.on('POST /v1/events', (body) => {
    const event = (body as { events: { event: { header: { id: string } } }[] }).events[0]!.event;
    seq += 1;
    return { results: [{ id: event.header.id, scope_id: t.wg, scope_seq: seq }] };
  });
}

describe('workgroup profiles and merge rules', () => {
  it('lists the profiles, adds and deletes own ones', async () => {
    const t = await taskWorld();
    serve(t);
    t.client.on(`PUT /v1/workgroups/${t.wg}/agent-profiles/scan`, () => ({}));
    t.client.on(`PUT /v1/orgs/${t.org}/agent-profiles/scan`, () => {
      throw new HubError('forbidden', 403);
    });
    t.client.on(`DELETE /v1/workgroups/${t.wg}/agent-profiles/triage`, () => ({}));
    t.client.on(`DELETE /v1/orgs/${t.org}/agent-profiles/auditor`, () => ({}));
    mount(t, routes as never, `/o/${t.org}/w/${t.wg}/settings`);
    await waitFor(() => expect(screen.getAllByTestId('wg-settings.profile')).toHaveLength(3));
    expect(screen.getAllByTestId('wg-settings.profile.delete')).toHaveLength(2);
    const save = screen.getByTestId('wg-settings.profile.save');
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByTestId('wg-settings.profile.name'), { target: { value: ' scan ' } });
    fireEvent.change(screen.getByTestId('wg-settings.profile.reads'), { target: { value: 'task' } });
    const types = screen.getAllByTestId('wg-settings.profile.type');
    fireEvent.click(types[0]!);
    fireEvent.click(types[1]!);
    fireEvent.click(types[1]!);
    fireEvent.click(save);
    await waitFor(() => expect(t.client.of(`PUT /v1/workgroups/${t.wg}/agent-profiles/scan`)).toHaveLength(1));
    expect(t.client.of(`PUT /v1/workgroups/${t.wg}/agent-profiles/scan`)[0]!.body).toEqual({
      operations: ['github.get_issue'],
      reads: 'task',
    });
    fireEvent.change(screen.getByTestId('wg-settings.profile.scope'), { target: { value: 'org' } });
    fireEvent.click(save);
    expect(await screen.findByTestId('wg-settings.profiles.error')).toBeVisible();
    for (const button of screen.getAllByTestId('wg-settings.profile.delete')) fireEvent.click(button);
    await waitFor(() => expect(t.client.of(`DELETE /v1/orgs/${t.org}/agent-profiles/auditor`)).toHaveLength(1));
    expect(t.client.of(`DELETE /v1/workgroups/${t.wg}/agent-profiles/triage`)).toHaveLength(1);
  });

  it('shows a failed list and saves the merge rules with the settings', async () => {
    const t = await taskWorld();
    serve(t);
    t.client.on(`GET /v1/workgroups/${t.wg}/agent-profiles`, () => {
      throw new HubError('unavailable', 503);
    });
    mount(t, routes as never, `/o/${t.org}/w/${t.wg}/settings`);
    expect(await screen.findByTestId('wg-settings.profiles.error')).toBeVisible();
    fireEvent.change(screen.getByTestId('wg-settings.merge.verdicts'), { target: { value: '2' } });
    fireEvent.change(screen.getByTestId('wg-settings.merge.verdicts'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('wg-settings.merge.verdicts'), { target: { value: '1' } });
    fireEvent.change(screen.getByTestId('wg-settings.merge.profile'), { target: { value: 'qa' } });
    fireEvent.change(screen.getByTestId('wg-settings.merge.approvals'), { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('wg-settings.merge.green'));
    fireEvent.click(screen.getByTestId('wg-settings.merge.separate'));
    fireEvent.click(screen.getByTestId('wg-settings.save'));
    expect(await screen.findByTestId('wg-settings.saved')).toBeVisible();
    expect(screen.getByTestId('wg-settings.merge.separate')).toBeChecked();
    expect(screen.getByTestId('wg-settings.merge.profile')).toHaveValue('qa');
  });

  it('offers organization profiles to admins only', async () => {
    const t = await taskWorld({ member: true });
    serve(t);
    mount(t, routes as never, `/o/${t.org}/w/${t.wg}/settings`);
    await waitFor(() => expect(screen.getAllByTestId('wg-settings.profile')).toHaveLength(3));
    expect(screen.getAllByTestId('wg-settings.profile.delete')).toHaveLength(1);
    expect(screen.queryByTestId('wg-settings.profile.scope')).toBeNull();
  });
});

describe('the profile of a task and the role of a session', () => {
  async function task(row: Record<string, unknown>, writer = true) {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}/agent-profiles`, () => PROFILES);
    const found = taskRow(t, row) as TaskRow;
    putTasks(t.store, [found]);
    mount(
      t,
      [{ path: '/o/:org/t/:task', element: <TaskProfile row={found} writer={writer} /> }],
      `/o/${t.org}/t/${TASK}`,
    );
    return t;
  }

  it('shows every right without a profile and lets a writer choose and clear one', async () => {
    const t = await task({});
    expect(screen.getByTestId('task.profile.name')).toHaveTextContent('Every right (no profile)');
    let fail = true;
    t.client.on(`PUT /v1/tasks/${TASK}/agent-profile`, (body) => {
      if (fail) throw new HubError('peer_outdated', 409);
      return { profile: (body as { profile: string | null }).profile === null ? null : REVIEW };
    });
    fireEvent.click(screen.getByTestId('task.profile.change'));
    fireEvent.click(await screen.findByTestId('task.profile.cancel'));
    fireEvent.click(screen.getByTestId('task.profile.change'));
    fireEvent.change(await screen.findByTestId('task.profile.select'), { target: { value: 'review' } });
    fireEvent.click(screen.getByTestId('task.profile.save'));
    expect(await screen.findByTestId('task.profile.error')).toBeVisible();
    fail = false;
    fireEvent.click(screen.getByTestId('task.profile.save'));
    await waitFor(() => expect(t.client.of(`PUT /v1/tasks/${TASK}/agent-profile`)).toHaveLength(2));
    expect(t.store.getState().tasks.tasks[TASK]!.agent_profile).toEqual(REVIEW);
    fireEvent.click(await screen.findByTestId('task.profile.change'));
    fireEvent.change(await screen.findByTestId('task.profile.select'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('task.profile.save'));
    await waitFor(() => expect(t.client.of(`PUT /v1/tasks/${TASK}/agent-profile`)).toHaveLength(3));
    expect(t.client.of(`PUT /v1/tasks/${TASK}/agent-profile`)[2]!.body).toEqual({ profile: null });
  });

  it('lets a reader only look', async () => {
    await task({ agent_profile: REVIEW }, false);
    expect(screen.getByTestId('task.profile.name')).toHaveTextContent('review');
    expect(screen.queryByTestId('task.profile.change')).toBeNull();
  });

  it('names the profile and reports a failed list', async () => {
    const t = await task({ agent_profile: REVIEW });
    expect(screen.getByTestId('task.profile.name')).toHaveAttribute('data-profile', 'review');
    t.client.on(`GET /v1/workgroups/${t.wg}/agent-profiles`, () => {
      throw new HubError('unavailable', 503);
    });
    fireEvent.click(screen.getByTestId('task.profile.change'));
    expect(await screen.findByTestId('task.profile.error')).toBeVisible();
  });

  it('shows the role of sessions', async () => {
    const t = await taskWorld();
    putTasks(t.store, [taskRow(t, { agent_profile: { ...REVIEW, name: 'triage' } }) as TaskRow]);
    putSession(t.store, sessionRow(t, { session_id: MAIN }) as never, [], 0);
    const child = '0190f000-0000-7000-8000-0000000000a2';
    putSession(
      t.store,
      sessionRow(t, { session_id: child, parent_session_id: MAIN, depth: 1, agent_profile: REVIEW }) as never,
      [],
      0,
    );
    mount(
      t,
      [
        {
          path: '/o/:org/t/:task',
          element: (
            <>
              <RoleBadge session={MAIN} task={TASK} />
              <RoleBadge session={child} task={TASK} />
              <RoleBadge session="unknown" task="unknown" />
            </>
          ),
        },
      ],
      `/o/${t.org}/t/${TASK}`,
    );
    const badges = screen.getAllByTestId('session.role');
    expect(badges.map((badge) => [badge.dataset.role, badge.textContent])).toEqual([
      ['triage', 'triage'],
      ['review', 'Review'],
    ]);
  });
});
