// The history drawer of an entry (06-web.md section 6.1): every version (`GET /v1/whiteboards/{entry}/history`, read by
// the event reader) ascending, each with what happened, the author, the time (`HH:MM:SS.mmm`) and the line diff to
// the previous version. A dialog (focus trapped, Escape closes) shown as a full-screen sheet on phones.
import { useEffect } from 'react';
import { loadHistory } from '../../core/whiteboards/actions';
import type { BoardLocation } from '../../core/whiteboards/location';
import type { BoardEntry } from '../../core/whiteboards/project';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { Dialog } from '../screens/members/Dialog';
import { useAppEnv } from '../shell/context';
import { LineDiff } from './LineDiff';
import { Time } from './Time';

const ACTION: Readonly<Record<string, string>> = {
  'whiteboard.entry': 'whiteboard.history.created',
  'whiteboard.edit': 'whiteboard.history.edited',
  'whiteboard.delete': 'whiteboard.history.deleted',
};

export interface HistoryProps {
  readonly location: BoardLocation;
  readonly entry: BoardEntry;
  readonly name: (principal: string) => string;
  readonly onClose: () => void;
}

export function History({ location, entry, name, onClose }: HistoryProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const load = useStore((state) => state.whiteboards.histories[entry.id] ?? null, app.store);
  useEffect(() => {
    void loadHistory(app.tasks, location, entry.id);
  }, [app, location, entry.id]);
  return (
    <Dialog title={t('whiteboard.history.title')} testId="whiteboard.history" onClose={onClose}>
      {load?.status === 'error' && (
        <p role="alert" className="text-sm text-red-800" data-testid="whiteboard.history.error">
          {error(load.code!).title}
        </p>
      )}
      <ol className="flex flex-col gap-4">
        {entry.versions.map((version, index) => (
          <li key={version.eventId} className="flex flex-col gap-1" data-testid="whiteboard.history.version">
            <div className="flex flex-wrap items-baseline gap-2 text-sm">
              <span className="font-medium" data-testid="whiteboard.history.action">
                {t(ACTION[version.kind]!)}
              </span>
              <span data-testid="whiteboard.history.author">{name(version.author)}</span>
              <Time at={version.at} testId="whiteboard.history.time" />
            </div>
            <LineDiff before={entry.versions[index - 1]?.text ?? ''} after={version.text ?? ''} />
          </li>
        ))}
      </ol>
      <button
        type="button"
        className="min-h-11 self-end rounded border border-gray-300 px-4"
        onClick={onClose}
        data-testid="whiteboard.history.close"
      >
        {t('whiteboard.history.close')}
      </button>
    </Dialog>
  );
}
