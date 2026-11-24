// U-WEB-ui-chat-volume (HOT-05): a long chat stays small in the DOM – runs of file changes are one group whose list
// and patches are rendered only when opened (paged, patch length bounded), the timeline renders a window at the end
// with "Show earlier items" and "Jump to the latest item", and unknown kinds are skipped without breaking a run.
// The browser proof with 5 000 file events is `e2e/devices/chat-volume.spec.ts`.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, onTestFinished, vi } from 'vitest';
import { PAGE, WINDOW } from '../../src/ui/task-tabs/chat/Timeline';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { FILES_MAX, FILES_PAGE } from '../../src/ui/chat-items/file-group';
import { CLIP_CHARS as PATCH_CHARS } from '../../src/ui/lists/Clipped';
import { LINE_CHARS } from '../../src/ui/chat-items/session.tool';
import { ChatTab } from '../../src/ui/task-tabs/chat/ChatTab';
import { taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';

async function chat() {
  const t = await taskWorld();
  act(() => putTasks(t.store, [taskRow(t, {}) as TaskRow]));
  mount(t, [{ path: '/t', element: <ChatTab taskId={TASK} orgId={t.org} /> }], '/t');
  return t;
}

const put = (t: TaskWorld, entries: EventEntry[]) => act(() => putEvents(t.store, t.wg, entries));

const fileEvent = (t: TaskWorld, index: number, patch: string | null = `+line ${index}`) =>
  verified(
    t,
    'session.file',
    { path: `src/f${index}.txt`, op: 'write', ...(patch !== null && { patch }) },
    {
      session: MAIN,
      ts: `2026-11-21T12:00:${String(index % 60).padStart(2, '0')}.000Z`,
    },
  );

describe('file groups', () => {
  it('shows a run of file changes as one row and renders files and patches only when opened', async () => {
    const t = await chat();
    const long = 'x'.repeat(PATCH_CHARS + 5);
    put(t, [
      ...Array.from({ length: FILES_PAGE + 2 }, (_, index) => fileEvent(t, index, index === 0 ? long : undefined)),
      // a kind this client does not know (a newer guest) neither shows nor splits the run
      verified(t, 'session.files', { count: 9 }, { session: MAIN, ts: '2026-11-21T12:00:59.000Z' }),
    ]);
    put(t, [fileEvent(t, 59, null)]);
    const rows = screen.getAllByTestId('chat.item');
    expect(rows.map((row) => row.getAttribute('data-kind'))).toEqual(['file-group']);
    const group = screen.getByTestId('chat.files');
    expect(group).toHaveAttribute('data-count', String(FILES_PAGE + 3));
    expect(screen.getByTestId('chat.files.summary')).toHaveTextContent(`${FILES_PAGE + 3} files changed`);
    expect(within(group).getAllByTestId('chat.item.time')).toHaveLength(2);
    expect(screen.queryByTestId('chat.files.list')).toBeNull();
    const toggle = screen.getByTestId('chat.files.toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).not.toHaveAttribute('aria-controls');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveTextContent('Hide files');
    expect(document.getElementById(toggle.getAttribute('aria-controls')!)).not.toBeNull();
    expect(screen.getAllByTestId('chat.file')).toHaveLength(FILES_PAGE);
    expect(screen.queryByTestId('chat.file.patch')).toBeNull();
    fireEvent.click(screen.getByTestId('chat.files.more'));
    expect(screen.getAllByTestId('chat.file')).toHaveLength(FILES_PAGE + 3);
    expect(screen.queryByTestId('chat.files.more')).toBeNull();
    // the last file has no patch: no toggle
    expect(screen.getAllByTestId('chat.file.toggle')).toHaveLength(FILES_PAGE + 2);
    const first = screen.getAllByTestId('chat.file.toggle')[0]!;
    fireEvent.click(first);
    expect(first).toHaveTextContent('Hide changes');
    expect(screen.getByTestId('chat.file.patch').textContent).toHaveLength(PATCH_CHARS);
    fireEvent.click(screen.getByTestId('chat.more'));
    expect(screen.getByTestId('chat.file.patch').textContent).toHaveLength(PATCH_CHARS + 5);
    expect(screen.queryByTestId('chat.more')).toBeNull();
    fireEvent.click(first);
    expect(screen.queryByTestId('chat.file.patch')).toBeNull();
    fireEvent.click(toggle);
    expect(screen.queryByTestId('chat.files.list')).toBeNull();
  });

  it('renders at most FILES_MAX files of a very long run', async () => {
    const t = await chat();
    put(
      t,
      Array.from({ length: FILES_MAX + 60 }, (_, index) => fileEvent(t, index)),
    );
    fireEvent.click(screen.getByTestId('chat.files.toggle'));
    for (let step = 0; step < 4; step += 1) fireEvent.click(screen.getByTestId('chat.files.more'));
    // past the cap the first files drop: "Show previous files" and "Back to the first files" at the start
    expect(screen.getAllByTestId('chat.file')).toHaveLength(FILES_MAX);
    expect(screen.getAllByTestId('chat.file.path')[0]).not.toHaveTextContent('src/f0.txt');
    expect(screen.getByTestId('chat.files.previous')).toHaveTextContent(`Show previous files (${FILES_PAGE} more)`);
    expect(screen.getByTestId('chat.files.more')).toHaveTextContent('Show more files (10 more)');
    Element.prototype.scrollIntoView = vi.fn();
    fireEvent.click(screen.getByTestId('chat.files.first'));
    expect(screen.getAllByTestId('chat.file')).toHaveLength(FILES_PAGE);
    expect(screen.getAllByTestId('chat.file.path')[0]).toHaveTextContent('src/f0.txt');
  });

  it('shows the German texts', async () => {
    const t = await taskWorld();
    act(() => putTasks(t.store, [taskRow(t, {}) as TaskRow]));
    mount(t, [{ path: '/t', element: <ChatTab taskId={TASK} orgId={t.org} /> }], '/t', 'de');
    put(
      t,
      Array.from({ length: 1200 }, (_, index) => fileEvent(t, index)),
    );
    expect(screen.getByTestId('chat.files.summary')).toHaveTextContent('1.200 Dateien geändert');
  });
});

describe('large items', () => {
  it('clips long messages and renders tool details only while open, clipped as well', async () => {
    const t = await chat();
    const big = 'y'.repeat(PATCH_CHARS + 7);
    put(t, [
      verified(t, 'session.output', { text: big }, { session: MAIN }),
      verified(
        t,
        'session.tool',
        { name: 'Bash', input_summary: big, input: big, result_summary: big },
        { session: MAIN },
      ),
      verified(t, 'prompt', { text: big }),
    ]);
    const output = screen.getByTestId('chat.output');
    expect(output.textContent).toContain('y'.repeat(PATCH_CHARS));
    expect(output.textContent).not.toContain('y'.repeat(PATCH_CHARS + 1));
    fireEvent.click(within(output).getByTestId('chat.more'));
    expect(within(output).queryByTestId('chat.more')).toBeNull();
    expect(output.textContent).toContain(big);
    const prompt = screen.getByTestId('chat.prompt');
    expect(within(prompt).getByTestId('chat.prompt.text').textContent).toHaveLength(PATCH_CHARS);
    fireEvent.click(within(prompt).getByTestId('chat.more'));
    expect(within(prompt).getByTestId('chat.prompt.text').textContent).toBe(big);
    const tool = screen.getByTestId('chat.tool') as HTMLDetailsElement;
    // the summary line is bounded too
    expect(within(tool).getByTestId('chat.tool.summary').textContent).toBe(`Bash ${'y'.repeat(LINE_CHARS)}…`);
    expect(within(tool).queryByTestId('chat.tool.input')).toBeNull();
    act(() => {
      tool.open = true;
      tool.dispatchEvent(new Event('toggle'));
    });
    expect(within(tool).getByTestId('chat.tool.input').textContent).toHaveLength(PATCH_CHARS);
    expect(within(tool).getByTestId('chat.tool.result').textContent).toHaveLength(PATCH_CHARS);
    expect(within(tool).getAllByTestId('chat.more')).toHaveLength(2);
    act(() => {
      tool.open = false;
      tool.dispatchEvent(new Event('toggle'));
    });
    expect(within(tool).queryByTestId('chat.tool.input')).toBeNull();
  });
});

describe('long chats', () => {
  it('renders a window at the end, keeps it while items arrive and loads earlier items on demand', async () => {
    const t = await chat();
    const prompts = (from: number, count: number) =>
      Array.from({ length: count }, (_, index) =>
        verified(t, 'prompt', { text: `p${from + index}` }, { seq: 10_000 + from + index }),
      );
    put(t, prompts(0, WINDOW + 30));
    const texts = () => screen.getAllByTestId('chat.prompt.text').map((node) => node.textContent);
    expect(screen.getAllByTestId('chat.item')).toHaveLength(WINDOW);
    expect(texts()[0]).toBe('p30');
    expect(screen.getByTestId('chat.timeline')).toHaveAttribute('data-total', String(WINDOW + 30));
    expect(screen.getByTestId('chat.earlier')).toHaveTextContent('Show earlier items (30 more)');
    // new items: the first rendered row stays
    put(t, prompts(WINDOW + 30, 10));
    expect(texts()[0]).toBe('p30');
    expect(screen.getAllByTestId('chat.item')).toHaveLength(WINDOW + 10);
    // earlier items
    fireEvent.click(screen.getByTestId('chat.earlier'));
    expect(texts()[0]).toBe('p0');
    expect(screen.queryByTestId('chat.earlier')).toBeNull();
    // focus went to the first newly shown row; more new items than the budget allows would drop it in follow mode,
    // so the window is anchored instead and keeps the focused row (ADR-086 section 1)
    expect(document.activeElement).toBe(screen.getAllByTestId('chat.item')[0]);
    put(t, prompts(WINDOW + 40, 2 * PAGE));
    expect(screen.getAllByTestId('chat.item')).toHaveLength(WINDOW + 40);
    expect(texts()[0]).toBe('p0');
    expect(screen.getByTestId('chat.timeline')).toHaveAttribute('data-mode', 'anchored');
    // jump to the latest item: the window shrinks, the last row is scrolled to and focused
    const scroll = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = scroll;
    onTestFinished(() => {
      Element.prototype.scrollIntoView = original;
    });
    fireEvent.click(screen.getByTestId('chat.latest'));
    expect(scroll).toHaveBeenCalledWith({ block: 'end' });
    const rows = screen.getAllByTestId('chat.item');
    expect(rows).toHaveLength(WINDOW);
    expect(document.activeElement).toBe(rows.at(-1));
    expect(texts().at(-1)).toBe(`p${WINDOW + 40 + 2 * PAGE - 1}`);
  });

  it('reads the newest page when it opens and older pages on "Load more"', async () => {
    const t = await taskWorld();
    const targets: string[] = [];
    t.client.on(`GET /v1/scopes/${t.wg}/events`, (_body, target) => {
      targets.push(target);
      return { events: [], next_before: targets.length === 1 ? 500 : null, head: 900 };
    });
    act(() => putTasks(t.store, [taskRow(t, {}) as TaskRow]));
    mount(t, [{ path: '/t', element: <ChatTab taskId={TASK} orgId={t.org} /> }], '/t');
    put(t, [verified(t, 'prompt', { text: 'newest' })]);
    const more = await screen.findByTestId('chat.load-more');
    expect(targets[0]).toContain(`task=${TASK}`);
    fireEvent.click(more);
    await waitFor(() => expect(targets).toHaveLength(2));
    expect(targets[1]).toContain('before=500');
    await waitFor(() => expect(screen.queryByTestId('chat.load-more')).toBeNull());
  });

  it('shows the empty text when every item is of an unknown kind', async () => {
    const t = await chat();
    put(t, [verified(t, 'session.future', {}, { session: MAIN })]);
    expect(screen.getByTestId('chat.empty')).toBeInTheDocument();
  });
});
