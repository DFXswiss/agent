// One group of the situation view (06-web.md section 4.0): a windowed list paged by the hub. The group loads its first
// page when shown and again after a reconnect (KOR-11); at most `WINDOW` entries are in the DOM (HOT-05): "Show later
// entries" (also when the end of the list becomes visible) moves the window by `STEP`, loading the hub's next page when
// the loaded entries run out, and "Show earlier entries" moves it back. An empty group shows one short line. The titles
// of the shown entries are pulled once the window rests for `TITLES_MS` (during a live burst the streamed `task.meta`
// events bring them; a pull per new entry would cost a request each).
import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { loadGroup, loadGroupMore } from '../../../core/attention/load';
import { earlierStart, groupWindow, laterStart, selectTaskRows, STEP } from '../../../core/attention/view';
import type { OrgState } from '../../../core/log/state';
import { attentionGroup, type Category } from '../../../core/store/slices/attention';
import type { Directory } from '../../../core/tasks/directory';
import { titles } from '../../../core/tasks/list';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { AttentionItem } from './AttentionItem';

const LINK = 'min-h-11 self-start py-2 text-sm text-blue-800 underline';

/** The shown entries' titles are pulled after the window rested this long. */
export const TITLES_MS = 500;

export interface AttentionGroupProps {
  readonly org: string;
  readonly category: Category;
  readonly state: OrgState;
  readonly userId: string;
  readonly directory: Directory;
  readonly now: number;
  /** The number in the heading (from the counters; none for "Recently finished"). */
  readonly count?: number | null;
  /** Further content of the heading (a link). */
  readonly extra?: ReactNode;
}

export function AttentionGroup({ org, category, state, userId, directory, now, count, extra }: AttentionGroupProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const group = useStore((store) => attentionGroup(store.attention, org, category), app.store);
  const [view, setView] = useState<{ start: string | null; from: number }>({ start: null, from: 0 });
  const span = groupWindow(group.ids, view.start, view.from);
  // the window keeps its place when its first entry leaves the group (state derived during render, no effect)
  if (span.from !== view.from) setView({ start: span.from === 0 ? null : group.ids[span.from]!, from: span.from });
  const end = useRef<HTMLLIElement>(null);
  const shownKey = group.ids.slice(span.from, span.to).join(' ');
  const select = useMemo(() => selectTaskRows(shownKey === '' ? [] : shownKey.split(' ')), [shownKey]);
  const rows = useStore(select, app.store);
  useEffect(() => {
    if (rows.length === 0) return;
    const timer = setTimeout(() => void titles(app.tasks, rows), TITLES_MS);
    return () => clearTimeout(timer);
  }, [app, rows]);
  useEffect(() => {
    void loadGroup(app.tasks, org, category);
  }, [app, org, category]);
  useLiveReload(app.store, () => void loadGroup(app.tasks, org, category, false, true));
  const more = async () => {
    const loaded = () => attentionGroup(app.store.getState().attention, org, category);
    if (loaded().ids.length - span.to < STEP) await loadGroupMore(app.tasks, org, category);
    const ids = loaded().ids;
    const start = laterStart(ids, span.from);
    setView({ start, from: start === null ? 0 : ids.indexOf(start) });
  };
  const latest = useRef(more);
  useEffect(() => {
    latest.current = more;
  });
  const later = span.to < group.ids.length || group.nextBefore !== null;
  useEffect(() => {
    const target = end.current;
    if (!later || target === null || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void latest.current();
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [later]);
  const earlier = () => {
    const start = earlierStart(group.ids, span.from);
    setView({ start, from: start === null ? 0 : group.ids.indexOf(start) });
  };
  const headingId = `attention-group-${category}`;
  return (
    <section
      className="flex flex-col gap-2"
      aria-labelledby={headingId}
      data-testid={`attention.group.${category}`}
      data-total={group.ids.length}
      data-from={span.from}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={headingId} className="font-semibold">
          {t(`attention.group.${category}`)}
          {count !== undefined && count !== null && (
            <span className="ml-2 text-sm font-normal text-gray-600" data-testid="attention.group.count">
              ({count})
            </span>
          )}
        </h2>
        {extra}
      </div>
      {group.code !== null && (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-red-800" data-testid="attention.error.load">
          <span>{error(group.code).title}</span>
          <button
            type="button"
            className="min-h-11 underline"
            onClick={() => void loadGroup(app.tasks, org, category, group.ids.length > 0)}
            data-testid="attention.reload"
          >
            {t('attention.reload')}
          </button>
        </div>
      )}
      {(group.status === 'idle' || (group.status === 'loading' && group.ids.length === 0)) && (
        <p className="text-sm text-gray-600" data-testid="attention.loading">
          {t('attention.loading')}
        </p>
      )}
      {group.status === 'ready' && group.ids.length === 0 && (
        <p className="text-sm text-gray-600" data-testid="attention.empty">
          {t(`attention.empty.${category}`)}
        </p>
      )}
      {span.from > 0 && (
        <button type="button" className={LINK} onClick={earlier} data-testid="attention.earlier">
          {t('attention.earlier')}
        </button>
      )}
      {group.ids.length > 0 && (
        <ul className="flex flex-col" data-testid="attention.list" aria-busy={group.status === 'loading'}>
          {group.ids.slice(span.from, span.to).map((id) => (
            <AttentionItem
              key={id}
              org={org}
              id={id}
              category={category}
              state={state}
              userId={userId}
              directory={directory}
              now={now}
            />
          ))}
          {later && <li ref={end} aria-hidden="true" data-testid="attention.end" />}
        </ul>
      )}
      {later && (
        <button type="button" className={LINK} onClick={() => void more()} data-testid="attention.more">
          {span.to < group.ids.length ? t('attention.later') : t('attention.more')}
        </button>
      )}
    </section>
  );
}
