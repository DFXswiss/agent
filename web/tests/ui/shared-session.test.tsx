// U-WEB-shared-session (KOR-46, ADR-083): the queue of a task above the composer with its senders, states and the
// withdrawal of one's own prompts, the marks of withdrawn prompts in the timeline, "Triggered by <name>" on operation
// cards and session rows, and the consumption per person in the billing section and the task's costs tab.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { type OperationRow, putOperations } from '../../src/core/store/slices/operations';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { putSession, type TaskRow } from '../../src/core/store/slices/tasks';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { BillingSection } from '../../src/ui/settings/sections/billing';
import { ChargesTab } from '../../src/ui/task-tabs/charges/ChargesTab';
import { BILLING, totals } from '../core/billing/world';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';

const BOB = '0190f000-0000-7000-8000-0000000000b0';
const CAROL = '0190f000-0000-7000-8000-0000000000c0';
const QUEUE = `GET /v1/tasks/${TASK}/queue`;

let seq = 900;

/** Bob's verified `member.profile` in the org scope. */
function profile(t: TaskWorld, user: string, name: string): EventEntry {
  const header = { id: `p${seq}`, kind: 'member.profile', ref: user, author: user, ts: '2026-11-24T09:00:00.000Z' };
  return {
    org: t.org,
    scopeId: t.org,
    scopeSeq: seq++,
    receivedAt: null,
    event: { header },
    status: 'verified',
    code: null,
    header,
    body: { display_name: name },
  };
}

const queued = (event: string, author: string, overrides: Record<string, unknown> = {}) => ({
  event_id: event,
  author_id: author,
  via_agent: false,
  scope_seq: 3,
  received_at: '2026-11-24T10:00:00.000Z',
  session_id: MAIN,
  device_id: null,
  state: 'waiting',
  ...overrides,
});

async function page(path = '', answer: () => unknown = () => ({ task_id: TASK, prompts: [], withdrawals: [] })) {
  const t = await taskWorld();
  const task = taskRow(t) as TaskRow;
  t.client.on(`GET /v1/tasks/${TASK}`, () => ({
    task,
    sessions: [{ session: sessionRow(t, { state: 'working' }), transitions: [] }],
  }));
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
  t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: [], next_before: null }));
  t.client.on(QUEUE, answer);
  act(() => {
    setActiveOrg(t.store, t.org);
    putEvents(t.store, t.org, [profile(t, BOB, 'Bob')]);
  });
  const mounted = mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}${path}`);
  await screen.findByTestId('task.screen');
  return { t, ...mounted };
}

describe('queue of the task', () => {
  it('lists the waiting instructions with sender and state and withdraws one of one’s own', async () => {
    let prompts: unknown[] = [];
    const { t } = await page('', () => ({ task_id: TASK, prompts, withdrawals: [] }));
    await waitFor(() => expect(t.client.of(QUEUE).length).toBeGreaterThan(0));
    // nothing while no instruction waits
    expect(screen.queryByTestId('chat.queue')).toBeNull();
    prompts = [
      queued('e1', t.me.id),
      queued('e2', t.me.id, { via_agent: true, state: 'waiting_for_device', scope_seq: 4 }),
      queued('e3', BOB, { scope_seq: 5 }),
      queued('e4', CAROL, { via_agent: true, state: 'withdrawing', scope_seq: 6 }),
      queued('e5', BOB, { via_agent: true, scope_seq: 7 }),
      queued('e6', t.me.id, { state: 'withdrawing', scope_seq: 8 }),
    ];
    // a change of the task's sessions reads the queue again
    act(() => putSession(t.store, sessionRow(t, { state: 'idle' }) as never, [], 0));
    const queue = await screen.findByTestId('chat.queue', {}, { timeout: 3000 });
    expect(queue).toHaveAttribute('data-count', '6');
    expect(within(queue).getByTestId('chat.queue.heading')).toHaveTextContent('6 instructions waiting');
    const items = within(queue).getAllByTestId('chat.queue.item');
    expect(items.map((item) => item.getAttribute('data-event'))).toEqual(['e1', 'e2', 'e3', 'e4', 'e5', 'e6']);
    expect(items.map((item) => within(item).getByTestId('chat.queue.author').textContent)).toEqual([
      'You',
      'You (through your agent)',
      'Bob',
      'Member (through their agent)',
      'Bob (through their agent)',
      'You',
    ]);
    expect(items.map((item) => item.getAttribute('data-state'))).toEqual([
      'waiting',
      'waiting_for_device',
      'waiting',
      'withdrawing',
      'waiting',
      'withdrawing',
    ]);
    expect(items[1]).toHaveAttribute('data-author', t.me.id);
    expect(within(items[1]!).getByTestId('chat.queue.state')).toHaveTextContent('Waiting for the device');
    expect(within(items[3]!).getByTestId('chat.queue.state')).toHaveTextContent('Being withdrawn');
    // only one's own prompts that are not being withdrawn can be withdrawn
    expect(items.map((item) => within(item).queryByTestId('chat.queue.withdraw') !== null)).toEqual([
      true,
      true,
      false,
      false,
      false,
      false,
    ]);
    const withdraw = `POST /v1/tasks/${TASK}/prompts/e1/withdraw`;
    t.client.on(withdraw, () => {
      throw new HubError('peer_outdated', 409);
    });
    fireEvent.click(within(items[0]!).getByTestId('chat.queue.withdraw'));
    const outdated = await within(items[0]!).findByTestId('chat.queue.error');
    expect(outdated).toHaveAttribute('data-code', 'peer_outdated');
    expect(outdated).toHaveTextContent('the device of the session must be updated first');
    t.client.on(withdraw, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(within(items[0]!).getByTestId('chat.queue.withdraw'));
    await waitFor(() =>
      expect(within(items[0]!).getByTestId('chat.queue.error')).toHaveAttribute('data-code', 'forbidden'),
    );
    expect(within(items[0]!).getByTestId('chat.queue.error')).toHaveTextContent('could not be withdrawn');
    let accept: () => void = () => undefined;
    t.client.on(withdraw, () => new Promise((resolve) => (accept = () => resolve({ state: 'withdrawing' }))));
    fireEvent.click(within(items[0]!).getByTestId('chat.queue.withdraw'));
    expect(within(items[0]!).getByTestId('chat.queue.withdraw')).toHaveTextContent('Withdrawing …');
    expect(within(items[1]!).getByTestId('chat.queue.withdraw')).toBeDisabled();
    expect(within(items[0]!).queryByTestId('chat.queue.error')).toBeNull();
    prompts = [queued('e1', t.me.id, { state: 'withdrawing' })];
    act(() => accept());
    await waitFor(() => expect(screen.getByTestId('chat.queue')).toHaveAttribute('data-count', '1'));
    expect(screen.getByTestId('chat.queue.heading')).toHaveTextContent('1 instruction waiting');
    expect(screen.queryByTestId('chat.queue.withdraw')).toBeNull();
  });

  it('offers no withdrawal to readers', async () => {
    const { t } = await page('', () => ({ task_id: TASK, prompts: [queued('e1', 'reader')], withdrawals: [] }));
    act(() => t.store.update('me', (me) => ({ ...me, userId: 'reader' })));
    const queue = await screen.findByTestId('chat.queue');
    expect(screen.getByTestId('chat.read-only')).toBeInTheDocument();
    expect(within(queue).getByTestId('chat.queue.author')).toHaveTextContent('You');
    expect(within(queue).queryByTestId('chat.queue.withdraw')).toBeNull();
  });
});

describe('withdrawn prompts in the timeline', () => {
  it('marks a prompt as withdrawn, being withdrawn or too late', async () => {
    const { t } = await page('', () => ({
      task_id: TASK,
      prompts: [],
      withdrawals: [
        { event_id: 'id-801', state: 'withdrawn', requested_at: '2026-11-24T10:00:00.000Z' },
        { event_id: 'id-802', state: 'withdrawing', requested_at: '2026-11-24T10:00:00.000Z' },
        { event_id: 'id-803', state: 'too_late', requested_at: '2026-11-24T10:00:00.000Z' },
      ],
    }));
    act(() =>
      putEvents(t.store, t.wg, [
        verified(t, 'prompt', { text: 'one' }, { seq: 801 }),
        verified(t, 'prompt', { text: 'two' }, { seq: 802 }),
        verified(t, 'prompt', { text: 'three' }, { seq: 803 }),
        verified(t, 'prompt', { text: 'four' }, { seq: 804 }),
      ]),
    );
    await waitFor(() =>
      expect(screen.getAllByTestId('chat.prompt').map((item) => item.getAttribute('data-withdrawal'))).toEqual([
        'withdrawn',
        'withdrawing',
        'too_late',
        null,
      ]),
    );
    expect(screen.getAllByTestId('chat.prompt.withdrawal').map((item) => item.textContent)).toEqual([
      'Withdrawn',
      'Being withdrawn',
      'Could not be withdrawn: it had already reached the AI',
    ]);
  });
});

describe('triggered by', () => {
  const op = (t: TaskWorld, id: string, triggered: unknown): OperationRow =>
    ({
      operation_id: id,
      org_id: t.org,
      wg_id: t.wg,
      task_id: TASK,
      session_id: MAIN,
      device_id: '0190f000-0000-7000-8000-0000000000d1',
      actor_id: t.me.id,
      type: 'github.comment',
      state: 'executing',
      error_code: null,
      retryable: false,
      duplicate_of: null,
      attempt: 1,
      params_event_id: null,
      result_event_id: null,
      created_at: `2026-11-21T12:00:0${id.slice(-1)}.000Z`,
      decided_at: null,
      started_at: null,
      finished_at: null,
      ...(triggered !== undefined && { triggered_by: triggered }),
    }) as OperationRow;

  it('names the person on operation cards and in the detail drawer', async () => {
    const { t } = await page('?tab=operations');
    act(() =>
      putOperations(t.store, [
        op(t, 'op-1', t.me.id),
        op(t, 'op-2', BOB),
        op(t, 'op-3', CAROL),
        op(t, 'op-4', null),
        op(t, 'op-5', undefined),
      ]),
    );
    const cards = await screen.findAllByTestId('operation.card');
    const named = Object.fromEntries(
      cards.map((card) => [
        card.getAttribute('data-operation'),
        within(card).queryByTestId('operation.triggered-by')?.textContent ?? null,
      ]),
    );
    expect(named).toEqual({
      'op-1': 'Triggered by you',
      'op-2': 'Triggered by Bob',
      'op-3': 'Triggered by Member',
      'op-4': null,
      'op-5': null,
    });
    const bob = cards.find((card) => card.getAttribute('data-operation') === 'op-2')!;
    expect(within(bob).getByTestId('operation.triggered-by')).toHaveAttribute('data-user', BOB);
  });

  it('names the person on session rows', async () => {
    const { t } = await page('?tab=sessions');
    act(() => {
      putSession(t.store, sessionRow(t, { state: 'working', triggered_by: BOB }) as never, [], 0);
      putSession(
        t.store,
        sessionRow(t, { session_id: 'child', parent_session_id: MAIN, depth: 1, triggered_by: null }) as never,
        [],
        0,
      );
    });
    const nodes = await screen.findAllByTestId('sessions.node');
    expect(within(nodes[0]!).getAllByTestId('session.triggered-by')[0]).toHaveTextContent('Triggered by Bob');
    expect(within(nodes[0]!).getAllByTestId('session.triggered-by')[0]).toHaveAttribute('data-user', BOB);
    expect(within(nodes[1]!).queryByTestId('session.triggered-by')).toBeNull();
  });
});

describe('consumption per person', () => {
  const people = (t: TaskWorld) => [
    { user_id: t.me.id, ...totals },
    { user_id: BOB, ...totals, runtime_micro_usd: 0 },
    { user_id: null, ...totals, tokens_micro_usd: 0 },
  ];

  it('shows the people of the organization when the hub sends them', async () => {
    const t = await taskWorld();
    let answer: unknown = { ...BILLING, people: people(t) };
    t.client.on(`GET /v1/orgs/${t.org}/billing`, () => answer);
    act(() => {
      setActiveOrg(t.store, t.org);
      putEvents(t.store, t.org, [profile(t, BOB, 'Bob')]);
    });
    mount(t, [{ path: '/settings', element: <BillingSection /> }], '/settings');
    const list = await screen.findByTestId('billing.people');
    const items = within(list).getAllByTestId('billing.people.item');
    expect(items.map((item) => item.getAttribute('data-user'))).toEqual([t.me.id, BOB, '']);
    expect(items[0]).toHaveTextContent('You: $0.45 (model $0.45, runtime $0.0030)');
    expect(items[1]).toHaveTextContent('Bob: $0.45 (model $0.45, runtime $0.00)');
    expect(items[2]).toHaveTextContent('Not attributed: $0.0030');
    answer = { ...BILLING, people: [] };
    fireEvent.click(screen.getByTestId('billing.reload'));
    expect(await screen.findByTestId('billing.people.empty')).toHaveTextContent('No usage yet.');
    answer = BILLING;
    fireEvent.click(screen.getByTestId('billing.reload'));
    await waitFor(() => expect(screen.queryByTestId('billing.people.empty')).toBeNull());
    expect(screen.queryByTestId('billing.people')).toBeNull();
    expect(screen.getByTestId('billing.balance')).toBeInTheDocument();
  });

  it('shows the people of a task when the hub sends them', async () => {
    const t = await taskWorld();
    let answer: unknown = {
      sessions: [{ session_id: MAIN, ...totals }],
      total_micro_usd: 448_200,
      people: people(t),
    };
    t.client.on(`GET /v1/tasks/${TASK}/charges`, () => answer);
    mount(t, [{ path: '/', element: <ChargesTab orgId={t.org} taskId={TASK} /> }], '/');
    const list = await screen.findByTestId('charges.people');
    expect(
      within(list)
        .getAllByTestId('charges.people.item')
        .map((item) => item.getAttribute('data-user')),
    ).toEqual([t.me.id, BOB, '']);
    expect(within(list).getAllByTestId('charges.people.item')[1]).toHaveTextContent('Member:');
    answer = { sessions: [{ session_id: MAIN, ...totals }], total_micro_usd: 448_200 };
    fireEvent.click(screen.getByTestId('charges.reload'));
    await waitFor(() => expect(screen.queryByTestId('charges.people')).toBeNull());
    expect(screen.getByTestId('charges.total')).toBeInTheDocument();
  });
});
