// U-WEB-ui-whiteboard-volume (ADR-086 section 1): a large board stays small in the DOM – the entries are a window
// (newest first, at most `MAX_ROWS`), memoized on their content, with the Markdown only near the viewport and further
// pages on demand; the board selects only the whiteboard events of its scope; the history is a window of versions
// whose diffs are collapsed, built on demand, reduced to the changed hunks and clipped.
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { uuid7 } from '../../src/core/encoding/ids';
import { createI18n } from '../../src/core/i18n';
import { ORG } from '../../src/core/log/state';
import type { StoreState } from '../../src/core/store';
import { type EventEntry, putEvents } from '../../src/core/store/slices/events';
import { type BoardLocation, orgBoard } from '../../src/core/whiteboards/location';
import type { BoardEntry, BoardItem, Version } from '../../src/core/whiteboards/project';
import { I18nProvider } from '../../src/ui/i18n';
import { CLIP_CHARS } from '../../src/ui/lists/Clipped';
import { resetNearViewport } from '../../src/ui/lists/near';
import { Board, MAX_ROWS, PAGE, WINDOW } from '../../src/ui/whiteboard/Board';
import { entryAttributes, sameItem } from '../../src/ui/whiteboard/EntryItem';
import {
  MAX_ROWS as HISTORY_MAX,
  sameVersion,
  versionItems,
  WINDOW as HISTORY_WINDOW,
} from '../../src/ui/whiteboard/History';
import { LineDiff } from '../../src/ui/whiteboard/LineDiff';
import { selectBoardEvents } from '../../src/ui/whiteboard/select';
import { type BoardWorld, boardWorld } from '../core/whiteboards/world';
import { mount } from './chat-world';

const empty = { events: [], next_after: null, head: 0 };

/** The admin's org board with no rows on the hub (`next`: an older page follows); entries are put into the store. */
async function board(next: string | null = null) {
  const w = await boardWorld();
  w.client.on('GET /v1/whiteboards', (_body, target) =>
    target.includes('before=') ? { entries: [], next_before: null } : { entries: [], next_before: next },
  );
  w.client.on(`GET /v1/scopes/${w.org}/events`, () => empty);
  const location: BoardLocation = orgBoard(w.org);
  mount(w, [{ path: '/b', element: <Board location={location} /> }], '/b');
  // the load completed: events put from now on are newer than what the rows were checked against
  await screen.findByTestId('whiteboard.empty');
  return w;
}

/** A verified whiteboard event of the org board at `seq`. */
function event(w: BoardWorld, seq: number, kind: string, ref: string, body: unknown): EventEntry {
  const header = {
    id: uuid7(1_790_000_000_000 + seq),
    kind,
    org: w.org,
    scope: ORG,
    scope_id: w.org,
    wg: '',
    task: '',
    ref,
    author: w.ada.id,
    ts: '2026-11-21T12:00:05.000Z',
  };
  return {
    org: w.org,
    scopeId: w.org,
    scopeSeq: seq,
    receivedAt: null,
    event: { header },
    status: 'verified',
    code: null,
    header,
    body,
  };
}

const entries = (w: BoardWorld, count: number) =>
  Array.from({ length: count }, (_, index) =>
    event(w, index + 1, 'whiteboard.entry', `ref-${index + 1}`, { level: 'org', text: `Entry **${index + 1}**` }),
  );

const put = (w: BoardWorld, items: EventEntry[]) => act(() => putEvents(w.store, w.org, items));
const items = () => screen.queryAllByTestId('whiteboard.item');

describe('board window', () => {
  it('renders a window of a large board, newest first, and never more than the cap', async () => {
    const w = await board();
    put(w, entries(w, 500));
    const list = screen.getByTestId('whiteboard.list');
    expect(list).toHaveAttribute('data-total', '500');
    expect(items()).toHaveLength(WINDOW);
    expect(items()[0]).toHaveAttribute('data-entry', 'ref-500');
    expect(screen.getByTestId('whiteboard.list.older')).toHaveTextContent(`${500 - WINDOW} more entries – show`);
    fireEvent.click(screen.getByTestId('whiteboard.list.older'));
    expect(items()).toHaveLength(WINDOW + PAGE);
    fireEvent.click(screen.getByTestId('whiteboard.list.older'));
    expect(items()).toHaveLength(MAX_ROWS);
    expect(screen.getByTestId('whiteboard.list.newer')).toBeInTheDocument();
    // with every row listed there is no "Load more"
    expect(screen.queryByTestId('whiteboard.list.load-more')).toBeNull();
  });

  it('reads further pages of rows and versions from the hub on demand', async () => {
    const w = await board('c1');
    put(w, entries(w, 3));
    expect(w.client.of('GET /v1/whiteboards')).toHaveLength(1);
    fireEvent.click(screen.getByTestId('whiteboard.list.load-more'));
    await waitFor(() => expect(screen.queryByTestId('whiteboard.list.load-more')).toBeNull());
    expect(w.client.of('GET /v1/whiteboards')[1]!.target).toContain('before=c1');
    w.client.on('GET /v1/whiteboards/ref-3/history', (_body, target) => ({
      events: [],
      next_before: target.includes('before=') ? null : 1,
    }));
    fireEvent.click(within(items()[0]!).getByTestId('whiteboard.item.history'));
    const dialog = await screen.findByTestId('whiteboard.history');
    fireEvent.click(await within(dialog).findByTestId('whiteboard.history.list.load-more'));
    await waitFor(() => expect(within(dialog).queryByTestId('whiteboard.history.list.load-more')).toBeNull());
    expect(w.client.of('GET /v1/whiteboards/ref-3/history').map((call) => call.target)).toEqual([
      '/v1/whiteboards/ref-3/history?limit=50',
      '/v1/whiteboards/ref-3/history?limit=50&before=1',
    ]);
  });

  it('shows the Markdown only once the entry comes near the viewport', async () => {
    const original = globalThis.IntersectionObserver;
    class Never {
      observe() {}
      unobserve() {}
    }
    globalThis.IntersectionObserver = Never as unknown as typeof IntersectionObserver;
    try {
      const w = await board();
      put(w, entries(w, 2));
      const text = within(items()[0]!).getByTestId('whiteboard.item.text');
      expect(within(text).getByTestId('list.placeholder')).toHaveTextContent('Entry **2**');
      expect(within(text).queryByTestId('markdown')).toBeNull();
    } finally {
      globalThis.IntersectionObserver = original;
      resetNearViewport();
    }
  });
});

describe('history window', () => {
  it('renders the versions newest first in a window with collapsed diffs built on demand', async () => {
    // "Jump to the newest version" scrolls the newest row into view (jsdom has no layout)
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    const w = await board();
    const base = uuid7();
    put(w, [
      event(w, 1, 'whiteboard.entry', 'long', { level: 'org', text: 'v1' }),
      ...Array.from({ length: 99 }, (_, index) =>
        event(w, index + 2, 'whiteboard.edit', 'long', { text: `v${index + 2}`, base_event_id: base }),
      ),
    ]);
    w.client.on(`GET /v1/whiteboards/long/history`, () => ({ events: [] }));
    fireEvent.click(within(items()[0]!).getByTestId('whiteboard.item.history'));
    const dialog = await screen.findByTestId('whiteboard.history');
    const versions = () => within(dialog).getAllByTestId('whiteboard.history.version');
    expect(versions()).toHaveLength(HISTORY_WINDOW);
    expect(within(versions()[0]!).getByTestId('whiteboard.history.action')).toHaveTextContent('Edited');
    expect(within(dialog).getByTestId('whiteboard.history.list.older')).toHaveTextContent(
      `${100 - HISTORY_WINDOW} older versions – show`,
    );
    for (let move = 0; move < 3; move += 1)
      fireEvent.click(within(dialog).getByTestId('whiteboard.history.list.older'));
    expect(versions()).toHaveLength(HISTORY_MAX);
    expect(within(dialog).getByTestId('whiteboard.history.list.newer')).toHaveTextContent(/newer versions – show/);
    fireEvent.click(within(dialog).getByTestId('whiteboard.history.list.newest'));
    expect(scroll).toHaveBeenCalledOnce();
    expect(versions()).toHaveLength(HISTORY_WINDOW);
    // no diff is built before it is expanded
    expect(within(dialog).queryByTestId('whiteboard.diff')).toBeNull();
    const toggle = within(versions()[0]!).getByTestId('whiteboard.history.diff.toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveTextContent('Show changes');
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveTextContent('Hide changes');
    const diff = within(versions()[0]!).getByTestId('whiteboard.diff');
    expect(within(diff).getByTestId('whiteboard.diff.stats')).toHaveTextContent('+1 / −1 lines');
    expect(
      within(diff)
        .getAllByTestId('whiteboard.diff.line')
        .map((line) => line.textContent),
    ).toEqual(['@@ -1,1 +1,1 @@', '− v99', '+ v100']);
    fireEvent.click(toggle);
    expect(within(dialog).queryByTestId('whiteboard.diff')).toBeNull();
  });
});

describe('line diff', () => {
  const show = (before: string, after: string) =>
    render(
      <I18nProvider value={createI18n('en')}>
        <LineDiff before={before} after={after} />
      </I18nProvider>,
    );

  it('shows only the changed hunks with three lines of context', () => {
    const lines = (change: string) =>
      Array.from({ length: 100 }, (_, index) => (index === 49 ? change : `line ${index + 1}`)).join('\n');
    show(lines('old'), lines('new'));
    const shown = screen.getAllByTestId('whiteboard.diff.line');
    expect(shown.map((line) => line.dataset.op)).toEqual([
      'hunk',
      'same',
      'same',
      'same',
      'del',
      'add',
      'same',
      'same',
      'same',
    ]);
    expect(screen.queryByTestId('whiteboard.diff.more')).toBeNull();
  });

  it('says when no line changed', () => {
    show('same', 'same');
    expect(screen.getByTestId('whiteboard.diff.none')).toHaveTextContent('No lines changed.');
    expect(screen.queryByTestId('whiteboard.diff.line')).toBeNull();
  });

  it('clips a very long line with "Show more" and "Download"', () => {
    show('short', 'x'.repeat(CLIP_CHARS * 2));
    const text = screen
      .getAllByTestId('whiteboard.diff.line')
      .map((line) => line.textContent)
      .join('\n');
    expect(text).toHaveLength(CLIP_CHARS);
    expect(screen.getByTestId('whiteboard.diff.download')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('whiteboard.diff.more'));
    expect(screen.getAllByTestId('whiteboard.diff.line').at(-1)!.textContent!.length).toBeGreaterThan(CLIP_CHARS);
  });
});

describe('rows and selector', () => {
  const entry: BoardEntry = {
    type: 'entry',
    key: 'ea',
    id: 'a',
    seq: 1,
    author: 'u',
    createdAt: 1,
    updatedAt: 1,
    text: 'x',
    edited: false,
    deleted: false,
    latestEventId: 'e1',
    versions: [],
  };
  const neutral: BoardItem = { type: 'unverifiable', key: 'n2', seq: 2, code: 'c', ref: 'r' };
  const missing: BoardItem = { type: 'missing', key: 'pa', seq: 3, ref: 'a' };

  it('compares rows on their content', () => {
    expect(sameItem(entry, { ...entry, versions: [] })).toBe(true);
    for (const change of [
      { type: 'locked' as const },
      { key: 'eb' },
      { seq: 9 },
      { text: 'y' },
      { author: 'v' },
      { updatedAt: 2 },
      { edited: true },
      { deleted: true },
      { latestEventId: 'e2' },
    ])
      expect(sameItem(entry, { ...entry, ...change } as BoardItem)).toBe(false);
    expect(sameItem(neutral, { ...neutral })).toBe(true);
    expect(sameItem(neutral, { ...neutral, code: 'd' })).toBe(false);
    expect(sameItem(neutral, { ...neutral, ref: 'q' })).toBe(false);
    expect(sameItem(missing, { ...missing })).toBe(true);
    expect(sameItem(missing, { ...missing, ref: 'b' })).toBe(false);
  });

  it('puts the test hooks and the look on the row', () => {
    expect(entryAttributes(entry)).toMatchObject({ 'data-type': 'entry', 'data-entry': 'a' });
    expect(entryAttributes({ ...entry, deleted: true }).className).toContain('bg-gray-50');
    expect(entryAttributes(neutral)).toMatchObject({ 'data-type': 'unverifiable', 'data-code': 'c' });
    expect(entryAttributes(missing)).not.toHaveProperty('data-code');
  });

  it('pairs versions with the text before them, newest first', () => {
    const version = (eventId: string, text: string | null): Version => ({
      eventId,
      seq: 1,
      kind: 'whiteboard.edit',
      author: 'u',
      at: 1,
      text,
    });
    const built = versionItems([version('1', 'a'), version('2', 'b'), version('3', null)]);
    expect(built.map((item) => [item.version.eventId, item.before])).toEqual([
      ['3', 'b'],
      ['2', 'a'],
      ['1', ''],
    ]);
    const [first] = built;
    expect(sameVersion(first!, { ...first!, version: { ...first!.version } })).toBe(true);
    expect(sameVersion(first!, { ...first!, before: 'x' })).toBe(false);
    for (const change of [{ eventId: '9' }, { kind: 'x' }, { author: 'v' }, { at: 2 }, { text: 'z' }])
      expect(sameVersion(first!, { ...first!, version: { ...first!.version, ...change } })).toBe(false);
  });

  it('selects only the whiteboard events of the scope and keeps them while they are unchanged', () => {
    const select = selectBoardEvents('s');
    const stateOf = (entries: Map<number, EventEntry> | null) =>
      ({ events: entries === null ? {} : { s: { entries } } }) as unknown as StoreState;
    const kind = (seq: number, value: string): EventEntry =>
      ({
        scopeSeq: seq,
        header: null,
        event: { header: { kind: value } },
        status: 'pending',
      }) as unknown as EventEntry;
    expect(select(stateOf(null))).toBeUndefined();
    const board = kind(1, 'whiteboard.entry');
    const first = select(stateOf(new Map([[1, board]])));
    expect([...first!.entries.keys()]).toEqual([1]);
    // a chat event in the scope (a new scope object, the same whiteboard events): the same result
    const scope = new Map([
      [1, board],
      [2, kind(2, 'session.output')],
    ]);
    const state = stateOf(scope);
    expect(select(state)).toBe(first);
    // the same scope object again: not even scanned
    expect(select(state)).toBe(first);
    // a new whiteboard event or a changed one: a new result
    const added = select(stateOf(new Map([...scope, [3, kind(3, 'whiteboard.edit')]])));
    expect(added).not.toBe(first);
    expect([...added!.entries.keys()]).toEqual([1, 3]);
    const changed = select(
      stateOf(
        new Map([
          [1, kind(1, 'whiteboard.entry')],
          [3, added!.entries.get(3)!],
        ]),
      ),
    );
    expect(changed).not.toBe(added);
    expect(select(stateOf(null))).toBeUndefined();
  });
});
