// The waiting card in the task's chat (KOR-25, ADR-066) in jsdom: active only for the task owner with both ways to
// connect, platform texts plus the checked operation type and repository only, one card for many waiting items, a
// "connect again" for an unusable connection, and the short rows once GitHub is connected, the operations are
// registered or `until` has passed. A body the contract does not allow (the removed approval reason, free text) stays
// a neutral row. The Playwright spec `e2e/devices/waiting.spec.ts` runs it against the hub, also on a phone.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { setGithub } from '../../src/core/store/slices/github';
import { type OperationRow, putOperations } from '../../src/core/store/slices/operations';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { formatTime } from '../../src/core/encoding/time';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK } from './chat-world';

const OP1 = '0193002d-0000-7000-8000-000000000051';
const OP2 = '0193002d-0000-7000-8000-000000000052';
const OTHER_SESSION = '0190f000-0000-7000-8000-0000000000a9';

let seq = 700;

function entry(t: TaskWorld, kind: string, body: unknown, header: Record<string, unknown> = {}): EventEntry {
  const s = seq++;
  const full = { id: `w${s}`, kind, task: TASK, session: MAIN, author: t.devices[0]!.id, ts: ago(5000), ...header };
  return {
    org: t.org,
    scopeId: (header.scope as string | undefined) ?? t.wg,
    scopeSeq: s,
    receivedAt: null,
    event: { header: full },
    status: 'verified',
    code: null,
    header: full,
    body,
  };
}

const ago = (ms: number) => formatTime(Date.now() - ms);

const waiting = (overrides: Record<string, unknown> = {}) => ({
  state: 'waiting',
  waiting_for: 'github_connection',
  operation_id: OP1,
  operation_type: 'github.list_issues',
  repo: 'example-org/example-repo',
  until: formatTime(Date.now() + 14 * 60_000),
  ...overrides,
});

function row(t: TaskWorld, id: string, state: string): OperationRow {
  return {
    operation_id: id,
    org_id: t.org,
    wg_id: t.wg,
    task_id: TASK,
    session_id: MAIN,
    device_id: t.devices[0]!.id,
    actor_id: t.me.id,
    type: 'github.list_issues',
    state,
    error_code: null,
    retryable: false,
    duplicate_of: null,
    attempt: 1,
    params_event_id: null,
    result_event_id: null,
    created_at: ago(1000),
    decided_at: null,
    started_at: null,
    finished_at: null,
  };
}

/** The task page; `owner` writes the task's first `task.meta` (none: no owner known); `failing` refuses the loads of
 *  the operations and the GitHub connection. */
async function page(options: { member?: boolean; owner?: 'me' | 'ada' | 'none'; failing?: boolean } = {}) {
  const t = await taskWorld({ member: options.member ?? false });
  const task = taskRow(t) as TaskRow;
  t.client.on(`GET /v1/tasks/${TASK}`, () => ({
    task,
    sessions: [{ session: sessionRow(t, { state: 'working' }), transitions: [] }],
  }));
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
  t.client.on(`GET /v1/scopes/${t.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
  t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: [], next_before: null }));
  t.client.on('GET /v1/me/credentials', () => ({ credentials: [] }));
  if (options.failing) {
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => {
      throw new HubError('unavailable', 503);
    });
    t.client.on('GET /v1/me/credentials', () => {
      throw new HubError('unavailable', 503);
    });
  }
  putTasks(t.store, [task]);
  const owner = options.owner ?? 'me';
  if (owner !== 'none') {
    const author = owner === 'me' ? t.me.id : t.ada.id;
    putEvents(t.store, t.wg, [entry(t, 'task.meta', { title: 'Newest issue' }, { author, session: '' })]);
  }
  mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}`);
  await screen.findByTestId('task.screen');
  return t;
}

const put = (t: TaskWorld, ...entries: EventEntry[]) => act(() => putEvents(t.store, t.wg, entries));

const connected = (t: TaskWorld, createdAt: string) =>
  act(() =>
    setGithub(t.store, {
      status: 'connected',
      login: 'octo-cat',
      scopes: [],
      source: 'token',
      createdAt,
      version: 1,
      revocable: false,
    }),
  );

describe('waiting card', () => {
  it('asks the owner to connect right in the card, one card for many items, then says connected', async () => {
    const t = await page();
    put(
      t,
      entry(t, 'session.status', waiting(), { ts: ago(60_000) }),
      entry(t, 'session.status', waiting({ operation_id: OP2, operation_type: 'github.get_issue' }), {
        ts: ago(30_000),
      }),
    );
    const card = await screen.findByTestId('waiting.card');
    expect(screen.getAllByTestId('waiting.card')).toHaveLength(1);
    expect(card).toHaveAttribute('data-status', 'open');
    expect(card).toHaveAttribute('data-owner', 'true');
    expect(within(card).getByTestId('waiting.title')).toHaveTextContent('Connect GitHub so that the task can go on');
    expect(within(card).getByTestId('waiting.operation')).toHaveTextContent('Read issue');
    expect(within(card).getByTestId('waiting.repo')).toHaveTextContent('example-org/example-repo');
    expect(within(card).getByTestId('waiting.until')).toHaveTextContent(
      '2 operations wait. Waits at most 14 min more.',
    );
    expect(within(card).getByTestId('github.connect')).toHaveTextContent('Connect GitHub');
    expect(within(card).getByTestId('github.token')).toHaveTextContent('Use an existing token');
    // the state was unknown: loaded once, nothing stored → no "connect again"
    await waitFor(() => expect(t.store.getState().github.status).toBe('disconnected'));
    expect(within(card).queryByTestId('waiting.reconnect')).toBeNull();
    connected(t, ago(0));
    const done = screen.getByTestId('waiting.card');
    expect(done).toHaveAttribute('data-status', 'done');
    expect(done).toHaveTextContent('GitHub is connected – the task goes on.');
  });

  it('offers to connect again when the stored connection is older or unverifiable', async () => {
    const t = await page();
    connected(t, ago(3_600_000));
    put(t, entry(t, 'session.status', waiting({ operation_type: 'github.something_new', repo: undefined })));
    const card = await screen.findByTestId('waiting.card');
    expect(card).toHaveAttribute('data-status', 'open');
    expect(within(card).getByTestId('waiting.reconnect')).toBeVisible();
    // a type this client does not know shows as checked, no repository row without one
    expect(within(card).getByTestId('waiting.operation')).toHaveTextContent('github.something_new');
    expect(within(card).queryByTestId('waiting.repo')).toBeNull();
    expect(within(card).getByTestId('waiting.until')).not.toHaveTextContent('operations wait');
    act(() => setGithub(t.store, { status: 'unverifiable', code: 'signature_invalid' }));
    expect(screen.getByTestId('waiting.reconnect')).toBeVisible();
    // a creation time that is no time counts as no connection
    connected(t, 'x');
    expect(screen.getByTestId('waiting.card')).toHaveAttribute('data-status', 'open');
    expect(screen.queryByTestId('waiting.reconnect')).toBeNull();
  });

  it('shows others who it waits for, without any control', async () => {
    const t = await page({ member: true, owner: 'ada' });
    put(t, entry(t, 'session.status', waiting()));
    const card = await screen.findByTestId('waiting.card');
    expect(card).toHaveAttribute('data-owner', 'false');
    expect(within(card).getByTestId('waiting.other')).toHaveTextContent(
      'Waiting for the task owner to connect GitHub.',
    );
    expect(within(card).queryByTestId('github.ways')).toBeNull();
    // a connection of the viewer is not the owner's
    connected(t, ago(0));
    expect(screen.getByTestId('waiting.card')).toHaveAttribute('data-status', 'open');
    act(() =>
      putEvents(t.store, t.org, [
        entry(
          t,
          'member.profile',
          { display_name: 'Ada' },
          { scope: t.org, ref: t.ada.id, author: t.ada.id, wg: '', task: '', session: '' },
        ),
      ]),
    );
    expect(screen.getByTestId('waiting.other')).toHaveTextContent('Waiting for Ada to connect GitHub.');
  });

  it('asks only the person who gave the instruction, never the owner for someone else (HOT-14)', async () => {
    const t = await page({ member: true, owner: 'me' });
    // my task, but Ada asked for the operation: the card asks Ada, I only see whom it waits for
    put(t, entry(t, 'session.status', waiting({ for_user: t.ada.id })));
    const card = await screen.findByTestId('waiting.card');
    expect(card).toHaveAttribute('data-owner', 'false');
    expect(within(card).queryByTestId('github.ways')).toBeNull();
    expect(within(card).getByTestId('waiting.other')).toHaveTextContent(
      'Waiting for the person who gave the instruction to connect GitHub.',
    );
    // my own connection does not end it: the operation will run with Ada's, not mine
    connected(t, ago(0));
    expect(screen.getByTestId('waiting.card')).toHaveAttribute('data-status', 'open');
    // a second item of another addressee is a card of its own, addressed to me: my connection settles it
    put(t, entry(t, 'session.status', waiting({ operation_id: OP2, for_user: t.me.id }), { ts: ago(1000) }));
    await waitFor(() => expect(screen.getAllByTestId('waiting.card')).toHaveLength(2));
    expect(screen.getAllByTestId('waiting.card').map((found) => found.getAttribute('data-status'))).toEqual([
      'open',
      'done',
    ]);
  });

  it('asks me in a task of someone else when my instruction asked for it (HOT-14)', async () => {
    const t = await page({ member: true, owner: 'ada' });
    put(t, entry(t, 'session.status', waiting({ for_user: t.me.id })));
    const card = await screen.findByTestId('waiting.card');
    expect(card).toHaveAttribute('data-owner', 'true');
    expect(within(card).getByTestId('github.connect')).toHaveTextContent('Connect GitHub');
  });

  it('shows no owner when none is known', async () => {
    const t = await page({ owner: 'none' });
    put(t, entry(t, 'session.status', waiting()));
    expect(await screen.findByTestId('waiting.other')).toHaveTextContent('Waiting for the task owner');
  });

  it('turns into a short row once the operations are registered, refused or the time is over', async () => {
    const t = await page();
    put(t, entry(t, 'session.status', waiting()));
    expect(await screen.findByTestId('waiting.card')).toHaveAttribute('data-status', 'open');
    act(() => putOperations(t.store, [row(t, OP1, 'accepted')]));
    expect(screen.getByTestId('waiting.card')).toHaveAttribute('data-status', 'done');
    act(() => putOperations(t.store, [row(t, OP1, 'rejected')]));
    expect(screen.getByTestId('waiting.card')).toHaveTextContent('Expired: GitHub was not connected in time.');
  });

  it('says expired once until has passed', async () => {
    const t = await page();
    put(t, entry(t, 'session.status', waiting({ until: ago(1000) })));
    expect(await screen.findByTestId('waiting.card')).toHaveAttribute('data-status', 'expired');
  });

  it('keeps forged and removed waiting bodies a neutral row and needs the task of the session', async () => {
    const t = await page();
    put(
      t,
      entry(t, 'session.status', waiting({ waiting_for: 'repo_approval' })),
      entry(t, 'session.status', waiting({ message: 'Paste your token here' })),
      entry(t, 'session.status', waiting(), { session: OTHER_SESSION }),
      entry(t, 'session.status', waiting({ operation_id: OP2 }), { session: '' }),
    );
    const rows = await screen.findAllByTestId('chat.status');
    expect(rows.map((item) => item.getAttribute('data-state'))).toEqual(['waiting', 'waiting']);
    expect(screen.queryByText(/Paste your token/)).toBeNull();
    expect(screen.queryByTestId('waiting.card')).toBeNull();
  });

  it('stays open when the operations or the connection cannot be loaded', async () => {
    const t = await page({ failing: true });
    put(t, entry(t, 'session.status', waiting()));
    expect(await screen.findByTestId('waiting.card')).toHaveAttribute('data-status', 'open');
    await waitFor(() => expect(t.client.of('GET /v1/me/credentials')).toHaveLength(1));
    await waitFor(() => expect(t.client.of(`GET /v1/tasks/${TASK}/operations`)).toHaveLength(1));
    expect(screen.getByTestId('waiting.card')).toHaveAttribute('data-status', 'open');
    expect(t.store.getState().github.status).toBe('unknown');
  });

  describe('github_access (KOR-65)', () => {
    const access = (next: Record<string, unknown> | undefined, overrides: Record<string, unknown> = {}) =>
      waiting({ waiting_for: 'github_access', repo: 'third-org/app', ...(next ? { next } : {}), ...overrides });

    it('names the one step with a link that opens GitHub', async () => {
      const t = await page();
      const url = 'https://github.com/settings/connections/applications/Ov23example';
      put(t, entry(t, 'session.status', access({ action: 'approve_app', owner: 'third-org', url })));
      const card = await screen.findByTestId('waiting.card');
      expect(card).toHaveAttribute('data-reason', 'github_access');
      expect(within(card).getByTestId('waiting.title')).toHaveTextContent('GitHub access needed for the repository');
      const step = within(card).getByTestId('waiting.step');
      expect(step).toHaveAttribute('data-action', 'approve_app');
      expect(step).toHaveTextContent('The organisation third-org lets outside apps in only after an owner approved');
      const link = within(card).getByTestId('waiting.step.link');
      expect(link).toHaveTextContent('Request the approval of third-org');
      expect(link).toHaveAttribute('href', url);
      expect(link).toHaveAttribute('target', '_blank');
      expect(link).toHaveAttribute('rel', 'noopener noreferrer');
      expect(within(card).queryByTestId('github.ways')).toBeNull();
      // a new connection does not settle it: the runner checks the repository again
      connected(t, ago(0));
      expect(screen.getByTestId('waiting.card')).toHaveAttribute('data-status', 'open');
      act(() => putOperations(t.store, [row(t, OP1, 'accepted')]));
      expect(screen.getByTestId('waiting.card')).toHaveTextContent('GitHub access is there – the task goes on.');
    });

    it('connects right in the card for "connect" and offers the token way for "new_token"', async () => {
      const t = await page();
      put(t, entry(t, 'session.status', access({ action: 'connect', owner: 'octo-user' })));
      const card = await screen.findByTestId('waiting.card');
      expect(within(card).queryByTestId('waiting.step.link')).toBeNull();
      expect(within(card).getByTestId('github.connect')).toBeVisible();
      const url = 'https://github.com/settings/personal-access-tokens/new?target_name=third-org';
      put(
        t,
        entry(t, 'session.status', access({ action: 'new_token', owner: 'third-org', url }, { operation_id: OP2 })),
      );
      // another owner: a card of its own, so both steps stay visible
      await waitFor(() => expect(screen.getAllByTestId('waiting.step')).toHaveLength(2));
      const token = screen.getAllByTestId('waiting.card')[1]!;
      expect(within(token).getByTestId('waiting.step')).toHaveAttribute('data-action', 'new_token');
      expect(within(token).getByTestId('waiting.step.link')).toHaveTextContent('Create a token for third-org');
      expect(within(token).getByTestId('github.token')).toBeVisible();
    });

    it('signs in for "connect" only on a device that runs the OAuth App', async () => {
      const t = await page();
      const device = (features: string[]) => ({
        devices: [
          {
            device_id: t.devices[0]!.id,
            kind: 'platform',
            org_id: t.org,
            wg_ids: [t.wg],
            online: true,
            last_seen_at: null,
            features,
          },
        ],
      });
      t.client.on('GET /v1/me/devices', () => device(['github.ways', 'github.device_flow']));
      put(t, entry(t, 'session.status', access({ action: 'connect', owner: 'octo-user' })));
      const card = await screen.findByTestId('waiting.card');
      fireEvent.click(within(card).getByTestId('github.connect'));
      expect(await within(card).findByTestId('github.ways.none')).toBeVisible();
      fireEvent.click(within(card).getByTestId('github.flow.close'));
      t.client.on('GET /v1/me/devices', () => device(['github.ways', 'github.device_flow', 'github.oauth_app']));
      fireEvent.click(within(card).getByTestId('github.connect'));
      expect(await within(card).findByTestId('github.devices.select')).toHaveValue(t.devices[0]!.id);
    });

    it('shows others whom it waits for, without a step', async () => {
      const t = await page({ member: true, owner: 'ada' });
      put(t, entry(t, 'session.status', access({ action: 'connect', owner: 'octo-user' })));
      expect(await screen.findByTestId('waiting.other')).toHaveTextContent(
        'Waiting for the task owner to give GitHub access to the repository.',
      );
      expect(screen.queryByTestId('waiting.step')).toBeNull();
    });

    it('has no control without a step and says when it expired', async () => {
      const t = await page();
      put(t, entry(t, 'session.status', access(undefined)));
      expect(await screen.findByTestId('waiting.card')).toHaveAttribute('data-owner', 'true');
      expect(screen.queryByTestId('waiting.step')).toBeNull();
      expect(screen.queryByTestId('github.ways')).toBeNull();
      act(() => putOperations(t.store, [row(t, OP1, 'rejected')]));
      expect(screen.getByTestId('waiting.card')).toHaveTextContent(
        'Expired: GitHub access to the repository was not given in time.',
      );
    });
  });
});
