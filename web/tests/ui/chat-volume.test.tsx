// U-WEB-ui-chat-volume (HOT-05): a long chat stays small in the DOM – runs of file changes are one group whose list
// and patches are rendered only when opened (paged, patch length bounded), the timeline renders a window at the end
// with "Show earlier items" and "Jump to the latest item", and unknown kinds are skipped without breaking a run.
// The browser proof with 5 000 file events is `e2e/devices/chat-volume.spec.ts`.
import { act, fireEvent, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { PAGE, WINDOW } from '../../src/core/chat/files';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { FILES_PAGE } from '../../src/ui/chat-items/file-group';
import { PATCH_CHARS } from '../../src/ui/chat-items/file/FilePatch';
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
    fireEvent.click(screen.getByTestId('chat.file.patch.more'));
    expect(screen.getByTestId('chat.file.patch').textContent).toHaveLength(PATCH_CHARS + 5);
    expect(screen.queryByTestId('chat.file.patch.more')).toBeNull();
    fireEvent.click(first);
    expect(screen.queryByTestId('chat.file.patch')).toBeNull();
    fireEvent.click(toggle);
    expect(screen.queryByTestId('chat.files.list')).toBeNull();
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
    // more than PAGE new items beyond the budget: back to the last rows
    put(t, prompts(WINDOW + 40, 2 * PAGE));
    expect(screen.getAllByTestId('chat.item')).toHaveLength(WINDOW + PAGE);
    // jump to the latest item: the window shrinks, the last row is scrolled to and focused
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    fireEvent.click(screen.getByTestId('chat.latest'));
    expect(scroll).toHaveBeenCalledWith({ block: 'end' });
    const rows = screen.getAllByTestId('chat.item');
    expect(rows).toHaveLength(WINDOW);
    expect(document.activeElement).toBe(rows.at(-1));
    expect(texts().at(-1)).toBe(`p${WINDOW + 40 + 2 * PAGE - 1}`);
  });

  it('shows the empty text when every item is of an unknown kind', async () => {
    const t = await chat();
    put(t, [verified(t, 'session.future', {}, { session: MAIN })]);
    expect(screen.getByTestId('chat.empty')).toBeInTheDocument();
  });
});
