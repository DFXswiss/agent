// U-WEB-analysis (screens, ADR-097 sections 2, 5, 6 and 7): "Optimize processes" in the situation view and the workgroup
// view only for a person who may write a workgroup (resp. this one), the dialog with its defaults, the visibility
// sentence and the request (sealed fixed title and prompt, `analysis`), the failures, the badge, the locked profile,
// the orchestrator reason and "Adopt" into the workgroup or organization whiteboard as the person.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { openEvent } from '../../src/core/crypto/event';
import { b64uDecode } from '../../src/core/encoding/b64u';
import { fromUtf8 } from '../../src/core/encoding/bytes';
import { createI18n } from '../../src/core/i18n';
import { epochKey, ORG, WG } from '../../src/core/log/state';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { addKeys, setLog } from '../../src/core/store/slices/orgs';
import { putSession, putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { I18nProvider } from '../../src/ui/i18n';
import { HomeScreen } from '../../src/ui/screens/home/HomeScreen';
import { OrchestratorAction } from '../../src/ui/screens/session/OrchestratorAction';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { TaskListScreen } from '../../src/ui/screens/tasks/TaskListScreen';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';

const OTHER = '0190f000-0000-7000-8000-00000000000a';
const READ_ONLY = '0190f000-0000-7000-8000-00000000000b';
const ANALYSIS = { scope: 'org', since: '2026-11-19T12:00:00.000Z', until: '2026-11-26T12:00:00.000Z' };
const NOW = Date.parse('2026-11-26T12:00:00.000Z');

/**
 * A world whose user may write the default workgroup and `OTHER`; a member (not admin) may also read, not write,
 * `READ_ONLY`.
 */
async function world(options: { member?: boolean } = {}): Promise<TaskWorld> {
  const t = await taskWorld({ member: options.member ?? false });
  await t.world.batch(t.ada, [{ type: 'wg_create', wg: OTHER, epoch: 1 }]);
  if (options.member) {
    await t.world.batch(t.ada, [
      { type: 'grant', subject: t.me.id, wg: OTHER, access: 'write' },
      { type: 'wg_create', wg: READ_ONLY, epoch: 1 },
      { type: 'grant', subject: t.me.id, wg: READ_ONLY, access: 'read' },
    ]);
  }
  setLog(t.store, t.org, t.world.log!);
  addKeys(t.store, t.org, t.world.held.get(t.me.id)!);
  t.clock.now = NOW;
  for (const wg of [t.wg, OTHER, READ_ONLY]) {
    t.client.on(`GET /v1/workgroups/${wg}/tasks`, () => ({ tasks: [], next_before: null }));
    t.client.on(`GET /v1/workgroups/${wg}`, () => ({ devices: [{}] }));
  }
  t.client.on('POST /v1/tasks', (body) => ({
    task: taskRow(t, { task_id: (body as { task_id: string }).task_id, analysis: ANALYSIS }),
    session: { session_id: 's1' },
    events: [],
  }));
  return t;
}

const posted = (t: TaskWorld) =>
  t.client.of('POST /v1/tasks').map((call) => call.body as { task_id: string; wg_id: string; events: unknown[] });

describe('the button', () => {
  it('starts an analysis of the organization from the situation view in the default workgroup', async () => {
    const t = await world();
    const { router } = mount(t, [{ path: '/o/:org', element: <HomeScreen /> }], `/o/${t.org}`);
    const open = await screen.findByTestId('analysis.open');
    expect(open).toHaveTextContent('Optimize processes');
    expect(open).toHaveAttribute('data-scope', 'org');
    fireEvent.click(open);
    const dialog = screen.getByTestId('analysis.dialog');
    expect(dialog).toHaveAttribute('role', 'dialog');
    expect(dialog).toHaveAccessibleName('Optimize processes');
    expect(screen.getByTestId('analysis.wg')).toHaveFocus();
    expect(screen.getByTestId('analysis.wg')).toHaveValue(t.wg);
    expect([...screen.getByTestId('analysis.wg').querySelectorAll('option')].map((o) => o.value)).toEqual([
      t.wg,
      OTHER,
    ]);
    expect(screen.getByTestId('analysis.period')).toHaveValue('7');
    expect(screen.getByLabelText('Period')).toBe(screen.getByTestId('analysis.period'));
    expect(screen.getByTestId('analysis.visibility')).toHaveTextContent(
      'The report is visible to all members of the workgroup Default workgroup and may contain totals of all ' +
        'workgroups you can read.',
    );
    fireEvent.change(screen.getByTestId('analysis.wg'), { target: { value: OTHER } });
    expect(screen.getByTestId('analysis.visibility')).toHaveTextContent('members of the workgroup Workgroup and');
    fireEvent.change(screen.getByTestId('analysis.wg'), { target: { value: t.wg } });
    fireEvent.change(screen.getByTestId('analysis.period'), { target: { value: '30' } });
    fireEvent.click(screen.getByTestId('analysis.start'));
    await waitFor(() => expect(router.state.location.pathname).toMatch(new RegExp(`^/o/${t.org}/t/`)));
    const [body] = posted(t);
    expect(router.state.location.pathname).toBe(`/o/${t.org}/t/${body!.task_id}`);
    expect(body).toMatchObject({
      wg_id: t.wg,
      analysis: { scope: 'org', since: '2026-10-27T12:00:00.000Z', until: '2026-11-26T12:00:00.000Z' },
    });
    const key = t.world.held.get(t.me.id)!.get(epochKey(WG, t.wg, 1))!;
    const sig = b64uDecode(t.me.identity.public().sig);
    const [meta, prompt] = body!.events;
    expect(JSON.parse(fromUtf8(await openEvent(key, meta, sig)))).toEqual({ title: 'Optimize processes' });
    expect(JSON.parse(fromUtf8(await openEvent(key, prompt, sig)))).toEqual({
      text:
        'Optimize processes: analyse the organization from 2026-10-27T12:00:00.000Z to 2026-11-26T12:00:00.000Z ' +
        'and propose improvements.',
      attachments: [],
    });
  });

  it('starts an analysis of the current workgroup from the workgroup view, in German', async () => {
    const t = await world({ member: true });
    const { router } = mount(
      t,
      [{ path: '/o/:org/w/:wg', element: <TaskListScreen /> }],
      `/o/${t.org}/w/${OTHER}`,
      'de',
    );
    fireEvent.click(await screen.findByTestId('analysis.open'));
    expect(screen.getByTestId('analysis.open')).toHaveTextContent('Prozesse optimieren');
    expect(screen.getByTestId('analysis.wg')).toHaveValue(OTHER);
    expect(screen.getByTestId('analysis.visibility')).toHaveAttribute('data-scope', 'wg');
    expect(screen.getByTestId('analysis.visibility')).toHaveTextContent(
      'Der Bericht ist für alle Mitglieder der Workgroup Arbeitsgruppe sichtbar.',
    );
    fireEvent.click(screen.getByTestId('analysis.start'));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/t\//));
    const [body] = posted(t);
    expect(body).toMatchObject({ wg_id: OTHER, analysis: { scope: 'wg', since: '2026-11-19T12:00:00.000Z' } });
    const key = t.world.held.get(t.me.id)!.get(epochKey(WG, OTHER, 1))!;
    const sig = b64uDecode(t.me.identity.public().sig);
    expect(JSON.parse(fromUtf8(await openEvent(key, body!.events[1], sig)))).toMatchObject({
      text:
        'Prozesse optimieren: Analysiere die Workgroup Arbeitsgruppe von 2026-11-19T12:00:00.000Z bis ' +
        '2026-11-26T12:00:00.000Z und schlage Verbesserungen vor.',
    });
  });

  it('is shown only to a person who may write a workgroup (resp. this one)', async () => {
    const t = await world({ member: true });
    mount(t, [{ path: '/o/:org/w/:wg', element: <TaskListScreen /> }], `/o/${t.org}/w/${READ_ONLY}`);
    await screen.findByTestId('tasks.screen');
    expect(screen.queryByTestId('analysis.open')).toBeNull();
    document.body.innerHTML = '';
    mount(t, [{ path: '/o/:org', element: <HomeScreen /> }], `/o/${t.org}`);
    await screen.findByTestId('analysis.open');
    act(() => t.store.update('me', (me) => ({ ...me, userId: 'someone-else' })));
    expect(screen.getByTestId('home.read-only')).toBeInTheDocument();
    expect(screen.queryByTestId('analysis.open')).toBeNull();
  });

  it('shows failures, retries the same request after a passing one, and closes', async () => {
    const t = await world();
    mount(t, [{ path: '/o/:org', element: <HomeScreen /> }], `/o/${t.org}`);
    fireEvent.click(await screen.findByTestId('analysis.open'));
    const failures = [
      new HubError('peer_outdated', 409, { devices: ['d1'] }),
      new HubError('unavailable', 503),
      new HubError('forbidden', 403),
    ];
    t.client.on('POST /v1/tasks', () => {
      throw failures.shift()!;
    });
    const start = screen.getByTestId('analysis.start');
    fireEvent.click(start);
    const failed = await screen.findByTestId('analysis.error');
    expect(failed).toHaveAttribute('data-code', 'peer_outdated');
    expect(failed).toHaveTextContent('every device of the organization must be updated first');
    fireEvent.click(start);
    await waitFor(() => expect(screen.getByTestId('analysis.error')).toHaveAttribute('data-code', 'unavailable'));
    expect(screen.getByTestId('analysis.error')).toHaveTextContent(createI18n('en').error('unavailable').title);
    fireEvent.click(start);
    await waitFor(() => expect(screen.getByTestId('analysis.error')).toHaveAttribute('data-code', 'forbidden'));
    fireEvent.click(start);
    await waitFor(() => expect(posted(t)).toHaveLength(4));
    const ids = posted(t).map((body) => body.task_id);
    // after peer_outdated a new request; after unavailable the same one; after forbidden a new one
    expect(ids[1]).not.toBe(ids[0]);
    expect(ids[2]).toBe(ids[1]);
    expect(ids[3]).not.toBe(ids[2]);
    expect(screen.getByTestId('analysis.start')).toBeEnabled();
    fireEvent.click(screen.getByTestId('analysis.cancel'));
    expect(screen.queryByTestId('analysis.dialog')).toBeNull();
    fireEvent.click(screen.getByTestId('analysis.open'));
    fireEvent.keyDown(screen.getByTestId('analysis.dialog'), { key: 'Escape' });
    expect(screen.queryByTestId('analysis.dialog')).toBeNull();
  });
});

describe('analysis tasks', () => {
  it('carry the badge in the task list and on the task page; the profile cannot be changed there', async () => {
    const t = await world();
    const row = taskRow(t, { analysis: ANALYSIS, agent_profile: { name: 'analysis', operations: [], reads: 'all' } });
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => ({
      tasks: [row, taskRow(t, { task_id: '0190f000-0000-7000-8000-000000000002', analysis: null })],
      next_before: null,
    }));
    mount(t, [{ path: '/o/:org/w/:wg', element: <TaskListScreen /> }], `/o/${t.org}/w/${t.wg}`, 'de');
    await waitFor(() => expect(screen.getAllByTestId('tasks.row')).toHaveLength(2));
    expect(screen.getAllByTestId('tasks.row.analysis')).toHaveLength(1);
    const marked = screen.getAllByTestId('tasks.row').find((item) => item.getAttribute('data-task') === TASK)!;
    expect(within(marked).getByTestId('tasks.row.analysis')).toHaveTextContent('Analyse');
    document.body.innerHTML = '';
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({ task: row, sessions: [] }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    const { router } = mount(
      t,
      [{ path: '/o/:org/t/:task', element: <TaskScreen /> }],
      `/o/${t.org}/t/${TASK}?tab=chat`,
    );
    expect(await screen.findByTestId('task.analysis')).toHaveTextContent('Analysis');
    expect(screen.getByTestId('task.profile.name')).toHaveAttribute('data-profile', 'analysis');
    expect(screen.queryByTestId('task.profile.change')).toBeNull();
    expect(screen.getByTestId('task.profile.analysis')).toHaveTextContent(
      'An analysis task always reads only; start an ordinary task to change something.',
    );
    fireEvent.click(screen.getByTestId('task.profile.new'));
    expect(router.state.location.pathname).toBe(`/o/${t.org}/w/${t.wg}`);
  });

  it('show readers the reason without the link; ordinary tasks keep the change button', async () => {
    const t = await world({ member: true });
    putTasks(t.store, [
      taskRow(t, { wg_id: READ_ONLY, analysis: ANALYSIS }) as TaskRow,
      taskRow(t, { task_id: 'plain' }) as TaskRow,
    ]);
    t.client.on(`GET /v1/scopes/${READ_ONLY}/events`, () => ({ events: [], next_after: null, head: 0 }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}?tab=chat`);
    expect(await screen.findByTestId('task.profile.analysis')).toBeInTheDocument();
    expect(screen.queryByTestId('task.profile.new')).toBeNull();
    document.body.innerHTML = '';
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/plain`);
    expect(await screen.findByTestId('task.profile.change')).toBeInTheDocument();
    expect(screen.queryByTestId('task.profile.analysis')).toBeNull();
    expect(screen.queryByTestId('task.analysis')).toBeNull();
  });

  it('name why their session cannot become the main orchestrator', async () => {
    const t = await world();
    putTasks(t.store, [taskRow(t, { analysis: ANALYSIS }) as TaskRow]);
    putSession(t.store, sessionRow(t, { state: 'working' }) as never, [], 0);
    t.client.on(`GET /v1/sessions/${MAIN}/orchestrator`, () => ({
      session_id: MAIN,
      device_id: null,
      available: false,
      reason: 'analysis',
      requested_at: null,
      pairing: null,
    }));
    const router = createMemoryRouter(
      [{ path: '/', element: <OrchestratorAction entry={t.store.getState().tasks.sessions[MAIN]!} depth={0} /> }],
      { initialEntries: ['/'] },
    );
    const app = { store: t.store, tasks: t.context, client: t.client, userId: t.me.id } as never;
    render(
      <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
        <I18nProvider value={createI18n('en')}>
          <RouterProvider router={router} />
        </I18nProvider>
      </AppEnvProvider>,
    );
    expect(await screen.findByTestId('orchestrator.analysis')).toHaveTextContent(
      'A session of an analysis task cannot become your main orchestrator',
    );
    expect(screen.queryByTestId('orchestrator.make')).toBeNull();
  });
});

describe('adopt', () => {
  const REPORT = '# Efficiency\n\nTask 1 retried three times.';

  async function page(options: { member?: boolean; task?: Record<string, unknown>; locale?: 'en' | 'de' } = {}) {
    const t = await world({ member: options.member ?? false });
    const row = taskRow(t, { analysis: ANALYSIS, ...options.task });
    t.client.on(`GET /v1/tasks/${TASK}`, () => ({
      task: row,
      sessions: [{ session: sessionRow(t), transitions: [] }],
    }));
    t.client.on(`GET /v1/scopes/${row.wg_id as string}/events`, () => ({ events: [], next_after: null, head: 0 }));
    const mounted = mount(
      t,
      [{ path: '/o/:org/t/:task', element: <TaskScreen /> }],
      `/o/${t.org}/t/${TASK}?tab=chat`,
      options.locale,
    );
    await screen.findByTestId('task.screen');
    await waitFor(() => expect(t.store.getState().tasks.sessions[MAIN]).toBeDefined());
    return { t, ...mounted };
  }

  const put = (t: TaskWorld, scope: string, ...entries: EventEntry[]) =>
    act(() =>
      putEvents(
        t.store,
        scope,
        entries.map((entry) => ({ ...entry, scopeId: scope })),
      ),
    );

  it('writes an output into the organization whiteboard as the person and links it', async () => {
    const { t } = await page();
    let seq = 10;
    t.client.on('POST /v1/events', () => ({ results: [{ scope_seq: (seq += 1) }] }));
    put(t, t.wg, verified(t, 'session.output', { text: REPORT }, { session: MAIN }));
    fireEvent.click(await screen.findByTestId('analysis.adopt'));
    const dialog = screen.getByTestId('analysis.adopt.dialog');
    expect(dialog).toHaveAccessibleName('Adopt into a whiteboard');
    expect(screen.getByTestId('analysis.adopt.level.workgroup')).toBeChecked();
    expect(screen.getByTestId('analysis.adopt.visibility')).toHaveTextContent(
      'The report is visible to all members of the workgroup Workgroup.',
    );
    fireEvent.click(screen.getByTestId('analysis.adopt.level.org'));
    expect(screen.getByTestId('analysis.adopt.visibility')).toHaveTextContent(
      'The report is visible to all members of the organization and may contain totals of all workgroups you can read.',
    );
    fireEvent.click(screen.getByTestId('analysis.adopt.save'));
    expect(await screen.findByTestId('analysis.adopt.done')).toHaveTextContent('Adopted into the whiteboard.');
    expect(screen.getByTestId('analysis.adopt.open')).toHaveAttribute('href', `/o/${t.org}/whiteboard`);
    expect(screen.queryByTestId('analysis.adopt.save')).toBeNull();
    const [call] = t.client.of('POST /v1/events');
    const event = (call!.body as { events: { event: { header: Record<string, unknown> } }[] }).events[0]!.event;
    expect(event.header).toMatchObject({ kind: 'whiteboard.entry', scope: ORG, author: t.me.id });
    const key = t.world.held.get(t.me.id)!.get(epochKey(ORG, t.org, 1))!;
    const sig = b64uDecode(t.me.identity.public().sig);
    expect(JSON.parse(fromUtf8(await openEvent(key, event, sig)))).toEqual({ level: 'org', text: REPORT });
    fireEvent.click(screen.getByTestId('analysis.adopt.cancel'));
    expect(screen.queryByTestId('analysis.adopt.dialog')).toBeNull();
  });

  it('writes into the workgroup whiteboard, says when it failed, and offers a member only the workgroup', async () => {
    const { t } = await page({ member: true, locale: 'de' });
    const answers: (() => unknown)[] = [
      () => {
        throw new HubError('forbidden', 403);
      },
      () => ({ results: [{ scope_seq: 3 }] }),
    ];
    t.client.on('POST /v1/events', () => answers.shift()!());
    put(t, t.wg, verified(t, 'session.output', { text: REPORT }, { session: MAIN }));
    fireEvent.click(await screen.findByTestId('analysis.adopt'));
    expect(screen.getByTestId('analysis.adopt')).toHaveTextContent('Übernehmen');
    expect(screen.queryByTestId('analysis.adopt.level.org')).toBeNull();
    fireEvent.click(screen.getByTestId('analysis.adopt.save'));
    const failed = await screen.findByTestId('analysis.adopt.error');
    expect(failed).toHaveAttribute('data-code', 'forbidden');
    expect(failed).toHaveTextContent('Nicht übernommen:');
    fireEvent.click(screen.getByTestId('analysis.adopt.save'));
    await screen.findByTestId('analysis.adopt.done');
    expect(screen.getByTestId('analysis.adopt.open')).toHaveAttribute('href', `/o/${t.org}/w/${t.wg}/whiteboard`);
    const event = (t.client.of('POST /v1/events')[1]!.body as { events: { event: { header: object } }[] }).events[0]!
      .event;
    expect(event.header).toMatchObject({ kind: 'whiteboard.entry', scope: WG, wg: t.wg, task: '' });
    fireEvent.keyDown(screen.getByTestId('analysis.adopt.dialog'), { key: 'Escape' });
    expect(screen.queryByTestId('analysis.adopt.dialog')).toBeNull();
  });

  it('is offered only on outputs with text of an analysis task the person may write somewhere', async () => {
    const { t } = await page({ member: true, task: { wg_id: READ_ONLY } });
    put(t, READ_ONLY, verified(t, 'session.output', { text: REPORT }, { session: MAIN }));
    expect(await screen.findByTestId('chat.output')).toBeInTheDocument();
    expect(screen.queryByTestId('analysis.adopt')).toBeNull();
    document.body.innerHTML = '';
    const plain = await page({ task: { analysis: null } });
    put(plain.t, plain.t.wg, verified(plain.t, 'session.output', { text: REPORT }, { session: MAIN }));
    expect(await screen.findByTestId('chat.output')).toBeInTheDocument();
    expect(screen.queryByTestId('analysis.adopt')).toBeNull();
    document.body.innerHTML = '';
    const odd = await page();
    // another task's session, an unknown session, no session, no text: nothing to adopt
    act(() => putSession(odd.t.store, sessionRow(odd.t, { session_id: 'foreign', task_id: 'gone' }) as never, [], 0));
    put(
      odd.t,
      odd.t.wg,
      verified(odd.t, 'session.output', { text: 'a' }, { session: 'foreign' }),
      verified(odd.t, 'session.output', { text: 'b' }, { session: 'unknown' }),
      verified(odd.t, 'session.output', { text: 'c' }, { session: '' }),
      verified(odd.t, 'session.output', { text: '' }, { session: MAIN }),
      verified(odd.t, 'session.output', { text: REPORT }, { session: MAIN }),
    );
    await waitFor(() => expect(screen.getAllByTestId('chat.output')).toHaveLength(5));
    expect(screen.getAllByTestId('analysis.adopt')).toHaveLength(1);
  });
});
