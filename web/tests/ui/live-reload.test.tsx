// KOR-11 (AUD-02 NA-05): after every (re)connect (`hello`) and `resync` of the live channel the screens load their rows
// again – tasks, the task with its sessions and usage, a session, notifications and the unread count – because the hub
// replays events but no row frames. Not on mount (the screens load there) and not without a new round.
import { act, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { applyFrame, frameHandlers } from '../../src/core/net/frames';
import { HubError } from '../../src/core/net/signedFetch';
import type { Store } from '../../src/core/store/store';
import { I18nProvider } from '../../src/ui/i18n';
import { NotificationsScreen } from '../../src/ui/screens/notifications/NotificationsScreen';
import { SessionScreen } from '../../src/ui/screens/session/SessionScreen';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { TaskListScreen } from '../../src/ui/screens/tasks/TaskListScreen';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { Bell } from '../../src/ui/shell/header/bell';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';

const empty = { events: [], next_after: null, head: 0 };

function mount(t: TaskWorld, element: ReactNode, path: string, pattern: string) {
  const router = createMemoryRouter([{ path: pattern, element }], { initialEntries: [path] });
  const loadMe = vi.fn(async () => ({}));
  const terminal = {
    store: t.store,
    client: t.client,
    now: () => t.clock.now,
    read: vi.fn(async () => []),
    watch: vi.fn(() => () => undefined),
  };
  const app = { store: t.store, tasks: t.context, terminal, loadMe } as never;
  render(
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n('en')}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
  return { loadMe };
}

const frame = (store: Store, type: 'hello' | 'resync') =>
  act(() => {
    applyFrame(
      store,
      type,
      type === 'hello' ? { heads: {}, hub_time: '2026-11-22T12:00:00Z' } : {},
      0,
      frameHandlers(),
    );
  });

async function world(): Promise<TaskWorld> {
  const t = await taskWorld();
  t.client.on(`GET /v1/scopes/${t.org}/events`, () => empty);
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => empty);
  return t;
}

describe('screens after a reconnect or resync', () => {
  it('the task list loads its first page again and shows the state it missed', async () => {
    const t = await world();
    let state = 'idle';
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => ({ tasks: [taskRow(t, { state })], next_before: null }));
    mount(t, <TaskListScreen />, `/o/${t.org}/w/${t.wg}`, '/o/:org/w/:wg');
    await waitFor(() => expect(screen.getByTestId('tasks.row.state')).toHaveAttribute('data-state', 'idle'));
    expect(t.client.of(`GET /v1/workgroups/${t.wg}/tasks`)).toHaveLength(1);
    state = 'working'; // its `task` frame was lost while the channel was down
    frame(t.store, 'hello');
    await waitFor(() => expect(screen.getByTestId('tasks.row.state')).toHaveAttribute('data-state', 'working'));
    expect(t.client.of(`GET /v1/workgroups/${t.wg}/tasks`)).toHaveLength(2);
    state = 'closed';
    frame(t.store, 'resync');
    await waitFor(() => expect(screen.getByTestId('tasks.row.state')).toHaveAttribute('data-state', 'closed'));
  });

  it('the task page loads the task with its sessions and their usage again', async () => {
    const t = await world();
    let state = 'starting';
    let tokens = 1;
    const row = () => taskRow(t, { state });
    t.client.on(`GET /v1/tasks/${row().task_id}`, () => ({
      task: row(),
      sessions: [
        {
          session: sessionRow(t, { state: state === 'starting' ? 'requested' : 'working' }),
          usage: {
            requests: tokens,
            input_tokens: tokens,
            output_tokens: 0,
            cache_read_tokens: 0,
            cache_write_tokens: 0,
            thinking_tokens: 0,
            cost_micro_usd: 0,
            cost_basis: 'provider',
            context_used_tokens: 0,
            context_window_tokens: 1000,
            last_provider: null,
            updated_at: `2026-11-22T12:00:0${tokens}.000Z`,
          },
          transitions: [],
        },
      ],
      transitions: [],
    }));
    mount(t, <TaskScreen />, `/o/${t.org}/t/${row().task_id}?tab=chat`, '/o/:org/t/:task');
    await waitFor(() => expect(screen.getByTestId('task.state')).toHaveAttribute('data-state', 'starting'));
    const calls = t.client.of(`GET /v1/tasks/${row().task_id}`).length;
    state = 'working';
    tokens = 2;
    frame(t.store, 'resync');
    await waitFor(() => expect(screen.getByTestId('task.state')).toHaveAttribute('data-state', 'working'));
    expect(t.client.of(`GET /v1/tasks/${row().task_id}`)).toHaveLength(calls + 1);
    expect(t.store.getState().tasks.sessions[sessionRow(t).session_id]!.row.state).toBe('working');
    expect(t.store.getState().usage.bySession[sessionRow(t).session_id]!.usage.requests).toBe(2);
    // a failed reload keeps what the page shows; the next round tries again
    t.client.on(`GET /v1/tasks/${row().task_id}`, () => {
      throw new HubError('unavailable', 503);
    });
    frame(t.store, 'hello');
    await waitFor(() => expect(t.client.of(`GET /v1/tasks/${row().task_id}`)).toHaveLength(calls + 2));
    expect(screen.getByTestId('task.state')).toHaveAttribute('data-state', 'working');
  });

  it('the session screen loads the session again', async () => {
    const t = await world();
    const session = sessionRow(t);
    t.client.on(`GET /v1/sessions/${session.session_id}`, () => ({ session, transitions: [], children: [] }));
    t.client.on(`GET /v1/tasks/${session.task_id}`, () => ({ task: taskRow(t), sessions: [], transitions: [] }));
    mount(t, <SessionScreen />, `/o/${t.org}/s/${session.session_id}`, '/o/:org/s/:session');
    await waitFor(() => expect(t.client.of(`GET /v1/sessions/${session.session_id}`)).toHaveLength(1));
    frame(t.store, 'hello');
    await waitFor(() => expect(t.client.of(`GET /v1/sessions/${session.session_id}`)).toHaveLength(2));
  });

  it('the notification center loads its first page again', async () => {
    const t = await world();
    t.client.on('GET /v1/notifications', () => ({ notifications: [], next_before: null }));
    mount(t, <NotificationsScreen />, '/notifications', '/notifications');
    await waitFor(() => expect(t.client.of('GET /v1/notifications')).toHaveLength(1));
    frame(t.store, 'resync');
    await waitFor(() => expect(t.client.of('GET /v1/notifications')).toHaveLength(2));
  });

  it('the bell loads the unread count of the profile again, once per round and not on mount', async () => {
    const t = await world();
    const { loadMe } = mount(t, <Bell />, '/', '/');
    expect(loadMe).not.toHaveBeenCalled();
    frame(t.store, 'hello');
    await waitFor(() => expect(loadMe).toHaveBeenCalledTimes(1));
    act(() => t.store.update('connection', (connection) => ({ ...connection, online: false })));
    expect(loadMe).toHaveBeenCalledTimes(1);
    frame(t.store, 'resync');
    await waitFor(() => expect(loadMe).toHaveBeenCalledTimes(2));
    // a lower count (the store was reset at a sign-out) is no new round
    act(() => t.store.update('connection', (connection) => ({ ...connection, round: 0 })));
    expect(loadMe).toHaveBeenCalledTimes(2);
    frame(t.store, 'hello');
    await waitFor(() => expect(loadMe).toHaveBeenCalledTimes(3));
    // a failed reload changes nothing (the next round tries again)
    loadMe.mockRejectedValueOnce(new Error('offline'));
    frame(t.store, 'resync');
    await waitFor(() => expect(loadMe).toHaveBeenCalledTimes(4));
  });
});
