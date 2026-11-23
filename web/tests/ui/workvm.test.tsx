// U-WEB-ui-workvm: the work VM runtime of the session header (`WorkvmRuntime`, sums over the session's and the task's
// work VMs, ticking while one is created) and every branch of the chat items `workvm.run` and `workvm.state`, rendered
// on their own, and the session's work VM panel (`WorkvmPanel`: none, starting with countdown, running, deleted, runs).
// The chat timeline with these items is covered by `chat.test.tsx`.
import { act, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { putEvents, type EventEntry } from '../../src/core/store/slices/events';
import { type OperationRow, putOperations } from '../../src/core/store/slices/operations';
import { chatItem as runItem } from '../../src/ui/chat-items/workvm.run';
import { chatItem as stateItem } from '../../src/ui/chat-items/workvm.state';
import { I18nProvider } from '../../src/ui/i18n';
import { WorkvmPanel } from '../../src/ui/screens/session/WorkvmPanel';
import { WorkvmRuntime } from '../../src/ui/screens/session/WorkvmRuntime';
import { type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';

afterEach(() => vi.useRealTimers());

const OTHER = '0190f000-0000-7000-8000-0000000000b2';
const START = Date.parse('2026-11-21T12:00:00.000Z');

const vm = (overrides: Record<string, unknown> = {}) => ({
  workvm_id: 'wvm-0123abcd',
  state: 'created',
  scope: 'session',
  size: 'standard',
  cpus: 4,
  memory_mib: 8192,
  disk_gib: 50,
  implicit: false,
  reason: null,
  started_at: '2026-11-21T12:00:00.000Z',
  ended_at: null,
  runtime_ms: 0,
  ...overrides,
});

const job = (overrides: Record<string, unknown> = {}) => ({
  workvm_id: 'wvm-0123abcd',
  job_id: 'j-0123456789ab',
  kind: 'exec',
  command: 'make test',
  state: 'running',
  exit_code: null,
  started_at: '2026-11-21T12:00:01.000Z',
  ended_at: null,
  duration_ms: 0,
  output: 'line 1\n',
  output_bytes: 7,
  ...overrides,
});

async function header() {
  const t = await taskWorld();
  mount(t, [{ path: '/s', element: <WorkvmRuntime wg={t.wg} task={TASK} session={MAIN} /> }], '/s');
  return t;
}

const put = (t: TaskWorld, ...entries: EventEntry[]) => act(() => putEvents(t.store, t.wg, entries));

describe('work VM runtime in the session header', () => {
  it('shows nothing while the task had no work VM', async () => {
    const t = await header();
    expect(screen.queryByTestId('session.workvm-runtime')).toBeNull();
    // other kinds, other tasks, unverified entries and invalid bodies do not count
    const ofOtherTask = verified(t, 'workvm.state', vm(), { session: MAIN });
    const unverified = verified(t, 'workvm.state', vm(), { session: MAIN });
    put(
      t,
      verified(t, 'prompt', { text: 'hi' }, { session: MAIN }),
      verified(t, 'workvm.state', 'odd', { session: MAIN }),
      { ...ofOtherTask, header: { ...ofOtherTask.header!, task: 'other' } },
      { ...unverified, status: 'unverifiable', code: 'signature_invalid' } as EventEntry,
    );
    expect(screen.queryByTestId('session.workvm-runtime')).toBeNull();
  });

  it('sums the deleted VMs of the session and of the whole task', async () => {
    const t = await header();
    put(
      t,
      // a VM of this session: created, then deleted after 1 h 2 min 3 s
      verified(t, 'workvm.state', vm(), { session: MAIN }),
      verified(t, 'workvm.state', vm({ state: 'deleted', reason: 'released', runtime_ms: 3_723_000 }), {
        session: MAIN,
      }),
      // a VM of another session of the task: 5 min
      verified(t, 'workvm.state', vm({ workvm_id: 'wvm-0000000b', scope: 'task', implicit: true }), {
        session: OTHER,
      }),
      verified(
        t,
        'workvm.state',
        vm({ workvm_id: 'wvm-0000000b', state: 'deleted', reason: 'idle', runtime_ms: 300_000 }),
        { session: OTHER },
      ),
    );
    const runtime = screen.getByTestId('session.workvm-runtime');
    expect(runtime).toHaveAttribute('data-running', 'false');
    expect(runtime.textContent).toBe('Work VM: 1 h 2 min 3 s in this session, 1 h 7 min 3 s in the task');
  });

  it('ticks every second while a VM is created and not deleted', async () => {
    const t = await header();
    vi.useFakeTimers({ now: START + 65_000 });
    put(
      t,
      verified(t, 'workvm.state', vm({ workvm_id: 'wvm-0000000b' }), { session: OTHER }),
      verified(t, 'workvm.state', vm({ workvm_id: 'wvm-0000000b', state: 'deleted', runtime_ms: 4000 }), {
        session: OTHER,
      }),
      verified(t, 'workvm.state', vm(), { session: MAIN }),
    );
    act(() => vi.advanceTimersByTime(1000));
    const runtime = screen.getByTestId('session.workvm-runtime');
    expect(runtime).toHaveAttribute('data-running', 'true');
    expect(runtime.textContent).toBe('Work VM: 1 min 6 s in this session, 1 min 10 s in the task (running)');
    act(() => vi.advanceTimersByTime(2000));
    expect(runtime.textContent).toBe('Work VM: 1 min 8 s in this session, 1 min 12 s in the task (running)');
  });
});

function renderItem(item: typeof runItem, body: unknown, at = START) {
  const { Component } = item;
  return render(
    <I18nProvider value={createI18n('en')}>
      <Component item={{ key: 'k', kind: item.kind, at, seq: 1, sessionId: MAIN, author: null, body }} />
    </I18nProvider>,
  );
}

describe('chat item workvm.run', () => {
  it.each([
    ['running', null, 'Running', 'false'],
    ['exited', 0, 'Exited', 'false'],
    ['exited', 1, 'Exited', 'true'],
    ['killed', 137, 'Killed', 'true'],
    ['timed_out', null, 'Timed out', 'true'],
    ['lost', null, 'Lost', 'true'],
  ])('shows state %s with exit code %s', (state, exitCode, label, failed) => {
    const ended = state === 'running' ? {} : { ended_at: '2026-11-21T12:00:04.000Z', duration_ms: 3000 };
    renderItem(runItem, job({ state, exit_code: exitCode, ...ended }));
    const run = screen.getByTestId('chat.workvm.run');
    expect(run).toHaveAttribute('data-state', state);
    const badge = screen.getByTestId('chat.workvm.run.state');
    expect(badge).toHaveTextContent(label);
    expect(badge).toHaveAttribute('data-failed', failed);
    if (exitCode === null) expect(screen.queryByTestId('chat.workvm.run.exit')).toBeNull();
    else expect(screen.getByTestId('chat.workvm.run.exit')).toHaveTextContent(`Exit code ${exitCode}`);
    if (state === 'running') expect(screen.queryByTestId('chat.workvm.run.duration')).toBeNull();
    else expect(screen.getByTestId('chat.workvm.run.duration')).toHaveTextContent('3.0 s');
  });

  it('shows the output, its tail marker only when truncated, and a note without output', () => {
    const { unmount } = renderItem(runItem, job({ kind: 'browse', output: 'ä', output_bytes: 2 }));
    expect(screen.getByText('Browser')).toBeInTheDocument();
    expect(screen.getByTestId('chat.workvm.run.text').textContent).toBe('ä');
    expect(screen.queryByTestId('chat.workvm.run.tail')).toBeNull();
    unmount();
    const second = renderItem(runItem, job({ output: 'tail', output_bytes: 9999 }));
    expect(screen.getByTestId('chat.workvm.run.tail')).toHaveTextContent('Last part of 9,999 bytes');
    second.unmount();
    renderItem(runItem, job({ output: '', output_bytes: 0 }), 0);
    expect(screen.getByTestId('chat.workvm.run.no-output')).toHaveTextContent('No output.');
    expect(screen.queryByTestId('chat.workvm.run.output')).toBeNull();
    expect(screen.queryByTestId('chat.item.time')).toBeNull();
  });

  it('shows a neutral row for a body that does not follow the contract', () => {
    renderItem(runItem, { ...job(), extra: 1 });
    expect(screen.getByTestId('chat.workvm.invalid')).toHaveTextContent('Unverifiable item (body_invalid)');
    expect(screen.queryByTestId('chat.workvm.run')).toBeNull();
  });
});

describe('chat item workvm.state', () => {
  it('shows a created VM with its resources and no runtime', () => {
    renderItem(stateItem, vm({ implicit: true, size: 'xlarge', memory_mib: 1536 }));
    const row = screen.getByTestId('chat.workvm.state');
    expect(row).toHaveAttribute('data-state', 'created');
    expect(row).toHaveAttribute('data-workvm', 'wvm-0123abcd');
    expect(screen.getByTestId('chat.workvm.state.title').textContent).toBe('Work VM created');
    expect(screen.getByTestId('chat.workvm.state.resources').textContent).toBe(
      'Extra large: 4 CPU, 1.5 GiB RAM, 50 GiB disk',
    );
    expect(screen.queryByTestId('chat.workvm.state.runtime')).toBeNull();
  });

  it.each([
    ['released', 'Work VM deleted (released)'],
    ['session_ended', 'Work VM deleted (session ended)'],
    ['task_closed', 'Work VM deleted (task closed)'],
    ['failed', 'Work VM deleted (failed)'],
    ['restart', 'Work VM deleted (restart)'],
    [null, 'Work VM deleted'],
  ])('shows a deleted VM with reason %s and its runtime', (reason, title) => {
    renderItem(stateItem, vm({ state: 'deleted', reason, ended_at: '2026-11-21T12:04:00.000Z', runtime_ms: 240_000 }));
    expect(screen.getByTestId('chat.workvm.state')).toHaveAttribute('data-state', 'deleted');
    expect(screen.getByTestId('chat.workvm.state.title').textContent).toBe(title);
    expect(screen.getByTestId('chat.workvm.state.runtime').textContent).toBe('Runtime 4 min');
  });

  it('shows a neutral row for a body that does not follow the contract', () => {
    renderItem(stateItem, vm({ cpus: 0 }));
    expect(screen.getByTestId('chat.workvm.invalid')).toBeInTheDocument();
    expect(screen.queryByTestId('chat.workvm.state')).toBeNull();
  });
});

const request = (t: TaskWorld, overrides: Partial<OperationRow> = {}): OperationRow => ({
  operation_id: '0190f000-0000-7000-8000-00000000c001',
  org_id: t.org,
  wg_id: t.wg,
  task_id: TASK,
  session_id: MAIN,
  device_id: 'd1',
  actor_id: 'd1',
  type: 'workvm.request',
  state: 'executing',
  error_code: null,
  retryable: false,
  duplicate_of: null,
  attempt: 1,
  params_event_id: null,
  result_event_id: null,
  created_at: '2026-11-21T12:00:00.000Z',
  decided_at: null,
  started_at: null,
  finished_at: null,
  ...overrides,
});

async function panel(locale: 'en' | 'de' = 'en') {
  const t = await taskWorld();
  t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: [], next_before: null }));
  mount(t, [{ path: '/s', element: <WorkvmPanel wg={t.wg} task={TASK} session={MAIN} /> }], '/s', locale);
  await waitFor(() => expect(t.client.of(`GET /v1/tasks/${TASK}/operations`)).toHaveLength(1));
  return t;
}

describe('work VM panel of the session', () => {
  it('shows that the session has no work VM yet', async () => {
    await panel();
    const box = screen.getByTestId('workvm.panel');
    expect(box).toHaveAttribute('data-phase', 'none');
    expect(screen.getByTestId('workvm.panel.state')).toHaveTextContent('none');
    expect(screen.getByTestId('workvm.panel.note')).toHaveTextContent('This session has no work VM.');
    expect(screen.queryByTestId('workvm.panel.runs')).toBeNull();
    expect(screen.queryByTestId('workvm.panel.countdown')).toBeNull();
  });

  it('counts down while the request is open, then shows the running VM ticking every second', async () => {
    const t = await panel();
    vi.useFakeTimers({ now: START + 3_000 });
    act(() => putOperations(t.store, [request(t)]));
    expect(screen.getByTestId('workvm.panel')).toHaveAttribute('data-phase', 'starting');
    // the clock ticks every 100 ms while the request is open
    act(() => vi.advanceTimersByTime(100));
    expect(screen.getByTestId('countdown.digits')).toHaveTextContent('6.9');
    expect(screen.getByTestId('workvm.panel.countdown.text')).toHaveTextContent('Starting for 3.1 s');
    act(() => vi.advanceTimersByTime(8_400));
    const text = screen.getByTestId('workvm.panel.countdown.text');
    expect(text).toHaveAttribute('data-overdue', 'true');
    expect(text).toHaveTextContent('Delayed: starting for 11.5 s.');
    expect(screen.getByTestId('countdown.digits')).toHaveTextContent('0.0');
    act(() => {
      putEvents(t.store, t.wg, [verified(t, 'workvm.state', vm(), { session: MAIN })]);
      putOperations(t.store, [request(t, { state: 'succeeded' })]);
    });
    expect(screen.getByTestId('workvm.panel')).toHaveAttribute('data-phase', 'running');
    expect(screen.getByTestId('workvm.panel.state')).toHaveTextContent('running');
    expect(screen.getByTestId('workvm.panel.resources')).toHaveTextContent('Standard: 4 CPU, 8 GiB RAM, 50 GiB disk');
    expect(screen.getByTestId('workvm.panel.runtime')).toHaveTextContent('Runtime 11 s');
    act(() => vi.advanceTimersByTime(2_000));
    expect(screen.getByTestId('workvm.panel.runtime')).toHaveTextContent('Runtime 13 s');
    expect(screen.getByTestId('workvm.panel.note')).toHaveTextContent('releases its work VM');
  });

  it('shows the deleted VM with its reason and the runs newest first with their output', async () => {
    const t = await panel('de');
    put(
      t,
      verified(t, 'workvm.state', vm(), { session: MAIN }),
      verified(t, 'workvm.run', job(), { session: MAIN }),
      verified(
        t,
        'workvm.run',
        job({ state: 'exited', exit_code: 1, ended_at: '2026-11-21T12:00:03.000Z', duration_ms: 2000 }),
        { session: MAIN },
      ),
      verified(
        t,
        'workvm.run',
        job({ job_id: 'j-00000000000b', command: 'npm ci', started_at: '2026-11-21T12:01:00.000Z' }),
        { session: MAIN },
      ),
      verified(t, 'workvm.run', job({ job_id: 'j-00000000000c' }), { session: OTHER }),
      verified(t, 'workvm.state', vm({ state: 'deleted', reason: 'idle', runtime_ms: 1_800_000 }), {
        session: MAIN,
      }),
    );
    expect(screen.getByTestId('workvm.panel')).toHaveAttribute('data-phase', 'deleted');
    expect(screen.getByTestId('workvm.panel.state')).toHaveTextContent('Arbeits-VM gelöscht (inaktiv)');
    expect(screen.getByTestId('workvm.panel.runtime')).toHaveTextContent('Laufzeit 30 min');
    const runs = screen.getByTestId('workvm.panel.runs');
    expect(within(runs).getByText('Läufe (2)')).toBeInTheDocument();
    const items = within(runs).getAllByTestId('chat.workvm.run');
    expect(items.map((item) => item.getAttribute('data-job'))).toEqual(['j-00000000000b', 'j-0123456789ab']);
    expect(items[1]).toHaveAttribute('data-state', 'exited');
    expect(within(items[1]!).getByTestId('chat.workvm.run.text').textContent).toBe('line 1\n');
  });

  it('shows a deletion without a reason', async () => {
    const t = await panel();
    put(
      t,
      verified(t, 'workvm.state', vm(), { session: MAIN }),
      verified(t, 'workvm.state', vm({ state: 'deleted', runtime_ms: 1000 }), { session: MAIN }),
    );
    expect(screen.getByTestId('workvm.panel.state')).toHaveTextContent('Work VM deleted');
  });

  it('stays usable when the operations cannot be loaded', async () => {
    const t = await taskWorld();
    mount(t, [{ path: '/s', element: <WorkvmPanel wg={t.wg} task={TASK} session={MAIN} /> }], '/s');
    await waitFor(() => expect(t.client.of(`GET /v1/tasks/${TASK}/operations`)).toHaveLength(1));
    expect(screen.getByTestId('workvm.panel')).toHaveAttribute('data-phase', 'none');
  });
});
