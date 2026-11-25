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

describe('issue and pull request operations', () => {
  const ISSUE_URL = 'https://github.com/dfx/agent/issues/4';
  const issue = (number: number, title: string, extra: Record<string, unknown> = {}) => ({
    number,
    title,
    state: 'open',
    state_reason: null,
    author: 'octo',
    labels: [],
    assignees: [],
    comments: 0,
    created_at: '2026-11-20T10:00:00Z',
    updated_at: '2026-11-20T10:00:00Z',
    closed_at: null,
    url: `https://github.com/dfx/agent/issues/${number}`,
    ...extra,
  });
  const op = (t: TaskWorld, id: string, type: string, p: unknown, result: unknown) => [
    event(t, 'operation.params', { operation_id: id, type, params: p }, id),
    event(t, 'operation.result', { operation_id: id, status: 'succeeded', result }, id),
  ];

  it('shows follow-up instructions and stops of subagents with the session and its state (ADR-088)', async () => {
    const { t } = await page();
    const child = '0190f000-0000-7000-8000-0000000000c1';
    act(() => {
      putOperations(t.store, [
        row(t, { operation_id: 'op-1', type: 'subagent.instruct', state: 'succeeded' }),
        row(t, { operation_id: 'op-2', type: 'subagent.stop', state: 'succeeded' }),
      ]);
      putEvents(t.store, t.wg, [
        ...op(
          t,
          'op-1',
          'subagent.instruct',
          { session_id: child, prompt: 'Check the tests.' },
          {
            session_id: child,
            instruction_id: 'op-1',
          },
        ),
        ...op(t, 'op-2', 'subagent.stop', { session_id: child }, { session_id: child, state: 'stopped' }),
      ]);
    });
    const cards = await screen.findAllByTestId('operation.card');
    const by = (id: string) => cards.find((card) => card.getAttribute('data-operation') === id)!;
    expect(within(by('op-1')).getByTestId('operation.type')).toHaveTextContent('Instruct subagent');
    expect(within(by('op-1')).getByTestId('operation.subject')).toHaveTextContent('Check the tests.');
    expect(within(by('op-1')).getByTestId('operation.fact').querySelector('a')).toHaveAttribute(
      'href',
      `/o/${t.org}/s/${child}`,
    );
    expect(within(by('op-2')).getByTestId('operation.type')).toHaveTextContent('Stop subagent');
    const facts = within(by('op-2')).getAllByTestId('operation.fact');
    expect(facts.map((fact) => fact.getAttribute('data-label'))).toEqual(['session', 'stop_state']);
    expect(facts[1]).toHaveTextContent('State: ended');
  });

  it('lists issues and pull requests with number, title, state and labels, as text only', async () => {
    const { t } = await page();
    act(() => {
      putOperations(t.store, [
        row(t, { operation_id: 'op-1', type: 'github.list_issues', state: 'succeeded' }),
        row(t, { operation_id: 'op-2', type: 'github.list_pull_requests', state: 'succeeded' }),
        row(t, { operation_id: 'op-3', type: 'github.list_issues', state: 'succeeded' }),
      ]);
      putEvents(t.store, t.wg, [
        ...op(
          t,
          'op-1',
          'github.list_issues',
          { repo: 'dfx/agent' },
          {
            issues: [
              issue(4, '<img src=x onerror=alert(1)>', { labels: ['bug', 'p1'] }),
              issue(3, 'Closed one', { state: 'closed', url: 'javascript:alert(1)' }),
            ],
            next_page: 2,
            truncated: true,
          },
        ),
        ...op(
          t,
          'op-2',
          'github.list_pull_requests',
          { repo: 'dfx/agent', state: 'all' },
          {
            pull_requests: [
              {
                number: 7,
                title: 'Draft PR',
                state: 'open',
                draft: true,
                author: 'octo',
                head: 'feat',
                base: 'main',
                created_at: '2026-11-20T10:00:00Z',
                updated_at: '2026-11-20T10:00:00Z',
                url: 'https://github.com/dfx/agent/pull/7',
              },
            ],
            next_page: null,
            truncated: false,
          },
        ),
        ...op(
          t,
          'op-3',
          'github.list_issues',
          { repo: 'dfx/other' },
          { issues: [], next_page: null, truncated: false },
        ),
      ]);
    });
    const cards = await screen.findAllByTestId('operation.card');
    const by = (id: string) => cards.find((card) => card.getAttribute('data-operation') === id)!;
    const issues = within(by('op-1'));
    expect(issues.getByTestId('operation.type')).toHaveTextContent('List issues');
    expect(issues.getByTestId('operation.subject')).toHaveTextContent('dfx/agent');
    expect(issues.getByTestId('operation.fact')).toHaveTextContent('Issues: 2');
    expect(issues.getByTestId('operation.listing')).toHaveAttribute('data-kind', 'issues');
    const entries = issues.getAllByTestId('operation.entry');
    expect(entries.map((entry) => entry.getAttribute('data-number'))).toEqual(['4', '3']);
    // untrusted text stays text: no element is created from it
    expect(within(entries[0]!).getByTestId('operation.entry.title')).toHaveTextContent('<img src=x onerror=alert(1)>');
    expect(by('op-1').querySelector('img')).toBeNull();
    expect(within(entries[0]!).getByRole('link')).toHaveAttribute('href', 'https://github.com/dfx/agent/issues/4');
    expect(within(entries[0]!).getByRole('link')).toHaveAttribute('rel', 'noopener noreferrer nofollow');
    expect(
      within(entries[0]!)
        .getAllByTestId('operation.entry.label')
        .map((l) => l.textContent),
    ).toEqual(['bug', 'p1']);
    // a URL that is not https is no link
    expect(within(entries[1]!).queryByRole('link')).toBeNull();
    expect(within(entries[1]!).getByTestId('operation.entry.state')).toHaveTextContent('closed');
    expect(issues.getByTestId('operation.listing.next')).toHaveTextContent('More on page 2.');
    expect(issues.getByTestId('operation.truncated')).toBeInTheDocument();
    const prs = within(by('op-2'));
    expect(prs.getByTestId('operation.type')).toHaveTextContent('List pull requests');
    expect(prs.getByTestId('operation.listing')).toHaveAttribute('data-kind', 'pull_requests');
    expect(prs.getByTestId('operation.entry.draft')).toHaveTextContent('draft');
    expect(prs.queryByTestId('operation.listing.next')).toBeNull();
    expect(within(by('op-3')).getByTestId('operation.listing.empty')).toHaveTextContent('No issues.');
  });

  it('shows a read issue with its text and comments, a created and an updated issue and the connection', async () => {
    const { t } = await page();
    act(() => {
      putOperations(t.store, [
        row(t, { operation_id: 'op-1', type: 'github.get_issue', state: 'succeeded' }),
        row(t, { operation_id: 'op-2', type: 'github.create_issue', state: 'succeeded' }),
        row(t, { operation_id: 'op-3', type: 'github.update_issue', state: 'succeeded' }),
        row(t, { operation_id: 'op-4', type: 'github.request_connection', state: 'succeeded' }),
        row(t, { operation_id: 'op-5', type: 'github.get_issue', state: 'succeeded' }),
      ]);
      putEvents(t.store, t.wg, [
        ...op(
          t,
          'op-1',
          'github.get_issue',
          { repo: 'dfx/agent', number: 4 },
          {
            ...issue(4, 'Login fails', { state: 'closed', state_reason: 'completed', labels: ['bug'] }),
            body: 'Steps:\n1. <script>alert(1)</script>',
            pull_request: false,
            comments_total: 3,
            comments: [
              { id: 11, author: 'alice', body: 'Seen it too', created_at: '2026-11-21T09:00:00Z' },
              { id: 12, author: 'bob', body: '**not markdown**', created_at: '2026-11-21T10:00:00Z' },
            ],
            comments_next_page: 2,
            truncated: false,
          },
        ),
        ...op(
          t,
          'op-2',
          'github.create_issue',
          { repo: 'dfx/agent', title: 'New bug', body: 'x' },
          { number: 5, url: 'https://github.com/dfx/agent/issues/5', id: 55 },
        ),
        ...op(
          t,
          'op-3',
          'github.update_issue',
          { repo: 'dfx/agent', number: 4, state: 'closed', state_reason: 'not_planned' },
          { number: 4, url: ISSUE_URL, state: 'closed', state_reason: 'not_planned', title: 'Login fails', labels: [] },
        ),
        ...op(t, 'op-4', 'github.request_connection', {}, { login: 'octo' }),
        ...op(
          t,
          'op-5',
          'github.get_issue',
          { repo: 'dfx/agent', number: 7 },
          {
            ...issue(7, 'A pull request'),
            body: null,
            pull_request: true,
            comments_total: 0,
            comments: [],
            comments_next_page: null,
            truncated: true,
          },
        ),
      ]);
    });
    const cards = await screen.findAllByTestId('operation.card');
    const by = (id: string) => cards.find((card) => card.getAttribute('data-operation') === id)!;
    const read = within(by('op-1'));
    expect(read.getByTestId('operation.type')).toHaveTextContent('Read issue');
    expect(read.getByTestId('operation.subject')).toHaveTextContent('dfx/agent#4');
    expect(read.getByTestId('operation.fact').querySelector('a')).toHaveAttribute('href', ISSUE_URL);
    expect(read.getByTestId('operation.entry.title')).toHaveTextContent('Login fails');
    expect(read.getByTestId('operation.entry.state')).toHaveTextContent('closed');
    expect(read.getByTestId('operation.issue.reason')).toHaveTextContent('(completed)');
    expect(read.getByTestId('operation.issue.author')).toHaveTextContent('octo');
    expect(read.getByTestId('operation.issue.body')).toHaveTextContent('1. <script>alert(1)</script>');
    expect(by('op-1').querySelector('script')).toBeNull();
    expect(read.getByTestId('operation.issue.comments-count')).toHaveTextContent('Comments: 2 of 3');
    const comments = read.getAllByTestId('operation.issue.comment');
    expect(comments).toHaveLength(2);
    expect(comments[0]).toHaveTextContent('alice2026-11-21Seen it too');
    expect(comments[1]).toHaveTextContent('**not markdown**');
    expect(by('op-1').querySelector('strong')).toBeNull();
    expect(read.getByTestId('operation.issue.next')).toHaveTextContent('More comments on page 2.');
    expect(read.queryByTestId('operation.truncated')).toBeNull();
    const created = within(by('op-2'));
    expect(created.getByTestId('operation.subject')).toHaveTextContent('dfx/agent: New bug');
    expect(created.getByTestId('operation.fact')).toHaveTextContent('Issue: #5');
    expect(created.getByTestId('operation.fact').querySelector('a')).toHaveAttribute(
      'href',
      'https://github.com/dfx/agent/issues/5',
    );
    const updated = within(by('op-3')).getAllByTestId('operation.fact');
    expect(updated.map((fact) => fact.textContent)).toEqual(['Issue: #4', 'State: closed', 'Reason: not planned']);
    expect(within(by('op-4')).getByTestId('operation.type')).toHaveTextContent('Request GitHub connection');
    expect(within(by('op-4')).getByTestId('operation.fact')).toHaveTextContent('Connected as: octo');
    const pr = within(by('op-5'));
    expect(pr.getByTestId('operation.issue.pull-request')).toHaveTextContent('This number is a pull request.');
    expect(pr.queryByTestId('operation.issue.body')).toBeNull();
    expect(pr.queryByTestId('operation.issue.comment')).toBeNull();
    expect(pr.getByTestId('operation.issue.comments-count')).toHaveTextContent('Comments: 0 of 0');
    expect(pr.getByTestId('operation.truncated')).toBeInTheDocument();
  });

  it('shows the comment count without a total and comments without author or date', async () => {
    const { t } = await page();
    act(() => {
      putOperations(t.store, [row(t, { operation_id: 'op-1', type: 'github.get_issue', state: 'succeeded' })]);
      putEvents(
        t.store,
        t.wg,
        op(
          t,
          'op-1',
          'github.get_issue',
          { repo: 'dfx/agent', number: 4 },
          { number: 4, title: 'T', body: '', comments: [{ id: 1, body: 'anon' }] },
        ),
      );
    });
    const card = within(await screen.findByTestId('operation.card'));
    expect(card.getByTestId('operation.issue.comments-count')).toHaveTextContent('Comments: 1 of ?');
    expect(card.getByTestId('operation.issue.comment')).toHaveTextContent('?anon');
    expect(card.queryByTestId('operation.issue.body')).toBeNull();
    expect(card.queryByTestId('operation.issue.author')).toBeNull();
    expect(card.queryByRole('link', { name: '#4' })).toBeNull();
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
    expect(transitions[0]).toHaveTextContent('Device without a name');
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

  it('shows subagents at any depth, indented up to the last indentation level (ADR-088)', async () => {
    const t = await taskWorld();
    putTasks(t.store, [taskRow(t) as TaskRow]);
    putSession(t.store, sessionRow(t, { state: 'working' }) as never, [], 0);
    const chain = [1, 2, 3, 4, 5, 6].map((depth) => `0190f000-0000-7000-8000-0000000000e${depth}`);
    chain.forEach((id, index) =>
      putSession(
        t.store,
        sessionRow(t, {
          session_id: id,
          parent_session_id: index === 0 ? MAIN : chain[index - 1],
          depth: index + 1,
        }) as never,
        [],
        0,
      ),
    );
    await sessionPage(t, MAIN);
    const children = await screen.findAllByTestId('session.child');
    expect(children.map((child) => child.getAttribute('data-depth'))).toEqual(['1', '2', '3', '4', '5', '6']);
    expect(children[5]).toHaveTextContent('Subagent 1.1.1.1.1.1');
    const lists = screen.getAllByTestId('session.children.list');
    expect(lists.map((list) => list.classList.contains('ml-4'))).toEqual([true, true, true, false, false]);
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
