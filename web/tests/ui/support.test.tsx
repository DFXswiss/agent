// DFX support in jsdom (ADR-096, KOR-56): the release section of the workgroup settings with its confirmation dialog
// (release, change, revoke, refusals, readers, archived workgroups), the support area of a support person (releases,
// workgroup, conversation, composer, read release, ended release, polling), members' names of support authors in the
// chat and in the audit, and the navigation entry.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { senderMark } from '../../src/core/community/mark';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { WG } from '../../src/core/log/state';
import { POLL_MS } from '../../src/core/support/area';
import type { TimelineItem } from '../../src/core/chat/timeline';
import { chatItem } from '../../src/ui/chat-items/prompt';
import { I18nProvider } from '../../src/ui/i18n';
import { AuthorNames, supportText, useAuthorOverride } from '../../src/ui/names/support';
import { AuditScreen } from '../../src/ui/screens/audit/AuditScreen';
import { nav, supportHidden } from '../../src/ui/screens/support/nav';
import { routes as supportRoutes } from '../../src/ui/screens/support/routes';
import { routes as settingsRoutes } from '../../src/ui/screens/wg-settings/routes';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { idOf } from '../core/log/world';
import { auditRow, T0 } from '../core/monitoring/fixtures';
import { type OrgWorld, orgWorld } from '../core/org/world';
import { type SupportWorld, supportWorld, TASK } from '../core/support/world';
import { taskRow } from '../core/tasks/world';
import { mount, names } from './org-world';

afterEach(() => {
  vi.useRealTimers();
});

const SAM_NAME = 'Sam';

/** The admin's settings world: workgroup `one` (bob reads it), sam and tom are support. */
async function settingsWorld(options: { as?: 'ada' | 'bob'; support?: ('sam' | 'tom')[] } = {}) {
  const one = await idOf('wg one');
  const w = await orgWorld({
    as: options.as ?? 'ada',
    support: options.support ?? ['sam', 'tom'],
    setup: async (world, a) => {
      await world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]);
      await world.admitUser(a.ada, a.bob, 'member', { [one]: 'read' });
    },
  });
  names(w, { [w.others.ada.id]: 'Ada', [w.others.bob.id]: 'Bob' }, { [one]: 'Team' });
  for (const scope of [w.org, one])
    w.client.on(`GET /v1/scopes/${scope}/events`, () => ({ events: [], next_after: null }));
  w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({
    releases: [...w.world.state.support].map(([wg, [access, seq]]) => ({
      wg_id: wg,
      access,
      granted_seq: seq,
      granted_at: '2026-11-21T12:00:00.000Z',
    })),
    people: [{ user_id: w.others.sam.id, name: SAM_NAME }],
  }));
  return { w, one };
}

const openSettings = (w: OrgWorld, wg: string) => mount(w, settingsRoutes as never, `/o/${w.org}/w/${wg}/settings`);

async function confirm(button: string) {
  fireEvent.click(await screen.findByTestId(button));
  const dialog = await screen.findByTestId('support.dialog');
  fireEvent.click(within(dialog).getByTestId('support.dialog.confirm'));
  await waitFor(() => expect(screen.getByTestId('support.section.done')).toBeVisible());
}

describe('release section', () => {
  it('releases after the confirmation, changes the access and revokes', async () => {
    const { w, one } = await settingsWorld();
    openSettings(w, one);
    const state = await screen.findByTestId('support.section.state');
    expect(state).toHaveTextContent('Not released to support.');
    const mark = await senderMark(w.others.sam.id);
    const tomMark = await senderMark(w.others.tom.id);
    await waitFor(() => expect(screen.getByTestId('support.section.people')).toHaveTextContent(`Support ${tomMark}`));
    expect(screen.getByTestId('support.section.people')).toHaveTextContent(`Support ${SAM_NAME}`);
    expect(screen.getByTestId('support.section.people')).not.toHaveTextContent(mark);
    // the dialog says what support sees, keeps and what devices do, and can be cancelled
    fireEvent.click(screen.getByTestId('support.section.read'));
    const dialog = await screen.findByTestId('support.dialog');
    expect(within(dialog).getByTestId('support.dialog.access')).toHaveTextContent('Access: read');
    expect(within(dialog).getByTestId('support.dialog.sees')).toHaveTextContent('Support sees every task');
    expect(within(dialog).getByTestId('support.dialog.kept')).toHaveTextContent('stays read');
    expect(within(dialog).getByTestId('support.dialog.devices')).toHaveTextContent('DFX devices');
    expect(within(dialog).getByTestId('support.dialog.people')).toHaveTextContent('Current support persons');
    fireEvent.click(within(dialog).getByTestId('support.dialog.cancel'));
    expect(screen.queryByTestId('support.dialog')).toBeNull();
    await confirm('support.section.read');
    expect(screen.getByTestId('support.section.done')).toHaveTextContent('The release is saved.');
    await waitFor(() => expect(screen.getByTestId('support.section.state')).toHaveAttribute('data-release', 'read'));
    await waitFor(() => expect(screen.getByTestId('support.section.state')).toHaveTextContent('read (since'));
    const posted = w.client.of(`POST /v1/orgs/${w.org}/log`);
    expect((posted[0]!.body as { events: unknown[] }).events).toHaveLength(1);
    await confirm('support.section.to-write');
    await waitFor(() => expect(screen.getByTestId('support.section.state')).toHaveAttribute('data-release', 'write'));
    fireEvent.click(screen.getByTestId('support.section.revoke'));
    expect(await screen.findByTestId('support.dialog.revoke')).toHaveTextContent('loses access at once');
    fireEvent.click(screen.getByTestId('support.dialog.confirm'));
    await waitFor(() => expect(screen.getByTestId('support.section.done')).toHaveTextContent('revoked'));
    await waitFor(() => expect(screen.getByTestId('support.section.state')).toHaveAttribute('data-release', 'none'));
    expect(w.world.state.epoch(WG, one)).toBe(2);
  });

  it('reports a refused release and names no persons while none is support', async () => {
    const { w, one } = await settingsWorld({ support: [] });
    w.client.on(`POST /v1/orgs/${w.org}/log`, () => {
      throw new HubError('forbidden', 403);
    });
    openSettings(w, one);
    expect(await screen.findByTestId('support.section.people')).toHaveTextContent('There is no support person');
    fireEvent.click(screen.getByTestId('support.section.write'));
    fireEvent.click(await screen.findByTestId('support.dialog.confirm'));
    expect(await screen.findByTestId('support.section.failed')).toHaveTextContent('No access');
  });

  it('shows the release without its time while the hub has not named it, loading persons', async () => {
    const { w, one } = await settingsWorld();
    await w.world.batch(w.others.ada, [{ type: 'support_grant', wg: one, access: 'write' }]);
    await w.sync();
    let answer: (value: unknown) => void = () => undefined;
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => new Promise((resolve) => (answer = resolve)));
    openSettings(w, one);
    expect(await screen.findByTestId('support.section.state')).toHaveTextContent(
      'Released to support: read and write.',
    );
    expect(screen.getByTestId('support.section.people')).toHaveTextContent('Loading the support persons');
    expect(screen.getByTestId('support.section.to-read')).toBeVisible();
    await act(async () => answer({ releases: [], people: [] }));
  });

  it('offers only the revocation of an archived workgroup and nothing to readers', async () => {
    const { w, one } = await settingsWorld();
    const archived = await idOf('archived wg');
    await w.world.batch(w.others.ada, [{ type: 'wg_create', wg: archived, epoch: 1 }]);
    await w.world.batch(w.others.ada, [{ type: 'wg_archive', wg: archived }]);
    await w.sync();
    w.client.on(`GET /v1/scopes/${archived}/events`, () => ({ events: [], next_after: null }));
    openSettings(w, archived);
    expect(await screen.findByTestId('support.section.archived')).toBeVisible();
    expect(screen.queryByTestId('support.section.read')).toBeNull();
    const reader = await settingsWorld({ as: 'bob' });
    openSettings(reader.w, reader.one);
    expect(await screen.findAllByTestId('wg-settings.screen')).toHaveLength(2);
    expect(screen.getAllByTestId('support.section')).toHaveLength(1);
    expect(one).toBeTruthy();
  });

  it('offers the revocation of a released archived workgroup', async () => {
    const { w, one } = await settingsWorld();
    await w.world.batch(w.others.ada, [{ type: 'support_grant', wg: one, access: 'read' }]);
    await w.world.batch(w.others.ada, [{ type: 'wg_archive', wg: one }]);
    await w.sync();
    openSettings(w, one);
    expect(await screen.findByTestId('support.section.revoke')).toBeVisible();
    expect(screen.queryByTestId('support.section.to-write')).toBeNull();
    expect(screen.queryByTestId('support.section.archived')).toBeNull();
  });
});

/** sam's browser over the support world. */
function samWorld(s: SupportWorld): OrgWorld {
  return {
    ...s.w,
    me: s.a.sam,
    store: s.store,
    client: s.client,
    context: { ...s.context, refreshLog: vi.fn(async () => true), ingest: vi.fn(async () => undefined) },
  } as unknown as OrgWorld;
}

function serveTasks(s: SupportWorld, closed: string | null = null) {
  const row = taskRow(
    { org: s.w.org, wg: s.one, me: s.a.bob },
    { task_id: TASK, main_session_id: null, closed_at: closed },
  );
  s.client.on(`GET /v1/workgroups/${s.one}/tasks`, () => ({ tasks: [row], next_before: null }));
  s.client.on(`GET /v1/tasks/${TASK}`, () => ({ task: row, sessions: [] }));
  s.client.on('GET /v1/support/releases', () => ({
    releases: [
      { org_id: s.w.org, wg_id: s.one, access: 'write', granted_seq: 5, granted_at: '2026-11-21T12:00:00.000Z' },
    ],
  }));
}

describe('support area', () => {
  it('lists the releases by the names of support.info, opens the workgroup and the conversation and writes', async () => {
    const s = await supportWorld({ access: 'write' });
    serveTasks(s);
    await s.write(s.a.bob, 'task.meta', { title: 'Broken build' });
    await s.write(s.a.bob, 'prompt', { text: 'Please help', attachments: [] });
    const { router } = mount(samWorld(s), supportRoutes as never, '/support');
    const release = await screen.findByTestId('support.release');
    await waitFor(() =>
      expect(within(release).getByTestId('support.release.name')).toHaveTextContent('Team in DFX AG'),
    );
    expect(within(release).getByTestId('support.release.access')).toHaveTextContent('Read and write');
    fireEvent.click(within(release).getByTestId('support.release.open'));
    expect(await screen.findByTestId('support.wg.access')).toHaveAttribute('data-access', 'write');
    const task = await screen.findByTestId('support.task.open');
    await waitFor(() => expect(task).toHaveTextContent('Broken build'));
    fireEvent.click(task);
    expect(router.state.location.pathname).toBe(`/support/${s.w.org}/${s.one}/${TASK}`);
    await waitFor(() => expect(screen.getByTestId('chat.prompt.author')).toHaveTextContent('Bob'));
    fireEvent.change(screen.getByTestId('support.composer.text'), { target: { value: 'Support here' } });
    fireEvent.click(screen.getByTestId('support.composer.send'));
    await waitFor(() => expect(screen.getAllByTestId('chat.prompt')).toHaveLength(2));
    expect(screen.getAllByTestId('chat.prompt.author')[1]).toHaveTextContent('You');
    expect(screen.getByTestId('support.composer.text')).toHaveValue('');
    // a refused prompt keeps its text
    s.client.on(`POST /v1/tasks/${TASK}/prompts`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.change(screen.getByTestId('support.composer.text'), { target: { value: 'Again' } });
    fireEvent.click(screen.getByTestId('support.composer.send'));
    expect(await screen.findByTestId('support.composer.failed')).toHaveAttribute('data-code', 'forbidden');
    fireEvent.click(screen.getByTestId('support.task.back'));
    expect(await screen.findByTestId('support.tasks')).toBeVisible();
    fireEvent.click(screen.getByTestId('support.back'));
    expect(await screen.findByTestId('support.releases')).toBeVisible();
  });

  it('shows no composer with a read release and names authors by their mark without support.info', async () => {
    const s = await supportWorld({ access: 'read' });
    serveTasks(s, '2026-11-21T12:00:09.000Z');
    s.events.splice(0, s.events.length);
    await s.write(s.a.bob, 'prompt', { text: 'Please help', attachments: [] });
    mount(samWorld(s), supportRoutes as never, `/support/${s.w.org}/${s.one}/${TASK}`);
    expect(await screen.findByTestId('support.task.read-only')).toHaveTextContent('cannot write');
    const mark = await senderMark(s.a.bob.id);
    await waitFor(() => expect(screen.getByTestId('chat.prompt.author')).toHaveTextContent(mark));
    expect(screen.getByTestId('support.wg.title')).toHaveTextContent(
      'Workgroup without a name in Organization without a name',
    );
  });

  it('ends a view whose release is gone, reports a failed check and polls', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const s = await supportWorld({ access: 'read' });
    serveTasks(s);
    mount(samWorld(s), supportRoutes as never, `/support/${s.w.org}/${s.one}`);
    expect(await screen.findByTestId('support.tasks')).toBeVisible();
    // the organization revokes the release; the next poll shows it
    await s.w.world.batch(s.a.ada, [{ type: 'support_revoke', wg: s.one }]);
    await act(() => vi.advanceTimersByTimeAsync(POLL_MS));
    expect(await screen.findByTestId('support.gone')).toBeVisible();
    s.client.on(`GET /v1/orgs/${s.w.org}/log`, () => {
      throw new HubError('unavailable', 503);
    });
    await act(() => vi.advanceTimersByTimeAsync(POLL_MS));
    expect(await screen.findByTestId('support.failed')).toHaveAttribute('data-code', 'unavailable');
  });

  it('checks nothing for a user outside the roster, ends a task view without release, and stops after leaving', async () => {
    const former = await supportWorld({ former: true });
    mount(samWorld(former), supportRoutes as never, `/support/${former.w.org}/${former.one}`);
    expect(await screen.findByTestId('support.area.none')).toBeVisible();
    cleanup();
    mount(samWorld(former), supportRoutes as never, `/support/${former.w.org}/${former.one}/${TASK}`);
    expect(await screen.findByTestId('support.area.none')).toBeVisible();
    expect(former.client.calls).toHaveLength(0);
    cleanup();
    const none = await supportWorld({ access: null });
    serveTasks(none);
    mount(samWorld(none), supportRoutes as never, `/support/${none.w.org}/${none.one}/${TASK}`);
    expect(await screen.findByTestId('support.gone')).toBeVisible();
    cleanup();
    // a check that fails after the view was left changes nothing
    let fail: (error: unknown) => void = () => undefined;
    const fresh = await supportWorld({ access: null });
    fresh.client.on(`GET /v1/orgs/${fresh.w.org}/log`, () => new Promise((_resolve, reject) => (fail = reject)));
    mount(samWorld(fresh), supportRoutes as never, `/support/${fresh.w.org}/${fresh.one}`);
    expect(await screen.findByTestId('support.checking')).toBeVisible();
    cleanup();
    await act(async () => fail(new HubError('unavailable', 503)));
  });

  it('is not enabled for a user outside the roster, and shows the states of the release list', async () => {
    const s = await supportWorld({ former: true });
    mount(samWorld(s), supportRoutes as never, '/support');
    expect(await screen.findByTestId('support.area.none')).toBeVisible();
    expect(supportHidden(s.store.getState())).toBe(true);
    expect(nav[0]!.to).toBe('/support');
  });

  it('keeps a release without names when its support.info cannot be read', async () => {
    const s = await supportWorld({ access: 'read' });
    serveTasks(s);
    s.client.on(`GET /v1/scopes/${s.one}/events`, () => {
      throw new HubError('unavailable', 503);
    });
    mount(samWorld(s), supportRoutes as never, '/support');
    const release = await screen.findByTestId('support.release');
    await waitFor(() => expect(s.client.of(`GET /v1/scopes/${s.one}/events`).length).toBeGreaterThan(0));
    expect(within(release).getByTestId('support.release.name')).toHaveTextContent(
      'Workgroup without a name in Organization without a name',
    );
  });

  it('loads a further page of the task list', async () => {
    const s = await supportWorld({ access: 'read' });
    const row = taskRow({ org: s.w.org, wg: s.one, me: s.a.bob }, { task_id: TASK, main_session_id: null });
    const targets: string[] = [];
    s.client.on(`GET /v1/workgroups/${s.one}/tasks`, (_body, target) => {
      targets.push(target);
      return { tasks: targets.length === 1 ? [row] : [], next_before: targets.length === 1 ? 'older' : null };
    });
    mount(samWorld(s), supportRoutes as never, `/support/${s.w.org}/${s.one}`);
    fireEvent.click(await screen.findByTestId('support.tasks.load-more'));
    await waitFor(() => expect(targets.some((target) => target.includes('before=older'))).toBe(true));
  });

  it('shows the release list as loading, unknown, failed and empty, and reloads', async () => {
    const s = await supportWorld();
    expect(supportHidden(s.store.getState())).toBe(false);
    let answer: () => unknown = () => ({ releases: [] });
    s.client.on('GET /v1/support/releases', () => answer());
    mount(samWorld(s), supportRoutes as never, '/support');
    expect(await screen.findByTestId('support.area.empty')).toBeVisible();
    answer = () => {
      throw new HubError('not_found', 404);
    };
    fireEvent.click(screen.getByTestId('support.area.reload'));
    expect(await screen.findByTestId('support.area.unknown')).toBeVisible();
    answer = () => {
      throw new HubError('unavailable', 503);
    };
    fireEvent.click(screen.getByTestId('support.area.reload'));
    expect(await screen.findByTestId('support.area.failed')).toBeVisible();
    expect(screen.queryByTestId('support.area.loading')).toBeNull();
  });
});

describe('members see support', () => {
  function promptOf(author: string): TimelineItem {
    return {
      key: 'e1',
      kind: 'prompt',
      source: 'event',
      at: T0,
      seq: 1,
      sessionId: null,
      author,
      eventId: 'p1',
      body: { text: 'From support' },
      code: null,
      subagent: null,
    };
  }

  function renderPrompt(w: OrgWorld, author: string, names: ((id: string) => string | null) | null = null) {
    const app = { store: w.store, tasks: w.context, client: w.client };
    render(
      <AppEnvProvider value={{ app: app as never, credentials: {} as never, origin: 'https://hub.example' }}>
        <I18nProvider value={createI18n('en')}>
          <AuthorNames.Provider value={names}>
            <chatItem.Component item={promptOf(author)} />
          </AuthorNames.Provider>
        </I18nProvider>
      </AppEnvProvider>,
    );
  }

  it('names a support person "Support <Name>" in the chat', async () => {
    const { w } = await settingsWorld();
    w.store.update('session', (session) => ({ ...session, activeOrg: w.org }));
    renderPrompt(w, w.others.sam.id);
    await waitFor(() => expect(screen.getByTestId('chat.prompt.author')).toHaveTextContent(`Support ${SAM_NAME}`));
    expect(w.client.of(`GET /v1/orgs/${w.org}/support`)).toHaveLength(1);
  });

  it('uses the support area names first and falls back to the directory', async () => {
    const { w } = await settingsWorld();
    renderPrompt(w, w.others.bob.id, (id) => (id === 'x' ? 'X' : null));
    expect(screen.getByTestId('chat.prompt.author')).toHaveTextContent('Member');
  });

  it('labels support persons in the audit with name and mark', async () => {
    const { w } = await settingsWorld();
    const sam = w.others.sam.id;
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({ releases: [], people: [{ user_id: sam, name: SAM_NAME }] }));
    w.client.on(`GET /v1/orgs/${w.org}/audit`, () => ({
      rows: [
        auditRow(2, T0 + 2000, { actor_id: sam, action: 'support.write' }),
        auditRow(1, T0 + 1000, { actor_id: sam, action: 'read.workgroup.tasks' }),
        auditRow(0, T0, { actor_kind: 'hub', actor_id: null, action: 'audit.repeat' }),
      ],
      next_before: null,
    }));
    mount(w, [{ path: '/o/:org/audit', element: <AuditScreen /> }], `/o/${w.org}/audit`);
    const mark = await senderMark(sam);
    await waitFor(() =>
      expect(screen.getAllByTestId('audit.row.actor')[0]).toHaveTextContent(`Support ${SAM_NAME} ${mark}`),
    );
    expect(screen.getAllByTestId('audit.row.actor')[1]).toHaveTextContent(`Support ${SAM_NAME} ${mark}`);
  });

  it('reads the names of an org once, and names nobody without an author or a platform log', async () => {
    const { w } = await settingsWorld();
    w.store.update('support', (support) => ({
      ...support,
      orgs: { [w.org]: { status: 'ready', code: null, releases: [], people: { [w.others.sam.id]: 'Known' } } },
    }));
    w.store.update('session', (session) => ({ ...session, activeOrg: w.org }));
    renderPrompt(w, w.others.sam.id);
    await waitFor(() => expect(screen.getByTestId('chat.prompt.author')).toHaveTextContent('Support Known'));
    expect(w.client.of(`GET /v1/orgs/${w.org}/support`)).toHaveLength(0);
    cleanup();
    renderPrompt(w, null as never);
    expect(screen.getByTestId('chat.prompt.author')).toHaveTextContent('Member');
  });

  it('labels audit rows without a platform log and with the names loaded', async () => {
    const one = await idOf('wg one');
    const w = await orgWorld({
      noPlatform: true,
      setup: async (world, a) => world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]).then(() => undefined),
    });
    w.store.update('support', (support) => ({
      ...support,
      orgs: { [w.org]: { status: 'ready', code: null, releases: [], people: {} } },
    }));
    for (const scope of [w.org])
      w.client.on(`GET /v1/scopes/${scope}/events`, () => ({ events: [], next_after: null }));
    w.client.on(`GET /v1/orgs/${w.org}/audit`, () => ({
      rows: [auditRow(1, T0 + 1000, { actor_id: w.others.sam.id, action: 'read.workgroup.tasks' })],
      next_before: null,
    }));
    mount(w, [{ path: '/o/:org/audit', element: <AuditScreen /> }], `/o/${w.org}/audit`);
    expect(await screen.findByTestId('audit.row.actor')).toHaveTextContent('Name not known yet');
    cleanup();
    const { w: t } = await settingsWorld();
    t.store.update('support', (support) => ({
      ...support,
      orgs: { [t.org]: { status: 'ready', code: null, releases: [], people: {} } },
    }));
    t.client.on(`GET /v1/orgs/${t.org}/audit`, () => ({
      rows: [auditRow(1, T0 + 1000, { actor_id: t.others.sam.id, action: 'read.workgroup.tasks' })],
      next_before: null,
    }));
    mount(t, [{ path: '/o/:org/audit', element: <AuditScreen /> }], `/o/${t.org}/audit`);
    await waitFor(() => expect(screen.getByTestId('audit.row.actor')).toHaveTextContent(/^Support #/));
    expect(t.client.of(`GET /v1/orgs/${t.org}/support`)).toHaveLength(0);
  });

  it('names no support persons without a platform log in the settings', async () => {
    const one = await idOf('wg one');
    const w = await orgWorld({
      noPlatform: true,
      setup: async (world, a) => world.batch(a.ada, [{ type: 'wg_create', wg: one, epoch: 1 }]).then(() => undefined),
    });
    for (const scope of [w.org, one])
      w.client.on(`GET /v1/scopes/${scope}/events`, () => ({ events: [], next_after: null }));
    w.client.on(`GET /v1/orgs/${w.org}/support`, () => ({ releases: [], people: [] }));
    openSettings(w, one);
    expect(await screen.findByTestId('support.section.people')).toHaveTextContent('There is no support person');
  });

  it('builds every form of the support label', () => {
    const t = createI18n('en').t;
    expect(supportText(t, { name: 'Sam', mark: '#ABCD' }, true)).toBe('Support Sam #ABCD');
    expect(supportText(t, { name: 'Sam', mark: null }, true)).toBe('Support Sam');
    expect(supportText(t, { name: null, mark: '#ABCD' })).toBe('Support #ABCD');
    expect(supportText(t, { name: null, mark: null })).toBe('Support');
    function Probe({ id }: { readonly id: string | null }) {
      return <span data-testid="probe">{useAuthorOverride(id) ?? '-'}</span>;
    }
    render(<Probe id="a" />);
    expect(screen.getByTestId('probe')).toHaveTextContent('-');
    cleanup();
    render(
      <AuthorNames.Provider value={() => 'A'}>
        <Probe id={null} />
      </AuthorNames.Provider>,
    );
    expect(screen.getByTestId('probe')).toHaveTextContent('-');
  });
});
