// The pull-request views in jsdom (KOR-48, ADR-084): the workgroup's "Pull requests" screen (levels, rows with state,
// conflict state, GitHub link and task, "Fix conflicts" with the instruction as the task's prompt, "Check again",
// unlink, "Load more", errors), "Paste a pull-request address" (invalid, created, linked already, refused), the
// task's tab, readers without controls, the row variants (fork, not found, Connect GitHub, being checked, merged,
// closed, draft, details unavailable, the fix feedback) and the compact line of the situation view – against the
// core task world with sealed link bodies. The Playwright specs run the views against the hub.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { b64uDecode } from '../../src/core/encoding/b64u';
import { fromUtf8 } from '../../src/core/encoding/bytes';
import { openEvent } from '../../src/core/crypto/event';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { type PullRef, pullKey } from '../../src/core/pulls/address';
import { fixConflictsInstruction } from '../../src/core/pulls/instruction';
import type { PullState } from '../../src/core/pulls/checks';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { addKeys, setLog } from '../../src/core/store/slices/orgs';
import { type PullStatus, setStatuses } from '../../src/core/pulls/state';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { I18nProvider } from '../../src/ui/i18n';
import { PullLine } from '../../src/ui/screens/home/PullLine';
import { nav } from '../../src/ui/screens/pulls/nav';
import { PullsScreen } from '../../src/ui/screens/pulls/PullsScreen';
import { routes } from '../../src/ui/screens/pulls/routes';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { tab } from '../../src/ui/task-tabs/pulls';
import { PullsTab } from '../../src/ui/task-tabs/pulls/PullsTab';
import { idOf } from '../core/log/world';
import { sealedRow, wgKey } from '../core/pulls/rows';
import { taskRow, taskWorld, type TaskWorld } from '../core/tasks/world';
import { mount } from './chat-world';
import { names } from './org-world';

const TASK = '0190f000-0000-7000-8000-000000000001';
const OTHER = '0190f000-0000-7000-8000-000000000002';
const DEVICE = '0190f000-0000-7000-8000-0000000000d1';
const A: PullRef = { repo: 'Org/App', number: 7 };
const B: PullRef = { repo: 'Org/Lib', number: 8 };
const SHA = 'a'.repeat(40);

afterEach(() => vi.useRealTimers());

const state = (ref: PullRef, extra: Partial<Record<string, unknown>> = {}): PullState =>
  ({
    repo: ref.repo,
    number: ref.number,
    found: true,
    title: `Title of ${ref.number}`,
    state: 'open',
    draft: false,
    merged: false,
    mergeable: false,
    mergeable_state: 'dirty',
    head_ref: 'feature',
    head_sha: SHA,
    head_repo: ref.repo,
    base_ref: 'main',
    url: `https://github.com/${ref.repo}/pull/${ref.number}`,
    updated_at: '2026-11-24T12:00:00Z',
    ...extra,
  }) as PullState;

const ready = (ref: PullRef, extra: Partial<Record<string, unknown>> = {}): PullStatus => ({
  status: 'ready',
  state: state(ref, extra),
  at: 1,
});

function statuses(t: TaskWorld, entries: [PullRef, PullStatus][], wg = t.wg) {
  setStatuses(
    t.store,
    wg,
    entries.map(([ref, value]) => [pullKey(ref), value]),
  );
}

const title = (t: TaskWorld, task: string, text: string, seq: number, wg = t.wg): EventEntry => ({
  org: t.org,
  scopeId: wg,
  scopeSeq: seq,
  receivedAt: null,
  event: {},
  status: 'verified',
  code: null,
  header: { id: `title-${seq}`, kind: 'task.meta', task, author: t.me.id, ts: '2026-11-24T12:00:00.000Z' },
  body: { title: text },
});

/** The routes every view needs besides the links. */
function base(t: TaskWorld, rows: TaskRow[]) {
  putTasks(t.store, rows);
  for (const row of rows) t.client.on(`GET /v1/tasks/${row.task_id}`, () => ({ task: row, sessions: [] }));
  t.client.on(`GET /v1/scopes/${t.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
  t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => ({ tasks: rows, next_before: null }));
  t.client.on('GET /v1/me/devices', () => ({ devices: [] }));
  t.client.on(`POST /v1/tasks/${TASK}/prompts`, () => ({ session_id: 's1', scope_seq: 9 }));
}

const SCREEN = [
  { path: '/o/:org/pulls', element: <PullsScreen /> },
  { path: '/o/:org/w/:wg/pulls', element: <PullsScreen /> },
  { path: '/o/:org/t/:task', element: <p data-testid="task-page">task</p> },
];

const item = (ref: PullRef | null) =>
  screen
    .getAllByTestId('pulls.item')
    .find((node) => node.dataset.pull === (ref === null ? '' : `${ref.repo}#${ref.number}`))!;

/** The texts of the prompts sent to TASK. */
async function prompts(t: TaskWorld): Promise<string[]> {
  const key = wgKey(t);
  const sig = b64uDecode(t.me.identity.public().sig);
  return Promise.all(
    t.client.of(`POST /v1/tasks/${TASK}/prompts`).map(async (call) => {
      const event = (call.body as { event: never }).event;
      return (JSON.parse(fromUtf8(await openEvent(key, event, sig))) as { text: string }).text;
    }),
  );
}

describe('registration', () => {
  it('adds the view, its navigation entry and the task tab', () => {
    expect(routes.map((route) => route.path)).toEqual(['/o/:org/pulls', '/o/:org/w/:wg/pulls']);
    expect(nav).toEqual([{ id: 'pulls', order: 20, to: '/o/:org/pulls', labelKey: 'pullnav.nav' }]);
    expect(tab).toMatchObject({ id: 'pulls', labelKey: 'pulls.tab', Component: PullsTab });
  });
});

describe('Pull requests screen', () => {
  it('waits for the user and the log and hides a workgroup the user cannot read', async () => {
    const t = await taskWorld();
    base(t, []);
    act(() => t.store.update('me', (me) => ({ ...me, userId: null })));
    mount(t, SCREEN, `/o/${t.org}/pulls`);
    expect(screen.getByTestId('pulls.screen.loading')).toBeInTheDocument();
    cleanup();
    const s = await taskWorld();
    base(s, []);
    mount(s, SCREEN, `/o/other-org/pulls`);
    expect(screen.getByTestId('pulls.screen.loading')).toBeInTheDocument();
    cleanup();
    const u = await taskWorld();
    base(u, []);
    mount(u, SCREEN, `/o/${u.org}/w/${await idOf('nowhere')}/pulls`);
    expect(screen.getByTestId('pulls.not-found')).toBeInTheDocument();
  });

  it('lists the links with state, conflict state, GitHub link and task, and fixes a conflict', async () => {
    const t = await taskWorld();
    const row = taskRow(t, { task_id: TASK }) as TaskRow;
    const other = taskRow(t, { task_id: OTHER, main_session_id: null }) as TaskRow;
    base(t, [row, other]);
    putEvents(t.store, t.wg, [title(t, TASK, 'Release notes', 1)]);
    const device = await sealedRow(t, { task: TASK, ref: A });
    const person = await sealedRow(t, { task: OTHER, ref: B, linkedBy: t.me.id });
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, (_body, target) =>
      target.includes('cursor=')
        ? { items: [{ ...person, tag: 'legacy-tag', sealed: null }], next_cursor: null }
        : { items: [device, person], next_cursor: 'c1' },
    );
    statuses(t, [
      [A, ready(A)],
      [B, ready(B, { mergeable: true })],
    ]);
    mount(t, SCREEN, `/o/${t.org}/pulls`);
    expect(await screen.findByTestId('pulls.screen')).toHaveAttribute('data-wg', t.wg);
    await waitFor(() => expect(screen.getAllByTestId('pulls.item')).toHaveLength(2));
    const first = item(A);
    expect(first).toHaveAttribute('data-state', 'open');
    expect(first).toHaveAttribute('data-conflict', 'conflict');
    expect(within(first).getByTestId('pulls.title')).toHaveTextContent('Title of 7');
    expect(within(first).getByTestId('pulls.github')).toHaveAttribute('href', 'https://github.com/Org/App/pull/7');
    expect(within(first).getByTestId('pulls.task')).toHaveTextContent('Task: Release notes');
    expect(within(first).queryByTestId('pulls.unlink')).toBeNull();
    const second = item(B);
    expect(within(second).getByTestId('pulls.conflict')).toHaveTextContent('No conflict');
    expect(within(second).queryByTestId('pulls.fix')).toBeNull();
    // the other task's title is not read: "loading" stays
    expect(within(second).getByTestId('pulls.task')).toHaveTextContent('Loading the task');
    fireEvent.click(within(first).getByTestId('pulls.fix'));
    expect(await within(first).findByTestId('pulls.fix.sent')).toBeInTheDocument();
    expect(await prompts(t)).toEqual([fixConflictsInstruction(A)]);
    // a failed send shows its error
    t.client.on(`POST /v1/tasks/${TASK}/prompts`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(within(first).getByTestId('pulls.fix'));
    expect(await within(first).findByTestId('pulls.error')).toHaveAttribute('data-code', 'forbidden');
    // the next page
    fireEvent.click(screen.getByTestId('pulls.more'));
    await waitFor(() => expect(screen.getAllByTestId('pulls.item')).toHaveLength(3));
    expect(within(item(null)).getByTestId('pulls.unavailable')).toHaveAttribute('data-code', 'legacy');
    expect(within(item(null)).getByTestId('pulls.task')).toBeInTheDocument();
    expect(screen.queryByTestId('pulls.more')).toBeNull();
    fireEvent.click(within(first).getByTestId('pulls.task'));
    expect(await screen.findByTestId('task-page')).toBeInTheDocument();
  });

  it('checks again and unlinks a link a person made', async () => {
    const t = await taskWorld();
    base(t, [taskRow(t, { task_id: TASK }) as TaskRow]);
    const person = await sealedRow(t, { task: TASK, ref: B, linkedBy: t.me.id });
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, () => ({ items: [person], next_cursor: null }));
    statuses(t, [[B, ready(B, { state: 'closed', merged: true })]]);
    mount(t, SCREEN, `/o/${t.org}/pulls`);
    const row = await screen.findByTestId('pulls.item');
    expect(row).toHaveAttribute('data-state', 'merged');
    expect(within(row).queryByTestId('pulls.conflict')).toBeNull();
    // no device serves the workgroup: the read fails with its reason
    fireEvent.click(within(row).getByTestId('pulls.recheck'));
    await waitFor(() => expect(screen.getByTestId('pulls.item')).toHaveAttribute('data-state', 'failed'));
    expect(within(screen.getByTestId('pulls.item')).getByTestId('pulls.state')).toHaveTextContent(
      'The state could not be read',
    );
    const route = `DELETE /v1/workgroups/${t.wg}/pr-links/1/${person.tag}`;
    t.client.on(route, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(within(screen.getByTestId('pulls.item')).getByTestId('pulls.unlink'));
    expect(await screen.findByTestId('pulls.error')).toHaveAttribute('data-code', 'forbidden');
    t.client.on(route, () => ({ deleted: true }));
    fireEvent.click(within(screen.getByTestId('pulls.item')).getByTestId('pulls.unlink'));
    expect(await screen.findByTestId('pulls.empty')).toBeInTheDocument();
  });

  it('shows the load error with "Try again" and the workgroup levels', async () => {
    const t = await taskWorld();
    const one = await idOf('wg pulls one');
    await t.world.batch(t.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
    setLog(t.store, t.org, t.world.log!);
    addKeys(t.store, t.org, t.world.held.get(t.me.id)!);
    base(t, []);
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, () => {
      throw new HubError('unavailable', 503);
    });
    t.client.on(`GET /v1/workgroups/${one}/pr-links`, () => ({ items: [], next_cursor: null }));
    const { router } = mount(t, SCREEN, `/o/${t.org}/pulls`);
    expect(await screen.findByTestId('pulls.error.list')).toHaveAttribute('data-code', 'unavailable');
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, () => ({ items: [], next_cursor: null }));
    fireEvent.click(screen.getByTestId('pulls.reload'));
    expect(await screen.findByTestId('pulls.empty')).toHaveTextContent('No pull request is linked');
    act(() => names(t as never, {}, { [t.wg]: 'Main' }));
    const levels = screen.getAllByTestId('pulls.level');
    expect(levels.map((node) => node.dataset.wg)).toEqual([t.wg, one]);
    expect(levels[0]).toHaveAttribute('aria-current', 'page');
    expect(levels[0]).toHaveTextContent('Main');
    expect(screen.getByTestId('pulls.title')).toHaveTextContent('Pull requests of Main');
    expect(levels[1]).toHaveTextContent('Unnamed workgroup');
    fireEvent.click(levels[1]!);
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${t.org}/w/${one}/pulls`));
    expect(await screen.findByTestId('pulls.screen')).toHaveAttribute('data-wg', one);
  });

  it('shows a reader the rows without controls', async () => {
    const t = await taskWorld({ member: true });
    const one = await idOf('wg pulls read');
    await t.world.batch(t.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
    await t.world.batch(t.ada, [{ type: 'grant', subject: t.me.id, wg: one, access: 'read' }]);
    setLog(t.store, t.org, t.world.log!);
    addKeys(t.store, t.org, t.world.held.get(t.me.id)!);
    base(t, []);
    const link = await sealedRow(t, { task: TASK, ref: A, linkedBy: t.ada.id, wg: one });
    t.client.on(`GET /v1/workgroups/${one}/pr-links`, () => ({ items: [link], next_cursor: null }));
    t.client.on(`GET /v1/scopes/${one}/events`, () => ({ events: [], next_after: null, head: 0 }));
    setStatuses(t.store, one, [[pullKey(A), ready(A)]]);
    mount(t, SCREEN, `/o/${t.org}/w/${one}/pulls`);
    const row = await screen.findByTestId('pulls.item');
    expect(row).toHaveAttribute('data-conflict', 'conflict');
    expect(within(row).queryByTestId('pulls.fix')).toBeNull();
    expect(within(row).queryByTestId('pulls.unlink')).toBeNull();
    expect(screen.queryByTestId('pulls.link')).toBeNull();
  });
});

describe('row variants', () => {
  async function rows(entries: [PullRef, PullStatus | null][], extra?: (t: TaskWorld) => void) {
    const t = await taskWorld();
    base(t, [taskRow(t, { task_id: TASK, title_event_id: 'tev' }) as TaskRow]);
    putEvents(t.store, t.wg, [
      {
        ...title(t, TASK, 'x', 1),
        status: 'unverifiable',
        code: 'signature_invalid',
        event: { header: { id: 'tev' } },
      },
    ]);
    const items = await Promise.all(entries.map(([ref]) => sealedRow(t, { task: TASK, ref })));
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, () => ({ items, next_cursor: null }));
    statuses(
      t,
      entries.filter((entry): entry is [PullRef, PullStatus] => entry[1] !== null),
    );
    extra?.(t);
    mount(t, SCREEN, `/o/${t.org}/pulls`);
    await waitFor(() => expect(screen.getAllByTestId('pulls.item')).toHaveLength(entries.length));
    return t;
  }

  it('shows forks, drafts, closed, missing and unreadable pull requests', async () => {
    const refs = [1, 2, 3, 4, 5].map((number) => ({ repo: 'o/r', number }));
    await rows([
      [refs[0]!, ready(refs[0]!, { head_repo: 'someone/r' })],
      [refs[1]!, ready(refs[1]!, { draft: true, mergeable_state: 'draft' })],
      [refs[2]!, ready(refs[2]!, { state: 'closed' })],
      [refs[3]!, { status: 'ready', at: 1, state: { repo: 'o/r', number: 4, found: false, code: 'repo_not_allowed' } }],
      [refs[4]!, { status: 'checking' }],
    ]);
    const fork = item(refs[0]!);
    expect(within(fork).getByTestId('pulls.fork')).toBeInTheDocument();
    expect(within(fork).queryByTestId('pulls.fix')).toBeNull();
    expect(within(fork).getByTestId('pulls.task')).toHaveTextContent('Title cannot be verified (signature_invalid)');
    expect(item(refs[1]!)).toHaveAttribute('data-state', 'draft');
    expect(within(item(refs[1]!)).getByTestId('pulls.fix')).toBeInTheDocument();
    expect(item(refs[2]!)).toHaveAttribute('data-state', 'closed');
    expect(within(item(refs[3]!)).getByTestId('pulls.missing')).toHaveAttribute('data-code', 'repo_not_allowed');
    expect(within(item(refs[3]!)).queryByTestId('pulls.github')).toBeNull();
    expect(item(refs[4]!)).toHaveAttribute('data-state', 'checking');
    expect(within(item(refs[4]!)).queryByTestId('pulls.recheck')).toBeNull();
  });

  it('offers "Connect GitHub" when the device has no usable GitHub account', async () => {
    await rows([[A, { status: 'failed', code: 'github_not_connected' }]]);
    const row = item(A);
    expect(within(row).getByTestId('pulls.state')).toHaveTextContent('No GitHub account you may use is connected.');
    expect(within(row).getByTestId('pulls.connect')).toBeInTheDocument();
    expect(await within(row).findByTestId('github.connect')).toBeInTheDocument();
  });

  it('says "Conflicts fixed, branch updated" with the merge commit and reads the state again', async () => {
    const merge = 'c'.repeat(40);
    const op = (id: string, type: string, params: object, result: object, seq: number): EventEntry[] =>
      (['operation.params', 'operation.result'] as const).map((kind, index) => ({
        org: 'org',
        scopeId: 'wg',
        scopeSeq: seq + index,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { id: `${id}-${index}`, kind, ref: id, task: TASK, author: DEVICE, ts: '2026-11-24T12:10:00.000Z' },
        body: index === 0 ? { operation_id: id, type, params } : { operation_id: id, status: 'succeeded', result },
      }));
    const t = await rows([[A, ready(A)]], (w) =>
      putEvents(w.store, w.wg, [
        ...op('c1', 'github.commit', { repo: A.repo, merge: true }, { commit_sha: merge }, 10),
        ...op('p1', 'github.push', { repo: A.repo }, { branch: 'feature', sha: merge }, 20),
      ]),
    );
    // the fresh read (no device here: it fails with its reason); the feedback stays
    await waitFor(() => expect(item(A)).toHaveAttribute('data-state', 'failed'));
    expect(within(item(A)).getByTestId('pulls.fixed')).toHaveTextContent('merge commit ccccccc');
    expect(t.client.of('GET /v1/me/devices').length).toBeGreaterThan(0);
  });
});

describe('lazy reads', () => {
  it('reads a state once the row is shown and keeps going when pulls fail', async () => {
    const t = await taskWorld();
    base(t, [taskRow(t, { task_id: TASK }) as TaskRow]);
    t.client.routes.delete(`GET /v1/scopes/${t.wg}/events`);
    const items = [
      await sealedRow(t, { task: TASK, ref: A }),
      await sealedRow(t, { task: TASK, ref: B }),
      { ...(await sealedRow(t, { task: OTHER, ref: { repo: 'o/z', number: 3 } })), task_id: TASK },
    ];
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, () => ({ items, next_cursor: null }));
    statuses(t, [[B, ready(B)]]);
    mount(t, SCREEN, `/o/${t.org}/pulls`);
    await waitFor(() => expect(screen.getAllByTestId('pulls.item')).toHaveLength(3));
    // no device serves the workgroup: the lazy read of A fails with its reason
    await waitFor(() => expect(item(A)).toHaveAttribute('data-state', 'failed'));
    expect(item(B)).toHaveAttribute('data-conflict', 'conflict');
    expect(within(item(null)).getByTestId('pulls.unavailable')).toHaveTextContent('cannot be verified');
    expect(within(item(B)).getByTestId('pulls.task')).toHaveTextContent('Loading the task');
  });
});

describe('paste a pull-request address', () => {
  async function form() {
    const t = await taskWorld();
    const row = taskRow(t, { task_id: TASK }) as TaskRow;
    base(t, [row]);
    putEvents(t.store, t.wg, [title(t, TASK, 'Release notes', 1)]);
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, () => ({ items: [], next_cursor: null }));
    mount(t, SCREEN, `/o/${t.org}/pulls`);
    await screen.findByTestId('pulls.empty');
    return t;
  }

  it('refuses an invalid address and links a new one to the chosen task', async () => {
    const t = await form();
    const submit = screen.getByTestId('pulls.link.submit');
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByTestId('pulls.link.input'), { target: { value: 'not a pull request' } });
    expect(submit).toBeDisabled();
    // Enter without a chosen task does nothing
    fireEvent.submit(screen.getByTestId('pulls.link'));
    expect(screen.queryByTestId('pulls.link.invalid')).toBeNull();
    await waitFor(() => expect(within(screen.getByTestId('pulls.link.task')).getAllByRole('option')).toHaveLength(2));
    expect(screen.getByRole('option', { name: 'Release notes' })).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('pulls.link.task'), { target: { value: TASK } });
    fireEvent.click(submit);
    expect(await screen.findByTestId('pulls.link.invalid')).toBeInTheDocument();
    t.client.on(`POST /v1/workgroups/${t.wg}/pr-links`, (body) => ({
      created: true,
      link: { ...(body as object), session_id: null, linked_by: t.me.id, created_at: '2026-11-24T12:00:00.000Z' },
    }));
    statuses(t, [[A, ready(A)]]);
    fireEvent.change(screen.getByTestId('pulls.link.input'), {
      target: { value: 'https://github.com/Org/App/pull/7' },
    });
    fireEvent.submit(screen.getByTestId('pulls.link'));
    expect(await screen.findByTestId('pulls.link.created')).toBeInTheDocument();
    expect(screen.getByTestId('pulls.link.input')).toHaveValue('');
    expect(await screen.findByTestId('pulls.item')).toHaveAttribute('data-pull', 'Org/App#7');
  });

  it('shows a pull request linked already with its task, and a refusal', async () => {
    const t = await form();
    const existing = await sealedRow(t, { task: TASK, ref: A });
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links/1/${existing.tag}`, () => existing);
    await waitFor(() => expect(within(screen.getByTestId('pulls.link.task')).getAllByRole('option')).toHaveLength(2));
    fireEvent.change(screen.getByTestId('pulls.link.task'), { target: { value: TASK } });
    fireEvent.change(screen.getByTestId('pulls.link.input'), { target: { value: 'Org/App#7' } });
    fireEvent.click(screen.getByTestId('pulls.link.submit'));
    expect(await screen.findByTestId('pulls.link.exists')).toBeInTheDocument();
    expect(await screen.findByTestId('pulls.link.exists.task')).toHaveTextContent('Release notes');
    expect(screen.getByTestId('pulls.link.input')).toHaveValue('Org/App#7');
    t.client.on(`POST /v1/workgroups/${t.wg}/pr-links`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.change(screen.getByTestId('pulls.link.input'), { target: { value: 'Org/Lib#8' } });
    fireEvent.click(screen.getByTestId('pulls.link.submit'));
    expect(await screen.findByTestId('pulls.link.error')).toHaveAttribute('data-code', 'forbidden');
  });

  it('names a task without a verified title neutrally', async () => {
    const t = await taskWorld();
    base(t, [taskRow(t, { task_id: OTHER }) as TaskRow]);
    const existing = await sealedRow(t, { task: OTHER, ref: A });
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, () => ({ items: [], next_cursor: null }));
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links/1/${existing.tag}`, () => existing);
    mount(t, SCREEN, `/o/${t.org}/pulls`);
    await waitFor(() => expect(within(screen.getByTestId('pulls.link.task')).getAllByRole('option')).toHaveLength(2));
    expect(screen.getByRole('option', { name: 'Untitled task' })).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('pulls.link.task'), { target: { value: OTHER } });
    fireEvent.change(screen.getByTestId('pulls.link.input'), { target: { value: 'Org/App#7' } });
    fireEvent.click(screen.getByTestId('pulls.link.submit'));
    expect(await screen.findByTestId('pulls.link.exists.task')).toHaveTextContent('Untitled task');
  });
});

describe('task tab', () => {
  it('waits for the task row, then lists its links and links to the task', async () => {
    const t = await taskWorld();
    base(t, []);
    const link = await sealedRow(t, { task: TASK, ref: A });
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, (_body, target) => {
      expect(target).toContain(`task=${TASK}`);
      return { items: [{ ...link, sealed: null, tag: 'old' }, link], next_cursor: null };
    });
    statuses(t, [[A, ready(A)]]);
    mount(t, [{ path: '/o/:org/t/:task', element: <PullsTab orgId={t.org} taskId={TASK} /> }], `/o/${t.org}/t/${TASK}`);
    expect(screen.getByTestId('task.pulls.loading')).toBeInTheDocument();
    act(() => putTasks(t.store, [taskRow(t, { task_id: TASK }) as TaskRow]));
    const panel = await screen.findByTestId('task.pulls');
    await waitFor(() => expect(within(panel).getAllByTestId('pulls.item')).toHaveLength(2));
    expect(within(panel).queryByTestId('pulls.task')).toBeNull();
    expect(within(panel).queryByTestId('pulls.link.task')).toBeNull();
    expect(within(panel).getByTestId('pulls.fix')).toBeInTheDocument();
  });

  it('shows the empty text of a task and no form to a reader', async () => {
    const t = await taskWorld({ member: true });
    const one = await idOf('wg pulls tab read');
    await t.world.batch(t.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
    await t.world.batch(t.ada, [{ type: 'grant', subject: t.me.id, wg: one, access: 'read' }]);
    setLog(t.store, t.org, t.world.log!);
    base(t, []);
    putTasks(t.store, [taskRow(t, { task_id: TASK, wg_id: one }) as TaskRow]);
    t.client.on(`GET /v1/workgroups/${one}/pr-links`, () => ({ items: [], next_cursor: null }));
    mount(t, [{ path: '/o/:org/t/:task', element: <PullsTab orgId={t.org} taskId={TASK} /> }], `/o/${t.org}/t/${TASK}`);
    expect(await screen.findByTestId('pulls.empty')).toHaveTextContent('No pull request is linked to this task yet.');
    expect(screen.queryByTestId('pulls.link')).toBeNull();
  });
});

describe('situation view line', () => {
  function line(t: TaskWorld, writer: boolean) {
    const router = createMemoryRouter(
      [{ path: '*', element: <PullLine org={t.org} task={t.store.getState().tasks.tasks[TASK]!} writer={writer} /> }],
      { initialEntries: ['/'] },
    );
    render(
      <AppEnvProvider
        value={{ app: { store: t.store, tasks: t.context } as never, credentials: {} as never, origin: 'x' }}
      >
        <I18nProvider value={createI18n('en')}>
          <RouterProvider router={router} />
        </I18nProvider>
      </AppEnvProvider>,
    );
  }

  it('names the linked pull requests of the entry and fixes a conflict from there', async () => {
    const t = await taskWorld();
    base(t, [taskRow(t, { task_id: TASK }) as TaskRow]);
    const links = await Promise.all([
      sealedRow(t, { task: TASK, ref: A }),
      sealedRow(t, { task: TASK, ref: B }),
      sealedRow(t, { task: OTHER, ref: { repo: 'o/x', number: 1 } }),
    ]);
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, () => ({
      items: [...links, { ...links[0]!, tag: 'old', sealed: null }],
      next_cursor: null,
    }));
    statuses(t, [
      [A, ready(A)],
      [B, { status: 'failed', code: 'timeout' }],
    ]);
    line(t, true);
    await waitFor(() => expect(screen.getAllByTestId('attention.pull')).toHaveLength(3));
    const [a, b, old] = screen.getAllByTestId('attention.pull');
    expect(within(a!).getByTestId('attention.pull.state')).toHaveTextContent('Open · Conflict');
    expect(within(b!).getByTestId('attention.pull.state')).toHaveTextContent('State not readable');
    expect(old).toHaveTextContent('Pull request (details unavailable)');
    t.client.on(`POST /v1/tasks/${TASK}/prompts`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(within(a!).getByTestId('attention.fix-conflicts'));
    expect(await within(a!).findByTestId('attention.fix-conflicts.error')).toHaveAttribute('data-code', 'forbidden');
    t.client.on(`POST /v1/tasks/${TASK}/prompts`, () => ({ session_id: 's1', scope_seq: 9 }));
    fireEvent.click(within(a!).getByTestId('attention.fix-conflicts'));
    expect(await within(a!).findByTestId('attention.fix-conflicts.sent')).toBeInTheDocument();
    expect(within(a!).queryByTestId('attention.fix-conflicts')).toBeNull();
    expect(await prompts(t)).toEqual([fixConflictsInstruction(A), fixConflictsInstruction(A)]);
  });

  it('shows nothing for a task without links and no button to a reader', async () => {
    const t = await taskWorld();
    base(t, [taskRow(t, { task_id: TASK }) as TaskRow]);
    t.client.on(`GET /v1/workgroups/${t.wg}/pr-links`, () => ({ items: [], next_cursor: null }));
    line(t, true);
    await waitFor(() => expect(t.client.of(`GET /v1/workgroups/${t.wg}/pr-links`)).toHaveLength(1));
    expect(screen.queryByTestId('attention.pulls')).toBeNull();
    const u = await taskWorld();
    base(u, [taskRow(u, { task_id: TASK }) as TaskRow]);
    const link = await sealedRow(u, { task: TASK, ref: A });
    u.client.on(`GET /v1/workgroups/${u.wg}/pr-links`, () => ({ items: [link], next_cursor: null }));
    statuses(u, [[A, ready(A)]]);
    line(u, false);
    expect(await screen.findByTestId('attention.pull')).toHaveAttribute('data-conflict', 'conflict');
    expect(screen.queryByTestId('attention.fix-conflicts')).toBeNull();
  });
});
