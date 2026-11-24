// The chat timeline (06-web.md section 5.2): one row per item of `core/chat/timeline.ts`, rendered by the chat-item
// registry (`ui/chat-items/<kind>.tsx`); kinds without a renderer are not shown (an event kind newer than this client
// is skipped quietly). Unverifiable events are a neutral row with the reader's code, never content. Subagent items are
// indented and labelled with their place in the session tree ("Subagent 1.2").
//
// Long chats (HOT-05, ADR-063 section 1): runs of file changes are one `file-group` row (`core/chat/files.ts`); the
// rows are a `WindowedList` in follow mode at the end (`WINDOW` rows at first, "Show earlier items" adds `PAGE`, at
// most `MAX_ROWS`), memoized on their content (`sameItem`) so a new event renders only the new row. "Jump to the
// latest item" scrolls to and focuses the last row.
import type { TimelineItem } from '../../../core/chat/timeline';
import { groupFiles, sameItem } from '../../../core/chat/files';
import type { ListPaging } from '../../../core/lists/paging';
import { useMemo } from 'react';
import { useI18n } from '../../i18n';
import { WindowedList, type RowAttributes } from '../../lists/WindowedList';
import { chatItems } from '../../registries/chat-items';
import { EventTime } from './EventTime';

const RENDERERS = chatItems();

/** Rows rendered at first and after "Jump to the latest item", rows added by "Show earlier items", the cap. */
export const WINDOW = 100;
export const PAGE = 50;
export const MAX_ROWS = 150;

const SUBAGENT = 'ml-4 border-l-2 border-gray-200 pl-3 sm:ml-8';
const UNVERIFIABLE = 'rounded border border-dashed border-gray-300 px-3 py-2 text-sm text-gray-500';

function Row({ item }: { readonly item: TimelineItem }) {
  const { t } = useI18n();
  if (item.kind === 'unverifiable')
    return (
      <>
        <EventTime at={item.at} /> {t('task.chat.unverifiable', { code: item.code! })}
      </>
    );
  const renderer = RENDERERS.get(item.kind)!;
  const subagent = item.subagent;
  return (
    <>
      {subagent !== null && (
        <span className="text-xs font-medium text-gray-600" data-testid="chat.item.subagent">
          {t('task.chat.subagent', { path: subagent.path.join('.') })}
        </span>
      )}
      <renderer.Component item={item} />
    </>
  );
}

const key = (item: TimelineItem): string => item.key;

function attributes(item: TimelineItem): RowAttributes {
  if (item.kind === 'unverifiable')
    return {
      className: UNVERIFIABLE,
      'data-testid': 'chat.item',
      'data-kind': 'unverifiable',
      'data-code': item.code!,
    };
  return {
    className: item.subagent === null ? '' : SUBAGENT,
    'data-testid': 'chat.item',
    'data-kind': item.kind,
    'data-seq': item.seq,
  };
}

const shown = (item: TimelineItem): boolean => item.kind === 'unverifiable' || RENDERERS.has(item.kind);

export function Timeline({
  items,
  empty,
  listId,
  paging,
}: {
  readonly items: readonly TimelineItem[];
  readonly empty: string;
  readonly listId: 'task.chat' | 'session.chat';
  /** Server paging of earlier items, when the loader offers it. */
  readonly paging?: ListPaging;
}) {
  const rows = useMemo(() => groupFiles(items.filter(shown)), [items]);
  return (
    <WindowedList
      listId={listId}
      items={rows}
      getKey={key}
      Row={Row}
      same={sameItem}
      newest="end"
      size={WINDOW}
      page={PAGE}
      maxRows={MAX_ROWS}
      as="ol"
      className="flex flex-col gap-3"
      rowAttributes={attributes}
      testId="chat.timeline"
      testIds={{ older: 'chat.earlier', newest: 'chat.latest', newer: 'chat.later', loadMore: 'chat.load-more' }}
      labels={{ older: 'chat-list.earlier', newest: 'chat-list.latest' }}
      {...(paging !== undefined && { hasMore: paging.hasMore, loading: paging.loading, onLoadMore: paging.loadMore })}
      empty={
        <p className="text-sm text-gray-600" data-testid="chat.empty">
          {empty}
        </p>
      }
    />
  );
}
