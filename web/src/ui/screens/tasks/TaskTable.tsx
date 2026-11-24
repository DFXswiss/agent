// The building blocks of every task list (06-web.md sections 5.1 and 5.1a; KOR-32): the rows of one or more
// workgroups merged newest activity first (`core/tasks/overview.ts`; one workgroup is the merge of one list), the error
// with a retry, the empty state given by the screen, and the state filter. Used by the task list of a workgroup, the
// home screen's recent tasks and the task overview of the organization.
//
// Volume (ADR-063 section 1): the rows are a `WindowedList` – at most `MAX_ROWS` rendered, newest first, "Load more"
// loads the next page on request only (no automatic infinite scroll). Titles and owners come from one selector for the
// list (`useRowInfo`); the rows are memoized on their content. The title search and "only mine" load at most
// `SCAN_PAGES` further pages per request and then say how many tasks were searched (`scan.ts`).
import { type ReactNode, useEffect, useId, useMemo } from 'react';
import { formatNumber } from '../../../core/i18n';
import type { ListId } from '../../../core/lists/ids';
import { TASK_STATES } from '../../../core/tasks/countdown';
import { PAGE_SIZE } from '../../../core/tasks/list';
import { loadOverview, loadOverviewMore, selectOverview } from '../../../core/tasks/overview';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { WindowedList } from '../../lists/WindowedList';
import { useAppEnv } from '../../shell/context';
import { useNow } from '../../countdown/useNow';
import { useDirectory, useOrgView, useRowInfo, useTitles } from './hooks';
import { type SearchScan, useSearchScan } from './scan';
import { sameTaskItem, TaskItem, type TaskItemData, taskKey, taskRowAttributes } from './TaskItem';

/** The most rows rendered at once. */
export const MAX_ROWS = 200;

export interface TaskTableProps {
  readonly org: string;
  /** The windowed list this table is (`tasks.list` for a workgroup and home, `tasks.overview` for the overview). */
  readonly listId?: ListId;
  /** The workgroups to list (only ones the user can read). */
  readonly wgs: readonly string[];
  /** State filter (null: all states). */
  readonly state: string | null;
  /** Only the tasks of this owner (null: everyone's). */
  readonly mine?: string | null;
  /** Title search (empty: none). */
  readonly search?: string;
  /** Show the workgroup of each row (lists of several workgroups). */
  readonly showWorkgroup?: boolean;
  /** Show at most this many rows and no paging (the home screen's recent tasks). */
  readonly limit?: number;
  /** What an empty list says. */
  readonly empty: ReactNode;
}

export function TaskTable({
  org,
  listId = 'tasks.list',
  wgs,
  state,
  mine = null,
  search = '',
  showWorkgroup = false,
  limit,
  empty,
}: TaskTableProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const { state: orgState } = useOrgView(org);
  const directory = useDirectory(org, orgState !== null);
  // the same array while the workgroups are the same (the lists load again only when they or the state change)
  const wgsKey = JSON.stringify(wgs);
  const lists = useMemo(() => JSON.parse(wgsKey) as string[], [wgsKey]);
  const query = useMemo(() => ({ org, wgs: lists, state, mine, search }), [org, lists, state, mine, search]);
  const select = useMemo(() => selectOverview(query), [query]);
  const view = useStore(select, app.store);
  const rows = useMemo(() => (limit === undefined ? view.rows : view.rows.slice(0, limit)), [view.rows, limit]);
  const info = useRowInfo(org, rows);
  const now = useNow(true, 30_000);
  // the search and "only mine" need the titles and owners of every merged row, not only of the shown ones
  useTitles(limit === undefined ? view.merged : rows);
  useEffect(() => {
    void loadOverview(app.tasks, lists, state);
  }, [app, lists, state]);
  useLiveReload(app.store, () => void loadOverview(app.tasks, query.wgs, query.state, true));
  const filtering = limit === undefined && (mine !== null || search.trim() !== '');
  const scan = useSearchScan(app.tasks, query, view, filtering);
  const unnamed = t('tasks.wg.unnamed');
  const items = useMemo(
    () =>
      rows.map((row): TaskItemData => {
        const { title, owner } = info.get(row.task_id)!;
        return {
          org,
          row,
          title,
          owner: (owner === null ? undefined : directory.members.get(owner)) ?? null,
          workgroup: showWorkgroup ? (directory.workgroups.get(row.wg_id) ?? unnamed) : null,
          now,
        };
      }),
    [rows, info, directory, org, showWorkgroup, unnamed, now],
  );
  // "Load more" of the plain list; while filtering, "Search further" loads the next pages instead
  const paging = limit === undefined && !filtering;
  return (
    <div className="flex flex-col gap-2" data-testid="tasks.table" aria-busy={view.busy || scan.loading}>
      {view.code !== null && (
        <div role="alert" className="flex items-center gap-2 text-red-800" data-testid="tasks.error">
          <span>{error(view.code).title}</span>
          <button
            type="button"
            className="min-h-11 underline"
            onClick={() => void loadOverview(app.tasks, view.failed, query.state)}
          >
            {t('tasks.reload')}
          </button>
        </div>
      )}
      {(view.status === 'loading' || (scan.loading && rows.length === 0)) && (
        <p className="text-gray-600" data-testid="tasks.list-loading">
          {t('tasks.loading')}
        </p>
      )}
      {view.status === 'ready' && view.code === null && rows.length === 0 && !scan.loading && (
        <div data-testid="tasks.empty">{empty}</div>
      )}
      <WindowedList
        listId={listId}
        items={items}
        getKey={taskKey}
        Row={TaskItem}
        same={sameTaskItem}
        newest="start"
        page={PAGE_SIZE}
        maxRows={MAX_ROWS}
        testId="tasks.list"
        testIds={{ loadMore: 'tasks.more' }}
        rowAttributes={taskRowAttributes}
        hasMore={paging && view.binding !== null}
        loading={view.busy}
        onLoadMore={() => void loadOverviewMore(app.tasks, query)}
      />
      {filtering && view.status === 'ready' && view.code === null && <ScanStatus scan={scan} />}
    </div>
  );
}

/** How far a search went, with "Search further" while a list has a further page. */
export function ScanStatus({ scan }: { readonly scan: SearchScan }) {
  const { t, locale } = useI18n();
  if (!scan.hasMore || scan.loading) return null;
  const scanned = formatNumber(scan.scanned, locale);
  return (
    <div className="flex flex-wrap items-center gap-x-2 text-sm text-gray-700" data-testid="tasks.search.status">
      <p>
        {scan.total === null
          ? t('tasks.search.scanned', { scanned })
          : t('tasks.search.scanned-of', { scanned, total: formatNumber(scan.total, locale) })}
      </p>
      <button
        type="button"
        className="min-h-11 min-w-11 text-blue-800 underline"
        onClick={scan.searchFurther}
        data-testid="tasks.search.further"
      >
        {t('tasks.search.further')}
      </button>
    </div>
  );
}

/** The state filter of a task list. */
export function StateFilter({ value, onChange }: { value: string | null; onChange: (state: string | null) => void }) {
  const { t } = useI18n();
  const id = useId();
  return (
    <div className="flex items-center gap-2">
      <label htmlFor={id} className="text-sm">
        {t('tasks.filter.label')}
      </label>
      <select
        id={id}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={value ?? ''}
        onChange={(event) => onChange(event.target.value === '' ? null : event.target.value)}
        data-testid="tasks.filter"
      >
        <option value="">{t('tasks.filter.all')}</option>
        {TASK_STATES.map((name) => (
          <option key={name} value={name}>
            {t(`tasks.state.${name}`)}
          </option>
        ))}
      </select>
    </div>
  );
}
