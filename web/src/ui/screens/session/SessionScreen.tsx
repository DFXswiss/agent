// The session detail (06-web.md section 5.5): tabs **Terminal** (default, the read-only terminal, loaded lazily),
// **Chat** (the outputs of this session only, with its work VM jobs; the header adds the work VM runtime),
// **Children** (its subagent sessions, `children/ChildrenTab.tsx`) and **Browser** (the session's platform browser:
// live picture and action record, `browser/BrowserTab.tsx`, section 5.4b). The session row comes from
// `GET /v1/sessions/{id}`, the task (title, sibling and child rows) from `GET /v1/tasks/{task}`; the chat events of the
// session are read newest page first, older pages on "Load more" (`useChatPaging`).
import { lazy, Suspense, useEffect, useMemo } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import { CHAT_KINDS } from '../../../core/chat/actions';
import { selectTimeline } from '../../../core/chat/timeline';
import { selectSessionIndex } from '../../../core/sessions/tree';
import { loadSession } from '../../../core/store/slices/sessions';
import type { SessionEntry } from '../../../core/store/slices/tasks';
import { pullOnce } from '../../../core/tasks/pull';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { ChunkBoundary } from '../../lazy/ChunkBoundary';
import { useAppEnv } from '../../shell/context';
import { LoadError } from '../errors/pages';
import { SessionFacts, SessionState } from '../../task-tabs/sessions/SessionsTab';
import { ChildrenTab } from './children/ChildrenTab';
import { WorkvmPanel } from './WorkvmPanel';
import { WorkvmRuntime } from './WorkvmRuntime';
import { Timeline } from '../../task-tabs/chat/Timeline';
import { useChatPaging } from '../../task-tabs/chat/useChatPaging';
import { activeTab, Tabs } from '../task/Tabs';

const TerminalView = lazy(() => import('../../terminal/TerminalView'));
const BrowserTab = lazy(() => import('../../browser/BrowserTab'));

const TAB_IDS = ['terminal', 'chat', 'children', 'browser'] as const;

function SessionChat({ entry }: { readonly entry: SessionEntry }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const { wg_id: wg, task_id: task, session_id: session, org_id: org } = entry.row;
  const select = useMemo(() => selectTimeline(wg, task, session), [wg, task, session]);
  const items = useStore(select, app.store);
  const paging = useChatPaging(org, wg, task, session);
  return <Timeline listId="session.chat" items={items} paging={paging} empty={t('session.chat.empty')} />;
}

function SessionView({ entry }: { readonly entry: SessionEntry }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const location = useLocation();
  const select = useMemo(() => selectSessionIndex(entry.row.task_id), [entry.row.task_id]);
  const node = useStore(select, app.store).get(entry.row.session_id);
  const tabs = TAB_IDS.map((id) => ({ id, label: t(`session.tab.${id}`) }));
  const active = activeTab(location.search, tabs);
  const { org_id: org, wg_id: wg, task_id: task, session_id: session } = entry.row;
  // the chat events of the task (also the `workvm.state` events of the header), whichever tab is open
  useEffect(() => {
    pullOnce(app.tasks, org, wg, { kinds: CHAT_KINDS, task }).catch(() => 0);
  }, [app, org, wg, task]);
  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-4 p-4" data-testid="session.screen">
      <header className="flex flex-col gap-1">
        <Link
          className="min-h-11 self-start py-2 text-sm text-blue-800 underline"
          to={`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(task)}`}
          data-testid="session.back"
        >
          {t('session.back')}
        </Link>
        <h1 className="flex flex-wrap items-center gap-2 text-xl font-semibold" data-testid="session.title">
          {node !== undefined && node.depth > 0
            ? t('task.chat.subagent', { path: node.path.join('.') })
            : t('session.title')}
          <SessionState state={entry.row.state} />
        </h1>
        <SessionFacts node={{ entry }} />
        <WorkvmRuntime wg={wg} task={task} session={session} />
      </header>
      <WorkvmPanel wg={wg} task={task} session={session} />
      <Tabs tabs={tabs} label={t('session.tabs.label')} testId="session.tabs" />
      {active === 'terminal' && (
        <ChunkBoundary
          fallback={
            <p className="text-sm text-red-800" role="alert" data-testid="terminal.chunk-unavailable">
              {t('terminal.chunk-unavailable')}
            </p>
          }
        >
          <Suspense fallback={<p data-testid="terminal.loading">{t('terminal.loading')}</p>}>
            <TerminalView org={org} wg={wg} session={session} />
          </Suspense>
        </ChunkBoundary>
      )}
      {active === 'chat' && <SessionChat entry={entry} />}
      {active === 'children' && <ChildrenTab entry={entry} />}
      {active === 'browser' && (
        <ChunkBoundary
          fallback={
            <p className="text-sm text-red-800" role="alert" data-testid="browser.chunk-unavailable">
              {t('app.screen-unavailable.title')}
            </p>
          }
        >
          <Suspense fallback={<p data-testid="browser.loading">{t('app.loading')}</p>}>
            <BrowserTab org={org} wg={wg} task={task} session={session} />
          </Suspense>
        </ChunkBoundary>
      )}
    </main>
  );
}

export function SessionScreen() {
  const { session } = useParams() as { readonly session: string };
  const { app } = useAppEnv();
  const { t } = useI18n();
  const entry = useStore((state) => state.tasks.sessions[session] ?? null, app.store);
  const detail = useStore((state) => state.sessions.details[session] ?? null, app.store);
  useEffect(() => {
    void loadSession(app.tasks, session);
  }, [app, session]);
  useLiveReload(app.store, () => void loadSession(app.tasks, session));
  if (entry !== null) return <SessionView entry={entry} />;
  if (detail?.status === 'error') {
    return <LoadError code={detail.code!} testId="session.error" retry={() => void loadSession(app.tasks, session)} />;
  }
  return (
    <p className="p-4" data-testid="session.loading">
      {t('session.loading')}
    </p>
  );
}
