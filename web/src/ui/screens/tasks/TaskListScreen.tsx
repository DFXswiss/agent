// Task list of a workgroup (06-web.md section 5.1, route `/o/:org/w/:wg`, right `wg:read`): newest first with the
// decrypted title, state, owner and last activity; a state filter; endless scrolling (the next page loads when the end
// of the list becomes visible, with a "Load more" button as fallback); the composer at the top for users with
// `wg:write`; a link to the workgroup settings (WEB-09). A workgroup the user cannot read is "not found" (existence is not revealed, 03-api.md section 1).
import { useEffect, useId, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { WG } from '../../../core/log/state';
import { TASK_STATES } from '../../../core/tasks/countdown';
import { loadTasks, selectListRows } from '../../../core/tasks/list';
import { taskList } from '../../../core/store/slices/tasks';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { Composer } from '../../composer/Composer';
import { useNow } from '../../countdown/useNow';
import { useDirectory, useOrgView, useTitles } from './hooks';
import { TaskItem } from './TaskItem';

export interface TaskListProps {
  readonly org: string;
  readonly wg: string;
  /** Show at most this many rows and no paging (the home screen's recent tasks). */
  readonly limit?: number;
}

/** The rows of a workgroup's list with paging (also the home screen's "recent tasks"). */
export function TaskList({ org, wg, limit }: TaskListProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const filterId = useId();
  const { state } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  const [filter, setFilter] = useState<string | null>(null);
  const list = useStore((s) => taskList(s.tasks, wg, filter), app.store);
  const all = useStore(selectListRows(wg, filter), app.store);
  const rows = limit === undefined ? all : all.slice(0, limit);
  const now = useNow(true, 30_000);
  const end = useRef<HTMLLIElement>(null);
  useTitles(rows);
  useEffect(() => {
    void loadTasks(app.tasks, wg, filter);
  }, [app, wg, filter]);
  useLiveReload(app.store, () => void loadTasks(app.tasks, wg, filter, false, true));
  const paging = limit === undefined && list.nextBefore !== null;
  useEffect(() => {
    const target = end.current;
    if (!paging || target === null || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void loadTasks(app.tasks, wg, filter, true);
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [app, wg, filter, paging]);
  return (
    <div className="flex flex-col gap-2">
      {limit === undefined && (
        <div className="flex items-center gap-2">
          <label htmlFor={filterId} className="text-sm">
            {t('tasks.filter.label')}
          </label>
          <select
            id={filterId}
            className="min-h-11 rounded border border-gray-300 px-2"
            value={filter ?? ''}
            onChange={(event) => setFilter(event.target.value === '' ? null : event.target.value)}
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
      )}
      {list.status === 'error' && (
        <div role="alert" className="flex items-center gap-2 text-red-800" data-testid="tasks.error">
          <span>{error(list.code!).title}</span>
          <button type="button" className="min-h-11 underline" onClick={() => void loadTasks(app.tasks, wg, filter)}>
            {t('tasks.reload')}
          </button>
        </div>
      )}
      {list.status === 'ready' && rows.length === 0 && (
        <p className="text-gray-600" data-testid="tasks.empty">
          {t('tasks.empty')}
        </p>
      )}
      <ul data-testid="tasks.list" aria-busy={list.status === 'loading'}>
        {rows.map((row) => (
          <TaskItem key={row.task_id} org={org} row={row} directory={directory} now={now} />
        ))}
        {paging && <li ref={end} aria-hidden="true" data-testid="tasks.end" />}
      </ul>
      {paging && (
        <button
          type="button"
          className="min-h-11 self-start underline"
          onClick={() => void loadTasks(app.tasks, wg, filter, true)}
          data-testid="tasks.more"
        >
          {t('tasks.more')}
        </button>
      )}
    </div>
  );
}

export function TaskListScreen() {
  const { org, wg } = useParams() as { readonly org: string; readonly wg: string };
  const { t } = useI18n();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  if (state === null) {
    return (
      <p className="p-4" data-testid="tasks.loading">
        {t('tasks.loading')}
      </p>
    );
  }
  if (userId === null || !state.canRead(userId, WG, wg)) {
    return (
      <p className="p-4" data-testid="tasks.not-found">
        {t('tasks.not-found')}
      </p>
    );
  }
  const name = directory.workgroups.get(wg) ?? t('tasks.wg.unnamed');
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="tasks.screen">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-xl font-semibold" data-testid="tasks.title">
          {name}
        </h1>
        <Link
          className="flex min-h-11 items-center text-sm text-blue-800 underline"
          to={`/o/${encodeURIComponent(org)}/w/${encodeURIComponent(wg)}/settings`}
          data-testid="tasks.settings"
        >
          {t('wg-settings.link')}
        </Link>
      </div>
      {state.canWriteWg(userId, wg) && <Composer org={org} wg={wg} />}
      <TaskList org={org} wg={wg} />
    </main>
  );
}
