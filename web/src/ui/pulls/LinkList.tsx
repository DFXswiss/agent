// The linked pull requests of a workgroup or of one task (KOR-48): loaded newest first (`core/pulls/links.ts`), one
// `PullRow` each, the load error with "Try again", the empty text, and "Load more" while the hub has further pages.
// Shared by the workgroup's "Pull requests" view and the task's panel. The rows are a `WindowedList` (ADR-086 section
// 1, lists `pulls` and `task.pulls`): at most `MAX_ROWS` rendered – and only the rendered rows read their state from
// the device – over the hub's cursor pages.
import { useCallback, useEffect, useMemo } from 'react';
import { loadLinks } from '../../core/pulls/links';
import { linkList, listKey, type PullLink } from '../../core/pulls/state';
import type { StoreState } from '../../core/store/store';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { WindowedList } from '../lists/WindowedList';
import { MAX_ROWS, PAGE, sameValue } from '../screens/members/lists';
import { useAppEnv } from '../shell/context';
import { PullRow } from './PullRow';

const BUTTON = 'min-h-11 self-start rounded border border-gray-300 px-4';

/** The links of a list in its order (the same array while neither the list nor the links changed). */
export function selectLinks(key: string) {
  let last: { list: unknown; links: unknown; items: readonly PullLink[] } | null = null;
  return (state: StoreState): readonly PullLink[] => {
    const list = linkList(state.pulls, key);
    if (last !== null && last.list === list && last.links === state.pulls.links) return last.items;
    // a listed ID always has its link (`removeLink` takes it out of every list)
    const items = list.ids.map((id) => state.pulls.links[id]!);
    last = { list, links: state.pulls.links, items };
    return items;
  };
}

/** What a row shows. */
interface LinkEntry {
  readonly org: string;
  readonly link: PullLink;
  readonly writer: boolean;
  readonly showTask: boolean;
}

function LinkRow({ item }: { readonly item: LinkEntry }) {
  return <PullRow org={item.org} link={item.link} writer={item.writer} showTask={item.showTask} />;
}

const linkKey = (entry: LinkEntry) => `${entry.link.epoch} ${entry.link.tag}`;

export function LinkList({
  org,
  wg,
  task,
  writer,
}: {
  readonly org: string;
  readonly wg: string;
  /** Only the links of this task (the task's panel). */
  readonly task: string | null;
  readonly writer: boolean;
}) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const key = listKey(wg, task);
  const list = useStore((state) => linkList(state.pulls, key), app.store);
  const select = useMemo(() => selectLinks(key), [key]);
  const items = useStore(select, app.store);
  const entries = useMemo(
    () => items.map((link): LinkEntry => ({ org, link, writer, showTask: task === null })),
    [items, org, writer, task],
  );
  const more = useCallback(() => void loadLinks(app.tasks, org, wg, task, true), [app, org, wg, task]);
  useEffect(() => {
    void loadLinks(app.tasks, org, wg, task);
  }, [app, org, wg, task]);
  return (
    <div className="flex flex-col gap-2">
      {list.status === 'error' && (
        <div className="flex flex-col gap-2" role="alert" data-testid="pulls.error.list" data-code={list.code!}>
          <p className="text-sm text-red-800">{t('pulls.load-failed', { reason: error(list.code!).title })}</p>
          <button
            type="button"
            className={BUTTON}
            onClick={() => void loadLinks(app.tasks, org, wg, task)}
            data-testid="pulls.reload"
          >
            {t('pulls.reload')}
          </button>
        </div>
      )}
      {list.status === 'loading' && items.length === 0 && (
        <p className="text-sm text-gray-600" data-testid="pulls.loading">
          {t('pulls.loading')}
        </p>
      )}
      {list.status === 'ready' && items.length === 0 && (
        <p className="text-sm text-gray-700" data-testid="pulls.empty">
          {t(task === null ? 'pulls.empty' : 'pulls.empty.task')}
        </p>
      )}
      <WindowedList
        listId={task === null ? 'pulls' : 'task.pulls'}
        items={entries}
        getKey={linkKey}
        Row={LinkRow}
        same={sameValue}
        newest="start"
        page={PAGE}
        maxRows={MAX_ROWS}
        testId="pulls.list"
        testIds={{ loadMore: 'pulls.more' }}
        className="flex flex-col"
        rowHeight="lg"
        hasMore={list.nextCursor !== null}
        loading={list.status === 'loading'}
        onLoadMore={more}
      />
    </div>
  );
}
