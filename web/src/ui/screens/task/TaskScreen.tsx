// The task page (06-web.md sections 2.4 and 5, route `/o/:org/t/:task`, right `task:read`): title and state, the task
// menu with "Close task" for writers, "Renamed by session" after a `task.set_title` (WEB-09), and the tab frame. Tabs are discovered at `ui/task-tabs/<area>/index.tsx`
// (ADR-027 section 4): Chat (default) and Sessions here; Operations, Whiteboard and Audit join from their packages.
// The send sequence of the home screen navigates here at once (WEB-04); the countdown is part of the chat. The header
// names device and VM of the main session ("on <device> · vm-…", ADR-060 section 6).
import { useEffect, useMemo, useState } from 'react';
import { useLocation, useParams } from 'react-router-dom';
import { closeTask, loadChat } from '../../../core/chat/actions';
import { selectNewestTitle, selectRenamed } from '../../../core/operations/renamed';
import type { TaskRow } from '../../../core/store/slices/tasks';
import { loadTask, sendErrorCode } from '../../../core/tasks/send';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { taskTabs } from '../../registries/task-tabs';
import { useAppEnv } from '../../shell/context';
import { LoadError } from '../errors/pages';
import { useDirectory, useOrgView, useTitle } from '../tasks/hooks';
import { activeTab, Tabs } from './Tabs';
import { Unlocking } from '../../unlock/Unlocking';
import { sessionVm } from '../../../core/tasks/placement';
import { useDeviceNames } from '../devices/labels';
import { placementText } from '../../task-tabs/sessions/SessionsTab';

/** Where the task's main session runs (nothing before it has a device). */
function TaskPlacement({ row }: { readonly row: TaskRow }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const name = useDeviceNames([row.org_id]);
  const session = useStore(
    (state) => (row.main_session_id === null ? null : (state.tasks.sessions[row.main_session_id]?.row ?? null)),
    app.store,
  );
  if (session?.device_id == null) return null;
  const vm = sessionVm(session);
  return (
    <span
      className="text-sm text-gray-600"
      data-testid="task.placement"
      data-device={session.device_id}
      data-vm={vm ?? ''}
    >
      {t('task.sessions.device', { device: placementText(name(session.device_id), vm) })}
    </span>
  );
}

const TABS = taskTabs();

function TaskMenu({ task }: { readonly task: TaskRow }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const [confirm, setConfirm] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  if (task.state === 'closed') return null;
  const close = async () => {
    setCode(null);
    try {
      await closeTask(app.tasks, task.task_id);
      setConfirm(false);
    } catch (failure) {
      setCode(sendErrorCode(failure));
    }
  };
  return (
    <div className="flex flex-col items-end gap-2" data-testid="task.menu">
      {confirm ? (
        <div className="flex flex-wrap items-center justify-end gap-2" role="group" aria-label={t('task.menu')}>
          <span className="text-sm">{t('task.close.confirm')}</span>
          <button
            type="button"
            className="min-h-11 rounded bg-red-700 px-3 text-white"
            onClick={() => void close()}
            data-testid="task.close.confirm"
          >
            {t('task.close.yes')}
          </button>
          <button
            type="button"
            className="min-h-11 rounded border border-gray-300 px-3"
            onClick={() => setConfirm(false)}
            data-testid="task.close.cancel"
          >
            {t('task.close.no')}
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="min-h-11 rounded border border-gray-300 px-3 text-sm"
          onClick={() => setConfirm(true)}
          data-testid="task.close"
        >
          {t('task.close')}
        </button>
      )}
      {code !== null && (
        <p className="text-sm text-red-800" role="alert" data-testid="task.close.error" data-code={code}>
          {t('task.close.failed', { reason: error(code).title })}
        </p>
      )}
    </div>
  );
}

function TaskView({ row }: { readonly row: TaskRow }) {
  const { t } = useI18n();
  const location = useLocation();
  const title = useTitle(row);
  const { app } = useAppEnv();
  const renamed = useStore(
    useMemo(() => selectRenamed(row.org_id, row.wg_id, row.task_id), [row.org_id, row.wg_id, row.task_id]),
    app.store,
  );
  const newestTitle = useStore(
    useMemo(() => selectNewestTitle(row.wg_id, row.task_id), [row.wg_id, row.task_id]),
    app.store,
  );
  // a title event newer than the one the row names (a rename by the session): the row is reloaded (WEB-09)
  useEffect(() => {
    if (newestTitle !== null && newestTitle !== row.title_event_id) loadTask(app.tasks, row.task_id).catch(() => null);
  }, [app, newestTitle, row.title_event_id, row.task_id]);
  // the task with its sessions and their usage after a reconnect or resync (frames may have been lost, KOR-11)
  useLiveReload(app.store, () => void loadTask(app.tasks, row.task_id).catch(() => null));
  const { state, userId } = useOrgView(row.org_id);
  useDirectory(row.org_id, state !== null);
  const writer = state !== null && userId !== null && state.canWriteWg(userId, row.wg_id);
  const tabs = TABS.map((tab) => ({ id: tab.id, label: t(tab.labelKey) }));
  const active = TABS.find((tab) => tab.id === activeTab(location.search, tabs))!;
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="task.screen">
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold" data-testid="task.title">
            {title.status === 'verified' && title.text}
            {title.status === 'missing' && t('tasks.title.loading')}
            {title.status === 'unverifiable' && t('tasks.title.unverifiable', { code: title.code })}
          </h1>
          {renamed && (
            <span className="text-xs text-gray-600" data-testid="task.renamed">
              {t('operations.renamed')}
            </span>
          )}
          <span className="text-sm text-gray-600" data-testid="task.state" data-state={row.state}>
            {t(`tasks.state.${row.state}`)}
          </span>
          <TaskPlacement row={row} />
        </div>
        {writer && <TaskMenu task={row} />}
      </header>
      <Tabs tabs={tabs} label={t('task.tabs.label')} testId="task.tabs" />
      <Unlocking org={row.org_id} wg={row.wg_id}>
        <active.Component orgId={row.org_id} taskId={row.task_id} />
      </Unlocking>
    </main>
  );
}

export function TaskScreen() {
  const { task } = useParams() as { readonly task: string };
  const { app } = useAppEnv();
  const { t } = useI18n();
  const row = useStore((state) => state.tasks.tasks[task] ?? null, app.store);
  const load = useStore((state) => state.chat.loads[task] ?? null, app.store);
  useEffect(() => {
    void loadChat(app.tasks, task);
  }, [app, task]);
  if (row !== null) return <TaskView row={row} />;
  if (load?.status === 'error') {
    return <LoadError code={load.code!} testId="task.error" retry={() => void loadChat(app.tasks, task)} />;
  }
  return (
    <p className="p-4" data-testid="task.loading">
      {t('task.loading')}
    </p>
  );
}
