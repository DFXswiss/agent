// One whiteboard (06-web.md section 6.1; shared by the org and workgroup screens and the task tab): the entries newest
// first with the decrypted Markdown, author, time and "edited"; "New entry" with the optional notification, Edit and
// Delete only with the board's write right (org level: admin; workgroup and task level: wg write) – without it the
// controls are not rendered and a notice says so; and the history drawer with the line diff.
// New entries of others appear live: their events arrive over the org channel and enter the projection.
import { useEffect, useMemo, useState } from 'react';
import { createEntry, deleteEntry, editEntry, loadBoard } from '../../core/whiteboards/actions';
import { type BoardLocation, boardKey, boardScope, canWriteBoard } from '../../core/whiteboards/location';
import { type BoardEntry, projectBoard } from '../../core/whiteboards/project';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useDirectory, useOrgView } from '../screens/tasks/hooks';
import { useAppEnv } from '../shell/context';
import { Editor } from './Editor';
import { EntryItem } from './EntryItem';
import { History } from './History';

/** `location` must be stable (memoized by the caller): a new object reloads the board. */
export function Board({ location }: { readonly location: BoardLocation }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const { state, userId } = useOrgView(location.org);
  const directory = useDirectory(location.org, state !== null);
  const key = boardKey(location);
  const { scopeId } = boardScope(location);
  const scope = useStore((s) => s.events[scopeId], app.store);
  const load = useStore((s) => s.whiteboards.boards[key] ?? null, app.store);
  const [creating, setCreating] = useState(false);
  const [history, setHistory] = useState<BoardEntry | null>(null);
  useEffect(() => {
    void loadBoard(app.tasks, location);
  }, [app, location]);
  const rows = load?.rows;
  const checked = load?.checked ?? null;
  const view = useMemo(() => projectBoard(scope, location, rows ?? [], checked), [scope, location, rows, checked]);
  const writer = canWriteBoard(state, userId, location);
  const name = (principal: string) =>
    directory.members.get(principal) ?? t('whiteboard.author.unnamed', { id: principal.slice(0, 8) });
  const actions = {
    writer,
    name,
    onEdit: (entry: BoardEntry, text: string) => editEntry(app.tasks, location, entry, text),
    onDelete: (entry: BoardEntry) => deleteEntry(app.tasks, location, entry),
    onHistory: (entry: BoardEntry) => setHistory(entry),
  };
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
      <ul className="flex flex-col gap-3" data-testid="whiteboard.list">
        {view.items.map((item) => (
          <EntryItem key={item.key} item={item} actions={actions} />
        ))}
      </ul>
      {open !== null && <History location={location} entry={open} name={name} onClose={() => setHistory(null)} />}
    </section>
  );
}
