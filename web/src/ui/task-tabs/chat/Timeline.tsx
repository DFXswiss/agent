// The chat timeline (06-web.md section 5.2): one row per item of `core/chat/timeline.ts`, rendered by the chat-item
// registry (`ui/chat-items/<kind>.tsx`); kinds without a renderer are not shown (an event kind newer than this client
// is skipped quietly). Unverifiable events are a neutral row with the reader's code, never content. Subagent items are
// indented and labelled with their place in the session tree ("Subagent 1.2").
//
// Long chats (HOT-05): runs of file changes are one `file-group` row (`core/chat/files.ts`); only a window at the end
// of the list is rendered (`WINDOW` rows, "Show earlier items" adds `PAGE`), the window keeps its first row while new
// items arrive, rows are memoized on their content (`sameItem`) so a new event renders only the new row, and rows off
// screen skip layout and paint (`content-visibility: auto`; they stay in the accessibility tree and in the browser's
// find-in-page). "Jump to the latest item" scrolls to and focuses the last row.
import { memo, useMemo, useRef, useState } from 'react';
import { groupFiles, PAGE, sameItem, WINDOW, windowStart } from '../../../core/chat/files';
import type { TimelineItem } from '../../../core/chat/timeline';
import { formatNumber } from '../../../core/i18n';
import { useI18n } from '../../i18n';
import { chatItems } from '../../registries/chat-items';
import { EventTime } from './EventTime';

const RENDERERS = chatItems();

const ROW =
  '[contain-intrinsic-size:auto_4rem] [content-visibility:auto] focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-700';
const LINK = 'min-h-11 py-2 text-sm text-blue-800 underline';

const Row = memo(
  function Row({ item }: { readonly item: TimelineItem }) {
    const { t } = useI18n();
    if (item.kind === 'unverifiable')
      return (
        <li
          className={`${ROW} rounded border border-dashed border-gray-300 px-3 py-2 text-sm text-gray-500`}
          tabIndex={-1}
          data-testid="chat.item"
          data-kind="unverifiable"
          data-code={item.code!}
        >
          <EventTime at={item.at} /> {t('task.chat.unverifiable', { code: item.code! })}
        </li>
      );
    const renderer = RENDERERS.get(item.kind)!;
    const subagent = item.subagent;
    return (
      <li
        className={subagent === null ? ROW : `${ROW} ml-4 border-l-2 border-gray-200 pl-3 sm:ml-8`}
        tabIndex={-1}
        data-testid="chat.item"
        data-kind={item.kind}
        data-seq={item.seq}
      >
        {subagent !== null && (
          <span className="text-xs font-medium text-gray-600" data-testid="chat.item.subagent">
            {t('task.chat.subagent', { path: subagent.path.join('.') })}
          </span>
        )}
        <renderer.Component item={item} />
      </li>
    );
  },
  (previous, next) => sameItem(previous.item, next.item),
);

const shown = (item: TimelineItem): boolean => item.kind === 'unverifiable' || RENDERERS.has(item.kind);

export function Timeline({ items, empty }: { readonly items: readonly TimelineItem[]; readonly empty: string }) {
  const { t, locale } = useI18n();
  const list = useRef<HTMLOListElement>(null);
  const rows = useMemo(() => groupFiles(items.filter(shown)), [items]);
  const [view, setView] = useState<{ start: string | null; budget: number }>({ start: null, budget: WINDOW });
  const from = windowStart(rows, view.start, view.budget);
  const first = rows[from]?.key ?? null;
  // keep the first rendered row while new items arrive (state derived during render, no effect)
  if (first !== view.start) setView({ start: first, budget: view.budget });
  if (rows.length === 0)
    return (
      <p className="text-sm text-gray-600" data-testid="chat.empty">
        {empty}
      </p>
    );
  const latest = () => {
    setView({ start: null, budget: WINDOW });
    const last = list.current!.lastElementChild as HTMLElement;
    last.scrollIntoView({ block: 'end' });
    last.focus({ preventScroll: true });
  };
  return (
    <div className="flex flex-col gap-3">
      {from > 0 && (
        <div className="flex flex-wrap gap-x-4">
          <button
            type="button"
            className={LINK}
            onClick={() => setView({ start: rows[Math.max(0, from - PAGE)]!.key, budget: view.budget + PAGE })}
            data-testid="chat.earlier"
          >
            {t('chat-list.earlier', { count: formatNumber(from, locale) })}
          </button>
          <button type="button" className={LINK} onClick={latest} data-testid="chat.latest">
            {t('chat-list.latest')}
          </button>
        </div>
      )}
      <ol ref={list} className="flex flex-col gap-3" data-testid="chat.timeline" data-total={rows.length}>
        {rows.slice(from).map((item) => (
          <Row key={item.key} item={item} />
        ))}
      </ol>
    </div>
  );
}
