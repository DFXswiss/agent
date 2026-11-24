// U-WEB-ui-whiteboard: the boards in jsdom against the core board world (real reader, scriptable client): the org and
// workgroup screens with their level bar, loading and not-found states, create/edit/delete with their failures, the
// reader without controls, neutral rows, paging, load errors, the history drawer with the line diff, the task tab.
// The Playwright specs (`e2e/whiteboard.spec.ts`, `e2e/devices/whiteboard-task.spec.ts`) run them against the hub.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import type { EventEntry } from '../../src/core/store/slices/events';
import { putEvents } from '../../src/core/store/slices/events';
import { orgEntry } from '../../src/core/store/slices/orgs';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { orgBoard, taskBoard, wgBoard } from '../../src/core/whiteboards/location';
import { WhiteboardScreen } from '../../src/ui/screens/whiteboard/WhiteboardScreen';
import { WhiteboardTab } from '../../src/ui/task-tabs/whiteboard/WhiteboardTab';
import { Time } from '../../src/ui/whiteboard/Time';
import { boardEvent, type BoardWorld, boardWorld, deliver, entryId } from '../core/whiteboards/world';
import { taskRow } from '../core/tasks/world';
import { mount, TASK } from './chat-world';

const ROUTES = [
  { path: '/o/:org/whiteboard', element: <WhiteboardScreen /> },
  { path: '/o/:org/w/:wg/whiteboard', element: <WhiteboardScreen /> },
];

const empty = { events: [], next_after: null, head: 0 };
/** Positions of accepted writes (shared, so that a re-served client never repeats one). */
let seq = 100;

/** Scope pulls return what was delivered so far; the list returns `rows`; writes are accepted. */
function serve(w: BoardWorld, rows: unknown[] = [], nextBefore: string | null = null) {
  w.client.on('GET /v1/whiteboards', () => ({ entries: rows, next_before: nextBefore }));
  w.client.on(`GET /v1/scopes/${w.org}/events`, () => empty);
  w.client.on(`GET /v1/scopes/${w.wg}/events`, () => empty);
  w.client.on(`GET /v1/scopes/${w.research}/events`, () => empty);
  w.client.on('POST /v1/events', () => {
    seq += 1;
    return { results: [{ scope_seq: seq }] };
  });
}

async function orgScreen(options: { member?: boolean } = {}) {
  const w = await boardWorld(options);
  serve(w);
  const mounted = mount(w, ROUTES, `/o/${w.org}/whiteboard`);
  await screen.findByTestId('whiteboard.screen');
  return { w, ...mounted };
}

const items = () => screen.queryAllByTestId('whiteboard.item');

describe('org board', () => {
  it('creates, edits, deletes and shows the history as the admin', async () => {
    const { w } = await orgScreen();
    expect(screen.getByTestId('whiteboard.title')).toHaveTextContent('Organization whiteboard');
    expect(await screen.findByTestId('whiteboard.empty')).toBeInTheDocument();
    expect(screen.queryByTestId('whiteboard.readonly')).toBeNull();
    // create: cancel, then save (disabled while empty), with notify
    fireEvent.click(screen.getByTestId('whiteboard.create'));
    fireEvent.click(screen.getByTestId('whiteboard.editor.cancel'));
    fireEvent.click(screen.getByTestId('whiteboard.create'));
    expect(screen.getByTestId('whiteboard.editor.save')).toBeDisabled();
    fireEvent.change(screen.getByTestId('whiteboard.editor.text'), { target: { value: 'First *note*' } });
    fireEvent.click(screen.getByTestId('whiteboard.editor.notify'));
    fireEvent.click(screen.getByTestId('whiteboard.editor.save'));
    const entry = await screen.findByTestId('whiteboard.item');
    await waitFor(() => expect(within(entry).getByTestId('markdown')).toHaveTextContent('First note'));
    const sent = w.client.of('POST /v1/events')[0]!.body as { events: { notify?: boolean }[] };
    expect(sent.events[0]!.notify).toBe(true);
    expect(within(entry).getByTestId('whiteboard.item.author')).toHaveTextContent('Name not known yet');
    // edit: a failure keeps the editor with the message, then the edit succeeds
    fireEvent.click(within(entry).getByTestId('whiteboard.item.edit'));
    expect(screen.queryByTestId('whiteboard.editor.notify')).toBeNull();
    fireEvent.click(screen.getByTestId('whiteboard.editor.cancel'));
    fireEvent.click(within(entry).getByTestId('whiteboard.item.edit'));
    expect(screen.getByTestId('whiteboard.editor.text')).toHaveValue('First *note*');
    w.client.on('POST /v1/events', () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.change(screen.getByTestId('whiteboard.editor.text'), { target: { value: 'Second' } });
    fireEvent.click(screen.getByTestId('whiteboard.editor.save'));
    expect(await screen.findByTestId('whiteboard.editor.error')).toHaveAttribute('data-code', 'forbidden');
    serve(w);
    fireEvent.click(screen.getByTestId('whiteboard.editor.save'));
    await waitFor(() => expect(within(entry).getByTestId('whiteboard.item.edited')).toBeInTheDocument());
    // delete: cancel, a failure, then the delete
    fireEvent.click(within(entry).getByTestId('whiteboard.item.delete'));
    fireEvent.click(within(entry).getByTestId('whiteboard.item.delete.cancel'));
    fireEvent.click(within(entry).getByTestId('whiteboard.item.delete'));
    w.client.on('POST /v1/events', () => {
      throw new HubError('unavailable', 503);
    });
    fireEvent.click(within(entry).getByTestId('whiteboard.item.delete.confirm'));
    expect(await within(entry).findByTestId('whiteboard.item.delete.error')).toBeInTheDocument();
    serve(w);
    fireEvent.click(within(entry).getByTestId('whiteboard.item.delete.confirm'));
    expect(await within(entry).findByTestId('whiteboard.item.deleted')).toBeInTheDocument();
    expect(within(entry).queryByTestId('whiteboard.item.edit')).toBeNull();
    // history: three versions with diffs; the history load failed (unknown route), the versions are shown anyway
    fireEvent.click(within(entry).getByTestId('whiteboard.item.history'));
    const dialog = await screen.findByTestId('whiteboard.history');
    expect(within(dialog).getAllByTestId('whiteboard.history.version')).toHaveLength(3);
    expect(
      within(dialog)
        .getAllByTestId('whiteboard.history.action')
        .map((node) => node.textContent),
    ).toEqual(['Created', 'Edited', 'Deleted']);
    expect(await within(dialog).findByTestId('whiteboard.history.error')).toBeInTheDocument();
    // while an event of the entry is re-read (pending), the drawer stays open with the last known versions
    const creating = [...w.store.getState().events[w.org]!.entries.values()].find(
      (item) => item.header?.kind === 'whiteboard.entry',
    )!;
    act(() => putEvents(w.store, w.org, [{ ...creating, status: 'pending', header: null, body: null }]));
    expect(screen.getByTestId('whiteboard.history')).toBeInTheDocument();
    expect(within(dialog).getAllByTestId('whiteboard.history.version')).toHaveLength(3);
    act(() => putEvents(w.store, w.org, [creating]));
    const stats = within(dialog)
      .getAllByTestId('whiteboard.diff.stats')
      .map((node) => node.textContent);
    expect(stats).toEqual(['+1 / −0 lines', '+1 / −1 lines', '+0 / −1 lines']);
    fireEvent.click(within(dialog).getByTestId('whiteboard.history.close'));
    expect(screen.queryByTestId('whiteboard.history')).toBeNull();
  });

  it('shows a member the board without controls and names authors from their profiles', async () => {
    const { w } = await orgScreen({ member: true });
    expect(await screen.findByTestId('whiteboard.readonly')).toHaveTextContent('You have read access.');
    expect(screen.queryByTestId('whiteboard.create')).toBeNull();
    const board = orgBoard(w.org);
    await act(async () => {
      await deliver(w, board, [
        await boardEvent(w, board, 'member.profile', w.ada.id, { display_name: 'Ada' }),
        await boardEvent(w, board, 'whiteboard.entry', await entryId('m'), { level: 'org', text: 'Hi' }),
      ]);
    });
    const entry = await screen.findByTestId('whiteboard.item');
    expect(within(entry).getByTestId('whiteboard.item.author')).toHaveTextContent('Ada');
    expect(within(entry).queryByTestId('whiteboard.item.edit')).toBeNull();
    expect(within(entry).getByTestId('whiteboard.item.history')).toBeInTheDocument();
  });

  it('shows neutral rows, placeholders, paging and load errors', async () => {
    const w = await boardWorld();
    const board = orgBoard(w.org);
    let rows: unknown[] = [];
    w.client.on('GET /v1/whiteboards', () => {
      throw new HubError('unavailable', 503);
    });
    w.client.on(`GET /v1/scopes/${w.org}/events`, () => empty);
    mount(w, ROUTES, `/o/${w.org}/whiteboard`);
    expect(await screen.findByTestId('whiteboard.error')).toHaveTextContent(/./);
    const row = (entry: string) => ({
      entry_id: entry,
      org_id: w.org,
      level: 'org',
      wg_id: null,
      task_id: null,
      author_id: w.ada.id,
      created_at: '2026-11-21T12:00:00.000Z',
      updated_at: '2026-11-21T12:00:00.000Z',
      deleted: false,
      latest_event: { scope_id: w.org, scope_seq: 50 },
    });
    rows = [row('gone')];
    w.client.on('GET /v1/whiteboards', (_body, target) =>
      target.includes('before=')
        ? { entries: [row('older')], next_before: null }
        : { entries: rows, next_before: 'next' },
    );
    // every page is listed: both rows at once
    fireEvent.click(within(screen.getByTestId('whiteboard.error')).getByRole('button'));
    await waitFor(() => expect(screen.queryByTestId('whiteboard.error')).toBeNull());
    await waitFor(() => expect(items().map((item) => item.dataset.type)).toEqual(['missing', 'missing']));
    expect(items()[0]).toHaveTextContent('This entry is not available.');
    // an unverifiable and a locked event
    const raw = (seq: number, status: 'unverifiable' | 'locked', code: string | null): EventEntry => ({
      org: w.org,
      scopeId: w.org,
      scopeSeq: seq,
      receivedAt: null,
      event: { header: { kind: 'whiteboard.entry', org: w.org, scope: 'org', scope_id: w.org, task: '', ref: '' } },
      status,
      code,
      header: null,
      body: null,
    });
    act(() =>
      putEvents(w.store, w.org, [
        raw(60, 'unverifiable', 'author_not_writer'),
        raw(61, 'locked', null),
        raw(62, 'unverifiable', null),
      ]),
    );
    const neutral = items().filter((item) => item.dataset.type !== 'missing');
    expect(neutral.map((item) => [item.dataset.type, item.dataset.code])).toEqual([
      ['unverifiable', 'unverifiable'],
      ['locked', undefined],
      ['unverifiable', 'author_not_writer'],
    ]);
    expect(neutral[2]).toHaveTextContent('Unverifiable item (error code: author_not_writer)');
    expect(board.level).toBe('org');
  });
});

describe('workgroup screen', () => {
  it('lists the readable workgroups and shows a workgroup board', async () => {
    const w = await boardWorld({ member: true });
    serve(w);
    const { router } = mount(w, ROUTES, `/o/${w.org}/w/${w.research}/whiteboard`);
    expect(await screen.findByTestId('whiteboard.title')).toHaveTextContent('Whiteboard of Workgroup');
    const levels = screen.getAllByTestId('whiteboard.level');
    expect(levels.map((level) => level.dataset.level)).toEqual(['org', w.wg, w.research]);
    expect(screen.getByTestId('whiteboard.readonly')).toBeInTheDocument();
    fireEvent.click(levels[1]!);
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${w.org}/w/${w.wg}/whiteboard`));
    expect(await screen.findByTestId('whiteboard.create')).toBeInTheDocument();
    expect(screen.getByTestId('whiteboard.board')).toHaveAttribute('data-level', 'workgroup');
    expect(wgBoard(w.org, w.wg).level).toBe('workgroup');
  });

  it('is not found without read access and loading without a verified log', async () => {
    const w = await boardWorld({ member: true });
    serve(w);
    mount(w, ROUTES, `/o/${w.org}/w/0190f000-0000-7000-8000-00000000abcd/whiteboard`);
    expect(await screen.findByTestId('whiteboard.not-found')).toBeInTheDocument();
    act(() =>
      w.store.update('orgs', (orgs) => ({
        ...orgs,
        byId: { ...orgs.byId, [w.org]: { ...orgEntry(orgs, w.org), log: null } },
      })),
    );
    expect(await screen.findByTestId('whiteboard.loading')).toBeInTheDocument();
  });
});

describe('task tab and time', () => {
  it('shows nothing without the task row', async () => {
    const w = await boardWorld();
    mount(w, [{ path: '/t', element: <WhiteboardTab orgId={w.org} taskId={TASK} /> }], '/t');
    await waitFor(() => expect(screen.queryByTestId('elsewhere')).toBeNull());
    expect(screen.queryByTestId('whiteboard.board')).toBeNull();
  });

  it('shows the task board of the task row', async () => {
    const w = await boardWorld();
    serve(w);
    putTasks(w.store, [taskRow(w) as TaskRow]);
    mount(w, [{ path: '/t', element: <WhiteboardTab orgId={w.org} taskId={TASK} /> }], '/t');
    expect(await screen.findByTestId('whiteboard.board')).toHaveAttribute('data-level', 'task');
    const pull = new URL(w.client.of(`GET /v1/scopes/${w.wg}/events`)[0]!.target, 'https://h');
    expect(pull.searchParams.get('task')).toBe(TASK);
    expect(taskBoard(w.org, w.wg, TASK).task).toBe(TASK);
  });

  it('shows no time without a usable one', async () => {
    const w = await boardWorld();
    mount(w, [{ path: '/', element: <Time at={0} testId="t" /> }], '/');
    await waitFor(() => expect(screen.queryByTestId('elsewhere')).toBeNull());
    expect(screen.queryByTestId('t')).toBeNull();
  });
});
