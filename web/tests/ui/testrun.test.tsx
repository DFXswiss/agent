// U-WEB-ui-testrun (ADR-080): the chat item `test.run` in every state, the log viewer behind "Open log" (pages,
// jump to end, truncated note, download, failures, close), "Open full log" of a `workvm.run` with a log, the operation
// card of `test.run` ("Tests: 3 failed – open log") and the test runs and stopped / implicitly starting VM of the
// session's work VM panel.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CHUNK_BYTES } from '../../src/core/crypto/runlog';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { putEvents } from '../../src/core/store/slices/events';
import { type OperationRow, putOperations } from '../../src/core/store/slices/operations';
import { chatItem as testItem } from '../../src/ui/chat-items/test.run';
import { chatItem as runItem } from '../../src/ui/chat-items/workvm.run';
import { chatItem as stateItem } from '../../src/ui/chat-items/workvm.state';
import { OperationCard } from '../../src/ui/chat-items/operation/Card';
import { I18nProvider } from '../../src/ui/i18n';
import { saveBlob } from '../../src/ui/logs/LogView';
import { PANEL_TESTS, WorkvmPanel } from '../../src/ui/screens/session/WorkvmPanel';
import { ascii, buildLog, logPath, logWorld, OBJECT, REF, type LogWorld } from '../core/logs/world';
import { MAIN, mount, TASK, verified } from './chat-world';

afterEach(() => vi.useRealTimers());

const START = Date.parse('2026-11-21T12:00:00.000Z');
const LOG = { object_id: OBJECT, bytes: 100, stored_bytes: 100, truncated: false };

const testRun = (overrides: Record<string, unknown> = {}) => ({
  run_id: REF,
  operation_id: '0190f000-0000-7000-8000-00000000c009',
  kind: 'unit',
  command: 'npm test',
  cwd: '/workspace',
  size: 'standard',
  state: 'failed',
  exit_code: 1,
  started_at: '2026-11-21T12:00:00.000Z',
  ended_at: '2026-11-21T12:01:00.000Z',
  duration_ms: 60_000,
  ready_ms: 4_000,
  summary: { passed: 12, failed: 3, skipped: null, errors: null },
  log: null,
  tail: 'FAIL a.test.ts\n',
  reason: null,
  ...overrides,
});

const job = (overrides: Record<string, unknown> = {}) => ({
  workvm_id: 'wvm-0123abcd',
  job_id: 'j-0123456789ab',
  kind: 'exec',
  command: 'make',
  state: 'exited',
  exit_code: 0,
  started_at: '2026-11-21T12:00:01.000Z',
  ended_at: '2026-11-21T12:00:02.000Z',
  duration_ms: 1000,
  output: 'tail',
  output_bytes: 99_999,
  ...overrides,
});

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

function renderAlone(item: typeof testItem, body: unknown) {
  const { Component } = item;
  return render(
    <I18nProvider value={createI18n('en')}>
      <Component item={{ key: 'k', kind: item.kind, at: START, seq: 1, sessionId: MAIN, author: null, body }} />
    </I18nProvider>,
  );
}

describe('chat item test.run', () => {
  it.each([
    ['running', null, 'Running', 'false'],
    ['passed', 0, 'Passed', 'false'],
    ['failed', 1, 'Failed', 'true'],
    ['error', null, 'Error', 'true'],
    ['timed_out', null, 'Timed out', 'true'],
    ['cancelled', null, 'Cancelled', 'true'],
  ])('shows state %s', (state, exitCode, label, failed) => {
    renderAlone(testItem, testRun({ state, exit_code: exitCode }));
    const item = screen.getByTestId('chat.test.run');
    expect(item).toHaveAttribute('data-state', state);
    expect(item).toHaveAttribute('data-run', REF);
    const badge = screen.getByTestId('chat.test.run.state');
    expect(badge).toHaveTextContent(label);
    expect(badge).toHaveAttribute('data-failed', failed);
    if (exitCode === null) expect(screen.queryByTestId('chat.test.run.exit')).toBeNull();
    else expect(screen.getByTestId('chat.test.run.exit')).toHaveTextContent(`Exit code ${exitCode}`);
    if (state === 'running') expect(screen.queryByTestId('chat.test.run.duration')).toBeNull();
    else expect(screen.getByTestId('chat.test.run.duration')).toHaveTextContent('60.0 s');
  });

  it('shows kind, command, ready time, summary, tail and no log button without a log', () => {
    renderAlone(testItem, testRun());
    expect(screen.getByText('Unit tests')).toBeInTheDocument();
    expect(screen.getByTestId('chat.test.run.command')).toHaveTextContent('npm test');
    expect(screen.getByTestId('chat.test.run.ready')).toHaveTextContent('VM ready after 4.0 s');
    expect(screen.getByTestId('chat.test.run.summary')).toHaveTextContent('Tests: 3 failed, 12 passed');
    expect(screen.getByTestId('chat.test.run.text').textContent).toBe('FAIL a.test.ts\n');
    expect(screen.queryByTestId('chat.test.run.reason')).toBeNull();
    expect(screen.queryByTestId('log.open')).toBeNull();
  });

  it('shows the reason, no summary, no ready time and no output', () => {
    renderAlone(
      testItem,
      testRun({ state: 'error', exit_code: null, reason: 'transfer_failed', summary: null, ready_ms: 0, tail: '' }),
    );
    expect(screen.getByTestId('chat.test.run.reason')).toHaveTextContent('Reason: the workspace could not be copied');
    expect(screen.queryByTestId('chat.test.run.summary')).toBeNull();
    expect(screen.queryByTestId('chat.test.run.ready')).toBeNull();
    expect(screen.getByTestId('chat.test.run.no-tail')).toHaveTextContent('No output.');
  });

  it('shows a neutral row for a body that does not follow the contract', () => {
    renderAlone(testItem, { ...testRun(), extra: 1 });
    expect(screen.getByTestId('chat.workvm.invalid')).toBeInTheDocument();
    expect(screen.queryByTestId('chat.test.run')).toBeNull();
  });
});

/** Mounts the chat item of a stored verified event (the log button finds the event's workgroup and task). */
function mountItem(w: LogWorld, item: typeof testItem, kind: string, body: unknown, stored = true, locale?: 'de') {
  const entry = verified(w.t, kind, body, { session: MAIN, seq: 7 });
  if (stored) putEvents(w.t.store, w.t.wg, [entry]);
  const { Component } = item;
  mount(
    w.t,
    [
      {
        path: '/s',
        element: <Component item={{ key: 'k', kind, at: START, seq: 7, sessionId: MAIN, author: null, body }} />,
      },
    ],
    '/s',
    locale,
  );
}

let saved: Blob[] = [];

function stubDownload() {
  saved = [];
  URL.createObjectURL = vi.fn((blob: Blob) => {
    saved.push(blob);
    return 'blob:log';
  });
  URL.revokeObjectURL = vi.fn();
  return vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
}

describe('log viewer', () => {
  it('pages through the log, jumps to the end and back, downloads it and closes', async () => {
    const w = await logWorld();
    const output = ascii(2 * CHUNK_BYTES + 5, 'x');
    const built = await buildLog(w, output, { bytes: 3 * 1024 * 1024 });
    mountItem(w, testItem, 'test.run', testRun({ log: built.source.log }));
    const open = screen.getByTestId('log.open');
    expect(open).toHaveTextContent('Open log');
    expect(open).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(open);
    const viewer = screen.getByTestId('log.viewer');
    expect(screen.getByTestId('log.loading')).toHaveTextContent('Loading log…');
    await waitFor(() => expect(screen.getByTestId('log.text').textContent).toHaveLength(CHUNK_BYTES));
    expect(viewer).toHaveAttribute('data-status', 'ready');
    expect(screen.getByTestId('log.truncated')).toHaveTextContent(
      'Truncated: only the last 0.5 MiB of 3 MiB are kept.',
    );
    expect(screen.getByTestId('log.part')).toHaveTextContent('Part 1 of 3');
    expect(screen.getByTestId('log.first')).toBeDisabled();
    expect(screen.getByTestId('log.previous')).toBeDisabled();
    fireEvent.click(screen.getByTestId('log.next'));
    await waitFor(() => expect(screen.getByTestId('log.part')).toHaveTextContent('Part 2 of 3'));
    fireEvent.click(screen.getByTestId('log.last'));
    await waitFor(() => expect(screen.getByTestId('log.text').textContent).toBe('xxxxx'));
    expect(screen.getByTestId('log.next')).toBeDisabled();
    expect(screen.getByTestId('log.last')).toBeDisabled();
    fireEvent.click(screen.getByTestId('log.previous'));
    await waitFor(() => expect(viewer).toHaveAttribute('data-index', '1'));
    fireEvent.click(screen.getByTestId('log.first'));
    await waitFor(() => expect(viewer).toHaveAttribute('data-index', '0'));
    // only one page is ever in the DOM
    expect(screen.getByTestId('log.text').textContent!.length).toBeLessThanOrEqual(CHUNK_BYTES);
    const click = stubDownload();
    fireEvent.click(screen.getByTestId('log.download'));
    expect(screen.getByTestId('log.download')).toHaveTextContent('Downloading…');
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(saved[0]!.size).toBe(output.length);
    expect(screen.getByTestId('log.download')).toHaveTextContent('Download');
    fireEvent.click(screen.getByTestId('log.close'));
    expect(screen.queryByTestId('log.viewer')).toBeNull();
    fireEvent.click(open);
    expect(open).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(open);
    expect(screen.queryByTestId('log.viewer')).toBeNull();
  });

  it('shows why the log, a page or the download failed', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(CHUNK_BYTES + 10));
    w.t.client.on(logPath('/chunks/1'), () => {
      throw new HubError('unavailable', 503);
    });
    mountItem(w, testItem, 'test.run', testRun({ log: built.source.log }), true, 'de');
    fireEvent.click(screen.getByTestId('log.open'));
    await waitFor(() => expect(screen.getByTestId('log.text').textContent).toHaveLength(CHUNK_BYTES));
    expect(screen.queryByTestId('log.truncated')).toBeNull();
    fireEvent.click(screen.getByTestId('log.next'));
    const failed = await screen.findByTestId('log.failed');
    expect(failed).toHaveAttribute('data-code', 'unavailable');
    expect(screen.getByTestId('log.text')).toHaveTextContent('Log wird geladen…');
    const click = stubDownload();
    fireEvent.click(screen.getByTestId('log.download'));
    expect(await screen.findByTestId('log.download-failed')).toHaveTextContent('Herunterladen fehlgeschlagen');
    expect(click).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('log.close'));
    w.t.client.on(logPath(), () => {
      throw new HubError('not_found', 404);
    });
    fireEvent.click(screen.getByTestId('log.open'));
    expect(await screen.findByTestId('log.failed')).toHaveAttribute('data-code', 'not_found');
    expect(screen.getByTestId('log.viewer')).toHaveAttribute('data-status', 'failed');
    expect(screen.queryByTestId('log.text')).toBeNull();
  });

  it('offers no log while the store does not hold the event', async () => {
    const w = await logWorld();
    mountItem(w, testItem, 'test.run', testRun({ log: LOG }), false);
    expect(screen.getByTestId('chat.test.run')).toBeInTheDocument();
    expect(screen.queryByTestId('log.open')).toBeNull();
  });

  it('saves a blob through a link', () => {
    const click = stubDownload();
    vi.useFakeTimers();
    saveBlob('x.log', new Blob(['a']));
    expect(click).toHaveBeenCalledTimes(1);
    expect(document.querySelector('a[download]')).toBeNull();
    vi.runAllTimers();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:log');
  });
});

describe('workvm items with logs', () => {
  it('offers the full log of a job whose output was longer than the event keeps', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(100), { manifest: { ref: 'j-0123456789ab' } });
    mountItem(w, runItem, 'workvm.run', job({ log: built.source.log }));
    fireEvent.click(screen.getByTestId('log.open'));
    expect(screen.getByTestId('log.open')).toHaveTextContent('Open full log');
    await waitFor(() => expect(screen.getByTestId('log.viewer')).toHaveAttribute('data-status', 'ready'));
  });

  it('offers no log for a job without one', () => {
    renderAlone(runItem, job({ log: null }));
    expect(screen.getByTestId('chat.workvm.run')).toBeInTheDocument();
    expect(screen.queryByTestId('log.open')).toBeNull();
  });

  it.each([
    [vm({ state: 'starting', implicit: true }), 'Work VM starting', false],
    [vm({ state: 'stopped', reason: 'session_paused', runtime_ms: 60_000 }), 'Work VM stopped (session paused)', true],
    [vm({ state: 'stopped', runtime_ms: 60_000 }), 'Work VM stopped', true],
    [
      vm({ state: 'deleted', reason: 'credit_exhausted', runtime_ms: 60_000 }),
      'Work VM deleted (credit used up)',
      true,
    ],
  ])('shows a work VM state %#', (body, title, runtime) => {
    renderAlone(stateItem, body);
    expect(screen.getByTestId('chat.workvm.state.title').textContent).toBe(title);
    if (runtime) expect(screen.getByTestId('chat.workvm.state.runtime')).toHaveTextContent('Runtime 1 min');
    else expect(screen.queryByTestId('chat.workvm.state.runtime')).toBeNull();
  });
});

const operation = (t: LogWorld['t'], overrides: Partial<OperationRow> = {}): OperationRow => ({
  operation_id: '0190f000-0000-7000-8000-00000000c009',
  org_id: t.org,
  wg_id: t.wg,
  task_id: TASK,
  session_id: MAIN,
  device_id: t.me.id,
  actor_id: t.me.id,
  type: 'test.run',
  state: 'succeeded',
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

describe('operation card of a test run', () => {
  it('shows the summary with the log, the state without a summary, and no log without one', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(100));
    const { t } = w;
    const ids = ['0190f000-0000-7000-8000-00000000c009', '0190f000-0000-7000-8000-00000000c00a'];
    const result = (id: string, value: Record<string, unknown>) =>
      verified(t, 'operation.result', { operation_id: id, status: 'succeeded', result: value });
    const params = (id: string) =>
      verified(t, 'operation.params', {
        operation_id: id,
        type: 'test.run',
        params: { kind: 'unit', command: 'npm test' },
      });
    const {
      operation_id: _o,
      kind: _k,
      command: _c,
      cwd: _w,
      size: _s,
      started_at: _a,
      ended_at: _e,
      ...first
    } = testRun({ log: built.source.log });
    const second = { ...first, run_id: 'tr-00000000000b', state: 'passed', exit_code: 0, summary: null, log: null };
    const events = [params(ids[0]!), result(ids[0]!, first), params(ids[1]!), result(ids[1]!, second)].map((entry) => ({
      ...entry,
      header: { ...entry.header!, ref: (entry.body as { operation_id: string }).operation_id },
    }));
    putEvents(t.store, t.wg, events);
    putOperations(t.store, [operation(w.t), operation(w.t, { operation_id: ids[1]! })]);
    t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: [], next_before: null }));
    mount(
      t,
      [
        {
          path: '/s',
          element: (
            <>
              <OperationCard org={t.org} wg={t.wg} task={TASK} operation={ids[0]!} />
              <OperationCard org={t.org} wg={t.wg} task={TASK} operation={ids[1]!} />
            </>
          ),
        },
      ],
      '/s',
    );
    const [red, green] = screen.getAllByTestId('operation.card');
    expect(within(red!).getByTestId('operation.type')).toHaveTextContent('Run tests');
    expect(within(red!).getByTestId('operation.subject')).toHaveTextContent('unit: npm test');
    const line = within(red!).getByTestId('operation.test-run');
    expect(line).toHaveAttribute('data-failed', 'true');
    expect(within(line).getByTestId('operation.test-run.summary')).toHaveTextContent('Tests: 3 failed, 12 passed');
    fireEvent.click(within(line).getByTestId('log.open'));
    await waitFor(() => expect(screen.getByTestId('log.viewer')).toHaveAttribute('data-status', 'ready'));
    const passed = within(green!).getByTestId('operation.test-run');
    expect(passed).toHaveAttribute('data-failed', 'false');
    expect(passed).toHaveTextContent('Passed');
    expect(within(passed).queryByTestId('log.open')).toBeNull();
  });
});

describe('work VM panel with test runs', () => {
  async function panel(w: LogWorld) {
    w.t.client.on(`GET /v1/tasks/${TASK}/operations`, () => ({ operations: [], next_before: null }));
    mount(w.t, [{ path: '/s', element: <WorkvmPanel wg={w.t.wg} task={TASK} session={MAIN} /> }], '/s');
    await waitFor(() => expect(w.t.client.of(`GET /v1/tasks/${TASK}/operations`)).toHaveLength(1));
  }

  it('lists the newest test runs and the jobs with their logs', async () => {
    const w = await logWorld();
    const built = await buildLog(w, ascii(100));
    await panel(w);
    const runs = Array.from({ length: PANEL_TESTS + 2 }, (_, index) =>
      verified(
        w.t,
        'test.run',
        testRun({
          run_id: `tr-${String(index).padStart(12, '0')}`,
          started_at: new Date(START + index * 1000).toISOString(),
          log: index === PANEL_TESTS + 1 ? built.source.log : null,
        }),
        { session: MAIN },
      ),
    );
    act(() =>
      putEvents(w.t.store, w.t.wg, [
        ...runs,
        verified(w.t, 'workvm.run', job({ log: built.source.log }), { session: MAIN }),
        verified(w.t, 'workvm.run', job({ job_id: 'j-00000000000b' }), { session: MAIN }),
      ]),
    );
    const tests = screen.getByTestId('workvm.panel.tests');
    expect(within(tests).getByText(`Test runs (${PANEL_TESTS + 2})`)).toBeInTheDocument();
    const items = within(tests).getAllByTestId('chat.test.run');
    expect(items).toHaveLength(PANEL_TESTS);
    expect(items[0]).toHaveAttribute('data-run', `tr-${String(PANEL_TESTS + 1).padStart(12, '0')}`);
    expect(within(items[0]!).getByTestId('log.open')).toBeInTheDocument();
    expect(within(items[1]!).queryByTestId('log.open')).toBeNull();
    const jobs = screen.getByTestId('workvm.panel.runs');
    expect(within(jobs).getAllByTestId('log.open')).toHaveLength(1);
    fireEvent.click(within(items[0]!).getByTestId('log.open'));
    await waitFor(() => expect(screen.getByTestId('log.viewer')).toHaveAttribute('data-status', 'failed'));
    // the manifest names run REF, the event another run: refused
    expect(screen.getByTestId('log.failed')).toHaveAttribute('data-code', 'header_invalid');
  });

  it('shows a stopped VM and counts down from an implicit start', async () => {
    const w = await logWorld();
    await panel(w);
    act(() =>
      putEvents(w.t.store, w.t.wg, [
        verified(w.t, 'workvm.state', vm(), { session: MAIN }),
        verified(w.t, 'workvm.state', vm({ state: 'stopped', reason: 'session_paused', runtime_ms: 125_000 }), {
          session: MAIN,
        }),
      ]),
    );
    expect(screen.getByTestId('workvm.panel')).toHaveAttribute('data-phase', 'stopped');
    expect(screen.getByTestId('workvm.panel.state')).toHaveTextContent('stopped – starts again on next use');
    expect(screen.getByTestId('workvm.panel.runtime')).toHaveTextContent('Runtime 2 min 5 s');
    vi.useFakeTimers({ now: START + 600_000 + 2_000 });
    act(() =>
      putEvents(w.t.store, w.t.wg, [
        verified(w.t, 'workvm.state', vm({ state: 'starting', started_at: '2026-11-21T12:10:00.000Z' }), {
          session: MAIN,
        }),
      ]),
    );
    expect(screen.getByTestId('workvm.panel')).toHaveAttribute('data-phase', 'starting');
    act(() => vi.advanceTimersByTime(100));
    expect(screen.getByTestId('workvm.panel.countdown.text')).toHaveTextContent('Starting for 2.1 s');
  });
});
