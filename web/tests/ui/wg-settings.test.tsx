// Workgroup settings screen in jsdom (06-web.md section 8.1): defaults, editing with line errors, saving as a
// `settings.workgroup` event, following newer settings while unchanged, the concurrent-edit warning (show newer /
// save anyway), readers without controls, unknown workgroups and failed loads or saves.
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { blockOrg, setLog } from '../../src/core/store/slices/orgs';
import { routes } from '../../src/ui/screens/wg-settings/routes';
import { taskWorld, type TaskWorld } from '../core/tasks/world';
import { mount } from './chat-world';

const SAVED = { repositories: ['dfx/agent'], protected_branches: ['main'], pr_draft_default: false };

function settingsEvent(t: TaskWorld, seq: number, body: unknown): EventEntry {
  const header = { kind: 'settings.workgroup', author: t.me.id, id: `s${seq}` };
  return {
    org: t.org,
    scopeId: t.wg,
    scopeSeq: seq,
    receivedAt: null,
    event: { header },
    status: 'verified',
    code: null,
    header,
    body,
  };
}

function serve(t: TaskWorld, events: unknown[] = []) {
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events, next_after: null }));
  let seq = 40;
  t.client.on('POST /v1/events', (body) => {
    const event = (body as { events: { event: { header: { id: string } } }[] }).events[0]!.event;
    seq += 1;
    return { results: [{ id: event.header.id, scope_id: t.wg, scope_seq: seq }] };
  });
}

const open = (t: TaskWorld, wg = t.wg) => mount(t, routes as never, `/o/${t.org}/w/${wg}/settings`);

describe('workgroup settings', () => {
  it('shows the defaults, refuses invalid lines and saves the event', async () => {
    const t = await taskWorld();
    serve(t);
    open(t);
    expect(await screen.findByTestId('wg-settings.defaults')).toBeVisible();
    expect(screen.getByTestId('wg-settings.protected')).toHaveValue('main\nmaster\ndevelop\nrelease/*');
    expect(screen.getByTestId('wg-settings.draft')).toBeChecked();
    expect(screen.getByTestId('wg-settings.back')).toHaveAttribute('href', `/o/${t.org}/w/${t.wg}`);
    fireEvent.change(screen.getByTestId('wg-settings.repositories'), { target: { value: 'DFX/Agent\nnot a repo' } });
    fireEvent.change(screen.getByTestId('wg-settings.protected'), { target: { value: 'main\nbad pattern' } });
    fireEvent.click(screen.getByTestId('wg-settings.save'));
    const errors = await screen.findAllByTestId('wg-settings.error');
    expect(errors.map((item) => item.textContent)).toEqual([
      'Line 2: “not a repo” is not owner/name.',
      'Line 2: “bad pattern” is not a branch name or pattern.',
    ]);
    fireEvent.change(screen.getByTestId('wg-settings.repositories'), { target: { value: 'DFX/Agent' } });
    fireEvent.change(screen.getByTestId('wg-settings.protected'), { target: { value: 'main' } });
    fireEvent.click(screen.getByTestId('wg-settings.draft'));
    fireEvent.click(screen.getByTestId('wg-settings.save'));
    expect(await screen.findByTestId('wg-settings.saved')).toBeVisible();
    expect(screen.getByTestId('wg-settings.repositories')).toHaveValue('dfx/agent');
    expect(t.client.of('POST /v1/events')).toHaveLength(1);
    expect(t.ingested.at(-1)!.items[0]!.scope_seq).toBe(41);
    fireEvent.change(screen.getByTestId('wg-settings.repositories'), { target: { value: 'dfx/agent\na/b' } });
    expect(screen.queryByTestId('wg-settings.saved')).toBeNull();
  });

  it('follows newer settings while unchanged and warns once edited', async () => {
    const t = await taskWorld();
    serve(t);
    putEvents(t.store, t.wg, [settingsEvent(t, 5, SAVED)]);
    open(t);
    expect(await screen.findByTestId('wg-settings.repositories')).toHaveValue('dfx/agent');
    act(() => putEvents(t.store, t.wg, [settingsEvent(t, 6, { ...SAVED, repositories: ['dfx/agent-core'] })]));
    expect(screen.getByTestId('wg-settings.repositories')).toHaveValue('dfx/agent-core');
    fireEvent.change(screen.getByTestId('wg-settings.repositories'), { target: { value: 'mine/repo' } });
    act(() => putEvents(t.store, t.wg, [settingsEvent(t, 7, { ...SAVED, repositories: ['their/repo'] })]));
    expect(screen.getByTestId('wg-settings.conflict')).toBeVisible();
    expect(screen.getByTestId('wg-settings.repositories')).toHaveValue('mine/repo');
    fireEvent.click(screen.getByTestId('wg-settings.conflict.reload'));
    expect(screen.getByTestId('wg-settings.repositories')).toHaveValue('their/repo');
    expect(screen.queryByTestId('wg-settings.conflict')).toBeNull();
    fireEvent.change(screen.getByTestId('wg-settings.repositories'), { target: { value: 'mine/repo' } });
    act(() => putEvents(t.store, t.wg, [settingsEvent(t, 8, SAVED)]));
    fireEvent.click(screen.getByTestId('wg-settings.save'));
    expect(await screen.findByTestId('wg-settings.conflict')).toBeVisible();
    expect(t.client.of('POST /v1/events')).toHaveLength(0);
    fireEvent.click(screen.getByTestId('wg-settings.conflict.overwrite'));
    expect(await screen.findByTestId('wg-settings.saved')).toBeVisible();
    expect(t.client.of('POST /v1/events')).toHaveLength(1);
  });

  it('refuses a save over newer settings that arrived without a store update in between', async () => {
    const t = await taskWorld();
    serve(t);
    open(t);
    fireEvent.change(await screen.findByTestId('wg-settings.repositories'), { target: { value: 'a/b' } });
    // the event lands while the hub answers: the save sees it and asks
    t.client.on('POST /v1/events', () => {
      throw new HubError('epoch_stale', 400);
    });
    fireEvent.click(screen.getByTestId('wg-settings.save'));
    expect(await screen.findByTestId('wg-settings.failed')).toBeVisible();
  });

  it('reports invalid stored settings and a refused save', async () => {
    const t = await taskWorld();
    serve(t);
    putEvents(t.store, t.wg, [settingsEvent(t, 5, { junk: true })]);
    t.client.on('POST /v1/events', () => {
      throw new HubError('forbidden', 403);
    });
    open(t);
    expect(await screen.findByTestId('wg-settings.invalid')).toBeVisible();
    fireEvent.click(screen.getByTestId('wg-settings.save'));
    expect(await screen.findByTestId('wg-settings.failed')).toHaveTextContent('No access');
  });

  it('names the code of a refused save that the core raised', async () => {
    const t = await taskWorld();
    serve(t);
    open(t);
    await screen.findByTestId('wg-settings.form');
    act(() => blockOrg(t.store, t.org, { code: 'log_fork', stored: null, served: null, lh: null }));
    fireEvent.click(screen.getByTestId('wg-settings.save'));
    expect(await screen.findByTestId('wg-settings.failed')).toBeVisible();
  });

  it('shows the values without controls to readers', async () => {
    const t = await taskWorld({ member: true });
    const readerWg = '0190f000-0000-7000-8000-00000000000c';
    await t.world.batch(t.ada, [
      { type: 'wg_create', wg: readerWg, epoch: 1 },
      { type: 'grant', subject: t.me.id, wg: readerWg, access: 'read' },
    ]);
    setLog(t.store, t.org, t.world.log!);
    t.client.on(`GET /v1/scopes/${readerWg}/events`, () => ({ events: [], next_after: null }));
    putEvents(t.store, readerWg, [{ ...settingsEvent(t, 3, SAVED), scopeId: readerWg }]);
    open(t, readerWg);
    expect(await screen.findByTestId('wg-settings.read-only')).toBeVisible();
    expect(screen.getByTestId('wg-settings.view.repositories')).toHaveTextContent('dfx/agent');
    expect(screen.getByTestId('wg-settings.view.protected')).toHaveTextContent('main');
    expect(screen.getByTestId('wg-settings.view.draft')).toHaveTextContent('ready for review');
    expect(screen.queryByTestId('wg-settings.form')).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
  });

  it('shows the defaults to readers and the workgroup name', async () => {
    const t = await taskWorld({ member: true });
    const readerWg = '0190f000-0000-7000-8000-00000000000d';
    await t.world.batch(t.ada, [
      { type: 'wg_create', wg: readerWg, epoch: 1 },
      { type: 'grant', subject: t.me.id, wg: readerWg, access: 'read' },
    ]);
    setLog(t.store, t.org, t.world.log!);
    t.client.on(`GET /v1/scopes/${readerWg}/events`, () => ({ events: [], next_after: null }));
    t.client.on(`GET /v1/scopes/${t.org}/events`, () => ({ events: [], next_after: null }));
    const header = { kind: 'wg.profile', wg: readerWg, author: t.ada.id, id: 'p1' };
    putEvents(t.store, t.org, [
      {
        org: t.org,
        scopeId: t.org,
        scopeSeq: 1,
        receivedAt: null,
        event: { header },
        status: 'verified',
        code: null,
        header,
        body: { name: 'Research' },
      },
    ]);
    open(t, readerWg);
    expect(await screen.findByTestId('wg-settings.view.repositories')).toHaveTextContent(
      'Every repository of the GitHub connection is allowed.',
    );
    expect(screen.getByTestId('wg-settings.view.draft')).toHaveTextContent('drafts by default');
    expect(screen.getByTestId('wg-settings.title')).toHaveTextContent('Research');
  });

  it('says so for unknown workgroups, unverified logs and failed loads', async () => {
    const t = await taskWorld();
    serve(t);
    open(t, '0190f000-0000-7000-8000-00000000ffff');
    expect(await screen.findByTestId('wg-settings.not-found')).toBeVisible();
  });

  it('waits for a verified log', async () => {
    const t = await taskWorld();
    t.store.update('orgs', (orgs) => ({ ...orgs, byId: {} }));
    open(t);
    expect(await screen.findByTestId('wg-settings.unverified')).toBeVisible();
  });

  it('reports a failed pull of the settings', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => {
      throw new HubError('unavailable', 503);
    });
    open(t);
    expect(await screen.findByTestId('wg-settings.load-failed')).toHaveTextContent('Service unavailable');
    await waitFor(() => expect(screen.queryByTestId('wg-settings.loading')).toBeNull());
  });
});
