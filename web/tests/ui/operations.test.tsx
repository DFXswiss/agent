// Operation cards and the Operations tab in jsdom (06-web.md sections 5.2 and 5.4): cards in the chat with live state,
// result links, duplicates and errors; the compact result row; the tab newest first with paging and the detail drawer
// (transitions, decrypted parameters and result); "Renamed by session" on the task page; the Children tab.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { type OperationRow, putOperations } from '../../src/core/store/slices/operations';
import { setLog } from '../../src/core/store/slices/orgs';
import { putSession, putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { SessionScreen } from '../../src/ui/screens/session/SessionScreen';
import { originOf } from '../../src/ui/screens/session/children/ChildrenTab';
import { tab } from '../../src/ui/task-tabs/operations';
import { MAX_ROWS, PAGE, selectTaskOperations, WINDOW } from '../../src/ui/task-tabs/operations/OperationsTab';
import { CLIP_CHARS } from '../../src/ui/lists/Clipped';
import { actor } from '../core/log/world';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK } from './chat-world';
import { labelDevice } from './device-labels';

const DEVICE = '0190f000-0000-7000-8000-0000000000d1';
const OP = '0190f000-0000-7000-8000-0000000000b1';
const OP2 = '0190f000-0000-7000-8000-0000000000b2';

function row(t: TaskWorld, overrides: Partial<OperationRow> = {}): OperationRow {
  return {
    operation_id: OP,
    org_id: t.org,
    wg_id: t.wg,
    task_id: TASK,
    session_id: MAIN,
    device_id: DEVICE,
    actor_id: t.me.id,
    type: 'github.create_pull_request',
    state: 'executing',
    error_code: null,
    retryable: false,
    duplicate_of: null,
    attempt: 1,
    params_event_id: null,
    result_event_id: null,
    created_at: '2026-11-21T12:00:01.000Z',
    decided_at: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

let seq = 500;

function event(t: TaskWorld, kind: string, body: unknown, op = OP, author = DEVICE): EventEntry {
  const s = seq++;
  const header = { id: `e${s}`, kind, ref: op, author, task: TASK, session: MAIN, ts: '2026-11-21T12:00:05.000Z' };
  return {
    org: t.org,
    scopeId: t.wg,
    scopeSeq: s,
    receivedAt: null,
    event: { header },
    status: 'verified',
    code: null,
    header,
    body,
  };
}

const prParams = (op = OP) => ({
  operation_id: op,
  type: 'github.create_pull_request',
  params: { repo: 'dfx/agent', head: 'feat', base: 'main', title: 'Add x' },
});
const prResult = (op = OP) => ({
  operation_id: op,
  status: 'succeeded',
  result: { number: 7, url: 'https://github.com/dfx/agent/pull/7', id: 1, draft: true },
});

async function page(path = '', setup: (t: TaskWorld) => void = () => undefined) {
  const t = await taskWorld();
  const task = taskRow(t) as TaskRow;
  t.client.on(`GET /v1/tasks/${TASK}`, () => ({
    task,
    sessions: [{ session: sessionRow(t, { state: 'working' }), transitions: [] }],
  }));
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
  t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: [], next_before: null }));
  putTasks(t.store, [task]);
  setup(t);
  const mounted = mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}${path}`);
  await screen.findByTestId('task.screen');
  return { t, ...mounted };
}

describe('operation cards in the chat', () => {
  it('shows type, live state, subject and result links, then the finished row', async () => {
    const { t } = await page();
    act(() => putEvents(t.store, t.wg, [event(t, 'operation.params', prParams())]));
    const card = await screen.findByTestId('operation.card');
    expect(card).toHaveAttribute('data-type', 'github.create_pull_request');
    expect(within(card).getByTestId('operation.type')).toHaveTextContent('Create pull request');
    expect(within(card).getByTestId('operation.subject')).toHaveTextContent('dfx/agent feat');
    // no row yet: the task's operations are loaded once
    await waitFor(() => expect(t.client.of(`GET /v1/tasks/${TASK}/operations`)).toHaveLength(1));
    expect(within(card).getByTestId('operation.state')).toHaveAttribute('data-state', 'received');
    act(() => putOperations(t.store, [row(t)]));
    expect(within(screen.getByTestId('operation.card')).getByTestId('operation.state')).toHaveTextContent('Running');
    act(() => {
      putOperations(t.store, [row(t, { state: 'succeeded' })]);
      putEvents(t.store, t.wg, [event(t, 'operation.result', prResult())]);
    });
    const fact = screen.getByTestId('operation.fact');
    expect(fact).toHaveAttribute('data-label', 'pull_request');
    expect(within(fact).getByRole('link')).toHaveAttribute('href', 'https://github.com/dfx/agent/pull/7');
    expect(within(fact).getByRole('link')).toHaveAttribute('rel', 'noopener noreferrer nofollow');
    expect(screen.getByTestId('chat.operation-result')).toHaveTextContent('Create pull request finished: Done');
    expect(screen.getByTestId('operation.open')).toHaveAttribute(
      'href',
      `/o/${t.org}/t/${TASK}?tab=operations&op=${OP}`,
    );
  });

  it('shows duplicates, errors, renames, subagent sessions, commits and unknown types', async () => {
    const { t } = await page();
    act(() => {
      putOperations(t.store, [
        row(t, { state: 'duplicate', duplicate_of: OP2 }),
        row(t, { operation_id: OP2, type: 'github.comment', state: 'failed', error_code: 'repo_not_allowed' }),
        row(t, { operation_id: 'op-3', type: 'task.set_title', state: 'succeeded' }),
        row(t, { operation_id: 'op-4', type: 'subagent.request', state: 'succeeded' }),
        row(t, { operation_id: 'op-5', type: 'github.commit', state: 'succeeded' }),
        row(t, { operation_id: 'op-6', type: 'custom.thing', state: 'accepted' }),
        row(t, { operation_id: 'op-7', type: 'github.push', state: 'accepted' }),
      ]);
      putEvents(t.store, t.wg, [
        event(t, 'operation.params', prParams()),
        event(
          t,
          'operation.params',
          { operation_id: OP2, type: 'github.comment', params: { repo: 'a/b', number: 2, body: 'x' } },
          OP2,
        ),
        event(
          t,
          'operation.params',
          { operation_id: 'op-3', type: 'task.set_title', params: { title: 'Better title' } },
          'op-3',
        ),
        event(t, 'operation.result', { operation_id: 'op-3', status: 'succeeded', result: {} }, 'op-3'),
        event(
          t,
          'operation.params',
          { operation_id: 'op-4', type: 'subagent.request', params: { purpose: 'Review', prompt: 'p' } },
          'op-4',
        ),
        event(
          t,
          'operation.result',
          { operation_id: 'op-4', status: 'succeeded', result: { session_id: 'child-session' } },
          'op-4',
        ),
        event(
          t,
          'operation.params',
          { operation_id: 'op-5', type: 'github.commit', params: { repo: 'a/b', branch: 'x', message: 'm' } },
          'op-5',
        ),
        event(
          t,
          'operation.result',
          { operation_id: 'op-5', status: 'succeeded', result: { commit_sha: 'a'.repeat(40) } },
          'op-5',
        ),
        event(t, 'operation.params', { operation_id: 'op-6', type: 'custom.thing', params: {} }, 'op-6'),
        event(t, 'operation.params', { operation_id: 'op-7', type: 'github.push', params: {} }, 'op-7', 'someone-else'),
      ]);
    });
    const cards = await screen.findAllByTestId('operation.card');
    const by = (op: string) => cards.find((card) => card.getAttribute('data-operation') === op)!;
    expect(within(by(OP)).getByTestId('operation.duplicate-of')).toHaveTextContent(`Duplicate of ${OP2.slice(0, 8)}`);
    expect(within(by(OP)).getByTestId('operation.duplicate-of')).toHaveAttribute(
      'href',
      `/o/${t.org}/t/${TASK}?tab=operations&op=${OP2}`,
    );
    expect(within(by(OP2)).getByTestId('operation.error')).toHaveAttribute('data-code', 'repo_not_allowed');
    expect(within(by('op-3')).getByTestId('operation.renamed')).toHaveTextContent('Renamed by session');
    expect(within(by('op-3')).getByTestId('operation.fact')).toHaveTextContent('New title: Better title');
    expect(within(by('op-4')).getByTestId('operation.fact').querySelector('a')).toHaveAttribute(
      'href',
      `/o/${t.org}/s/child-session`,
    );
    expect(within(by('op-5')).getByTestId('operation.fact')).toHaveTextContent(`Commit: ${'a'.repeat(12)}`);
    expect(within(by('op-6')).getByTestId('operation.type')).toHaveTextContent('custom.thing');
    expect(within(by('op-7')).getByTestId('operation.params.unverifiable')).toHaveTextContent('header_invalid');
    expect(screen.getAllByTestId('chat.operation-result').map((row) => row.getAttribute('data-status'))).toEqual([
      'succeeded',
      'succeeded',
      'succeeded',
    ]);
  });

  it('marks unverifiable results and result rows of operations it has no row for', async () => {
    const { t } = await page();
    act(() =>
      putEvents(t.store, t.wg, [
        event(t, 'operation.params', prParams()),
        event(t, 'operation.result', { operation_id: OP, status: 'succeeded' }),
        event(t, 'operation.result', { operation_id: 'op-9', status: 7 }, 'op-9'),
        event(t, 'operation.result', { status: 'failed' }, 'op-10'),
      ]),
    );
    expect(await screen.findByTestId('operation.result.unverifiable')).toHaveTextContent('params_invalid');
    const rows = screen.getAllByTestId('chat.operation-result');
    expect(rows.map((item) => item.getAttribute('data-status'))).toEqual(['succeeded']);
    // a verified result without a status or without an operation is a neutral row, never content
    expect(screen.getAllByTestId('chat.operation.invalid')).toHaveLength(2);
  });
});

describe('operation card loading', () => {
  it('stays without a row when the task operations cannot be loaded', async () => {
    const { t } = await page('', (w) =>
      w.client.on(`GET /v1/tasks/${TASK}/operations`, () => {
        throw new HubError('unavailable', 503);
      }),
    );
    act(() => putEvents(t.store, t.wg, [event(t, 'operation.params', prParams())]));
    await waitFor(() => expect(t.client.of(`GET /v1/tasks/${TASK}/operations`).length).toBeGreaterThan(0));
    expect(screen.getByTestId('operation.state')).toHaveAttribute('data-state', 'received');
  });
});

describe('operations outside the first page and other tasks', () => {
  it('loads the operation of a card by ID when the first page does not hold it', async () => {
    const { t } = await page('', (w) =>
      w.client.on(`GET /v1/operations/${OP}`, () => ({ operation: row(w, { state: 'succeeded' }), transitions: [] })),
    );
    act(() => putEvents(t.store, t.wg, [event(t, 'operation.params', prParams())]));
    await waitFor(() => expect(screen.getByTestId('operation.state')).toHaveAttribute('data-state', 'succeeded'));
  });

  it('opens the original of a duplicate that belongs to another task', async () => {
    const OTHER_TASK = '0190f000-0000-7000-8000-000000000002';
    await page(`?tab=operations&op=${OP2}`, (w) => {
      w.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({
        operations: [row(w, { state: 'duplicate', duplicate_of: OP2 })],
        next_before: null,
      }));
      w.client.on(`GET /v1/operations/${OP2}`, () => ({
        operation: row(w, { operation_id: OP2, task_id: OTHER_TASK, state: 'succeeded' }),
        transitions: [],
      }));
    });
    const detail = await screen.findByTestId('operations.detail');
    expect(within(detail).getByTestId('operation.card')).toHaveAttribute('data-operation', OP2);
    expect(screen.getAllByTestId('operations.item')).toHaveLength(1);
  });
});

describe('Operations tab', () => {
  it('is registered after the sessions tab', () => {
    expect(tab).toMatchObject({ id: 'operations', order: 30, labelKey: 'operations.tab' });
  });

  it('lists operations newest first, pages, opens the detail and closes it', async () => {
    let pages = 0;
    const { t, router } = await page('?tab=operations', (w) =>
      w.client.on(`GET /v1/tasks/${TASK}/operations`, () => {
        pages += 1;
        return pages === 1
          ? {
              operations: [
                row(w),
                row(w, { operation_id: OP2, created_at: '2026-11-21T12:00:09.000Z', type: 'github.push' }),
              ],
              next_before: 'c1',
            }
          : {
              operations: [row(w, { operation_id: 'op-old', created_at: '2026-11-21T11:00:00.000Z' })],
              next_before: null,
            };
      }),
    );
    const items = await screen.findAllByTestId('operations.item');
    expect(items.map((item) => item.getAttribute('data-operation'))).toEqual([OP2, OP]);
    act(() => putOperations(t.store, [row(t, { operation_id: 'op-odd-time', created_at: 'not a time' })]));
    expect(await screen.findByText('not a time')).toBeVisible();
    fireEvent.click(screen.getByTestId('operations.more'));
    await waitFor(() => expect(screen.getAllByTestId('operations.item')).toHaveLength(4));
    expect(screen.queryByTestId('operations.more')).toBeNull();
    const step = (n: number, state: string, by: string | null, code: string | null = null) => ({
      seq: n,
      state,
      error_code: code,
      at: `2026-11-21T12:00:0${n}.000Z`,
      by_id: by,
    });
    t.client.on(`GET /v1/operations/${OP}`, () => ({
      operation: row(t, { state: 'failed', error_code: 'upstream_error', retryable: true }),
      transitions: [
        step(1, 'received', DEVICE),
        step(2, 'accepted', null),
        step(3, 'failed', DEVICE, 'upstream_error'),
      ],
    }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null }));
    act(() =>
      putEvents(t.store, t.wg, [
        event(t, 'operation.params', prParams()),
        event(t, 'operation.result', {
          operation_id: OP,
          status: 'failed',
          error: { code: 'upstream_error', retryable: true },
        }),
      ]),
    );
    fireEvent.click(within(items[1]!).getByTestId('operations.open'));
    const detail = await screen.findByTestId('operations.detail');
    await waitFor(() => expect(within(detail).getAllByTestId('operations.transition')).toHaveLength(3));
    const transitions = within(detail).getAllByTestId('operations.transition');
    // the device by its neutral name until its label is read, then by its label (KOR-05, B-23)
    expect(transitions[0]).toHaveTextContent(`Device …${DEVICE.slice(-6)}`);
    act(() => labelDevice(t.store, t.org, DEVICE, 'Build box'));
    expect(transitions[0]).toHaveTextContent('Build box');
    expect(transitions[1]).toHaveTextContent('hub');
    expect(transitions[2]).toHaveTextContent('Failed');
    expect(within(detail).getByText('Can be retried')).toBeVisible();
    expect(within(detail).getByTestId('operations.detail.params')).toHaveTextContent('"repo": "dfx/agent"');
    expect(within(detail).getByTestId('operations.detail.result')).toHaveTextContent('"code": "upstream_error"');
    expect(t.client.calls.some((call) => call.target.includes(`ref=${OP}`))).toBe(true);
    fireEvent.click(within(detail).getByTestId('operations.detail.close'));
    await waitFor(() => expect(router.state.location.search).toBe('?tab=operations'));
    expect(screen.queryByTestId('operations.detail')).toBeNull();
  });

  it('shows missing parts, a failed detail load and the result of a success', async () => {
    const { t } = await page(`?tab=operations&op=${OP}`, (w) => {
      w.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({
        operations: [row(w, { state: 'succeeded' })],
        next_before: null,
      }));
      w.client.on(`GET /v1/operations/${OP}`, () => {
        throw new HubError('unavailable', 503);
      });
    });
    const detail = await screen.findByTestId('operations.detail');
    expect(within(detail).getByTestId('operations.detail.loading')).toBeVisible();
    expect(await within(detail).findByTestId('operations.detail.failed')).toHaveTextContent('Service unavailable');
    expect(within(detail).getByTestId('operations.detail.params')).toHaveAttribute('data-status', 'missing');
    act(() => putEvents(t.store, t.wg, [event(t, 'operation.result', prResult())]));
    expect(within(detail).getByTestId('operations.detail.result')).toHaveTextContent('"number": 7');
    act(() =>
      putEvents(t.store, t.wg, [
        { ...event(t, 'operation.params', null), status: 'unverifiable', code: 'signature_invalid', header: null },
      ]),
    );
    expect(within(detail).getByTestId('operations.detail.params')).toHaveTextContent('signature_invalid');
  });

  it('loads an opened operation that is not in the pages', async () => {
    await page(`?tab=operations&op=${OP2}`, (w) =>
      w.client.on(`GET /v1/operations/${OP2}`, () => ({ operation: row(w, { operation_id: OP2 }), transitions: [] })),
    );
    expect(await screen.findByTestId('operations.detail')).toBeVisible();
    expect(await screen.findByTestId('operations.transitions')).toBeEmptyDOMElement();
  });

  it('shows the empty state', async () => {
    const { t } = await page('?tab=operations');
    expect(await screen.findByTestId('operations.empty')).toBeVisible();
    t.store.reset();
    await waitFor(() => expect(screen.queryByTestId('task.screen')).toBeNull());
  });

  it('reports failed loads', async () => {
    const t = await taskWorld();
    const task = taskRow(t) as TaskRow;
    putTasks(t.store, [task]);
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task, sessions: [] }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null }));
    let call = 0;
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => {
      call += 1;
      if (call === 1) return { operations: [row(t)], next_before: 'more' };
      throw new HubError('rate_limited', 429);
    });
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}?tab=operations`);
    fireEvent.click(await screen.findByTestId('operations.more'));
    expect(await screen.findByTestId('operations.failed')).toHaveTextContent('Too many attempts');
  });

  it('reports a failed first page', async () => {
    const t = await taskWorld();
    const task = taskRow(t) as TaskRow;
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task, sessions: [] }));
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => {
      throw new HubError('unavailable', 503);
    });
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}?tab=operations`);
    expect(await screen.findByTestId('operations.failed')).toBeVisible();
    expect(screen.queryByTestId('operations.loading')).toBeNull();
  });
});

const at = (n: number) => new Date(Date.UTC(2026, 10, 20) + n * 1000).toISOString();

describe('Operations tab under volume', () => {
  it('renders a window of the newest rows, moves it and loads the next page at the end', async () => {
    const COUNT = 500;
    let pages = 0;
    let arrive: () => void = () => undefined;
    const { t } = await page('?tab=operations', (w) => {
      putOperations(w.store, [
        ...Array.from({ length: COUNT }, (_, n) => row(w, { operation_id: `op-${n}`, created_at: at(n + 10) })),
        row(w, { operation_id: 'op-foreign', task_id: '0190f000-0000-7000-8000-000000000002' }),
      ]);
      w.client.on(`GET /v1/tasks/${TASK}/operations`, () => {
        pages += 1;
        if (pages === 1) return { operations: [], next_before: 'c1' };
        return new Promise((resolve) => {
          arrive = () =>
            resolve({ operations: [row(w, { operation_id: 'op-old', created_at: at(0) })], next_before: null });
        });
      });
    });
    const list = await screen.findByTestId('operations.list');
    expect(within(list).getAllByTestId('operations.item')).toHaveLength(WINDOW);
    expect(within(list).getAllByTestId('operations.item')[0]).toHaveAttribute('data-operation', `op-${COUNT - 1}`);
    expect(list).toHaveAttribute('data-total', String(COUNT));
    await waitFor(() => expect(t.store.getState().operations.tasks[TASK]?.nextBefore).toBe('c1'));
    expect(screen.getByTestId('operations.list.older')).toHaveTextContent(String(COUNT - WINDOW));
    expect(screen.queryByTestId('operations.more')).toBeNull();
    fireEvent.click(screen.getByTestId('operations.list.older'));
    expect(within(list).getAllByTestId('operations.item')).toHaveLength(WINDOW + PAGE);
    while (screen.queryByTestId('operations.list.older') !== null) {
      fireEvent.click(screen.getByTestId('operations.list.older'));
      expect(within(list).getAllByTestId('operations.item').length).toBeLessThanOrEqual(MAX_ROWS);
    }
    // the window reached the loaded end: the next page from the hub
    fireEvent.click(screen.getByTestId('operations.more'));
    await waitFor(() => expect(screen.getByTestId('operations.more')).toBeDisabled());
    act(() => arrive());
    await waitFor(() => expect(list.querySelector('[data-operation="op-old"]')).not.toBeNull());
    expect(screen.queryByTestId('operations.more')).toBeNull();
    expect(list.querySelector('[data-operation="op-foreign"]')).toBeNull();
  });

  it('clips a large result in the detail and offers "Show more" and "Download"', async () => {
    const { t } = await page(`?tab=operations&op=${OP}`, (w) => {
      w.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({
        operations: [row(w, { state: 'succeeded' })],
        next_before: null,
      }));
      w.client.on(`GET /v1/operations/${OP}`, () => ({ operation: row(w, { state: 'succeeded' }), transitions: [] }));
    });
    const big = 'x'.repeat(CLIP_CHARS * 2);
    act(() =>
      putEvents(t.store, t.wg, [
        event(t, 'operation.result', { operation_id: OP, status: 'succeeded', result: { log: big } }),
      ]),
    );
    const detail = await screen.findByTestId('operations.detail');
    const result = within(detail).getByTestId('operations.detail.result');
    expect(result.textContent).toHaveLength(CLIP_CHARS);
    expect(within(detail).getByTestId('operations.detail.result.download')).toBeVisible();
    fireEvent.click(within(detail).getByTestId('operations.detail.result.more'));
    expect(within(detail).getByTestId('operations.detail.result').textContent!.length).toBeGreaterThan(CLIP_CHARS);
  });
});

describe('selectTaskOperations', () => {
  it('builds the views of the task once and keeps them while their row and events are the same', async () => {
    const t = await taskWorld();
    const select = selectTaskOperations(t.wg, TASK);
    putOperations(t.store, [row(t), row(t, { operation_id: OP2, created_at: at(99_999_999) })]);
    // no events of the workgroup yet
    const first = select(t.store.getState());
    expect(first.map((view) => view.id)).toEqual([OP2, OP]);
    expect(first[1]!.params.status).toBe('missing');
    expect(select(t.store.getState())).toBe(first);
    const pending: EventEntry = { ...event(t, 'operation.params', prParams()), status: 'pending', header: null };
    const broken: EventEntry = { ...event(t, 'operation.params', null), status: 'unverifiable', event: null };
    const noRef: EventEntry = {
      ...event(t, 'operation.params', null),
      status: 'unverifiable',
      code: 'signature_invalid',
      header: null,
      event: { header: { kind: 'operation.params', ref: 7 } },
    };
    act(() =>
      putEvents(t.store, t.wg, [
        event(t, 'chat.prompt', { text: 'hi' }),
        pending,
        broken,
        noRef,
        event(t, 'operation.params', prParams()),
        event(t, 'operation.result', prResult()),
        {
          ...event(t, 'operation.params', null, OP2),
          status: 'unverifiable',
          code: 'signature_invalid',
          header: null,
        },
      ]),
    );
    const second = select(t.store.getState());
    expect(second[1]!.params.status).toBe('verified');
    expect(second[1]!.facts[0]!.label).toBe('pull_request');
    expect(second[0]!.params).toEqual({ status: 'unverifiable', code: 'signature_invalid' });
    // another task's operation and events of other operations: the same array
    putOperations(t.store, [row(t, { operation_id: 'op-x', task_id: '0190f000-0000-7000-8000-000000000002' })]);
    act(() => putEvents(t.store, t.wg, [event(t, 'operation.params', prParams('op-x'), 'op-x')]));
    expect(select(t.store.getState())).toBe(second);
    // a changed row: a new array, the other view stays
    putOperations(t.store, [row(t, { operation_id: OP2, created_at: at(99_999_999), state: 'succeeded' })]);
    const third = select(t.store.getState());
    expect(third).not.toBe(second);
    expect(third[0]!.state).toBe('succeeded');
    expect(third[1]).toBe(second[1]);
  });
});

describe('renamed by session', () => {
  it('marks a title written by a device of the org', async () => {
    const { t } = await page();
    expect(screen.queryByTestId('task.renamed')).toBeNull();
    const device = await actor('rename-ui-device');
    await t.world.admitDevice(t.me, device, [t.wg]);
    act(() => {
      setLog(t.store, t.org, t.world.log!);
      putEvents(t.store, t.wg, [event(t, 'task.meta', { title: 'Better' }, '', device.id)]);
    });
    expect(screen.getByTestId('task.renamed')).toHaveTextContent('Renamed by session');
  });

  it('keeps the row when its reload fails', async () => {
    const { t } = await page();
    t.client.on(`GET /v1/tasks/${TASK}`, () => {
      throw new HubError('unavailable', 503);
    });
    act(() => putEvents(t.store, t.wg, [event(t, 'task.meta', { title: 'Newer' }, '', t.me.id)]));
    await waitFor(() => expect(t.client.of(`GET /v1/tasks/${TASK}`).length).toBeGreaterThan(1));
    expect(screen.getByTestId('task.screen')).toBeVisible();
  });

  it('shows nothing for operation items whose event does not refer to their operation', async () => {
    const { t } = await page();
    act(() =>
      putEvents(t.store, t.wg, [
        event(t, 'operation.params', prParams('other-op'), OP),
        event(t, 'operation.result', prResult('other-op'), OP),
      ]),
    );
    await waitFor(() => expect(screen.getAllByTestId('chat.item').length).toBeGreaterThan(0));
    expect(screen.queryByTestId('operation.card')).toBeNull();
    expect(screen.queryByTestId('chat.operation-result')).toBeNull();
  });
});

describe('Children tab', () => {
  async function sessionPage(t: TaskWorld, session: string) {
    const task = taskRow(t) as TaskRow;
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task, sessions: [] }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null }));
    t.client.on(`GET /v1/sessions/${session}`, () => ({ session: sessionRow(t, { session_id: session }) }));
    mount(t, [{ path: '/o/:org/s/:session', element: <SessionScreen /> }], `/o/${t.org}/s/${session}?tab=children`);
  }

  it('shows the subagent tree with the requested purposes', async () => {
    const t = await taskWorld();
    const CHILD = '0190f000-0000-7000-8000-0000000000a2';
    const GRAND = '0190f000-0000-7000-8000-0000000000a3';
    putTasks(t.store, [taskRow(t) as TaskRow]);
    putSession(t.store, sessionRow(t, { state: 'working' }) as never, [], 0);
    putSession(
      t.store,
      sessionRow(t, {
        session_id: CHILD,
        parent_session_id: MAIN,
        depth: 1,
        origin_operation_id: 'op-c',
        state: 'working',
        model_id: 'm1',
        created_at: '2026-11-21T12:00:01.000Z',
      }) as never,
      [],
      0,
    );
    putSession(
      t.store,
      sessionRow(t, {
        session_id: GRAND,
        parent_session_id: CHILD,
        depth: 2,
        origin_operation_id: 'op-g',
        state: 'idle',
        created_at: '2026-11-21T12:00:02.000Z',
      }) as never,
      [],
      0,
    );
    putEvents(t.store, t.wg, [
      event(
        t,
        'operation.params',
        { operation_id: 'op-c', type: 'subagent.request', params: { purpose: 'Review the PR', prompt: 'p' } },
        'op-c',
      ),
      {
        ...event(t, 'operation.params', null, 'op-g'),
        status: 'unverifiable',
        code: 'signature_invalid',
        header: null,
      },
    ]);
    putEvents(t.store, t.wg, [
      event(
        t,
        'operation.result',
        { operation_id: 'op-c', status: 'succeeded', result: { session_id: CHILD } },
        'op-c',
      ),
    ]);
    await sessionPage(t, MAIN);
    const children = await screen.findAllByTestId('session.child');
    expect(children.map((child) => child.getAttribute('data-depth'))).toEqual(['1', '2']);
    expect(within(children[0]!).getAllByTestId('children.purpose')[0]).toHaveTextContent('Purpose: Review the PR');
    expect(within(children[1]!).getByTestId('children.purpose')).toHaveTextContent(
      'Unverifiable purpose (signature_invalid)',
    );
    expect(within(children[1]!).getByTestId('session.child.link')).toHaveAttribute('href', `/o/${t.org}/s/${GRAND}`);
    expect(t.client.calls.some((call) => call.target.includes('kinds=operation.params%2Coperation.result'))).toBe(true);
  });

  it('refuses a purpose whose verified result names another session', async () => {
    const t = await taskWorld();
    const CHILD = '0190f000-0000-7000-8000-0000000000a2';
    putTasks(t.store, [taskRow(t) as TaskRow]);
    putSession(t.store, sessionRow(t, { state: 'working' }) as never, [], 0);
    putSession(
      t.store,
      sessionRow(t, { session_id: CHILD, parent_session_id: MAIN, depth: 1, origin_operation_id: 'op-s' }) as never,
      [],
      0,
    );
    putEvents(t.store, t.wg, [
      event(
        t,
        'operation.params',
        { operation_id: 'op-s', type: 'subagent.request', params: { purpose: 'Swapped', prompt: 'p' } },
        'op-s',
      ),
      event(
        t,
        'operation.result',
        { operation_id: 'op-s', status: 'succeeded', result: { session_id: 'another' } },
        'op-s',
      ),
    ]);
    await sessionPage(t, MAIN);
    expect(await screen.findByTestId('children.purpose')).toHaveTextContent('Unverifiable purpose (header_invalid)');
  });

  it('shows no children for a session outside the tree', async () => {
    const t = await taskWorld();
    const LOST = '0190f000-0000-7000-8000-0000000000a9';
    putTasks(t.store, [taskRow(t) as TaskRow]);
    putSession(
      t.store,
      sessionRow(t, { session_id: LOST, parent_session_id: 'unknown-parent', depth: 1 }) as never,
      [],
      0,
    );
    await sessionPage(t, LOST);
    expect(await screen.findByTestId('session.children.empty')).toBeVisible();
  });

  it('shows no purpose without an origin operation or before its parameters are read', async () => {
    const t = await taskWorld();
    const CHILD = '0190f000-0000-7000-8000-0000000000a2';
    putTasks(t.store, [taskRow(t) as TaskRow]);
    putSession(t.store, sessionRow(t, { state: 'working' }) as never, [], 0);
    putSession(
      t.store,
      sessionRow(t, { session_id: CHILD, parent_session_id: MAIN, depth: 1, origin_operation_id: 'op-x' }) as never,
      [],
      0,
    );
    await sessionPage(t, MAIN);
    expect(await screen.findByTestId('session.child')).toBeVisible();
    expect(screen.queryByTestId('children.purpose')).toBeNull();
    expect(originOf({ row: sessionRow(t) as never, transitions: [], estimatedAt: null })).toBeNull();
  });
});
