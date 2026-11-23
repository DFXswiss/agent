// One row of a board (06-web.md sections 1 and 6.1): an entry with its decrypted Markdown, author, updated time and the
// "edited" marker, Edit/Delete only for writers (not rendered otherwise), History for everybody; a deleted entry as a
// muted row that keeps its history; unverifiable, locked and not yet loaded entries as neutral rows with the reason –
// never content.
import { lazy, Suspense, useState } from 'react';
import { sendErrorCode } from '../../core/tasks/send';
import { type BoardEntry, type BoardItem, neutralLabel } from '../../core/whiteboards/project';
import { useI18n } from '../i18n';
import { ChunkBoundary } from '../lazy/ChunkBoundary';
import { Editor } from './Editor';
import { Time } from './Time';

const Markdown = lazy(() => import('../task-tabs/chat/Markdown'));

export interface EntryActions {
  readonly writer: boolean;
  readonly name: (principal: string) => string;
  readonly onEdit: (entry: BoardEntry, text: string) => Promise<void>;
  readonly onDelete: (entry: BoardEntry) => Promise<void>;
  readonly onHistory: (entry: BoardEntry) => void;
}

function Neutral({ item }: { readonly item: Exclude<BoardItem, BoardEntry> }) {
  const { t } = useI18n();
  const label = neutralLabel(item);
  return (
    <li
      className="rounded border border-dashed border-gray-300 p-3 text-sm text-gray-600"
      data-testid="whiteboard.item"
      data-type={item.type}
      data-code={label.code}
    >
      {t(label.key, label.params)}
    </li>
  );
}

function DeleteConfirm({
  entry,
  actions,
  onCancel,
}: {
  entry: BoardEntry;
  actions: EntryActions;
  onCancel: () => void;
}) {
  const { t, error } = useI18n();
  const [code, setCode] = useState<string | null>(null);
  const remove = async () => {
    setCode(null);
    try {
      await actions.onDelete(entry);
    } catch (failure) {
      setCode(sendErrorCode(failure));
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" data-testid="whiteboard.item.delete.dialog">
      <span className="text-sm">{t('whiteboard.delete.confirm')}</span>
      <button
        type="button"
        className="min-h-11 rounded bg-red-700 px-3 text-white"
        onClick={() => void remove()}
        data-testid="whiteboard.item.delete.confirm"
      >
        {t('whiteboard.delete.yes')}
      </button>
      <button
        type="button"
        className="min-h-11 rounded border border-gray-300 px-3"
        onClick={onCancel}
        data-testid="whiteboard.item.delete.cancel"
      >
        {t('whiteboard.delete.no')}
      </button>
      {code !== null && (
        <p role="alert" className="w-full text-sm text-red-800" data-testid="whiteboard.item.delete.error">
          {error(code).title}
        </p>
      )}
    </div>
  );
}

function Controls({ entry, actions, onEdit }: { entry: BoardEntry; actions: EntryActions; onEdit: () => void }) {
  const { t } = useI18n();
  const [confirm, setConfirm] = useState(false);
  if (confirm) return <DeleteConfirm entry={entry} actions={actions} onCancel={() => setConfirm(false)} />;
  return (
    <>
      <button type="button" className="min-h-11 min-w-11 underline" onClick={onEdit} data-testid="whiteboard.item.edit">
        {t('whiteboard.edit')}
      </button>
      <button
        type="button"
        className="min-h-11 min-w-11 text-red-800 underline"
        onClick={() => setConfirm(true)}
        data-testid="whiteboard.item.delete"
      >
        {t('whiteboard.delete')}
      </button>
    </>
  );
}

function Entry({ entry, actions }: { readonly entry: BoardEntry; readonly actions: EntryActions }) {
  const { t } = useI18n();
  const [editing, setEditing] = useState(false);
  return (
    <li
      className={`flex flex-col gap-2 rounded border p-3 ${entry.deleted ? 'border-gray-200 bg-gray-50' : 'border-gray-300'}`}
      data-testid="whiteboard.item"
      data-type="entry"
      data-entry={entry.id}
    >
      <div className="flex flex-wrap items-baseline gap-2 text-sm">
        <span className="font-medium" data-testid="whiteboard.item.author">
          {actions.name(entry.author)}
        </span>
        <Time at={entry.updatedAt} testId="whiteboard.item.time" />
        {entry.edited && (
          <span className="text-xs text-gray-500" data-testid="whiteboard.item.edited">
            {t('whiteboard.edited')}
          </span>
        )}
      </div>
      {entry.deleted && (
        <p className="text-sm italic text-gray-600" data-testid="whiteboard.item.deleted">
          {t('whiteboard.deleted')}
        </p>
      )}
      {!entry.deleted && editing && (
        <Editor
          initial={entry.text}
          withNotify={false}
          onSave={async (text) => {
            await actions.onEdit(entry, text);
            setEditing(false);
          }}
          onCancel={() => setEditing(false)}
        />
      )}
      {!entry.deleted && !editing && (
        <div data-testid="whiteboard.item.text">
          <ChunkBoundary fallback={<p className="whitespace-pre-wrap">{entry.text}</p>}>
            <Suspense fallback={<p className="whitespace-pre-wrap">{entry.text}</p>}>
              <Markdown text={entry.text} />
            </Suspense>
          </ChunkBoundary>
        </div>
      )}
      <div className="flex flex-wrap gap-3 text-sm">
        <button
          type="button"
          className="min-h-11 min-w-11 underline"
          onClick={() => actions.onHistory(entry)}
          data-testid="whiteboard.item.history"
        >
          {t('whiteboard.history')}
        </button>
        {actions.writer && !entry.deleted && !editing && (
          <Controls entry={entry} actions={actions} onEdit={() => setEditing(true)} />
        )}
      </div>
    </li>
  );
}

export function EntryItem({ item, actions }: { readonly item: BoardItem; readonly actions: EntryActions }) {
  return item.type === 'entry' ? <Entry entry={item} actions={actions} /> : <Neutral item={item} />;
}
