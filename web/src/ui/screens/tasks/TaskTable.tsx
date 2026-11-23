// The building blocks of every task list (06-web.md section 5.1; KOR-26B): the rows of one or more workgroups merged
// newest activity first (`core/tasks/overview.ts`; one workgroup is the merge of one list), endless scrolling (the
// next page loads when the end of the list becomes visible, with a "Load more" button as fallback), the error with a
// retry, the empty state given by the screen, and the state filter. Used by the task list of a workgroup, the home
// screen's recent tasks and the task overview of the organization.
import { type ReactNode, useEffect, useId, useMemo, useRef } from 'react';
import { TASK_STATES } from '../../../core/tasks/countdown';
import { loadOverview, loadOverviewMore, selectOverview } from '../../../core/tasks/overview';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useNow } from '../../countdown/useNow';
import { useDirectory, useOrgView, useTitles } from './hooks';
import { TaskItem } from './TaskItem';

export interface TaskTableProps {
  readonly org: string;
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

export function TaskTable({ org, wgs, state, mine = null, search = '', showWorkgroup, limit, empty }: TaskTableProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const { state: orgState } = useOrgView(org);
  const directory = useDirectory(org, orgState !== null);
  const wgsKey = JSON.stringify(wgs);
  const query = useMemo(
    () => ({ org, wgs: JSON.parse(wgsKey) as string[], state, mine, search }),
    [org, wgsKey, state, mine, search],
  );
  const select = useMemo(() => selectOverview(query), [query]);
  const view = useStore(select, app.store);
  const rows = limit === undefined ? view.rows : view.rows.slice(0, limit);
  const now = useNow(true, 30_000);
  const end = useRef<HTMLLIElement>(null);
  // the search and "only mine" need the titles and owners of every merged row, not only of the shown ones
  useTitles(limit === undefined ? view.merged : rows);
  useEffect(() => {
    void loadOverview(app.tasks, query.wgs, query.state);
  }, [app, query.wgs, query.state]);
  useLiveReload(app.store, () => void loadOverview(app.tasks, query.wgs, query.state, true));
  const paging = limit === undefined && view.binding !== null;
  useEffect(() => {
    const target = end.current;
    if (!paging || target === null || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void loadOverviewMore(app.tasks, query);
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [app, query, paging]);
  return (
    <div className="flex flex-col gap-2">
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
      {view.status === 'loading' && (
        <p className="text-gray-600" data-testid="tasks.list-loading">
          {t('tasks.loading')}
        </p>
      )}
      {view.status === 'ready' && view.code === null && rows.length === 0 && (
        <div data-testid="tasks.empty">{empty}</div>
      )}
      <ul data-testid="tasks.list" aria-busy={view.busy}>
        {rows.map((row) => (
          <TaskItem
            key={row.task_id}
            org={org}
            row={row}
            directory={directory}
            now={now}
            {...(showWorkgroup && { workgroup: directory.workgroups.get(row.wg_id) ?? t('tasks.wg.unnamed') })}
          />
        ))}
        {paging && <li ref={end} aria-hidden="true" data-testid="tasks.end" />}
      </ul>
      {paging && (
        <button
          type="button"
          className="min-h-11 self-start underline"
          onClick={() => void loadOverviewMore(app.tasks, query)}
          data-testid="tasks.more"
        >
          {t('tasks.more')}
        </button>
      )}
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
