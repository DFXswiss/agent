// U-WEB-ui-tasks (screens): composer, countdown panel, task list, home and task page in jsdom against the core test
// world (verified org, keys, platform log, scriptable client). The Playwright specs run the same screens against the
// real hub; these tests cover the states that are slow or impossible to produce there.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { setOnline } from '../../src/core/store/slices/connection';
import { putEvents } from '../../src/core/store/slices/events';
import { orgEntry, setLog } from '../../src/core/store/slices/orgs';
import { putLaunch, putSession, putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { CHIPS, Composer } from '../../src/ui/composer/Composer';
import { CountdownPanel } from '../../src/ui/countdown/CountdownPanel';
import { I18nProvider } from '../../src/ui/i18n';
import { HomeScreen } from '../../src/ui/screens/home/HomeScreen';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { relativeLabel } from '../../src/ui/screens/tasks/TaskItem';
import { TaskListScreen } from '../../src/ui/screens/tasks/TaskListScreen';
import { AppEnvProvider } from '../../src/ui/shell/context';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';

afterEach(() => vi.useRealTimers());

function mount(t: TaskWorld, routes: { path: string; element: ReactNode }[], path: string, locale: 'en' | 'de' = 'en') {
  const router = createMemoryRouter([...routes, { path: '*', element: <p data-testid="elsewhere">elsewhere</p> }], {
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

function created(t: TaskWorld, body: unknown) {
  const id = (body as { task_id: string }).task_id;
  return {
    task: taskRow(t, { task_id: id }),
    session: { session_id: 's1', state: 'requested', device_id: 'd', estimate_ms: 3000 },
    events: [],
  };
}

describe('composer', () => {
  it('fills from the chips, shows the GitHub hint and sends with Ctrl+Enter to the task page', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }));
    t.client.on('POST /v1/tasks', (body) => created(t, body));
    const router = mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} suggestions /> }], '/');
    expect(CHIPS).toEqual(['research', 'github', 'explain', 'plan']);
    const prompt = screen.getByTestId('composer.prompt');
    fireEvent.click(screen.getByTestId('composer.chip.github'));
    expect(prompt).toHaveValue('Open a pull request that ');
    expect(within(screen.getByTestId('composer.github-hint')).getByTestId('github.connect')).toBeVisible();
    fireEvent.change(prompt, { target: { value: 'What is 2+2?' } });
    fireEvent.keyDown(prompt, { key: 'Enter' });
    fireEvent.keyDown(prompt, { key: 'a', ctrlKey: true });
    expect(t.client.of('POST /v1/tasks')).toHaveLength(0);
    fireEvent.keyDown(prompt, { key: 'Enter', metaKey: true });
    await waitFor(() => expect(router.state.location.pathname).toMatch(new RegExp(`^/o/${t.org}/t/`)));
    expect(t.client.of('POST /v1/tasks')).toHaveLength(1);
  });

  it('keeps the prepared request after a failure and sends it again unchanged; waits without a device', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [] }));
    t.client.on(`GET /v1/workgroups/${t.wg}/device-candidates`, () => ({ candidates: [] }));
    const router = mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    expect(screen.queryByTestId('composer.chips')).toBeNull();
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Hello' } });
    let posts = 0;
    t.client.on('POST /v1/tasks', (body) => {
      posts += 1;
      if (posts === 1) throw new HubError('unavailable', 503);
      return created(t, body);
    });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(screen.getByTestId('composer.error')).toHaveAttribute('data-code', 'unavailable'));
    expect(screen.queryByTestId('home.device')).toBeNull();
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/t\//));
    const [first, second] = t.client.of('POST /v1/tasks');
    expect(JSON.stringify(first!.body)).toBe(JSON.stringify(second!.body));
    expect(first!.body).not.toHaveProperty('placement');
  });

  it('prepares a new request after a failure when the device choice changed', async () => {
    const t = await taskWorld();
    const d1 = '0190f000-0000-7000-8000-0000000000d1';
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({
      devices: [{ device_id: d1, kind: 'platform', online: true, vms: [] }],
      placement: null,
    }));
    let posts = 0;
    t.client.on('POST /v1/tasks', (body) => {
      posts += 1;
      if (posts === 1) throw new HubError('unavailable', 503);
      return created(t, body);
    });
    const router = mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    await waitFor(() => expect(screen.getByTestId('home.device')).toHaveValue('auto'));
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Hello' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(screen.getByTestId('composer.error')).toHaveAttribute('data-code', 'unavailable'));
    fireEvent.change(screen.getByTestId('home.device'), { target: { value: d1 } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/t\//));
    const [first, second] = t.client.of('POST /v1/tasks') as { body: { task_id: string } }[];
    expect(first!.body).not.toHaveProperty('placement');
    expect(second!.body).toMatchObject({ placement: { device_id: d1 } });
    expect(second!.body.task_id).not.toBe(first!.body.task_id);
    // "Automatic" without a workgroup default: the request takes no default, so nothing is released afterwards
    await act(() => router.navigate('/'));
    await waitFor(() => expect(screen.getByTestId('home.device')).toHaveValue('auto'));
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Again' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(t.client.of('POST /v1/tasks')).toHaveLength(3));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/t\//));
    expect(t.client.of('POST /v1/tasks')[2]!.body).not.toHaveProperty('placement');
    expect(t.client.calls.filter((call) => call.method === 'PUT')).toHaveLength(0);
  });

  it('chooses device and VM, preselects the workgroup default and applies "Automatic" over a default', async () => {
    const t = await taskWorld();
    const d1 = '0190f000-0000-7000-8000-0000000000d1';
    const d2 = '0190f000-0000-7000-8000-0000000000d2';
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({
      devices: [
        {
          device_id: d1,
          kind: 'platform',
          online: true,
          capacity: {
            vm_memory_budget_mib: 1,
            vm_memory_used_mib: 0,
            vm_slots_total: 1,
            vm_slots_free: 1,
            pool_ready: 0,
            sessions_per_vm: 16,
          },
          vms: [{ vm_id: 'vm-1a2b3c4d', state: 'running', sessions: 3 }],
        },
        { device_id: d2, kind: 'org', online: false, vms: [{ vm_id: 'vm-5e6f7a8b', state: 'running', sessions: 1 }] },
      ],
      placement: { device_id: d1, vm_id: null },
    }));
    t.client.on('POST /v1/tasks', (body) => created(t, body));
    const puts = () => t.client.calls.filter((call) => call.method === 'PUT');
    const router = mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    await waitFor(() => expect(screen.getByTestId('home.device')).toHaveValue(d1));
    const device = screen.getByTestId('home.device');
    expect(device).toHaveTextContent('Automatic');
    expect(device).toHaveTextContent('Device without a name (offline)');
    expect(screen.getByTestId('home.vm')).toHaveValue('any');
    expect(screen.getByTestId('home.vm')).toHaveTextContent('vm-1a2b3c4d · 3 of 16 sessions');
    fireEvent.change(device, { target: { value: d2 } });
    expect(screen.getByTestId('home.vm')).toHaveTextContent('vm-5e6f7a8b · 1 sessions');
    fireEvent.change(screen.getByTestId('home.vm'), { target: { value: 'vm-5e6f7a8b' } });
    fireEvent.change(screen.getByTestId('home.vm'), { target: { value: 'any' } });
    fireEvent.change(screen.getByTestId('home.vm'), { target: { value: 'vm-5e6f7a8b' } });
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Hello' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/t\//));
    expect(t.client.of('POST /v1/tasks')[0]!.body).toMatchObject({
      placement: { device_id: d2, vm_id: 'vm-5e6f7a8b' },
    });
    expect(puts()).toHaveLength(0);
    await act(() => router.navigate('/'));
    await waitFor(() => expect(screen.getByTestId('home.device')).toHaveValue(d1));
    fireEvent.change(screen.getByTestId('home.device'), { target: { value: 'auto' } });
    expect(screen.queryByTestId('home.vm')).toBeNull();
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Again' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(puts()).toHaveLength(1));
    const second = t.client.of('POST /v1/tasks')[1]!.body as { task_id: string };
    expect(second).not.toHaveProperty('placement');
    expect(puts()[0]).toMatchObject({
      target: `/v1/tasks/${second.task_id}/placement`,
      body: { device_id: null, vm_id: null },
    });
  });

  it('prepares a new request when the prompt changed, shows the countdown while sending, says Offline offline', async () => {
    const t = await taskWorld();
    let release: () => void = () => undefined;
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [] }));
    t.client.on(
      `GET /v1/workgroups/${t.wg}/device-candidates`,
      () => new Promise((resolve) => (release = () => resolve({ candidates: [] }))),
    );
    mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'One' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    expect(await screen.findByTestId('countdown.step.assigning')).toHaveAttribute('data-status', 'current');
    expect(screen.getByTestId('countdown.step.requested')).toHaveAttribute('data-status', 'pending');
    expect(screen.getByTestId('composer.send')).toBeDisabled();
    act(() => release());
    await screen.findByTestId('composer.error');
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => new Promise(() => undefined));
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Two' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    expect(await screen.findByTestId('countdown.step.requested')).toHaveAttribute('data-status', 'current');
    expect(screen.queryByTestId('countdown.step.assigning')).toBeNull();
    const prepared = t.client.calls.length;
    expect(prepared).toBeGreaterThan(0);
    act(() => setOnline(t.store, false));
    expect(screen.getByTestId('composer.send')).toHaveTextContent('Offline');
  });

  it('prepares a new request after a refusal a retry cannot fix', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}`, () => ({ devices: [{}] }));
    let posts = 0;
    t.client.on('POST /v1/tasks', (body) => {
      posts += 1;
      if (posts === 1) throw new HubError('epoch_stale', 400);
      return created(t, body);
    });
    const router = mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    fireEvent.change(screen.getByTestId('composer.prompt'), { target: { value: 'Hello' } });
    fireEvent.click(screen.getByTestId('composer.send'));
    await screen.findByTestId('composer.error');
    fireEvent.click(screen.getByTestId('composer.send'));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/\/t\//));
    const [first, second] = t.client.of('POST /v1/tasks');
    expect((first!.body as { task_id: string }).task_id).not.toBe((second!.body as { task_id: string }).task_id);
  });

  it('does not send a blank prompt', async () => {
    const t = await taskWorld();
    mount(t, [{ path: '/', element: <Composer org={t.org} wg={t.wg} /> }], '/');
    fireEvent.submit(screen.getByTestId('composer'));
    fireEvent.keyDown(screen.getByTestId('composer.prompt'), { key: 'Enter', ctrlKey: true });
    expect(t.client.of('POST /v1/tasks')).toHaveLength(0);
  });
});

describe('countdown panel', () => {
  const T = (t: TaskWorld) => ({ org: t.org, wg: t.wg, me: t.me });

  async function panel(
    setup: (t: TaskWorld, task: TaskRow) => void,
    taskOverrides: Record<string, unknown> = {},
    onRetry?: () => Promise<void>,
    locale: 'en' | 'de' = 'en',
  ) {
    const t = await taskWorld();
    const task = taskRow(T(t), { created_at: new Date().toISOString(), ...taskOverrides }) as TaskRow;
    setup(t, task);
    mount(t, [{ path: '/', element: <CountdownPanel task={task} {...(onRetry && { onRetry })} /> }], '/', locale);
    return t;
  }

  it('counts down with digits and ring, the assignment step and the steps reached', async () => {
    // the clock stands still: the expected digits must not depend on how long the setup takes on a loaded device
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['Date'] });
    await panel(
      (t, task) => {
        putLaunch(t.store, task.task_id, { sessionId: 's9', localAt: Date.now(), estimateMs: 8000, assignedMs: 1234 });
      },
      { main_session_id: null },
    );
    expect(screen.getByTestId('countdown.panel')).toHaveAttribute('data-phase', 'counting');
    expect(screen.getByTestId('countdown.digits')).toHaveTextContent(/^[78]$/);
    expect(screen.getByTestId('countdown.ring')).toBeInTheDocument();
    expect(screen.getByTestId('countdown.step.assigning')).toHaveTextContent('+1.2 s');
    expect(screen.getByTestId('countdown.step.requested')).toHaveAttribute('data-status', 'current');
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('About 10 seconds left: Task accepted');
    expect(document.querySelector('.animate-spin')).toBeNull();
  });

  it('shows "Delayed" with the current step and the elapsed time, in German with a decimal comma', async () => {
    // the clock stands still: the expected digits must not depend on how long the setup takes on a loaded device
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['Date'] });
    await panel(
      (t, task) => {
        putSession(
          t.store,
          sessionRow(T(t), {
            task_id: task.task_id,
            created_at: task.created_at,
            estimate_ms: 0,
            state: 'vm_ready',
          }) as never,
          [{ seq: 1, state: 'vm_ready', reason: null, at: task.created_at, received_at: task.created_at }],
          0,
        );
      },
      { created_at: new Date(Date.now() - 12_400).toISOString() },
      undefined,
      'de',
    );
    expect(screen.getByTestId('countdown.delayed')).toHaveTextContent('Verzögert');
    expect(screen.getByTestId('countdown.elapsed')).toHaveTextContent(/^Sandbox bereit – 12,\d s$/);
    expect(screen.getByTestId('countdown.step.vm-ready').querySelector('time')).toHaveTextContent(
      /^\d\d:\d\d:\d\d[.,]\d{3}$/,
    );
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('Verzögert, 10 Sekunden: Sandbox bereit');
  });

  it('names the reason of a waiting task, a generic text without one', async () => {
    await panel(
      (t, task) =>
        putSession(
          t.store,
          sessionRow(T(t), {
            task_id: task.task_id,
            created_at: task.created_at,
            estimate_ms: null,
            device_id: null,
          }) as never,
          [],
          0,
        ),
      { state: 'scheduling', state_reason: 'capacity_exhausted' },
    );
    expect(screen.getByTestId('countdown.reason')).toHaveAttribute('data-reason', 'capacity_exhausted');
    expect(screen.getByTestId('countdown.elapsed')).toHaveTextContent(/^Waiting for \d\.\d s$/);
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('Waiting for 0 seconds');
    document.body.innerHTML = '';
    await panel(() => undefined, { main_session_id: null });
    expect(screen.getByTestId('countdown.reason')).toHaveAttribute('data-reason', 'unknown');
    expect(screen.getByTestId('countdown.reason')).toHaveTextContent('Waiting for an execution device.');
  });

  it('names every waiting reason and the step the user can take (ADR-075)', async () => {
    const waiting = (reason: string, extra: Record<string, unknown> = {}) => ({
      state: 'scheduling',
      state_reason: reason,
      main_session_id: null,
      ...extra,
    });
    const texts: [string, string][] = [
      ['capacity_exhausted', 'All execution devices are full'],
      ['wg_vm_limit', 'as many VMs as allowed'],
      ['device_offline', 'The execution device is offline'],
      ['keys_pending', 'still receiving the keys'],
      ['platform_access_required', 'the operator has been notified'],
    ];
    for (const [reason, text] of texts) {
      document.body.innerHTML = '';
      await panel(() => undefined, waiting(reason));
      expect(screen.getByTestId('countdown.reason')).toHaveTextContent(text);
      expect(screen.queryByRole('button')).toBeNull();
    }
    // no device: admins assign one (a refusal is shown), everyone else learns who can
    document.body.innerHTML = '';
    const t = await panel((world) => {
      world.client.on(`GET /v1/workgroups/${world.wg}/device-candidates`, () => ({ candidates: [] }));
    }, waiting('no_device'));
    expect(screen.getByTestId('countdown.reason')).toHaveTextContent('No execution device is assigned');
    fireEvent.click(screen.getByTestId('countdown.assign-device'));
    await waitFor(() =>
      expect(screen.getByTestId('countdown.action-error')).toHaveAttribute('data-code', 'no_device_available'),
    );
    expect(t.client.of(`GET /v1/workgroups/${t.wg}/device-candidates`)).toHaveLength(1);
    document.body.innerHTML = '';
    await panel((world) => world.store.update('me', (me) => ({ ...me, userId: 'reader' })), waiting('no_device'));
    expect(screen.getByTestId('countdown.ask-admin')).toHaveTextContent('An admin of the organization can assign one.');
    document.body.innerHTML = '';
    await panel((world) => world.store.update('me', (me) => ({ ...me, userId: null })), waiting('no_device'));
    expect(screen.queryByTestId('countdown.ask-admin')).toBeNull();
  });

  it('names the chosen device that is full or offline and chooses automatically (ADR-075)', async () => {
    const device = '0190f000-0000-7000-8000-0000000000d1';
    const placement = { device_id: device, vm_id: null };
    let calls = 0;
    const t = await panel(
      (world, task) =>
        world.client.on(`PUT /v1/tasks/${task.task_id}/placement`, () => {
          calls += 1;
          if (calls === 1) throw new HubError('unavailable', 503);
          return { task: { ...task, placement: null } };
        }),
      { state: 'scheduling', state_reason: 'placement_offline', main_session_id: null, placement },
    );
    expect(screen.getByTestId('countdown.reason')).toHaveTextContent(
      'The chosen device Device without a name is offline',
    );
    fireEvent.click(screen.getByTestId('countdown.choose-automatic'));
    await waitFor(() =>
      expect(screen.getByTestId('countdown.action-error')).toHaveAttribute('data-code', 'unavailable'),
    );
    fireEvent.click(screen.getByTestId('countdown.choose-automatic'));
    await waitFor(() => expect(screen.queryByTestId('countdown.action-error')).toBeNull());
    expect(t.client.of(`PUT /v1/tasks/0190f000-0000-7000-8000-000000000001/placement`)[1]!.body).toEqual({
      device_id: null,
      vm_id: null,
    });
    document.body.innerHTML = '';
    await panel(() => undefined, { state: 'scheduling', state_reason: 'placement_full', main_session_id: null });
    expect(screen.getByTestId('countdown.reason')).toHaveTextContent('The chosen device device is full');
    document.body.innerHTML = '';
    await panel((world) => world.store.update('me', (me) => ({ ...me, userId: 'reader' })), {
      state: 'scheduling',
      state_reason: 'placement_full',
      main_session_id: null,
      placement,
    });
    expect(screen.queryByTestId('countdown.choose-automatic')).toBeNull();
  });

  it('collapses into "Ready in" once working is reached', async () => {
    await panel((t, task) => {
      const at = new Date(Date.parse(task.created_at) + 4200).toISOString();
      putSession(
        t.store,
        sessionRow(T(t), {
          task_id: task.task_id,
          created_at: task.created_at,
          state: 'working',
          first_working_at: at,
        }) as never,
        [],
        0,
      );
    });
    expect(screen.getByTestId('countdown.panel')).toHaveAttribute('data-phase', 'ready');
    expect(screen.getByTestId('countdown.summary')).toHaveTextContent('Ready in 4.2 s');
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('Ready in 4.2 s');
  });

  it('shows the catalog error of a failed start; Retry reports its own failure', async () => {
    let attempts = 0;
    const retry = vi.fn(async () => {
      attempts += 1;
      if (attempts === 1) throw new HubError('rate_limited', 429);
      if (attempts === 2) throw new Error('plain');
    });
    await panel(
      (t, task) =>
        putSession(
          t.store,
          sessionRow(T(t), {
            task_id: task.task_id,
            created_at: task.created_at,
            state: 'failed',
            state_reason: 'oom',
          }) as never,
          [],
          0,
        ),
      {},
      retry,
    );
    expect(screen.getByTestId('countdown.failed')).toHaveAttribute('data-reason', 'oom');
    expect(screen.getByTestId('countdown.announce')).toHaveTextContent('The session could not start.');
    fireEvent.click(screen.getByTestId('countdown.retry'));
    expect(await screen.findByTestId('countdown.retry-error')).toHaveTextContent(
      createI18n('en').error('rate_limited').title,
    );
    fireEvent.click(screen.getByTestId('countdown.retry'));
    await waitFor(() =>
      expect(screen.getByTestId('countdown.retry-error')).toHaveTextContent(
        createI18n('en').error('unavailable').title,
      ),
    );
    fireEvent.click(screen.getByTestId('countdown.retry'));
    await waitFor(() => expect(screen.queryByTestId('countdown.retry-error')).toBeNull());
    expect(retry).toHaveBeenCalledTimes(3);
  });

  it('offers no Retry without a handler and falls back to a generic error without a reason', async () => {
    await panel((t, task) =>
      putSession(
        t.store,
        sessionRow(T(t), { task_id: task.task_id, created_at: task.created_at, state: 'failed' }) as never,
        [],
        0,
      ),
    );
    expect(screen.getByTestId('countdown.failed')).toHaveAttribute('data-reason', '');
    expect(screen.getByTestId('countdown.failed')).toHaveTextContent(createI18n('en').error('unavailable').title);
    expect(screen.queryByTestId('countdown.retry')).toBeNull();
  });

  it('follows the clock while counting', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: false, toFake: ['setInterval', 'clearInterval', 'Date'] });
    await panel(
      (t, task) =>
        putLaunch(t.store, task.task_id, { sessionId: 'x', localAt: Date.now(), estimateMs: 5000, assignedMs: null }),
      {
        main_session_id: null,
      },
    );
    expect(screen.getByTestId('countdown.digits')).toHaveTextContent('5');
    act(() => void vi.advanceTimersByTime(2600));
    expect(screen.getByTestId('countdown.digits')).toHaveTextContent('2.4');
  });
});

describe('task list and home', () => {
  const rows = (t: TaskWorld, n: number, extra: Record<string, unknown> = {}) =>
    Array.from({ length: n }, (_, i) =>
      taskRow(t, { task_id: `0190f000-0000-7000-8000-0000000001${String(i).padStart(2, '0')}`, ...extra }),
    );

  it('shows rows with titles, owner and relative activity, pages with "Load more", reloads after an error', async () => {
    const t = await taskWorld();
    const base = `GET /v1/workgroups/${t.wg}/tasks`;
    let fail = true;
    t.client.on(base, (_b, target) => {
      if (fail) throw new HubError('unavailable', 503);
      return target.includes('before=')
        ? {
            tasks: rows(t, 1, {
              task_id: 'z',
              state: 'mystery',
              owner_id: 'stranger',
              created_at: '2026-11-20T12:00:00.000Z',
            }),
            next_before: null,
          }
        : { tasks: rows(t, 2, { state: 'working', owner_id: t.me.id }), next_before: 'c' };
    });
    t.client.on(`GET /v1/scopes/${t.org}/events`, () => ({ events: [], next_after: null, head: 0 }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    putEvents(t.store, t.org, [
      {
        org: t.org,
        scopeId: t.org,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'member.profile', ref: t.me.id, author: t.me.id, id: 'm1' },
        body: { display_name: 'Ada' },
      },
      {
        org: t.org,
        scopeId: t.org,
        scopeSeq: 2,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'wg.profile', wg: t.wg, author: t.me.id, id: 'w1' },
        body: { name: 'Default' },
      },
    ]);
    putEvents(t.store, t.wg, [
      {
        org: t.org,
        scopeId: t.wg,
        scopeSeq: 1,
        receivedAt: null,
        event: { header: { id: 'bad' } },
        status: 'unverifiable',
        code: 'signature_invalid',
        header: null,
        body: null,
      },
      {
        // the signed author of the second task's task.meta is the owner, whatever the hub's owner_id says
        org: t.org,
        scopeId: t.wg,
        scopeSeq: 2,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: {
          kind: 'task.meta',
          id: 'meta-2',
          task: '0190f000-0000-7000-8000-000000000101',
          author: t.me.id,
          ts: '2026-11-21T12:00:00.000Z',
        },
        body: { title: 'Second' },
      },
    ]);
    mount(t, [{ path: '/o/:org/w/:wg', element: <TaskListScreen /> }], `/o/${t.org}/w/${t.wg}`);
    expect(await screen.findByTestId('tasks.error')).toHaveTextContent(createI18n('en').error('unavailable').title);
    fail = false;
    fireEvent.click(screen.getByText('Try again'));
    await waitFor(() => expect(screen.getAllByTestId('tasks.row')).toHaveLength(2));
    expect(screen.getByTestId('tasks.title')).toHaveTextContent('Default');
    // the hub names Ada as owner of both: only the task with her signed task.meta shows her, the other has none yet
    expect(screen.getAllByTestId('tasks.row.owner')[0]).toHaveTextContent('Ada');
    expect(screen.getAllByTestId('tasks.row.owner')[1]).toHaveTextContent('Member');
    expect(screen.getAllByTestId('tasks.row.title')[1]).toHaveAttribute('data-status', 'missing');
    fireEvent.click(screen.getByTestId('tasks.more'));
    await waitFor(() => expect(screen.getAllByTestId('tasks.row')).toHaveLength(3));
    const last = screen.getAllByTestId('tasks.row')[2]!;
    expect(last.querySelector('[data-testid="tasks.row.state"]')).toHaveTextContent('tasks.state.mystery');
    expect(last.querySelector('[data-testid="tasks.row.owner"]')).toHaveTextContent('Member');
    expect(screen.queryByTestId('tasks.more')).toBeNull();
    act(() =>
      putTasks(t.store, [
        taskRow(t, { task_id: 'u', title_event_id: 'bad', created_at: '2026-11-22T00:00:00.000Z' }) as TaskRow,
      ]),
    );
    expect(screen.getAllByTestId('tasks.row.title')[0]).toHaveTextContent('Unverifiable title (signature_invalid)');
    fireEvent.change(screen.getByTestId('tasks.filter'), { target: { value: 'failed' } });
    await waitFor(() => expect(t.client.calls.some((call) => call.target.includes('state=failed'))).toBe(true));
  });

  it('formats relative activity labels', () => {
    expect(relativeLabel('en', 3 * 86_400_000)).toBe('3 days ago');
    expect(relativeLabel('en', 2 * 3_600_000)).toBe('2 hr. ago');
    expect(relativeLabel('en', 5 * 60_000)).toBe('5 min. ago');
    expect(relativeLabel('en', 12_000)).toBe('12 sec. ago');
    expect(relativeLabel('en', -5)).toBe('now');
  });

  it('waits for the verified log and hides unknown or unreadable workgroups', async () => {
    const t = await taskWorld();
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, log: null } },
    }));
    mount(t, [{ path: '/o/:org/w/:wg', element: <TaskListScreen /> }], `/o/${t.org}/w/${t.wg}`);
    expect(screen.getByTestId('tasks.loading')).toBeInTheDocument();
    act(() => setLog(t.store, t.org, t.world.log!));
    expect(screen.getByTestId('tasks.screen')).toBeInTheDocument();
    act(() => t.store.update('me', (me) => ({ ...me, userId: null })));
    expect(screen.getByTestId('tasks.not-found')).toBeInTheDocument();
  });

  it('home: loading, unnamed org, the workgroup choice and the read-only case', async () => {
    const t = await taskWorld();
    t.client.on(`GET /v1/workgroups/${t.wg}/tasks`, () => ({ tasks: [], next_before: null }));
    const log = orgEntry(t.store.getState().orgs, t.org).log!;
    t.store.update('orgs', (orgs) => ({
      ...orgs,
      byId: { ...orgs.byId, [t.org]: { ...orgs.byId[t.org]!, log: null } },
    }));
    mount(t, [{ path: '/o/:org', element: <HomeScreen /> }], `/o/${t.org}`);
    expect(screen.getByTestId('home.loading')).toBeInTheDocument();
    await t.world.batch(t.ada, [{ type: 'wg_create', wg: '0190f000-0000-7000-8000-00000000000a', epoch: 1 }]);
    act(() => setLog(t.store, t.org, t.world.log!));
    expect(screen.getByTestId('home.org')).toHaveTextContent('Organization');
    const select = screen.getByTestId('home.wg');
    expect([...select.querySelectorAll('option')].map((o) => o.textContent)).toEqual([
      'Default workgroup',
      'Workgroup',
    ]);
    fireEvent.change(select, { target: { value: '0190f000-0000-7000-8000-00000000000a' } });
    expect(select).toHaveValue('0190f000-0000-7000-8000-00000000000a');
    expect(screen.getByTestId('home.all-tasks')).toHaveAttribute('href', `/o/${t.org}/w/${t.wg}`);
    act(() => setLog(t.store, t.org, log));
    expect(select).toHaveValue(t.wg);
    act(() => t.store.update('me', (me) => ({ ...me, userId: 'someone-else' })));
    expect(screen.getByTestId('home.read-only')).toBeInTheDocument();
    expect(screen.queryByTestId('composer')).toBeNull();
    expect(screen.queryByTestId('home.wg')).toBeNull();
  });
});

describe('task page', () => {
  it('loads the task, shows title, prompt and countdown, and retries a failed start', async () => {
    const t = await taskWorld();
    const row = taskRow(t, { state: 'failed', state_reason: 'oom' });
    const session = sessionRow(t, { state: 'failed', state_reason: 'oom' });
    t.client.on(`GET /v1/tasks/${row.task_id}`, () => ({
      task: row,
      sessions: [{ session, usage: null, transitions: [] }],
      transitions: [],
    }));
    t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
    t.client.on(`POST /v1/tasks/${row.task_id}/prompts`, () => ({ scope_seq: 3, session_id: 's2' }));
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${row.task_id}`);
    expect(screen.getByTestId('task.loading')).toHaveTextContent('Loading the task…');
    expect(await screen.findByTestId('task.title')).toHaveTextContent('Loading title …');
    expect(screen.queryByTestId('chat.prompt')).toBeNull();
    act(() =>
      putEvents(t.store, t.wg, [
        {
          org: t.org,
          scopeId: t.wg,
          scopeSeq: 1,
          receivedAt: null,
          event: {},
          status: 'verified',
          code: null,
          header: { kind: 'task.meta', task: row.task_id, id: 'm' },
          body: { title: 'Hello' },
        },
        {
          org: t.org,
          scopeId: t.wg,
          scopeSeq: 2,
          receivedAt: null,
          event: {},
          status: 'verified',
          code: null,
          header: { kind: 'prompt', task: row.task_id, id: 'p' },
          body: { text: 'Hello there', attachments: [] },
        },
        {
          org: t.org,
          scopeId: t.wg,
          scopeSeq: 3,
          receivedAt: null,
          event: {},
          status: 'verified',
          code: null,
          header: { kind: 'prompt', task: row.task_id, id: 'p2' },
          body: { text: 3 },
        },
      ]),
    );
    expect(screen.getByTestId('task.title')).toHaveTextContent('Hello');
    expect(screen.getAllByTestId('chat.prompt.text')[0]).toHaveTextContent('Hello there');
    fireEvent.click(screen.getByTestId('countdown.retry'));
    await waitFor(() => expect(t.client.of(`POST /v1/tasks/${row.task_id}/prompts`)).toHaveLength(1));
  });

  it('names device and VM of the main session in the header and the sessions tab (ADR-075)', async () => {
    const t = await taskWorld();
    const row = taskRow(t, { state: 'working' });
    putTasks(t.store, [{ ...row, main_session_id: '0190f000-0000-7000-8000-0000000000a1' } as TaskRow]);
    putSession(t.store, sessionRow(t, { state: 'working', vm_id: 'vm-1a2b3c4d' }) as never, [], 0);
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${row.task_id}?tab=sessions`);
    const placement = await screen.findByTestId('task.placement');
    expect(placement).toHaveAttribute('data-device', '0190f000-0000-7000-8000-0000000000d1');
    expect(placement).toHaveAttribute('data-vm', 'vm-1a2b3c4d');
    expect(placement).toHaveTextContent('On Device without a name · vm-1a2b3c4d');
    expect(await screen.findByTestId('session.device')).toHaveTextContent('On Device without a name · vm-1a2b3c4d');
    act(() => putSession(t.store, sessionRow(t, { state: 'working', vm_id: null }) as never, [], 0));
    expect(screen.getByTestId('task.placement')).toHaveAttribute('data-vm', '');
    act(() => putSession(t.store, sessionRow(t, { state: 'requested', device_id: null }) as never, [], 0));
    expect(screen.queryByTestId('task.placement')).toBeNull();
  });

  it('offers Retry only to users who may write in the workgroup', async () => {
    const t = await taskWorld();
    const row = taskRow(t, { state: 'failed', state_reason: 'oom' });
    putTasks(t.store, [row as TaskRow]);
    putSession(t.store, sessionRow(t, { state: 'failed', state_reason: 'oom' }) as never, [], 0);
    t.store.update('me', (me) => ({ ...me, userId: 'reader' }));
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${row.task_id}`);
    expect(screen.getByTestId('countdown.failed')).toBeInTheDocument();
    expect(screen.queryByTestId('countdown.retry')).toBeNull();
  });

  it('shows the catalog error of a task that cannot be loaded', async () => {
    const t = await taskWorld();
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/x`);
    expect(await screen.findByTestId('task.error')).toHaveTextContent('Not available');
  });

  it('tells how the main session took over the context after a resume (KOR-33)', async () => {
    const t = await taskWorld();
    const row = taskRow(t);
    putTasks(t.store, [row as TaskRow]);
    const path = `/o/${t.org}/t/${row.task_id}`;
    const route = [{ path: '/o/:org/t/:task', element: <TaskScreen /> }];
    // an older hub sends neither field: nothing is shown
    putSession(t.store, sessionRow(t, { state: 'working' }) as never, [], 0);
    mount(t, route, path);
    expect(screen.queryByTestId('task.context.fallback')).toBeNull();
    expect(screen.queryByTestId('task.context.restored')).toBeNull();
    act(() => putSession(t.store, sessionRow(t, { state: 'working', context_restore: 'restored' }) as never, [], 0));
    expect(screen.getByTestId('task.context.restored')).toHaveTextContent(
      'Continued with the full context of the original session',
    );
    expect(screen.getByTestId('task.context.restored')).toHaveAttribute('data-workspace', 'full');
    // the conversation is complete, the workspace only partly or not at all (review 1 decision 5)
    for (const [reason, text] of [
      ['workspace_partial', 'Continued with the full conversation; the workspace was only partly restored.'],
      ['workspace_omitted', 'Continued with the full conversation; the workspace was too large and was not restored.'],
    ] as const) {
      act(() =>
        putSession(
          t.store,
          sessionRow(t, { state: 'working', context_restore: 'restored', context_reason: reason }) as never,
          [],
          0,
        ),
      );
      expect(screen.getByTestId('task.context.restored')).toHaveTextContent(text);
      expect(screen.getByTestId('task.context.restored')).toHaveAttribute(
        'data-workspace',
        reason.replace('workspace_', ''),
      );
    }
    act(() =>
      putSession(
        t.store,
        sessionRow(t, { state: 'working', context_restore: 'fallback', context_reason: 'context_missing' }) as never,
        [],
        0,
      ),
    );
    const notice = screen.getByTestId('task.context.fallback');
    expect(notice).toHaveAttribute('role', 'status');
    expect(notice).toHaveAttribute('data-reason', 'context_missing');
    expect(notice).toHaveTextContent(
      'This task was continued by a new AI because no saved context existed for the earlier session.',
    );
    expect(screen.queryByTestId('task.context.restored')).toBeNull();
    act(() =>
      putSession(
        t.store,
        sessionRow(t, { state: 'working', context_restore: 'fallback', context_reason: 'later_code' }) as never,
        [],
        0,
      ),
    );
    expect(screen.getByTestId('task.context.fallback')).toHaveAttribute('data-reason', 'unknown');
  });

  it('shows the resume notice in German and nothing for a task without a main session', async () => {
    const t = await taskWorld();
    const row = taskRow(t);
    putTasks(t.store, [row as TaskRow]);
    putSession(
      t.store,
      sessionRow(t, { state: 'working', context_restore: 'fallback', context_reason: 'restore_failed' }) as never,
      [],
      0,
    );
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${row.task_id}`, 'de');
    expect(screen.getByTestId('task.context.fallback')).toHaveTextContent(
      'Diese Aufgabe wurde mit einer neuen KI fortgesetzt, weil die Wiederherstellung auf der VM fehlschlug.',
    );
    for (const [reason, text] of [
      [
        'workspace_partial',
        'Fortgesetzt mit dem vollständigen Gesprächsverlauf; der Arbeitsstand wurde nur teilweise wiederhergestellt.',
      ],
      [
        'workspace_omitted',
        'Fortgesetzt mit dem vollständigen Gesprächsverlauf; der Arbeitsstand war zu groß und wurde nicht wiederhergestellt.',
      ],
    ] as const) {
      act(() =>
        putSession(
          t.store,
          sessionRow(t, { state: 'working', context_restore: 'restored', context_reason: reason }) as never,
          [],
          0,
        ),
      );
      expect(screen.getByTestId('task.context.restored')).toHaveTextContent(text);
    }
    act(() => putTasks(t.store, [{ ...(row as TaskRow), main_session_id: null }]));
    expect(screen.queryByTestId('task.context.fallback')).toBeNull();
    expect(screen.queryByTestId('task.context.restored')).toBeNull();
  });

  it('shows a prompt that is not text as an empty bubble', async () => {
    const t = await taskWorld();
    const row = taskRow(t);
    putTasks(t.store, [row as TaskRow]);
    putEvents(t.store, t.wg, [
      {
        org: t.org,
        scopeId: t.wg,
        scopeSeq: 1,
        receivedAt: null,
        event: {},
        status: 'verified',
        code: null,
        header: { kind: 'prompt', task: row.task_id, id: 'p' },
        body: { text: 3 },
      },
    ]);
    mount(t, [{ path: '/o/:org/t/:task', element: <TaskScreen /> }], `/o/${t.org}/t/${row.task_id}`);
    expect(screen.getByTestId('task.screen')).toBeInTheDocument();
    expect(screen.getByTestId('chat.prompt.text')).toHaveTextContent('');
  });
});
