// U-WEB-ui (KOR-40): errors the guest reported (`session.error`) in the chat, the runtime of sessions and tasks and
// whether the hub reaches a session's device.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { putEvents } from '../../src/core/store/slices/events';
import { putSession, type TaskRow } from '../../src/core/store/slices/tasks';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { GUEST_ERROR_CODES } from '../../src/ui/chat-items/session.error';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';

afterEach(() => vi.useRealTimers());

const DEVICE = '0190f000-0000-7000-8000-0000000000d1';

async function page(
  options: { task?: Partial<TaskRow>; session?: Record<string, unknown>; devices?: unknown } = {},
): Promise<TaskWorld> {
  const t = await taskWorld();
  t.client.on(`GET /v1/tasks/${TASK}`, () => ({
    task: taskRow(t, options.task ?? {}) as TaskRow,
    sessions: [{ session: sessionRow(t, options.session ?? {}), transitions: [] }],
  }));
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
  if (options.devices !== undefined) t.client.on(`GET /v1/orgs/${t.org}/devices`, () => options.devices);
  mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}`);
  await screen.findByTestId('task.screen');
  return t;
}

function device(online: boolean): Record<string, unknown> {
  return {
    device_id: DEVICE,
    kind: 'platform',
    state: 'active',
    online,
    last_seen_at: null,
    agent_version: '1',
    capacity: { vm_memory_budget_mib: 8192, vm_memory_used_mib: 0, vm_slots_total: 4, vm_slots_free: 2, pool_ready: 1 },
    capacity_low: false,
    assigned_wgs: [],
  };
}

describe('session.error in the chat', () => {
  it('shows the text of a known code and an unknown or malformed one as such', async () => {
    const t = await page();
    act(() => setActiveOrg(t.store, t.org));
    act(() =>
      putEvents(t.store, t.wg, [
        verified(t, 'session.error', { code: 'turn_unconfirmed' }, { session: MAIN }),
        verified(t, 'session.error', { code: 'brand_new' }, { session: MAIN }),
        verified(t, 'session.error', { code: 'Not A Code' }, { session: MAIN }),
        verified(t, 'session.error', null, { session: MAIN }),
      ]),
    );
    const rows = screen.getAllByTestId('chat.guest-error');
    expect(rows.map((row) => row.getAttribute('data-code'))).toEqual([
      'turn_unconfirmed',
      'brand_new',
      'unknown',
      'unknown',
    ]);
    expect(rows[0]).toHaveTextContent('Session error: A model call without a confirmed prompt was refused.');
    expect(rows[1]).toHaveTextContent('Unknown error (brand_new).');
    expect(GUEST_ERROR_CODES.size).toBe(13);
  });
});

describe('runtime and reachability', () => {
  it('counts the runtime of a running session and task and names a reachable device', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true, now: Date.parse('2026-11-21T12:10:00.000Z') });
    const t = await page({
      task: { created_at: '2026-11-21T12:00:00.000Z', closed_at: null },
      session: {
        state: 'working',
        created_at: '2026-11-21T12:01:00.000Z',
        first_working_at: '2026-11-21T12:02:00.000Z',
      },
      devices: { devices: [device(true)] },
    });
    expect(screen.getByTestId('task.runtime')).toHaveTextContent('Runtime 10 min');
    fireEvent.click(screen.getByTestId('task.tabs.sessions'));
    const node = (await screen.findAllByTestId('sessions.node'))[0]!;
    expect(within(node).getAllByTestId('session.runtime')[0]).toHaveTextContent('Runtime 8 min');
    await waitFor(() =>
      expect(within(node).getAllByTestId('session.device.reachable')[0]).toHaveAttribute('data-reachable', 'true'),
    );
    expect(t.client.of(`GET /v1/orgs/${t.org}/devices`).length).toBeGreaterThan(0);
  });

  it('stops at the end, hides an unknown end and names an unreachable device', async () => {
    const t = await page({
      task: { created_at: '2026-11-21T12:00:00.000Z', closed_at: '2026-11-21T12:00:30.000Z', state: 'closed' },
      session: { state: 'stopped', created_at: '2026-11-21T12:00:00.000Z', ended_at: '2026-11-21T12:05:00.000Z' },
      devices: { devices: [device(false)] },
    });
    expect(screen.getByTestId('task.runtime')).toHaveTextContent('Runtime 30 s');
    fireEvent.click(screen.getByTestId('task.tabs.sessions'));
    const node = (await screen.findAllByTestId('sessions.node'))[0]!;
    expect(within(node).getAllByTestId('session.runtime')[0]).toHaveTextContent('Runtime 5 min');
    await waitFor(() =>
      expect(within(node).getAllByTestId('session.device.reachable')[0]).toHaveTextContent('(not reachable)'),
    );
    act(() =>
      putSession(
        t.store,
        sessionRow(t, {
          session_id: 'old',
          parent_session_id: MAIN,
          depth: 1,
          state: 'failed',
          device_id: null,
        }) as never,
        [],
        0,
      ),
    );
    const child = screen.getAllByTestId('sessions.node')[1]!;
    expect(within(child).queryByTestId('session.runtime')).toBeNull(); // ended, but no end known (an older hub)
    expect(within(child).queryByTestId('session.device.reachable')).toBeNull();
  });

  it('shows no reachability when the devices cannot be read and no runtime for a malformed time', async () => {
    await page({ task: { created_at: 'garbage' }, devices: { devices: 'nope' } });
    expect(screen.queryByTestId('task.runtime')).toBeNull();
    fireEvent.click(screen.getByTestId('task.tabs.sessions'));
    const node = (await screen.findAllByTestId('sessions.node'))[0]!;
    expect(within(node).queryByTestId('session.device.reachable')).toBeNull();
  });
});
