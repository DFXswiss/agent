// One whiteboard (06-web.md section 6.1; shared by the org and workgroup screens and the task tab): the entries newest
// first with the decrypted Markdown, author, time and "edited"; "New entry" with the optional notification, Edit and
// Delete only with the board's write right (org level: admin; workgroup and task level: wg write) – without it the
// controls are not rendered and a notice says so; and the history drawer with the line diff.
// New entries of others appear live: their events arrive over the org channel and enter the projection.
//
// Volume (ADR-086 section 1): the board selects only the whiteboard events of its scope (`select.ts`), so chat traffic
// in a workgroup scope does not re-project it; the entries are a `WindowedList` (newest first, `WINDOW` rows at first,
// `PAGE` more per move, at most `MAX_ROWS`: entries are heavy Markdown), memoized on their content; the hub's rows are
// read page by page on demand ("Load more" when the window reaches the loaded end, `boardPaging`).
import { useCallback, useEffect, useMemo, useState } from 'react';
import { boardPaging, createEntry, deleteEntry, editEntry, loadBoard } from '../../core/whiteboards/actions';
import { type BoardLocation, boardKey, boardScope, canWriteBoard } from '../../core/whiteboards/location';
import { type BoardEntry, projectBoard } from '../../core/whiteboards/project';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useDirectory, useOrgView } from '../screens/tasks/hooks';
import { useAppEnv } from '../shell/context';
import { WindowedList } from '../lists/WindowedList';
import { Editor } from './Editor';
import { type EntryActions, entryAttributes, EntryContext, EntryItem, itemKey, sameItem } from './EntryItem';
import { History } from './History';
import { personName } from '../names/names';
import { selectBoardEvents } from './select';

/** Entries rendered at first, added per move, and at most. */
export const WINDOW = 40;
export const PAGE = 20;
export const MAX_ROWS = 60;

export interface BoardProps {
  /** Must be stable (memoized by the caller): a new object reloads the board. */
  readonly location: BoardLocation;
}

export function Board({ location }: BoardProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const { state, userId } = useOrgView(location.org);
  const directory = useDirectory(location.org, state !== null);
  const key = boardKey(location);
  const { scopeId } = boardScope(location);
  const select = useMemo(() => selectBoardEvents(scopeId), [scopeId]);
  const scope = useStore(select, app.store);
  const load = useStore((s) => s.whiteboards.boards[key] ?? null, app.store);
  const [creating, setCreating] = useState(false);
  const [history, setHistory] = useState<BoardEntry | null>(null);
  useEffect(() => {
    void loadBoard(app.tasks, location);
  }, [app, location]);
  const rows = load?.rows;
  const checked = load?.checked ?? null;
  const more = load !== null && load.next !== null;
  const view = useMemo(
    () => projectBoard(scope, location, rows ?? [], checked, more),
    [scope, location, rows, checked, more],
  );
  const paging = useMemo(() => boardPaging(app.tasks, location, load), [app, location, load]);
  const writer = canWriteBoard(state, userId, location);
  const name = useCallback((principal: string) => personName(t, directory, principal), [directory, t]);
  const actions: EntryActions = useMemo(
    () => ({
      writer,
      name,
      onEdit: (entry: BoardEntry, text: string) => editEntry(app.tasks, location, entry, text),
      onDelete: (entry: BoardEntry) => deleteEntry(app.tasks, location, entry),
      onHistory: (entry: BoardEntry) => setHistory(entry),
    }),
    [writer, name, app, location],
  );
  // the drawer stays open (with the entry as opened) while an event of the entry is being re-read by the reader
  const open = history === null ? null : (view.entries.get(history.id) ?? history);
  return (
    <section className="flex flex-col gap-3" data-testid="whiteboard.board" data-level={location.level}>
      {writer && !creating && (
        <button
          type="button"
          className="min-h-11 self-start rounded bg-blue-700 px-4 text-white"
          onClick={() => setCreating(true)}
          data-testid="whiteboard.create"
        >
          {t('whiteboard.create')}
        </button>
      )}
      {writer && creating && (
        <Editor
          initial=""
          withNotify
          onSave={async (text, notify) => {
            await createEntry(app.tasks, location, text, notify);
            setCreating(false);
          }}
          onCancel={() => setCreating(false)}
        />
      )}
      {!writer && (
        <p className="text-sm text-gray-600" data-testid="whiteboard.readonly">
          {t('whiteboard.readonly')}
        </p>
      )}
      {load?.status === 'error' && (
        <div role="alert" className="flex items-center gap-2 text-red-800" data-testid="whiteboard.error">
          <span>{error(load.code!).title}</span>
          <button
            type="button"
            className="min-h-11 min-w-11 underline"
            onClick={() => void loadBoard(app.tasks, location)}
          >
            {t('whiteboard.reload')}
          </button>
        </div>
      )}
      {view.items.length === 0 && load?.status === 'ready' && (
        <p className="text-sm text-gray-600" data-testid="whiteboard.empty">
          {t('whiteboard.empty')}
        </p>
      )}
      <EntryContext.Provider value={actions}>
        <WindowedList
          listId="whiteboard.entries"
          items={view.items}
          getKey={itemKey}
          Row={EntryItem}
          same={sameItem}
          newest="start"
          size={WINDOW}
          page={PAGE}
          maxRows={MAX_ROWS}
          className="flex flex-col gap-3"
          rowAttributes={entryAttributes}
          rowHeight="lg"
          testId="whiteboard.list"
          hasMore={paging.hasMore}
          loading={paging.loading}
          onLoadMore={paging.loadMore}
        />
      </EntryContext.Provider>
      {open !== null && <History location={location} entry={open} name={name} onClose={() => setHistory(null)} />}
    </section>
  );
}
