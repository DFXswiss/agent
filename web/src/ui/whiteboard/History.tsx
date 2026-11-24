// The history drawer of an entry (06-web.md section 6.1): the versions (`GET /v1/whiteboards/{entry}/history`, read by
// the event reader) newest first, each with what happened, the author and the time (`HH:MM:SS.mmm`); the line diff to
// the previous version is collapsed and built only when the reader expands it. A dialog (focus trapped, Escape
// closes) shown as a full-screen sheet on phones.
//
// Volume (ADR-086 section 1): the versions are a `WindowedList` (newest first, `WINDOW` rows at first, `PAGE` more per
// move, at most `MAX_ROWS`) memoized on their content; the hub's history is read page by page, newest first, older
// pages on demand ("Load more" when the window reaches the loaded end, `historyPaging`).
import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { historyPaging, loadHistory } from '../../core/whiteboards/actions';
import type { BoardLocation } from '../../core/whiteboards/location';
import type { BoardEntry, Version } from '../../core/whiteboards/project';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { WindowedList } from '../lists/WindowedList';
import { Dialog } from '../screens/members/Dialog';
import { useAppEnv } from '../shell/context';
import { LineDiff } from './LineDiff';
import { Time } from './Time';

/** Versions rendered at first, added per move, and at most. */
export const WINDOW = 40;
export const PAGE = 20;
export const MAX_ROWS = 60;

const ACTION: Readonly<Record<string, string>> = {
  'whiteboard.entry': 'whiteboard.history.created',
  'whiteboard.edit': 'whiteboard.history.edited',
  'whiteboard.delete': 'whiteboard.history.deleted',
};

/** A version with the text before it (the previous version's, empty for the first). */
export interface VersionItem {
  readonly version: Version;
  readonly before: string;
}

/** The versions newest first, each with the text of the version before it. */
export function versionItems(versions: readonly Version[]): VersionItem[] {
  return versions.map((version, index) => ({ version, before: versions[index - 1]?.text ?? '' })).reverse();
}

export const versionKey = (item: VersionItem): string => item.version.eventId;

/** Whether two builds of a version render the same row. */
export function sameVersion(a: VersionItem, b: VersionItem): boolean {
  const x = a.version;
  const y = b.version;
  return (
    a.before === b.before &&
    x.eventId === y.eventId &&
    x.kind === y.kind &&
    x.author === y.author &&
    x.at === y.at &&
    x.text === y.text
  );
}

const versionAttributes = () => ({ className: 'flex flex-col gap-1', 'data-testid': 'whiteboard.history.version' });

/** How the rows name an author (provided by the drawer). */
const NameContext = createContext<((principal: string) => string) | null>(null);

function VersionRow({ item }: { readonly item: VersionItem }) {
  const { t } = useI18n();
  const name = useContext(NameContext)!;
  const [open, setOpen] = useState(false);
  const { version } = item;
  return (
    <>
      <div className="flex flex-wrap items-baseline gap-2 text-sm">
        <span className="font-medium" data-testid="whiteboard.history.action">
          {t(ACTION[version.kind]!)}
        </span>
        <span data-testid="whiteboard.history.author">{name(version.author)}</span>
        <Time at={version.at} testId="whiteboard.history.time" />
      </div>
      <button
        type="button"
        className="min-h-11 min-w-11 self-start text-sm text-blue-800 underline"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        data-testid="whiteboard.history.diff.toggle"
      >
        {t(open ? 'whiteboard.history.diff.hide' : 'whiteboard.history.diff.show')}
      </button>
      {open && <LineDiff before={item.before} after={version.text ?? ''} />}
    </>
  );
}

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
  const items = useMemo(() => versionItems(entry.versions), [entry.versions]);
  const paging = useMemo(() => historyPaging(app.tasks, location, entry.id, load), [app, location, entry.id, load]);
  return (
    <Dialog title={t('whiteboard.history.title')} testId="whiteboard.history" onClose={onClose}>
      {load?.status === 'error' && (
        <p role="alert" className="text-sm text-red-800" data-testid="whiteboard.history.error">
          {error(load.code!).title}
        </p>
      )}
      <NameContext.Provider value={name}>
        <WindowedList
          listId="whiteboard.history"
          items={items}
          getKey={versionKey}
          Row={VersionRow}
          same={sameVersion}
          newest="start"
          size={WINDOW}
          page={PAGE}
          maxRows={MAX_ROWS}
          as="ol"
          className="flex flex-col gap-4"
          rowAttributes={versionAttributes}
          testId="whiteboard.history.list"
          labels={{
            older: 'whiteboard.history.older',
            newer: 'whiteboard.history.newer',
            newest: 'whiteboard.history.newest',
          }}
          hasMore={paging.hasMore}
          loading={paging.loading}
          onLoadMore={paging.loadMore}
        />
      </NameContext.Provider>
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
