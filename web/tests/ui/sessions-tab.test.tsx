// The Sessions tab under volume in jsdom (06-web.md section 5.3, ADR-063 section 1): the flattened tree is a window
// of at most `MAX_ROWS` rows with the depth as indentation, and one selector delivers the task's sessions and usage.
import { act, fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { putSession, putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { putUsage, type SessionUsage } from '../../src/core/store/slices/usage';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { MAX_ROWS, PAGE, selectTaskSessions, WINDOW } from '../../src/ui/task-tabs/sessions/SessionsTab';
import { sessionRow, taskRow, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK } from './chat-world';

const OTHER_TASK = '0190f000-0000-7000-8000-000000000002';

const id = (n: number) => `0190f000-0000-7000-8000-${n.toString(16).padStart(12, '0')}`;
const at = (n: number) => new Date(Date.UTC(2026, 10, 21, 12) + n * 1000).toISOString();

const usage = (input: number): SessionUsage => ({
  requests: 1,
  input_tokens: input,
  output_tokens: 1,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  thinking_tokens: 0,
  cost_micro_usd: 10,
  cost_basis: 'provider',
  context_used_tokens: 0,
  context_window_tokens: 0,
  last_provider: null,
  updated_at: at(0),
});

async function world() {
  const t = await taskWorld();
  const task = taskRow(t) as TaskRow;
  putTasks(t.store, [task]);
  t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task, sessions: [] }));
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
  return t;
}

describe('sessions tab under volume', () => {
  it('renders a window of the flattened tree, indents subagents and shows older rows by the button', async () => {
    const t = await world();
    const COUNT = 400;
    for (let n = 1; n <= COUNT; n += 1)
      putSession(t.store, sessionRow(t, { session_id: id(n), created_at: at(n), state: 'stopped' }) as never, [], 0);
    const CHILD = id(1001);
    putSession(
      t.store,
      sessionRow(t, { session_id: CHILD, parent_session_id: id(1), depth: 1, created_at: at(2000) }) as never,
      [],
      0,
    );
    putSession(
      t.store,
      sessionRow(t, { session_id: id(1002), parent_session_id: CHILD, depth: 2, created_at: at(2001) }) as never,
      [],
      0,
    );
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}?tab=sessions`);
    const tree = await screen.findByTestId('sessions.tree');
    const nodes = within(tree).getAllByTestId('sessions.node');
    expect(nodes).toHaveLength(WINDOW);
    expect(tree).toHaveAttribute('data-total', String(COUNT + 2));
    // depth first: the first main session, its child, the grandchild, then the next main session
    expect(nodes.slice(0, 4).map((node) => node.getAttribute('data-depth'))).toEqual(['0', '1', '2', '0']);
    expect(nodes[1]).toHaveClass('ml-4');
    expect(nodes[2]).toHaveClass('ml-8');
    expect(nodes[2]).toHaveTextContent('Subagent 1.1');
    expect(screen.getByTestId('sessions.tree.older')).toHaveTextContent(String(COUNT + 2 - WINDOW));
    fireEvent.click(screen.getByTestId('sessions.tree.older'));
    expect(within(tree).getAllByTestId('sessions.node')).toHaveLength(WINDOW + PAGE);
    for (let i = 0; i < 4; i += 1) fireEvent.click(screen.getByTestId('sessions.tree.older'));
    expect(within(tree).getAllByTestId('sessions.node').length).toBeLessThanOrEqual(MAX_ROWS);
  });

  it('updates only the changed row and ignores other tasks', async () => {
    const t = await world();
    putSession(t.store, sessionRow(t, { state: 'working' }) as never, [], 0);
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}?tab=sessions`);
    const node = await screen.findByTestId('sessions.node');
    act(() => putUsage(t.store, TASK, MAIN, usage(1234)));
    expect(within(node).getByTestId('session.usage')).toHaveTextContent('1,234 in');
    act(() => putSession(t.store, sessionRow(t, { session_id: id(77), task_id: OTHER_TASK }) as never, [], 0));
    expect(screen.getAllByTestId('sessions.node')).toHaveLength(1);
    expect(screen.getByTestId('sessions.node')).toBe(node);
  });
});

describe('selectTaskSessions', () => {
  it('keeps its value while the task sessions and their usage are the same', async () => {
    const t = await world();
    const select = selectTaskSessions(TASK);
    const empty = select(t.store.getState());
    expect(empty.sessions).toEqual({});
    expect(select(t.store.getState())).toBe(empty);
    putSession(t.store, sessionRow(t) as never, [], 0);
    const one = select(t.store.getState());
    expect(Object.keys(one.sessions)).toEqual([MAIN]);
    expect(one.usage).toEqual({ [MAIN]: null });
    // another task's session: same value
    putSession(t.store, sessionRow(t, { session_id: id(5), task_id: OTHER_TASK }) as never, [], 0);
    expect(select(t.store.getState())).toBe(one);
    // another session's usage: same value
    putUsage(t.store, OTHER_TASK, id(5), usage(1));
    expect(select(t.store.getState())).toBe(one);
    // own usage: the sessions stay, the usage is new
    putUsage(t.store, TASK, MAIN, usage(2));
    const used = select(t.store.getState());
    expect(used.sessions).toBe(one.sessions);
    expect(used.usage[MAIN]?.input_tokens).toBe(2);
    // own session: new sessions, the usage stays
    putSession(t.store, sessionRow(t, { state: 'working' }) as never, [], 0);
    const working = select(t.store.getState());
    expect(working.sessions).not.toBe(used.sessions);
    expect(working.usage).toBe(used.usage);
  });
});
