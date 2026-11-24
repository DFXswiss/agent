// U-WEB-ui-attention (the situation view, KOR-39): the home of an organization in jsdom against the core test world –
// counters, the four groups in order with their entries (reason in plain words, owner, agent state, time in the group,
// helpers), the actions without opening the task (answer, approve / decline with the exact prompt, try again,
// continue, connect, model settings, assign a computer or who can), read-only users without controls, entries of
// unknown or unreadable tasks, the agent tree, the window of a long group, live frames, reconnects, errors, the
// suggestions, the Home badge and the notification text. The Playwright specs run the same view against the hub.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { b64uDecode } from '../../src/core/encoding/b64u';
import { fromUtf8 } from '../../src/core/encoding/bytes';
import { openEvent } from '../../src/core/crypto/event';
import { createI18n } from '../../src/core/i18n';
import { epochKey, WG } from '../../src/core/log/state';
import { applyFrame, frameHandlers } from '../../src/core/net/frames';
import { HubError } from '../../src/core/net/signedFetch';
import { flushAttention, putSummary, queueAttention } from '../../src/core/store/slices/attention';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { setLog } from '../../src/core/store/slices/orgs';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { putSession, type SessionRow } from '../../src/core/store/slices/tasks';
import { I18nProvider } from '../../src/ui/i18n';
import { BottomNav } from '../../src/ui/responsive/BottomNav';
import { AttentionBadge } from '../../src/ui/screens/home/Badge';
import { HomeScreen } from '../../src/ui/screens/home/HomeScreen';
import { nav } from '../../src/ui/screens/home/nav';
import { NotificationText } from '../../src/ui/screens/notifications/text';
import { durationLabel } from '../../src/ui/screens/tasks/TaskItem';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { Nav } from '../../src/ui/shell/Nav';
import { idOf } from '../core/log/world';
import { at, record } from '../core/attention/rows';
import { sessionRow, taskRow, type TaskWorld, taskWorld, type WorldOptions } from '../core/tasks/world';

const OP = '0190f000-0000-7000-8000-0000000000b1';
const DEVICE = '0190f000-0000-7000-8000-0000000000d1';
const [Q, A, E, F] = ['201', '202', '203', '204'].map((n) => `0190f000-0000-7000-8000-000000000${n}`) as [
  string,
  string,
  string,
  string,
];
const SUMMARY = { needs_decision: 2, needs_decision_mine: 1, blocked: 3, working: 1, done_since: 4 };

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function wrap(t: TaskWorld, children: ReactNode) {
  const app = { store: t.store, tasks: t.context } as never;
  return (
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n('en')}>{children}</I18nProvider>
    </AppEnvProvider>
  );
}

function mount(t: TaskWorld) {
  const router = createMemoryRouter(
    [
      { path: '/o/:org', element: <HomeScreen /> },
      { path: '*', element: <p data-testid="elsewhere">elsewhere</p> },
    ],
    { initialEntries: [`/o/${t.org}`] },
  );
  render(wrap(t, <RouterProvider router={router} />));
  return router;
}

type Groups = Partial<Record<string, unknown[]>>;

/** A world whose hub answers the groups (by category) and the counters. */
async function world(groups: (t: TaskWorld) => Groups = () => ({}), options: WorldOptions = {}) {
  const t = await taskWorld(options);
  const items = groups(t);
  t.client.on(`GET /v1/orgs/${t.org}/attention`, (_body, target) => {
    const category = new URLSearchParams(target.split('?')[1]).get('category')!;
    return { items: items[category] ?? [], next_before: null };
  });
  t.client.on(`GET /v1/orgs/${t.org}/attention/summary`, () => SUMMARY);
  for (const scope of [t.org, t.wg])
    t.client.on(`GET /v1/scopes/${scope}/events`, () => ({ events: [], next_after: null, head: 0 }));
  return t;
}

const item = (
  t: TaskWorld,
  task: string,
  attention: Record<string, unknown> = {},
  row: Record<string, unknown> = {},
) => ({
  task: taskRow(t, { task_id: task, main_session_id: null, ...row }),
  attention: record(task, attention),
});

function entry(task: string): HTMLElement {
  return screen.getAllByTestId('attention.item').find((node) => node.dataset.task === task)!;
}

const verified = (t: TaskWorld, seq: number, header: Record<string, unknown>, body: unknown): EventEntry => ({
  org: t.org,
  scopeId: t.wg,
  scopeSeq: seq,
  receivedAt: null,
  event: { header },
  status: 'verified',
  code: null,
  header: { id: `e${seq}`, ts: '2026-11-21T12:00:00.000Z', ...header },
  body,
});

/** The texts of the prompts sent to `task` (opened with the workgroup key). */
async function sent(t: TaskWorld, task: string): Promise<string[]> {
  const key = t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, 1))!;
  const sig = b64uDecode(t.me.identity.public().sig);
  return Promise.all(
    t.client.of(`POST /v1/tasks/${task}/prompts`).map(async (call) => {
      const event = (call.body as { event: never }).event;
      return (JSON.parse(fromUtf8(await openEvent(key, event, sig))) as { text: string }).text;
    }),
  );
}

describe('situation view', () => {
  it('shows the counters and the four groups in order with their entries', async () => {
    const t = await world((w) => ({
      needs_decision: [item(w, 'q', { reason: 'question' })],
      blocked: [item(w, 'r', { category: 'blocked', reason: 'operation_refused' })],
      working: [
        item(
          w,
          'w',
          { category: 'working', reason: 'subagents', subagents_total: 3, subagents_active: 2 },
          { main_session_id: 'mw' },
        ),
        item(w, 'w2', { category: 'working', reason: 'later_phase' }),
      ],
    }));
    putSession(t.store, sessionRow(t, { session_id: 'mw', task_id: 'w', state: 'idle' }) as SessionRow, [], 0);
    putEvents(t.store, t.wg, [verified(t, 1, { kind: 'task.meta', task: 'q', author: t.me.id }, { title: 'Ask me' })]);
    mount(t);
    await waitFor(() => expect(screen.getAllByTestId('attention.item')).toHaveLength(4));
    expect(screen.getByTestId('attention.screen')).toHaveTextContent('Situation');
    await waitFor(() => expect(screen.getByTestId('attention.counter.mine')).toHaveAttribute('data-count', '1'));
    expect(screen.getByTestId('attention.counter.blocked')).toHaveTextContent('3Stuck');
    expect(screen.getByTestId('attention.counter.working')).toHaveTextContent('1Running');
    expect(screen.getByTestId('attention.counter.done')).toHaveTextContent('4Finished today');
    const groups = screen
      .getAllByTestId(/^attention\.group\.(needs_decision|blocked|working|done)$/)
      .map((node) => node.dataset.testid);
    expect(groups).toEqual([
      'attention.group.needs_decision',
      'attention.group.blocked',
      'attention.group.working',
      'attention.group.done',
    ]);
    expect(
      within(screen.getByTestId('attention.group.needs_decision')).getByTestId('attention.group.count'),
    ).toHaveTextContent('(2)');
    expect(within(screen.getByTestId('attention.group.done')).queryByTestId('attention.group.count')).toBeNull();
    expect(within(screen.getByTestId('attention.group.done')).getByTestId('attention.empty')).toHaveTextContent(
      'Nothing finished in the last 7 days.',
    );
    expect(within(entry('q')).getByTestId('tasks.row.title')).toHaveTextContent('Ask me');
    expect(within(entry('q')).getByTestId('attention.item.reason')).toHaveTextContent('The agent has a question');
    expect(within(entry('q')).getByTestId('attention.item.since')).toHaveTextContent(/^Waiting for /);
    expect(within(entry('r')).getByTestId('attention.item.reason')).toHaveTextContent('An action was refused');
    // a refused action offers no further step than opening the task
    expect(within(entry('r')).queryByTestId('attention.actions')).toBeNull();
    expect(within(entry('r')).getByTestId('attention.open')).toHaveAttribute('href', `/o/${t.org}/t/r`);
    expect(within(entry('w')).getByTestId('attention.item.subagents')).toHaveTextContent('2 of 3 helpers active');
    expect(within(entry('w')).getByTestId('attention.item.agent')).toHaveTextContent('Main agent: Idle');
    expect(within(entry('w')).getByTestId('attention.item.since')).toHaveTextContent(/^Running for /);
    // an unknown phase is shown with the category's generic text
    expect(within(entry('w2')).getByTestId('attention.item.reason')).toHaveTextContent('Running');
    expect(within(entry('w2')).queryByTestId('attention.item.subagents')).toBeNull();
    // the suggestions are behind a toggle once the organization has tasks
    expect(screen.queryByTestId('composer.chips')).toBeNull();
    fireEvent.click(screen.getByTestId('home.suggestions'));
    expect(screen.getByTestId('composer.chips')).toBeInTheDocument();
    expect(screen.getByTestId('home.all-tasks')).toHaveAttribute('href', `/o/${t.org}/w/${t.wg}`);
  });

  it('opens the composer suggestions when the organization has no task and says when a group is empty', async () => {
    const t = await world();
    mount(t);
    await waitFor(() => expect(screen.getAllByTestId('attention.empty')).toHaveLength(4));
    expect(screen.getByTestId('composer.chips')).toBeInTheDocument();
    expect(screen.queryByTestId('home.suggestions')).toBeNull();
    expect(screen.getAllByTestId('attention.empty').map((node) => node.textContent)).toEqual([
      'Nothing waits for a decision.',
      'Nothing is stuck.',
      'No agent is running.',
      'Nothing finished in the last 7 days.',
    ]);
  });

  it('answers a question from the view and reports a failed send', async () => {
    const t = await world((w) => ({ needs_decision: [item(w, Q, { reason: 'question', ref_id: OP })] }));
    t.client.on(`GET /v1/operations/${OP}`, () => {
      throw new HubError('unavailable', 503);
    });
    putEvents(t.store, t.wg, [
      verified(
        t,
        2,
        { kind: 'operation.params', ref: OP, author: DEVICE, task: Q },
        {
          operation_id: OP,
          type: 'human.question',
          params: { question: 'Which branch?' },
        },
      ),
    ]);
    let fail = true;
    t.client.on(`POST /v1/tasks/${Q}/prompts`, () => {
      if (fail) throw new HubError('forbidden', 403);
      return { scope_seq: 9, session_id: 's9' };
    });
    mount(t);
    const question = await screen.findByTestId('attention.question');
    expect(question).toHaveAttribute('data-status', 'verified');
    expect(question).toHaveTextContent('Which branch?');
    const send = screen.getByTestId('attention.answer.send');
    expect(send).toBeDisabled();
    fireEvent.change(screen.getByTestId('attention.answer'), { target: { value: 'main' } });
    fireEvent.click(send);
    expect(await screen.findByTestId('attention.error')).toHaveAttribute('data-code', 'forbidden');
    fail = false;
    fireEvent.submit(send.closest('form')!);
    expect(await screen.findByTestId('attention.sent')).toBeInTheDocument();
    expect(screen.getByTestId('attention.answer')).toHaveValue('');
    // an empty field sends nothing
    fireEvent.submit(send.closest('form')!);
    expect(await sent(t, Q)).toEqual(['main', 'main']);
  });

  it('shows a question that is not read yet or cannot be verified', async () => {
    const t = await world((w) => ({
      needs_decision: [
        item(w, 'q', { reason: 'question', ref_id: OP }),
        item(w, 'p', { reason: 'question', ref_id: null, ref_kind: null }),
      ],
    }));
    t.client.on(`GET /v1/operations/${OP}`, () => {
      throw new HubError('unavailable', 503);
    });
    mount(t);
    await waitFor(() => expect(screen.getAllByTestId('attention.question')).toHaveLength(2));
    expect(within(entry('p')).getByTestId('attention.question')).toHaveTextContent('Loading the question…');
    act(() =>
      putEvents(t.store, t.wg, [
        {
          ...verified(t, 3, { kind: 'operation.params', ref: OP }, null),
          status: 'unverifiable',
          code: 'signature_invalid',
          header: null,
        },
      ]),
    );
    expect(within(entry('q')).getByTestId('attention.question')).toHaveTextContent(
      'The question cannot be verified (signature_invalid).',
    );
  });

  it('approves and declines with exactly "Approved." / "Declined." and the comment', async () => {
    const t = await world((w) => ({ needs_decision: [item(w, A, { reason: 'approval', ref_id: OP })] }));
    t.client.on(`GET /v1/operations/${OP}`, () => {
      throw new HubError('unavailable', 503);
    });
    t.client.on(`POST /v1/tasks/${A}/prompts`, () => ({ scope_seq: 9, session_id: 's9' }));
    mount(t);
    fireEvent.click(await screen.findByTestId('attention.approve'));
    await screen.findByTestId('attention.sent');
    fireEvent.change(screen.getByTestId('attention.comment'), { target: { value: 'Not on Friday' } });
    fireEvent.click(screen.getByTestId('attention.decline'));
    await waitFor(() => expect(t.client.of(`POST /v1/tasks/${A}/prompts`)).toHaveLength(2));
    expect(await sent(t, A)).toEqual(['Approved.', 'Declined.\n\nNot on Friday']);
  });

  it('offers try again, continue, connect, the model settings and assigning a computer to an admin', async () => {
    const t = await world((w) => ({
      needs_decision: [
        item(w, 'c', { reason: 'connection_missing', ref_id: null, ref_kind: null }),
        item(w, 'd', { reason: 'device_missing', ref_id: null, ref_kind: null }),
        item(w, E, { reason: 'credit_empty', ref_id: null, ref_kind: null }),
      ],
      blocked: [
        item(w, F, { category: 'blocked', reason: 'session_failed', ref_kind: 'session' }),
        item(w, 'm', { category: 'blocked', reason: 'model_access_missing', ref_id: null, ref_kind: null }),
      ],
    }));
    putEvents(t.store, t.wg, [verified(t, 4, { kind: 'prompt', task: F }, { text: 'Build it', attachments: [] })]);
    for (const task of [E, F])
      t.client.on(`POST /v1/tasks/${task}/prompts`, () => ({ scope_seq: 9, session_id: 's9' }));
    mount(t);
    await waitFor(() => expect(screen.getAllByTestId('attention.item')).toHaveLength(5));
    expect(within(entry('c')).getByTestId('attention.connect')).toHaveAttribute('href', '/settings');
    expect(within(entry('m')).getByTestId('attention.model')).toHaveAttribute('href', '/settings');
    expect(within(entry('d')).getByTestId('attention.assign')).toHaveAttribute('href', `/o/${t.org}/devices`);
    expect(within(entry(E)).getByTestId('attention.credit')).toHaveTextContent('The operator sets the balance');
    fireEvent.click(within(entry(E)).getByTestId('attention.continue'));
    fireEvent.click(within(entry(F)).getByTestId('attention.retry'));
    await waitFor(() => expect(within(entry(F)).getByTestId('attention.sent')).toBeInTheDocument());
    expect(await sent(t, E)).toEqual(['Continue.']);
    expect(await sent(t, F)).toEqual(['Build it']);
  });

  it('tells a member who cannot assign a computer who can, and shows read-only users no controls', async () => {
    const t = await world(undefined, { member: true, private: false });
    const research = await idOf('research');
    const secret = await idOf('secret');
    await t.world.batch(t.ada, [
      { type: 'wg_create', wg: research, epoch: 1 },
      { type: 'grant', subject: t.me.id, wg: research, access: 'read' },
      { type: 'wg_create', wg: secret, epoch: 1 },
    ]);
    setLog(t.store, t.org, t.world.log!);
    t.client.on(`GET /v1/orgs/${t.org}/attention`, (_body, target) => {
      const category = new URLSearchParams(target.split('?')[1]).get('category')!;
      const items =
        category === 'needs_decision'
          ? [
              item(t, 'd', { reason: 'device_missing', ref_id: null, ref_kind: null }),
              item(t, 'ro', { reason: 'question' }, { wg_id: research }),
              item(t, 'hidden', { reason: 'question' }, { wg_id: secret }),
            ]
          : [];
      return { items, next_before: null };
    });
    mount(t);
    await waitFor(() => expect(screen.getAllByTestId('attention.item')).toHaveLength(2));
    expect(within(entry('d')).getByTestId('attention.assign.hint')).toHaveTextContent('An administrator');
    expect(within(entry('d')).queryByTestId('attention.assign')).toBeNull();
    expect(within(entry('ro')).queryByTestId('attention.actions')).toBeNull();
    expect(within(entry('ro')).getByTestId('attention.open')).toBeInTheDocument();
    // a workgroup the verified log does not let the user read never appears
    expect(screen.queryAllByTestId('attention.item').some((node) => node.dataset.task === 'hidden')).toBe(false);
  });

  it('loads the row of a task a live frame named, follows frames and refetches the counters', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const t = await world((w) => ({
      needs_decision: [item(w, 'q', { reason: 'question', ref_id: null, ref_kind: null })],
    }));
    setActiveOrg(t.store, t.org);
    t.client.on('GET /v1/tasks/n', () => ({ task: taskRow(t, { task_id: 'n', main_session_id: null }), sessions: [] }));
    mount(t);
    await waitFor(() => expect(screen.getAllByTestId('attention.item')).toHaveLength(1));
    const summaries = () => t.client.of(`GET /v1/orgs/${t.org}/attention/summary`).length;
    const before = summaries();
    act(() => {
      applyFrame(
        t.store,
        'attention',
        record('q', { category: 'working', reason: 'working', since: at(5), updated_at: at(5), seq: 2 }),
        0,
      );
      applyFrame(
        t.store,
        'attention',
        record('n', { category: 'blocked', reason: 'no_capacity', since: at(6), updated_at: at(6) }),
        0,
      );
      flushAttention(t.store);
    });
    expect(within(screen.getByTestId('attention.group.needs_decision')).queryByTestId('attention.item')).toBeNull();
    expect(within(screen.getByTestId('attention.group.working')).getByTestId('attention.item')).toHaveAttribute(
      'data-task',
      'q',
    );
    await waitFor(() => expect(within(entry('n')).getByTestId('attention.retry')).toBeInTheDocument());
    await act(async () => vi.advanceTimersByTimeAsync(1100));
    expect(summaries()).toBeGreaterThan(before);
  });

  it('shows a loading line until the row of a task is known', async () => {
    const t = await world();
    setActiveOrg(t.store, t.org);
    mount(t);
    await waitFor(() => expect(screen.getAllByTestId('attention.empty')).toHaveLength(4));
    act(() => {
      queueAttention(t.store, record('x', { since: at(1) }));
      flushAttention(t.store);
    });
    expect(entry('x')).toHaveAttribute('data-loading', 'true');
    expect(entry('x')).toHaveTextContent('Loading the task…');
  });

  it('loads the groups again after a reconnect and offers to retry a failed group', async () => {
    const t = await world();
    let fail = true;
    t.client.on(`GET /v1/orgs/${t.org}/attention`, () => {
      if (fail) throw new HubError('unavailable', 503);
      return { items: [item(t, 'q')], next_before: null };
    });
    mount(t);
    await waitFor(() => expect(screen.getAllByTestId('attention.error.load')).toHaveLength(4));
    fail = false;
    fireEvent.click(within(screen.getByTestId('attention.group.blocked')).getByTestId('attention.reload'));
    await waitFor(() =>
      expect(within(screen.getByTestId('attention.group.blocked')).queryByTestId('attention.error.load')).toBeNull(),
    );
    const calls = t.client.of(`GET /v1/orgs/${t.org}/attention`).length;
    act(() => {
      applyFrame(t.store, 'hello', { heads: {}, hub_time: '2026-11-22T12:00:00Z' }, 0, frameHandlers());
    });
    await waitFor(() =>
      expect(t.client.of(`GET /v1/orgs/${t.org}/attention`).length).toBeGreaterThanOrEqual(calls + 4),
    );
  });
});

describe('agent tree', () => {
  it('loads the agents when opened: main agent, subagents indented below their parent, the rest counted', async () => {
    const t = await world((w) => ({
      working: [item(w, 'w', { category: 'working', reason: 'subagents', subagents_total: 150, subagents_active: 1 })],
    }));
    const session = (id: string, parent: string | null, depth: number, state: string, minute: number) => ({
      session: sessionRow(t, {
        session_id: id,
        task_id: 'w',
        parent_session_id: parent,
        depth,
        state,
        created_at: at(minute),
      }),
      transitions: [],
    });
    t.client.on('GET /v1/tasks/w', () => ({
      task: taskRow(t, { task_id: 'w', main_session_id: 'm' }),
      sessions: [
        session('m', null, 0, 'working', 0),
        session('s1', 'm', 1, 'working', 1),
        session('s2', 'm', 1, 'stopped', 2),
        session('s11', 's1', 2, 'idle', 3),
      ],
    }));
    mount(t);
    const toggle = await screen.findByTestId('attention.item.toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveTextContent('Show agents');
    fireEvent.click(toggle);
    expect(screen.getByTestId('attention.tree')).toHaveTextContent('Loading the agents…');
    await waitFor(() => expect(screen.getAllByTestId('attention.tree.node')).toHaveLength(4));
    expect(screen.getAllByTestId('attention.tree.node').map((node) => [node.textContent, node.dataset.depth])).toEqual([
      ['Main agentWorking', '0'],
      ['Subagent 1Working', '1'],
      ['Subagent 1.1Idle', '2'],
      ['Subagent 2Ended', '1'],
    ]);
    expect(screen.getByTestId('attention.tree.more')).toHaveTextContent('147 more');
    fireEvent.click(toggle);
    expect(screen.queryByTestId('attention.tree')).toBeNull();
  });

  it('says when no agent started yet and when the agents cannot be loaded', async () => {
    const t = await world((w) => ({
      done: [item(w, 'd', { category: 'done', reason: 'idle' }), item(w, 'x', { category: 'done', reason: 'closed' })],
    }));
    t.client.on('GET /v1/tasks/d', () => ({ task: taskRow(t, { task_id: 'd', main_session_id: null }), sessions: [] }));
    mount(t);
    await waitFor(() => expect(screen.getAllByTestId('attention.item.toggle')).toHaveLength(2));
    expect(within(entry('d')).getByTestId('attention.item.since')).toHaveTextContent(/^Finished /);
    fireEvent.click(within(entry('d')).getByTestId('attention.item.toggle'));
    await waitFor(() =>
      expect(within(entry('d')).getByTestId('attention.tree')).toHaveTextContent('No agent has started yet.'),
    );
    fireEvent.click(within(entry('x')).getByTestId('attention.item.toggle'));
    expect(await within(entry('x')).findByTestId('attention.tree.error')).toBeInTheDocument();
  });
});

describe('window', () => {
  it('keeps at most 50 entries in the DOM, pages from the hub and moves by 25', async () => {
    const t = await taskWorld();
    const all = Array.from({ length: 120 }, (_, index) =>
      item(t, `t${String(index).padStart(3, '0')}`, { since: at(0) }),
    );
    t.client.on(`GET /v1/orgs/${t.org}/attention`, (_body, target) => {
      const params = new URLSearchParams(target.split('?')[1]);
      if (params.get('category') !== 'needs_decision') return { items: [], next_before: null };
      const start = Number(params.get('before') ?? 0);
      const end = start + 50;
      return { items: all.slice(start, end), next_before: end < all.length ? String(end) : null };
    });
    t.client.on(`GET /v1/orgs/${t.org}/attention/summary`, () => SUMMARY);
    let observed: IntersectionObserverCallback | null = null;
    vi.stubGlobal(
      'IntersectionObserver',
      class {
        constructor(callback: IntersectionObserverCallback) {
          observed = callback;
        }
        observe() {}
        disconnect() {}
      },
    );
    mount(t);
    const group = () => screen.getByTestId('attention.group.needs_decision');
    const shown = () =>
      within(group())
        .getAllByTestId('attention.item')
        .map((node) => node.dataset.task);
    await waitFor(() => expect(shown()).toHaveLength(50));
    expect(group()).toHaveAttribute('data-total', '50');
    expect(within(group()).queryByTestId('attention.earlier')).toBeNull();
    expect(within(group()).getByTestId('attention.more')).toHaveTextContent('Load more');
    // the end of the list becomes visible: the next page loads and the window moves by 25
    await act(async () =>
      observed!([{ isIntersecting: true } as IntersectionObserverEntry], {} as IntersectionObserver),
    );
    await waitFor(() => expect(group()).toHaveAttribute('data-from', '25'));
    expect(group()).toHaveAttribute('data-total', '100');
    expect(shown()).toHaveLength(50);
    expect(shown()[0]).toBe('t025');
    expect(within(group()).getByTestId('attention.more')).toHaveTextContent('Show later entries');
    await act(async () =>
      observed!([{ isIntersecting: false } as IntersectionObserverEntry], {} as IntersectionObserver),
    );
    fireEvent.click(within(group()).getByTestId('attention.more'));
    // enough entries loaded: the window moves without a request
    await waitFor(() => expect(group()).toHaveAttribute('data-from', '50'));
    expect(group()).toHaveAttribute('data-total', '100');
    fireEvent.click(within(group()).getByTestId('attention.more'));
    await waitFor(() => expect(group()).toHaveAttribute('data-total', '120'));
    await waitFor(() => expect(group()).toHaveAttribute('data-from', '70'));
    expect(within(group()).queryByTestId('attention.more')).toBeNull();
    fireEvent.click(within(group()).getByTestId('attention.earlier'));
    expect(group()).toHaveAttribute('data-from', '45');
    fireEvent.click(within(group()).getByTestId('attention.earlier'));
    fireEvent.click(within(group()).getByTestId('attention.earlier'));
    expect(group()).toHaveAttribute('data-from', '0');
    expect(within(group()).queryByTestId('attention.earlier')).toBeNull();
    // the first entry of a moved window leaves the group: the window stays near its place
    fireEvent.click(within(group()).getByTestId('attention.more'));
    await waitFor(() => expect(group()).toHaveAttribute('data-from', '25'));
    act(() => {
      queueAttention(t.store, record('t025', { category: 'done', reason: 'closed', updated_at: at(9) }));
      flushAttention(t.store);
    });
    expect(group()).toHaveAttribute('data-total', '119');
    expect(group()).toHaveAttribute('data-from', '25');
    expect(shown()[0]).toBe('t026');
    // an entry above the window leaves: the window keeps its first entry
    act(() => {
      queueAttention(t.store, record('t001', { category: 'done', reason: 'closed', updated_at: at(9) }));
      flushAttention(t.store);
    });
    expect(group()).toHaveAttribute('data-from', '24');
    expect(shown()[0]).toBe('t026');
  });
});

describe('badge and notifications', () => {
  it('shows the decisions waiting for the user next to Home (sidebar, row and bottom bar)', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/orgs/${t.org}/attention/summary`, () => ({ ...SUMMARY, needs_decision_mine: 0 }));
    setActiveOrg(t.store, t.org);
    render(
      wrap(
        t,
        <MemoryRouter>
          <Nav entries={nav} />
          <BottomNav entries={nav} />
        </MemoryRouter>,
      ),
    );
    await waitFor(() => expect(t.client.of(`GET /v1/orgs/${t.org}/attention/summary`)).toHaveLength(1));
    expect(screen.queryByTestId('shell.badge.attention')).toBeNull();
    act(() => putSummary(t.store, t.org, { ...SUMMARY, needs_decision_mine: 3 }));
    expect(screen.getAllByTestId('shell.badge.attention').map((node) => node.textContent)).toEqual([
      '33 waiting for your decision',
      '33 waiting for your decision',
    ]);
    act(() => putSummary(t.store, t.org, { ...SUMMARY, needs_decision_mine: 120 }));
    expect(screen.getAllByTestId('shell.badge.attention')[0]).toHaveTextContent('99+');
  });

  it('renders the badge alone and the text of a task that needs attention', async () => {
    const t = await taskWorld();
    render(wrap(t, <AttentionBadge org={t.org} />));
    expect(screen.queryByTestId('shell.badge.attention')).toBeNull();
    const row = {
      notification_id: 'n1',
      kind: 'task.attention',
      org_id: t.org,
      wg_id: t.wg,
      task_id: 'q',
      ref: 'needs_decision:question:2',
      created_at: at(0),
      read_at: null,
    };
    render(
      wrap(
        t,
        <MemoryRouter>
          <NotificationText row={row} />
          <NotificationText row={{ ...row, ref: 'blocked:no_capacity:wg' }} />
        </MemoryRouter>,
      ),
    );
    expect(screen.getAllByTestId('notifications.text').map((node) => node.textContent)).toEqual([
      'Task “…” needs your decision: The agent has a question',
      'Task “…” is stuck: All computers are busy',
    ]);
  });

  it('formats durations in the largest whole unit', () => {
    expect(durationLabel('en', 5_000)).toBe('5 sec');
    expect(durationLabel('en', -1)).toBe('0 sec');
    expect(durationLabel('en', 3 * 60_000)).toBe('3 min');
    expect(durationLabel('en', 2 * 3_600_000 + 1)).toBe('2 hr');
    expect(durationLabel('en', 3 * 86_400_000)).toBe('3 days');
  });
});
