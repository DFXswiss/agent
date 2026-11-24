// U-WEB-ui-chat: the task page with tabs, the chat timeline and every chat item (also with odd bodies), the composer
// and the session controls with their failures, the usage slot, Markdown link rules and the Sessions tab in jsdom
// against the core task world. The Playwright specs (`e2e/devices/{chat,sessions}.spec.ts`) run them against the hub.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createI18n } from '../../src/core/i18n';
import { HubError } from '../../src/core/net/signedFetch';
import { setOnline } from '../../src/core/store/slices/connection';
import { setLoad } from '../../src/core/store/slices/chat';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { putSession, putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { putUsage } from '../../src/core/store/slices/usage';
import { setActiveOrg } from '../../src/core/store/slices/session';
import { chatItems } from '../../src/ui/registries/chat-items';
import { TaskScreen } from '../../src/ui/screens/task/TaskScreen';
import { activeTab } from '../../src/ui/screens/task/Tabs';
import { ChatTab } from '../../src/ui/task-tabs/chat/ChatTab';
import Markdown from '../../src/ui/task-tabs/chat/Markdown';
import { UsageSlot, usageStrip } from '../../src/ui/task-tabs/chat/UsageSlot';
import { I18nProvider } from '../../src/ui/i18n';
import { sessionRow, taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';
import { labelDevice } from './device-labels';

afterEach(() => vi.useRealTimers());

const PAGE = '/o/:org/t/:task';

async function taskPage(
  options: { task?: Partial<TaskRow>; session?: Record<string, unknown> | null; locale?: 'en' | 'de' } = {},
) {
  const t = await taskWorld();
  const row = taskRow(t, options.task ?? {}) as TaskRow;
  t.client.on(`GET /v1/tasks/${TASK}`, () => ({
    task: row,
    sessions: options.session === null ? [] : [{ session: sessionRow(t, options.session ?? {}), transitions: [] }],
  }));
  t.client.on(`GET /v1/scopes/${t.wg}/events`, () => ({ events: [], next_after: null, head: 0 }));
  const mounted = mount(t, [{ path: PAGE, element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}`, options.locale);
  await screen.findByTestId('task.screen');
  return { t, ...mounted };
}

const put = (t: TaskWorld, ...entries: EventEntry[]) => act(() => putEvents(t.store, t.wg, entries));

describe('task page', () => {
  it('shows the tabs from the registry and switches by the tab parameter', async () => {
    const { t, router } = await taskPage();
    expect(screen.getByTestId('task.tabs.chat')).toHaveAttribute('aria-current', 'page');
    expect(screen.getByTestId('chat')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('task.tabs.sessions'));
    await screen.findByTestId('sessions');
    expect(router.state.location.search).toBe('?tab=sessions');
    expect(screen.getByTestId('task.tabs.sessions')).toHaveAttribute('aria-current', 'page');
    fireEvent.click(screen.getByTestId('task.tabs.chat'));
    await screen.findByTestId('chat');
    expect(router.state.location.search).toBe('');
    expect(activeTab('?tab=nope', [{ id: 'a', label: 'A' }])).toBe('a');
    expect(t.client.of(`GET /v1/tasks/${TASK}`)).toHaveLength(1);
  });

  it('shows the loading state, then the catalog error of a task that cannot be loaded', async () => {
    const t = await taskWorld();
    let fail: () => void = () => undefined;
    t.client.on(
      `GET /v1/tasks/${TASK}`,
      () => new Promise((_, reject) => (fail = () => reject(new HubError('forbidden', 403)))),
    );
    mount(t, [{ path: PAGE, element: <TaskScreen /> }], `/o/${t.org}/t/${TASK}`);
    expect(screen.getByTestId('task.loading')).toHaveTextContent('Loading the task…');
    act(() => fail());
    // forbidden and not found look the same (06-web.md section 12.6)
    expect(await screen.findByTestId('task.error')).toHaveTextContent('Not available');
  });

  it('shows an unverifiable title and the closed state without a task menu', async () => {
    const { t } = await taskPage({
      task: { state: 'closed', closed_at: '2026-11-21T12:00:09.000Z', title_event_id: 'bad' },
    });
    put(t, {
      ...verified(t, 'task.meta', null),
      event: { header: { id: 'bad', task: TASK } },
      status: 'unverifiable',
      code: 'signature_invalid',
      header: null,
      body: null,
    });
    expect(screen.getByTestId('task.title')).toHaveTextContent('Unverifiable title (signature_invalid)');
    expect(screen.getByTestId('task.state')).toHaveTextContent('Closed');
    expect(screen.queryByTestId('task.menu')).toBeNull();
  });

  it('closes a task after confirmation and reports a refusal', async () => {
    const { t } = await taskPage();
    fireEvent.click(screen.getByTestId('task.close'));
    expect(within(screen.getByTestId('task.menu')).getByRole('group')).toHaveTextContent('Close this task?');
    t.client.on(`POST /v1/tasks/${TASK}/close`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(screen.getByTestId('task.close.confirm'));
    expect(await screen.findByTestId('task.close.error')).toHaveAttribute('data-code', 'forbidden');
    t.client.on(`POST /v1/tasks/${TASK}/close`, () => ({ task: taskRow(t, { state: 'closed' }) }));
    fireEvent.click(screen.getByTestId('task.close.confirm'));
    await waitFor(() => expect(screen.queryByTestId('task.menu')).toBeNull());
  });
});

describe('chat timeline and items', () => {
  it('renders every item, odd bodies included, with neutral unverifiable rows', async () => {
    const { t } = await taskPage({ session: { state: 'working' } });
    act(() => setActiveOrg(t.store, t.org));
    const unverifiable: EventEntry = {
      ...verified(t, 'prompt', null),
      status: 'unverifiable',
      code: 'signature_invalid',
      header: null,
      body: null,
    };
    put(
      t,
      verified(t, 'prompt', { text: 'Hi' }, { ts: '2026-11-21T12:00:01.000Z' }),
      verified(t, 'prompt', 'odd', { author: 'someone-else', ts: '2026-11-21T12:00:02.000Z' }),
      verified(t, 'session.output', { text: 'Out', truncated: true }, { session: MAIN }),
      verified(t, 'session.output', 7, { session: MAIN }),
      verified(t, 'session.tool', { name: 'Bash', input_summary: 'ls', is_error: true }, { session: MAIN }),
      verified(t, 'session.tool', null, { session: MAIN }),
      verified(t, 'session.file', { path: 'gone.txt', op: 'delete' }, { session: MAIN }),
      verified(t, 'session.file', { op: 'chmod' }, { session: MAIN }),
      verified(t, 'session.file', null, { session: MAIN }),
      verified(t, 'session.hook', { decision: 'deny' }, { session: MAIN }),
      verified(t, 'session.status', { state: 'failed' }, { session: MAIN }),
      verified(t, 'session.status', { state: 'working', reason: 'x' }, { session: MAIN }),
      verified(t, 'session.status', null, { session: MAIN }),
      verified(t, 'operation.params', {}, { session: MAIN }),
      verified(t, 'platform.read', { tool: 'x', arguments: {}, result_count: 0 }, { session: MAIN }),
      unverifiable,
    );
    const kinds = screen.getAllByTestId('chat.item').map((item) => item.getAttribute('data-kind'));
    // operation cards (WEB-09) need a body that names its operation; this one names none
    expect(kinds).toContain('operation.params');
    expect(screen.queryByTestId('operation.card')).toBeNull();
    expect(screen.getByTestId('chat.operation.invalid')).toHaveTextContent('Unverifiable item (params_invalid)');
    // kinds without a renderer are not shown
    expect(kinds).not.toContain('platform.read');
    expect(screen.getAllByTestId('chat.prompt.author').map((item) => item.textContent)).toEqual(['You', 'Member']);
    expect(screen.getAllByTestId('chat.prompt.text').map((item) => item.textContent)).toEqual(['Hi', '']);
    expect(screen.getByTestId('chat.output.truncated')).toBeInTheDocument();
    expect(await screen.findAllByTestId('markdown')).toHaveLength(2);
    const tools = screen.getAllByTestId('chat.tool');
    expect(tools[0]).toHaveAttribute('data-error', 'true');
    expect(within(tools[0]!).getByTestId('chat.tool.error')).toHaveTextContent('Error');
    expect(within(tools[1]!).getByTestId('chat.tool.summary')).toHaveTextContent('');
    // the three file changes in a row are one group; its list is not in the DOM until opened
    expect(kinds).not.toContain('session.file');
    expect(screen.getByTestId('chat.files.summary')).toHaveTextContent('3 files changed');
    expect(screen.queryByTestId('chat.file')).toBeNull();
    fireEvent.click(screen.getByTestId('chat.files.toggle'));
    expect(screen.getAllByTestId('chat.file').map((item) => item.getAttribute('data-op'))).toEqual([
      'delete',
      'edit',
      'edit',
    ]);
    expect(screen.getAllByTestId('chat.file')[0]).toHaveTextContent('Deleted');
    expect(screen.queryByTestId('chat.file.patch')).toBeNull();
    expect(screen.getByTestId('chat.hook')).toHaveTextContent('Blocked: action');
    const statuses = screen.getAllByTestId('chat.status');
    expect(statuses[0]).toHaveTextContent(`Session failed: ${createI18n('en').error('unavailable').title}`);
    expect(statuses[1]).toHaveTextContent('Session state: working');
    expect(statuses[2]).toHaveTextContent('Session state:');
    const row = screen.getAllByTestId('chat.item').find((item) => item.getAttribute('data-kind') === 'unverifiable')!;
    expect(row).toHaveTextContent('Unverifiable item (signature_invalid)');
    expect(row).toHaveAttribute('data-code', 'signature_invalid');
  });

  it('shows work VM jobs (latest per job) and lifecycle rows, with neutral rows for invalid bodies', async () => {
    const { t } = await taskPage({ session: { state: 'working' } });
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
    const vm = (overrides: Record<string, unknown> = {}) => ({
      workvm_id: 'wvm-0123abcd',
      state: 'created',
      scope: 'task',
      size: 'standard',
      cpus: 4,
      memory_mib: 8192,
      disk_gib: 50,
      implicit: true,
      reason: null,
      started_at: '2026-11-21T12:00:00.000Z',
      ended_at: null,
      runtime_ms: 0,
      ...overrides,
    });
    const ended = { ended_at: '2026-11-21T12:00:04.000Z', duration_ms: 2500 };
    put(
      t,
      verified(t, 'workvm.state', vm(), { session: MAIN, ts: '2026-11-21T12:00:01.000Z' }),
      verified(t, 'workvm.run', job(), { session: MAIN, ts: '2026-11-21T12:00:02.000Z' }),
      verified(t, 'workvm.run', job({ job_id: 'j-bbbbbbbbbbbb', kind: 'browse', command: 'https://example.com' }), {
        session: MAIN,
        ts: '2026-11-21T12:00:03.000Z',
      }),
      verified(
        t,
        'workvm.run',
        job({ job_id: 'j-cccccccccccc', state: 'exited', exit_code: 2, output: '', ...ended }),
        {
          session: MAIN,
          ts: '2026-11-21T12:00:04.000Z',
        },
      ),
      verified(
        t,
        'workvm.run',
        job({ job_id: 'j-dddddddddddd', state: 'timed_out', output: 'tail', output_bytes: 123456, ...ended }),
        { session: MAIN, ts: '2026-11-21T12:00:04.500Z' },
      ),
      verified(t, 'workvm.run', job({ state: 'exited', exit_code: 0, output: 'line 1\nok\n', ...ended }), {
        session: MAIN,
        ts: '2026-11-21T12:00:05.000Z',
      }),
      verified(t, 'workvm.run', { job_id: 'j-0123456789ab' }, { session: MAIN, ts: '2026-11-21T12:00:06.000Z' }),
      verified(
        t,
        'workvm.state',
        vm({ state: 'deleted', reason: 'idle', size: 'large', memory_mib: 1536, runtime_ms: 7_503_400 }),
        { session: MAIN, ts: '2026-11-21T12:00:07.000Z' },
      ),
      verified(t, 'workvm.state', vm({ state: 'deleted', workvm_id: 'wvm-0000000b' }), {
        session: MAIN,
        ts: '2026-11-21T12:00:08.000Z',
      }),
      verified(t, 'workvm.state', 'odd', { session: MAIN, ts: '2026-11-21T12:00:09.000Z' }),
    );
    const runs = screen.getAllByTestId('chat.workvm.run');
    // the running item of the first job is replaced by its final one (after the browse job)
    expect(runs.map((item) => item.getAttribute('data-job'))).toEqual([
      'j-bbbbbbbbbbbb',
      'j-cccccccccccc',
      'j-dddddddddddd',
      'j-0123456789ab',
    ]);
    const [browse, failed, timedOut, done] = runs as [HTMLElement, HTMLElement, HTMLElement, HTMLElement];
    expect(browse).toHaveTextContent('Browser');
    expect(within(browse).getByTestId('chat.workvm.run.state')).toHaveTextContent('Running');
    expect(within(browse).queryByTestId('chat.workvm.run.exit')).toBeNull();
    expect(within(browse).queryByTestId('chat.workvm.run.duration')).toBeNull();
    expect(within(browse).getByTestId('chat.workvm.run.command')).toHaveTextContent('https://example.com');
    expect(within(failed).getByTestId('chat.workvm.run.state')).toHaveAttribute('data-failed', 'true');
    expect(within(failed).getByTestId('chat.workvm.run.exit')).toHaveTextContent('Exit code 2');
    expect(within(failed).getByTestId('chat.workvm.run.no-output')).toHaveTextContent('No output.');
    expect(within(timedOut).getByTestId('chat.workvm.run.state')).toHaveTextContent('Timed out');
    expect(within(timedOut).getByTestId('chat.workvm.run.tail')).toHaveTextContent('Last part of 123,456 bytes');
    expect(within(done).getByTestId('chat.workvm.run.state')).toHaveAttribute('data-failed', 'false');
    expect(within(done).getByTestId('chat.workvm.run.state')).toHaveTextContent('Exited');
    expect(within(done).getByTestId('chat.workvm.run.duration')).toHaveTextContent('2.5 s');
    expect(within(done).queryByTestId('chat.workvm.run.tail')).toBeNull();
    expect(within(done).getByTestId('chat.workvm.run.text').textContent).toBe('line 1\nok\n');
    const states = screen.getAllByTestId('chat.workvm.state');
    expect(states.map((item) => item.getAttribute('data-state'))).toEqual(['created', 'deleted', 'deleted']);
    expect(states[0]).toHaveTextContent('Work VM created');
    expect(states[0]).toHaveTextContent('Standard: 4 CPU, 8 GiB RAM, 50 GiB disk');
    expect(within(states[0]!).queryByTestId('chat.workvm.state.runtime')).toBeNull();
    expect(states[1]).toHaveTextContent('Work VM deleted (idle)');
    expect(states[1]).toHaveTextContent('Large: 4 CPU, 1.5 GiB RAM');
    expect(within(states[1]!).getByTestId('chat.workvm.state.runtime')).toHaveTextContent('Runtime 2 h 5 min 3 s');
    expect(within(states[2]!).getByTestId('chat.workvm.state.title')).toHaveTextContent(/^Work VM deleted$/);
    expect(screen.getAllByTestId('chat.workvm.invalid').map((item) => item.textContent)).toEqual([
      'Unverifiable item (body_invalid)',
      'Unverifiable item (body_invalid)',
    ]);
  });

  it('names members from their verified profile and shows no time for an item without one', async () => {
    const { t } = await taskPage();
    put(t, verified(t, 'prompt', { text: 'no time' }, { author: 'u-x', ts: 'garbage' }));
    expect(screen.getByTestId('chat.prompt.author')).toHaveTextContent('Member');
    expect(within(screen.getByTestId('chat.prompt')).queryByTestId('chat.item.time')).toBeNull();
  });

  it('shows plain text until the Markdown chunk is loaded and keeps only safe links', async () => {
    render(
      <I18nProvider value={createI18n('en')}>
        <Markdown
          text={
            '# H1\n\n## H2\n\n### H3\n\n- u\n\n1. o\n\n> quote\n\n```\nblock\n```\n\n' +
            '[a](https://ok.example) [b](ftp://x) ![](https://img.example/i.png) ![alt](mailto:a@b.c)'
          }
        />
      </I18nProvider>,
    );
    const links = screen.getAllByRole('link');
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      'https://ok.example',
      'https://img.example/i.png',
      'mailto:a@b.c',
    ]);
    expect(links.map((link) => link.textContent)).toEqual(['a', 'https://img.example/i.png', 'alt']);
    expect(screen.getByTestId('markdown.link-removed')).toHaveTextContent('b');
    const markdown = screen.getByTestId('markdown');
    expect([...markdown.children].map((node) => node.tagName)).toEqual([
      'H1',
      'H2',
      'H3',
      'UL',
      'OL',
      'BLOCKQUOTE',
      'PRE',
      'P',
    ]);
    expect(markdown.querySelector('pre code')).toHaveTextContent('block');
  });

  it('summarises earlier main sessions and offers no Retry to readers', async () => {
    const { t } = await taskPage({ session: { state: 'failed', state_reason: 'oom' }, task: { state: 'failed' } });
    expect(screen.getByTestId('countdown.retry')).toBeInTheDocument();
    const earlier = (id: string, overrides: Record<string, unknown>) =>
      act(() =>
        putSession(
          t.store,
          sessionRow(t, { session_id: id, created_at: '2026-11-21T11:00:00.000Z', ...overrides }) as never,
          [],
          0,
        ),
      );
    earlier('old-ready', { state: 'working', first_working_at: '2026-11-21T11:00:04.000Z' });
    earlier('old-failed', { state: 'failed', state_reason: 'vm_start_failed' });
    earlier('old-failed-bare', { state: 'failed', state_reason: null });
    earlier('old-starting', { state: 'vm_ready' });
    const summaries = screen.getAllByTestId('chat.countdown.summary');
    expect(summaries.map((item) => item.getAttribute('data-phase')).sort()).toEqual([
      'delayed',
      'failed',
      'failed',
      'ready',
    ]);
    expect(summaries.find((item) => item.getAttribute('data-phase') === 'ready')).toHaveTextContent('Ready in 4.0 s');
    expect(summaries.find((item) => item.getAttribute('data-phase') === 'delayed')).toHaveTextContent(
      'Session starting',
    );
    act(() => t.store.update('me', (me) => ({ ...me, userId: 'reader' })));
    expect(screen.queryByTestId('countdown.retry')).toBeNull();
    expect(screen.getByTestId('chat.read-only')).toHaveTextContent('You have read access');
    // a countdown whose session left the store renders nothing
    const Countdown = chatItems().get('countdown')!.Component;
    const other = await taskWorld();
    mount(other, [{ path: '/', element: <Countdown item={{ sessionId: 'unknown', body: { task: 'x' } }} /> }], '/');
    expect(screen.queryAllByTestId('countdown.panel')).toHaveLength(1);
    putTasks(other.store, [taskRow(other, { task_id: 'x' }) as TaskRow]);
    mount(other, [{ path: '/', element: <Countdown item={{ sessionId: 'unknown', body: { task: 'x' } }} /> }], '/');
    expect(screen.queryAllByTestId('countdown.panel')).toHaveLength(1);
  });

  it('shows the owner the card that connects model access, others that the owner was asked (ADR-078)', async () => {
    const { t } = await taskPage({
      session: { state: 'failed', state_reason: 'model_access_unavailable' },
      task: { state: 'failed' },
    });
    const card = screen.getByTestId('chat.model-access');
    expect(card).toHaveTextContent('Connect your Claude subscription');
    expect(within(card).getByTestId('chat.model-access.connect')).toHaveAttribute('href', '/settings');
    expect(within(card).getByTestId('chat.model-access.connect')).toHaveTextContent('Connect Claude subscription');
    act(() => t.store.update('me', (me) => ({ ...me, userId: 'reader' })));
    expect(screen.getByTestId('chat.model-access')).toHaveTextContent('the owner has been asked');
    expect(screen.queryByTestId('chat.model-access.connect')).toBeNull();
    // another failure reason or a working session shows no card
    act(() => putSession(t.store, sessionRow(t, { state: 'failed', state_reason: 'oom' }) as never, [], 0));
    expect(screen.queryByTestId('chat.model-access')).toBeNull();
    act(() => putSession(t.store, sessionRow(t, { state: 'working' }) as never, [], 0));
    expect(screen.queryByTestId('chat.model-access')).toBeNull();
  });

  it('shows no model-access card without a main session', async () => {
    await taskPage({ session: null, task: { main_session_id: null } });
    expect(screen.queryByTestId('chat.model-access')).toBeNull();
  });

  it('shows the load failure with a working "Try again" and the loading text', async () => {
    const { t } = await taskPage({ session: null, task: { main_session_id: null } });
    act(() => setLoad(t.store, TASK, 'error', 'unavailable'));
    expect(screen.getByTestId('chat.error')).toHaveAttribute('data-code', 'unavailable');
    fireEvent.click(screen.getByTestId('chat.reload'));
    await waitFor(() => expect(screen.queryByTestId('chat.error')).toBeNull());
    act(() => setLoad(t.store, TASK, 'loading'));
    expect(screen.getByTestId('chat.empty')).toHaveTextContent('Loading the chat…');
  });

  it('renders no chat for a task that is not in the store', async () => {
    const t = await taskWorld();
    mount(t, [{ path: '/', element: <ChatTab orgId={t.org} taskId="unknown" /> }], '/');
    expect(screen.queryByTestId('chat')).toBeNull();
  });

  it('renders a discovered usage strip in the slot', () => {
    expect(usageStrip({})).toBeNull();
    expect(usageStrip({ './strip.tsx': 5 })).toBeNull();
    const Strip = ({ taskId }: { taskId: string }) => <span data-testid="strip">{taskId}</span>;
    expect(usageStrip({ './strip.tsx': { UsageStrip: Strip } })).toBe(Strip);
    render(<UsageSlot orgId="o" taskId="t1" strip={Strip as never} />);
    expect(screen.getByTestId('strip')).toHaveTextContent('t1');
  });
});

describe('composer and controls', () => {
  it('sends with Ctrl+Enter only, reports failures and says Offline offline', async () => {
    const { t } = await taskPage({ session: { state: 'working' } });
    const prompt = screen.getByTestId('chat.composer.prompt');
    fireEvent.change(prompt, { target: { value: 'Next' } });
    fireEvent.keyDown(prompt, { key: 'Enter' });
    expect(t.client.of(`POST /v1/tasks/${TASK}/prompts`)).toHaveLength(0);
    t.client.on(`POST /v1/tasks/${TASK}/prompts`, () => {
      throw new HubError('rate_limited', 429);
    });
    fireEvent.keyDown(prompt, { key: 'Enter', ctrlKey: true });
    expect(await screen.findByTestId('chat.composer.error')).toHaveAttribute('data-code', 'rate_limited');
    expect(prompt).toHaveValue('Next');
    t.client.on(`POST /v1/tasks/${TASK}/prompts`, () => ({ scope_seq: 9, session_id: MAIN }));
    fireEvent.click(screen.getByTestId('chat.composer.send'));
    await waitFor(() => expect(prompt).toHaveValue(''));
    expect(screen.queryByTestId('chat.composer.error')).toBeNull();
    // nothing to send: the form does nothing
    fireEvent.submit(screen.getByTestId('chat.composer'));
    expect(t.client.of(`POST /v1/tasks/${TASK}/prompts`)).toHaveLength(2);
    act(() => setOnline(t.store, false));
    expect(screen.getByTestId('chat.composer.send')).toHaveTextContent('Offline');
  });

  it('stops and ends the main session and shows a refused control', async () => {
    const { t } = await taskPage({ session: { state: 'working' } });
    const bodies: unknown[] = [];
    t.client.on(`POST /v1/sessions/${MAIN}/control`, (body) => {
      bodies.push(body);
      return {};
    });
    fireEvent.click(screen.getByTestId('chat.control.stop'));
    fireEvent.click(screen.getByTestId('chat.control.end'));
    await waitFor(() => expect(bodies).toEqual([{ action: 'interrupt' }, { action: 'stop' }]));
    t.client.on(`POST /v1/sessions/${MAIN}/control`, () => {
      throw new HubError('forbidden', 403);
    });
    fireEvent.click(screen.getByTestId('chat.control.stop'));
    expect(await screen.findByTestId('chat.composer.error')).toHaveTextContent(
      'The session did not accept the request',
    );
    // an idle session can be ended but not stopped; an ended one neither
    act(() => putSession(t.store, sessionRow(t, { state: 'idle' }) as never, [], 0));
    expect(screen.queryByTestId('chat.control.stop')).toBeNull();
    expect(screen.getByTestId('chat.control.menu')).toBeInTheDocument();
    act(() => putSession(t.store, sessionRow(t, { state: 'stopped' }) as never, [], 0));
    expect(screen.queryByTestId('chat.control.menu')).toBeNull();
  });

  it('has no controls without a main session', async () => {
    await taskPage({ session: null, task: { main_session_id: null } });
    expect(screen.getByTestId('chat.composer')).toBeInTheDocument();
    expect(screen.queryByTestId('chat.control.stop')).toBeNull();
    expect(screen.queryByTestId('chat.control.menu')).toBeNull();
  });
});

describe('sessions tab', () => {
  it('shows the tree with state, device and model, and an empty tab', async () => {
    const { t } = await taskPage({ session: { model_id: 'claude-x' }, locale: 'de' });
    fireEvent.click(screen.getByTestId('task.tabs.sessions'));
    await screen.findByTestId('sessions.tree');
    act(() =>
      putSession(
        t.store,
        sessionRow(t, {
          session_id: 'child',
          parent_session_id: MAIN,
          depth: 1,
          device_id: null,
          state: 'suspended',
        }) as never,
        [],
        0,
      ),
    );
    const nodes = screen.getAllByTestId('sessions.node');
    expect(nodes.map((node) => node.getAttribute('data-depth'))).toEqual(['0', '1']);
    expect(within(nodes[0]!).getAllByTestId('session.model')[0]).toHaveTextContent('Modell claude-x');
    // the device by its label (until one is read: the neutral name), the usage of each session (KOR-05, B-23)
    const device = within(nodes[0]!).getAllByTestId('session.device')[0]!;
    expect(device).toHaveTextContent('Auf Gerät ohne Namen');
    act(() => labelDevice(t.store, t.org, '0190f000-0000-7000-8000-0000000000d1', 'Build box'));
    expect(device).toHaveTextContent('Auf Build box');
    const usage = within(nodes[0]!).getAllByTestId('session.usage')[0]!;
    expect(usage).toHaveTextContent('Noch kein Verbrauch');
    act(() =>
      putUsage(t.store, TASK, MAIN, {
        requests: 2,
        input_tokens: 12_000,
        output_tokens: 345,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        thinking_tokens: 0,
        cost_micro_usd: 1_250_000,
        cost_basis: 'list_price',
        context_used_tokens: 0,
        context_window_tokens: 0,
        last_provider: null,
        updated_at: '2026-11-21T12:00:09.000Z',
      }),
    );
    expect(usage).toHaveTextContent(/^12\.000 ein, 345 aus, 1,25\s\$ \(Listenpreis\)$/);
    expect(within(nodes[1]!).getByTestId('session.device')).toHaveTextContent('Kein Gerät');
    expect(within(nodes[1]!).getByTestId('session.model')).toHaveTextContent('Modell nicht gemeldet');
    expect(within(nodes[1]!).getByTestId('session.state')).toHaveTextContent('Pausiert');
    expect(nodes[1]).toHaveTextContent('Subagent 1');
    act(() => putTasks(t.store, [taskRow(t, { task_id: 'other' }) as TaskRow]));
    const empty = await taskPage({ session: null, task: { main_session_id: null } });
    fireEvent.click(screen.getAllByTestId('task.tabs.sessions').at(-1)!);
    expect(await screen.findByTestId('sessions.empty')).toBeInTheDocument();
    expect(empty.t.store.getState().tasks.sessions).toEqual({});
  });
});
