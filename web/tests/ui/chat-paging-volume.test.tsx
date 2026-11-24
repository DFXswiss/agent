// U-WEB-ui-chat-volume (KOR-35, ADR-086 section 1): the task chat of a big task stays shown while the window controls
// are used many times. The data set has the shape of the volume gate's seed: prompts written first (low scope seqs)
// and the device's events after them (high scope seqs), each with the time of its round – so a page read by scope seq
// ("Load more") inserts rows all over the timeline (ordered by time), not only at its oldest end, and runs of file
// changes grow at their oldest file (the group's key changes).
import { act, fireEvent, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { StoredEvent } from '../../src/core/net/sse';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { putTasks, type TaskRow } from '../../src/core/store/slices/tasks';
import { MAX_ROWS } from '../../src/ui/task-tabs/chat/Timeline';
import { ChatTab } from '../../src/ui/task-tabs/chat/ChatTab';
import { taskRow, type TaskWorld, taskWorld } from '../core/tasks/world';
import { MAIN, mount, TASK, verified } from './chat-world';

const time = (round: number, step: number) =>
  new Date(Date.UTC(2026, 10, 21, 0) + round * 1000 + step * 10).toISOString();

/**
 * `rounds` chat rounds: a prompt each (seqs first), then the device's events of every round – five rows of six events
 * (two files are one group), so one page (200 events) holds more rows than the window and a page boundary may split a
 * group.
 */
function dataSet(t: TaskWorld, rounds: number): Map<number, EventEntry> {
  const entries = new Map<number, EventEntry>();
  let seq = 1;
  const add = (kind: string, body: unknown, round: number, step: number, session = MAIN) => {
    entries.set(seq, verified(t, kind, body, { seq, session, ts: time(round, step) }));
    seq += 1;
  };
  for (let round = 0; round < rounds; round += 1) add('prompt', { text: `p${round}` }, round, 0, '');
  for (let round = 0; round < rounds; round += 1) {
    add('session.output', { text: `o${round}`, role: 'assistant' }, round, 1);
    add('session.file', { path: `f${round}.txt`, op: 'write' }, round, 2);
    add('session.file', { path: `g${round}.txt`, op: 'edit' }, round, 3);
    add('session.tool', { name: 'Bash', input_summary: `cat ${round}`, result_summary: 'ok' }, round, 4);
    add('session.hook', { event: 'PreToolUse', tool: 'Bash', decision: 'deny', reason: `r${round}` }, round, 5);
    add('session.error', { code: 'recorder_failed' }, round, 6);
  }
  return entries;
}

/** The events endpoint over the data set (newest first by scope seq) and a reader that settles in small chunks. */
function serve(t: TaskWorld, entries: Map<number, EventEntry>) {
  const seqs = [...entries.keys()].sort((a, b) => a - b);
  t.client.on(`GET /v1/scopes/${t.wg}/events`, (_body, target) => {
    const params = new URLSearchParams(target.split('?')[1]);
    const before = Number(params.get('before'));
    const limit = Number(params.get('limit'));
    const below = seqs.filter((seq) => seq < before);
    const page = below.slice(-limit);
    return {
      events: page.map((seq) => ({ scope_seq: seq, received_at: null, event: entries.get(seq)!.event })),
      next_before: below.length > limit ? page[0] : null,
      head: seqs.at(-1),
    };
  });
  // the reader settles a page in chunks (each a store update); the test drives it inside `act` (`settle`)
  t.context.ingest.mockImplementation(async (_org: string, scope: string, items: readonly StoredEvent[]) => {
    for (let from = 0; from < items.length; from += 40) {
      putEvents(
        t.store,
        scope,
        items.slice(from, from + 40).map((item) => entries.get(item.scope_seq)!),
      );
      await Promise.resolve();
    }
  });
}

/** Lets the client's requests and the reader's chunks finish (the doubles answer in microtasks). */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/** Uses the chat's window controls `moves` times ("Show earlier items", else "Load more"). */
async function useControls(moves: number, trace: string[]): Promise<string[]> {
  const used: string[] = [];
  for (let step = 0; step < moves; step += 1) {
    const control = screen.queryByTestId('chat.earlier') ?? screen.queryByTestId('chat.load-more');
    if (control === null) break;
    used.push(control.dataset.testid!);
    const list = () => screen.getByTestId('chat.timeline').dataset;
    const first = screen.getAllByTestId('chat.item')[0]!.dataset.key;
    const active = (document.activeElement as HTMLElement | null)?.dataset.key;
    trace.push(`${control.textContent} at ${list().total}/${list().shown}/${list().mode} ${first} ${active}`);
    await act(async () => {
      fireEvent.click(control);
      await settle();
    });
    // the page arrived ("Load more" is enabled again or gone)
    expect(screen.queryByTestId('chat.load-more')?.hasAttribute('disabled') ?? false).toBe(false);
    // the view is still there and renders a bounded window that holds focus where the move put it
    expect(screen.getByTestId('chat.timeline')).toBeInTheDocument();
    const rows = screen.getAllByTestId('chat.item');
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.length).toBeLessThanOrEqual(MAX_ROWS);
  }
  return used;
}

describe('a big task chat under the window controls', () => {
  it('stays shown through many earlier and load-more moves', { timeout: 120_000 }, async () => {
    const t = await taskWorld();
    const entries = dataSet(t, 1500);
    serve(t, entries);
    act(() => putTasks(t.store, [taskRow(t, {}) as TaskRow]));
    await act(async () => {
      mount(t, [{ path: '/t', element: <ChatTab taskId={TASK} orgId={t.org} /> }], '/t');
      await settle();
    });
    // the newest page is read and shown: the window offers earlier items
    expect(screen.getByTestId('chat.earlier')).toBeInTheDocument();
    const trace: string[] = [];
    const used = await useControls(60, trace);
    expect(used.filter((name) => name === 'chat.load-more').length, trace.join('\n')).toBeGreaterThan(5);
    expect(used.filter((name) => name === 'chat.earlier').length).toBeGreaterThan(5);
  });
});
