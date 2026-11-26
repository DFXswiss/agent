// U-WEB-ui-tasks (captured tasks, E-41, ADR-100 section 7) in jsdom against the core test world: "Capture task" (the
// fields, a topic created inline, the workgroup choice, the retry with the same request, refusals), the ticket filters
// (hub query and browser filters), the row badges, and the task page's fields panel (edit, status, "Start agent").
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { setLog } from '../../src/core/store/slices/orgs';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { EMPTY_DIRECTORY } from '../../src/core/tasks/directory';
import { I18nProvider } from '../../src/ui/i18n';
import { TaskFields } from '../../src/ui/screens/task/TaskFields';
import { CaptureForm } from '../../src/ui/screens/tasks/CaptureForm';
import { OverviewScreen } from '../../src/ui/screens/tasks/OverviewScreen';
import { TaskListScreen } from '../../src/ui/screens/tasks/TaskListScreen';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { idOf } from '../core/log/world';
import { taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';

const TASK = '0190f000-0000-7000-8000-000000000001';
const TOPIC = '0190f000-0000-7000-8000-0000000000c1';

function mount(t: TaskWorld, path: string, routes: { path: string; element: ReactNode }[], locale: 'en' | 'de' = 'en') {
  const router = createMemoryRouter([...routes, { path: '*', element: <p data-testid="elsewhere" /> }], {
    initialEntries: [path],
  });
  const app = { store: t.store, tasks: t.context } as never;
  render(
    <AppEnvProvider value={{ app, credentials: {} as never, origin: 'https://hub.example' }}>
      <I18nProvider value={createI18n(locale)}>
        <RouterProvider router={router} />
      </I18nProvider>
    </AppEnvProvider>,
  );
  return router;
}

function event(t: TaskWorld, scope: string, seq: number, header: Record<string, unknown>, body: unknown): EventEntry {
  return {
    org: t.org,
    scopeId: scope,
    scopeSeq: seq,
    receivedAt: null,
    event: {},
    status: 'verified',
    code: null,
    header: { id: `${scope}-${seq}`, ts: '2026-11-21T12:00:00.000Z', author: t.me.id, ...header },
    body,
  };
}

/** Ada's org with names, a topic, a workgroup "Research" and an empty task list per workgroup. */
async function world() {
  const t = await taskWorld();
  const research = await idOf('research');
  await t.world.batch(t.ada, [{ type: 'wg_create', wg: research, epoch: 1 }]);
  setLog(t.store, t.org, t.world.log!);
  putEvents(t.store, t.org, [
    event(t, t.org, 1, { kind: 'wg.profile', wg: t.wg }, { name: 'Default' }),
    event(t, t.org, 2, { kind: 'wg.profile', wg: research }, { name: 'Research' }),
    event(t, t.org, 3, { kind: 'member.profile', ref: t.me.id }, { display_name: 'Ada' }),
    event(t, t.org, 4, { kind: 'topic.profile', ref: TOPIC }, { name: 'Billing' }),
  ]);
  putEvents(t.store, t.wg, [
    event(t, t.wg, 1, { kind: 'settings.workgroup', wg: t.wg }, {
      repositories: ['dfxswiss/agent'],
      protected_branches: [],
      pr_draft_default: true,
    }),
  ]);
  const listed: string[] = [];
  for (const wg of [t.wg, research]) {
    t.client.on(`GET /v1/workgroups/${wg}/tasks`, (_body, target) => {
      listed.push(target);
      return { tasks: [], next_before: null };
    });
  }
  for (const scope of [t.org, t.wg, research]) {
    t.client.on(`GET /v1/scopes/${scope}/events`, () => ({ events: [], next_before: null }));
  }
  t.client.on('GET /v1/me/github-identities', () => ({ identities: [], defaults: [] }));
  return { t, research, listed };
}

const captureRoutes = (t: TaskWorld, wgs: string[]) => [
  {
    path: '/o/:org/capture',
    element: <CaptureForm org={t.org} wgs={wgs} name={(wg) => (wg === t.wg ? 'Default' : 'Research')} />,
  },
  { path: '/o/:org/t/:task', element: <p data-testid="task.page" /> },
];

describe('capture task', () => {
  it('captures a task with every field and a topic created inline, then links it', async () => {
    const { t, research } = await world();
    let created = 0;
    t.client.on('POST /v1/events', () => {
      created += 1;
      return { results: [{ id: 'x', scope_id: t.org, scope_seq: 10 + created }] };
    });
    t.client.on('POST /v1/tasks', (body) => ({
      task: taskRow(t, { task_id: (body as { task_id: string }).task_id, wg_id: research, main_session_id: null }),
      session: null,
      events: [{ id: 'm', scope_id: research, scope_seq: 1 }],
    }));
    const router = mount(t, `/o/${t.org}/capture`, captureRoutes(t, [t.wg, research]));
    fireEvent.click(screen.getByTestId('tickets.capture.open'));
    const form = screen.getByTestId('tickets.capture');
    expect(screen.getByTestId('tickets.capture.submit')).toBeDisabled();
    fireEvent.change(screen.getByTestId('tickets.capture.wg'), { target: { value: research } });
    fireEvent.change(screen.getByTestId('tickets.capture.title'), { target: { value: 'Review invoices' } });
    fireEvent.change(screen.getByTestId('tickets.capture.description'), { target: { value: 'All of them' } });
    fireEvent.change(screen.getByTestId('tickets.capture.person'), { target: { value: t.me.id } });
    expect(within(screen.getByTestId('tickets.capture.person')).getAllByRole('option')[1]).toHaveTextContent('Ada');
    // a repository fills an empty organisation with its owner, not a set one
    fireEvent.change(screen.getByTestId('tickets.capture.github-repo'), { target: { value: 'DFXswiss/agent' } });
    expect(screen.getByTestId('tickets.capture.github-org')).toHaveValue('DFXswiss');
    fireEvent.change(screen.getByTestId('tickets.capture.github-repo'), { target: { value: 'other/web' } });
    expect(screen.getByTestId('tickets.capture.github-org')).toHaveValue('DFXswiss');
    fireEvent.change(screen.getByTestId('tickets.capture.github-repo'), { target: { value: 'DFXswiss/agent' } });
    fireEvent.change(screen.getByTestId('tickets.capture.urgency'), { target: { value: 'urgent' } });
    fireEvent.change(screen.getByTestId('tickets.capture.topic'), { target: { value: '#new' } });
    expect(screen.getByTestId('tickets.capture.topic.create')).toBeDisabled();
    fireEvent.change(screen.getByTestId('tickets.capture.topic.name'), { target: { value: 'Payroll' } });
    fireEvent.click(screen.getByTestId('tickets.capture.topic.create'));
    await waitFor(() => expect(screen.queryByTestId('tickets.capture.topic.name')).toBeNull());
    const topic = (screen.getByTestId('tickets.capture.topic') as HTMLSelectElement).value;
    expect(topic).not.toBe('');
    fireEvent.change(screen.getByTestId('tickets.capture.topic'), { target: { value: TOPIC } });
    fireEvent.submit(form);
    await waitFor(() => expect(screen.getByTestId('tickets.capture.done')).toBeInTheDocument());
    const body = t.client.of('POST /v1/tasks')[0]!.body as Record<string, unknown>;
    expect(body).toMatchObject({ wg_id: research, urgency: 'urgent', assignee_id: t.me.id, topic_id: TOPIC });
    expect(body.events).toHaveLength(1);
    expect(screen.queryByTestId('tickets.capture')).toBeNull();
    fireEvent.click(screen.getByTestId('tickets.capture.show'));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/o/${t.org}/t/${body.task_id as string}`));
  });

  it('repeats the same request after a failure, explains invalid input and refusals, and cancels', async () => {
    const { t } = await world();
    let fail: HubError | null = new HubError('unavailable', 503);
    t.client.on('POST /v1/tasks', (body) => {
      if (fail !== null) throw fail;
      return { task: taskRow(t, { task_id: (body as { task_id: string }).task_id }), session: null, events: [] };
    });
    t.client.on('POST /v1/events', () => {
      throw new HubError('forbidden', 403);
    });
    mount(t, `/o/${t.org}/capture`, captureRoutes(t, [t.wg]), 'de');
    fireEvent.click(screen.getByTestId('tickets.capture.open'));
    expect(screen.queryByTestId('tickets.capture.wg')).toBeNull();
    expect(screen.getByTestId('tickets.capture')).toHaveTextContent('Task erfassen');
    fireEvent.change(screen.getByTestId('tickets.capture.topic'), { target: { value: '#new' } });
    fireEvent.change(screen.getByTestId('tickets.capture.topic.name'), { target: { value: 'Payroll' } });
    fireEvent.click(screen.getByTestId('tickets.capture.topic.create'));
    expect(await screen.findByTestId('tickets.capture.topic.error')).toHaveAttribute('data-code', 'forbidden');
    fireEvent.change(screen.getByTestId('tickets.capture.topic'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('tickets.capture.title'), { target: { value: 'Title' } });
    fireEvent.change(screen.getByTestId('tickets.capture.github-org'), { target: { value: '-bad' } });
    fireEvent.click(screen.getByTestId('tickets.capture.submit'));
    expect(await screen.findByTestId('tickets.capture.error')).toHaveTextContent('Prüfe die Felder');
    fireEvent.change(screen.getByTestId('tickets.capture.github-org'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('tickets.capture.submit'));
    await waitFor(() =>
      expect(screen.getByTestId('tickets.capture.error')).toHaveAttribute('data-code', 'unavailable'),
    );
    fail = null;
    fireEvent.click(screen.getByTestId('tickets.capture.submit'));
    await waitFor(() => expect(screen.getByTestId('tickets.capture.done')).toBeInTheDocument());
    const [first, second] = t.client.of('POST /v1/tasks');
    expect(second!.body).toBe(first!.body);
    fireEvent.click(screen.getByTestId('tickets.capture.open'));
    expect(screen.queryByTestId('tickets.capture.done')).toBeNull();
    fireEvent.click(screen.getByTestId('tickets.capture.cancel'));
    expect(screen.queryByTestId('tickets.capture')).toBeNull();
  });

  it('is not offered without a writable workgroup', async () => {
    const { t } = await world();
    mount(t, `/o/${t.org}/capture`, captureRoutes(t, []));
    expect(screen.queryByTestId('tickets.capture.open')).toBeNull();
  });
});

describe('ticket filters and rows', () => {
  it('sends status, urgency, person and topic to the hub and filters GitHub fields in the browser', async () => {
    const { t, listed } = await world();
    const rows = [
      taskRow(t, { task_id: 'a', status: 'todo', urgency: 'high', assignee_id: t.me.id, topic_id: TOPIC }),
      taskRow(t, { task_id: 'b', topic_id: 'unknown-topic', assignee_id: '0190f000-0000-7000-8000-0000000000ee' }),
    ];
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, (_body, target) => {
      listed.push(target);
      return { tasks: rows, next_before: null };
    });
    putEvents(t.store, t.wg, [
      event(t, t.wg, 2, { kind: 'task.meta', task: 'a' }, { title: 'Alpha', github_org: 'acme' }),
      event(t, t.wg, 3, { kind: 'task.meta', task: 'b' }, { title: 'Beta' }),
    ]);
    mount(t, `/o/${t.org}/w/${t.wg}`, [{ path: '/o/:org/w/:wg', element: <TaskListScreen /> }]);
    await waitFor(() => expect(screen.getAllByTestId('tasks.row')).toHaveLength(2));
    // newest first: the same activity time, then the higher task ID ("b" before "a")
    const [beta, alpha] = screen.getAllByTestId('tasks.row');
    expect(within(alpha!).getByTestId('tasks.row.status')).toHaveTextContent('To do');
    expect(within(alpha!).getByTestId('tasks.row.urgency')).toHaveTextContent('Urgency: High');
    expect(within(alpha!).getByTestId('tasks.row.person')).toHaveTextContent('Person: Ada');
    expect(within(alpha!).getByTestId('tasks.row.topic')).toHaveTextContent('Topic: Billing');
    expect(within(beta!).getByTestId('tasks.row.status')).toHaveTextContent('In progress');
    expect(within(beta!).getByTestId('tasks.row.topic')).toHaveTextContent('Topic: Topic');
    expect(screen.getByTestId('tickets.capture.open')).toBeInTheDocument();
    for (const [name, value] of [
      ['status', 'todo'],
      ['urgency', 'high'],
      ['assignee', 'none'],
      ['topic', TOPIC],
    ] as const) {
      fireEvent.change(screen.getByTestId(`tickets.filter.${name}`), { target: { value } });
    }
    await waitFor(() =>
      expect(listed.at(-1)).toContain(`status=todo&urgency=high&assignee=none&topic=${TOPIC}&limit=50`),
    );
    fireEvent.click(await screen.findByTestId('tasks.filter.reset'));
    fireEvent.change(screen.getByTestId('tickets.filter.github-org'), { target: { value: 'ACME' } });
    await waitFor(() => expect(screen.getAllByTestId('tasks.row.title').map((n) => n.textContent)).toEqual(['Alpha']));
    fireEvent.change(screen.getByTestId('tickets.filter.github-repo'), { target: { value: 'acme/web' } });
    expect(await screen.findByTestId('tasks.empty')).toHaveTextContent('No task matches the filters.');
    fireEvent.click(screen.getByTestId('tasks.filter.reset'));
    expect(screen.getByTestId('tickets.filter.github-repo')).toHaveValue('');
  });

  it('offers capture on the overview with the writable workgroups and filters there too', async () => {
    const { t, research, listed } = await world();
    mount(t, `/o/${t.org}/tasks`, [{ path: '/o/:org/tasks', element: <OverviewScreen /> }]);
    fireEvent.click(await screen.findByTestId('tickets.capture.open'));
    const options = within(screen.getByTestId('tickets.capture.wg')).getAllByRole('option');
    expect(options.map((node) => node.getAttribute('value'))).toEqual([t.wg, research]);
    fireEvent.change(screen.getByTestId('tickets.filter.urgency'), { target: { value: 'low' } });
    await waitFor(() => expect(listed.some((target) => target.includes('urgency=low'))).toBe(true));
    expect(await screen.findByTestId('tasks.empty')).toHaveTextContent('No task matches the filters.');
    fireEvent.click(screen.getByTestId('tasks.filter.reset'));
    expect(screen.getByTestId('tickets.filter.urgency')).toHaveValue('');
  });
});

describe('the fields panel of the task page', () => {
  function panel(t: TaskWorld, row: TaskRow, writer = true) {
    act(() => putTasks(t.store, [row]));
    const Panel = () => <TaskFields row={t.store.getState().tasks.tasks[row.task_id]!} writer={writer} directory={EMPTY_DIRECTORY} />;
    return mount(t, '/x', [{ path: '/x', element: <Panel /> }]);
  }

  it('shows the fields and edits the sealed and the hub fields', async () => {
    const { t } = await world();
    const row = taskRow(t, { state: 'idle', main_session_id: null, status: 'todo', topic_id: TOPIC }) as TaskRow;
    putEvents(t.store, t.wg, [
      event(t, t.wg, 2, { kind: 'task.meta', task: TASK }, { title: 'Alpha', description: 'Text', github_org: 'acme' }),
    ]);
    t.client
      .on('POST /v1/events', () => ({ results: [{ id: 'm', scope_id: t.wg, scope_seq: 5 }] }))
      .on(`GET /v1/tasks/${TASK}`, () => ({ task: row, sessions: [] }))
      .on(`PUT /v1/tasks/${TASK}/fields`, (body) => ({ task: { ...row, ...(body as object) } }));
    panel(t, row);
    expect(screen.getByTestId('task.fields.person')).toHaveTextContent('Nobody');
    expect(screen.getByTestId('task.fields.topic')).toHaveTextContent('Billing');
    expect(screen.getByTestId('task.fields.github-org')).toHaveTextContent('acme');
    expect(screen.getByTestId('task.fields.github-repo')).toHaveTextContent('–');
    expect(screen.getByTestId('task.fields.description')).toHaveTextContent('Text');
    fireEvent.click(screen.getByTestId('task.fields.edit'));
    expect(screen.getByTestId('task.fields.form.title')).toHaveValue('Alpha');
    // only the hub fields changed: no new task.meta
    fireEvent.change(screen.getByTestId('task.fields.form.urgency'), { target: { value: 'low' } });
    fireEvent.change(screen.getByTestId('task.fields.form.topic'), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('task.fields.save'));
    await waitFor(() => expect(screen.queryByTestId('task.fields.form')).toBeNull());
    expect(t.client.of(`PUT /v1/tasks/${TASK}/fields`)[0]!.body).toEqual({ urgency: 'low', topic_id: null });
    expect(t.client.of('POST /v1/events')).toHaveLength(0);
    // a sealed field changed: a new task.meta
    fireEvent.click(screen.getByTestId('task.fields.edit'));
    fireEvent.change(screen.getByTestId('task.fields.form.title'), { target: { value: 'Beta' } });
    fireEvent.click(screen.getByTestId('task.fields.save'));
    await waitFor(() => expect(t.client.of('POST /v1/events')).toHaveLength(1));
    await waitFor(() => expect(screen.queryByTestId('task.fields.form')).toBeNull());
    // refusals keep the form open; cancel closes it
    fireEvent.click(screen.getByTestId('task.fields.edit'));
    // a title longer than 120 characters (an input field drops line breaks itself)
    fireEvent.change(screen.getByTestId('task.fields.form.title'), { target: { value: 'x'.repeat(121) } });
    fireEvent.click(screen.getByTestId('task.fields.save'));
    expect(await screen.findByTestId('task.fields.error')).toHaveTextContent('Check the fields');
    fireEvent.change(screen.getByTestId('task.fields.form.title'), { target: { value: 'Alpha' } });
    fireEvent.change(screen.getByTestId('task.fields.form.urgency'), { target: { value: 'urgent' } });
    t.client.on(`PUT /v1/tasks/${TASK}/fields`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(screen.getByTestId('task.fields.save'));
    await waitFor(() => expect(screen.getByTestId('task.fields.error')).toHaveAttribute('data-code', 'forbidden'));
    fireEvent.click(screen.getByTestId('task.fields.cancel'));
    expect(screen.queryByTestId('task.fields.form')).toBeNull();
    expect(screen.queryByTestId('task.fields.error')).toBeNull();
  });

  it('changes the status and reports a refused change', async () => {
    const { t } = await world();
    const row = taskRow(t, { state: 'working', status: 'inProgress', assignee_id: t.me.id }) as TaskRow;
    t.client.on(`PUT /v1/tasks/${TASK}/fields`, () => ({
      task: { ...row, status: 'completed', state: 'closed', state_at: '2026-11-21T12:00:01.000Z' },
    }));
    panel(t, row);
    expect(screen.queryByTestId('task.start')).toBeNull();
    expect(screen.getByTestId('task.fields.person')).not.toHaveTextContent('Nobody');
    fireEvent.change(screen.getByTestId('task.fields.status'), { target: { value: 'completed' } });
    await waitFor(() => expect(t.store.getState().tasks.tasks[TASK]!.state).toBe('closed'));
    t.client.on(`PUT /v1/tasks/${TASK}/fields`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.change(screen.getByTestId('task.fields.status'), { target: { value: 'todo' } });
    expect(await screen.findByTestId('task.fields.status.error')).toHaveAttribute('data-code', 'forbidden');
  });

  it('starts the agent of a captured task with an editable first prompt', async () => {
    const { t } = await world();
    const row = taskRow(t, { state: 'idle', main_session_id: null, status: 'todo' }) as TaskRow;
    putEvents(t.store, t.wg, [
      event(t, t.wg, 2, { kind: 'task.meta', task: TASK }, { title: 'Alpha', description: 'Text' }),
    ]);
    let refuse = true;
    t.client
      .on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }))
      .on(`POST /v1/tasks/${TASK}/prompts`, () => {
        if (refuse) throw new HubError('unavailable', 503);
        return { session_id: 's1', scope_seq: 3 };
      })
      .on(`GET /v1/tasks/${TASK}`, () => ({ task: { ...row, main_session_id: 's1' }, sessions: [] }));
    panel(t, row);
    fireEvent.click(screen.getByTestId('task.start.open'));
    expect(screen.getByTestId('task.start.prompt')).toHaveValue('Alpha\n\nText');
    fireEvent.click(screen.getByTestId('task.start.cancel'));
    fireEvent.click(screen.getByTestId('task.start.open'));
    fireEvent.change(screen.getByTestId('task.start.prompt'), { target: { value: 'Do it' } });
    fireEvent.click(screen.getByTestId('task.start.submit'));
    expect(await screen.findByTestId('task.start.error')).toHaveAttribute('data-code', 'unavailable');
    refuse = false;
    fireEvent.click(screen.getByTestId('task.start.submit'));
    await waitFor(() => expect(t.store.getState().tasks.tasks[TASK]!.main_session_id).toBe('s1'));
  });

  it('shows the fields read-only to a reader', async () => {
    const { t } = await world();
    panel(t, taskRow(t, { state: 'idle', main_session_id: null }) as TaskRow, false);
    expect(screen.queryByTestId('task.fields.edit')).toBeNull();
    expect(screen.queryByTestId('task.start.open')).toBeNull();
    expect(screen.getByTestId('task.start')).toHaveTextContent('No agent works on this task yet.');
    expect(screen.getByTestId('task.fields.status')).toBeDisabled();
    expect(screen.getByTestId('task.fields.description')).toHaveTextContent('–');
  });
});
