// The support area (ADR-096 section 4; KOR-56), only for a person of the support roster of the browser's verified
// platform log (everybody else sees "not enabled"):
// - `/support`: the active releases (`GET /v1/support/releases`), named by the newest `support.info` of each workgroup
//   (decrypted with the person's own wraps), else by their IDs;
// - `/support/:org/:wg`: the workgroup after verifying the org's log and opening the person's keys – its access and its
//   tasks with their titles;
// - `/support/:org/:wg/:task`: the conversation of a task (the chat timeline of the task screens; authors named by
//   `support.info`, else by their mark), with a write release the composer (a prompt signed by the person), with a read
//   release a line why there is none.
// Support has no live channel: an open view reads again every `POLL_MS`. A release the hub no longer serves (`404`) or
// the verified log no longer shows ends the view with "not (or no longer) released".
import '../../../core/i18n/lazy/support';
import { type ReactNode, useEffect, useId, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { CHAT_KINDS, selectTimeline } from '../../../core/chat/timeline';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import { actionErrorCode, refuse } from '../../../core/org/batch';
import {
  accessOf,
  loadReleases,
  openOrg,
  POLL_MS,
  pullSupport,
  selectInfo,
  sendSupportPrompt,
} from '../../../core/support/area';
import { ensureMarks, isSupportPerson } from '../../../core/support/names';
import { INFO_KIND } from '../../../core/support/release';
import type { SupportRelease } from '../../../core/store/slices/support';
import { selectListRows, loadTasks } from '../../../core/tasks/list';
import { selectTitle, TITLE_KIND } from '../../../core/tasks/directory';
import { loadTask, sendable } from '../../../core/tasks/send';
import { type TaskRow, taskList } from '../../../core/store/slices/tasks';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { AuthorNames } from '../../names/support';
import { useAppEnv } from '../../shell/context';
import { Timeline } from '../../task-tabs/chat/Timeline';
import { BUTTON, PRIMARY } from '../members/ActionDialog';
import { type RowAttributes, type RowProps, WindowedList } from '../../lists/WindowedList';
import { NoName } from '../../names/names';

/** Rows rendered at first, rows added by a move, the cap (ADR-086 section 1). */
export const WINDOW = 50;
export const PAGE = 50;
export const MAX_ROWS = 150;

const LINK = 'min-h-11 self-start py-2 text-sm text-blue-800 underline';
const enc = encodeURIComponent;

/** What a view knows of its release: being checked, shown, ended (`404` or no release in the log), or failed. */
type ViewState = 'checking' | 'ready' | 'gone' | { readonly code: string };

/** Runs `refresh` now and every `POLL_MS` while mounted; its outcome is the view state. */
function usePolled(refresh: () => Promise<void>, deps: readonly unknown[]): ViewState {
  const [view, setView] = useState<ViewState>('checking');
  useEffect(() => {
    let live = true;
    const tick = () =>
      refresh().then(
        () => live && setView('ready'),
        (caught: unknown) => {
          const code = actionErrorCode(caught);
          if (live) setView(code === 'not_found' ? 'gone' : { code });
        },
      );
    void tick();
    const timer = setInterval(() => void tick(), POLL_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `deps` name what `refresh` reads
  }, deps);
  return view;
}

/** The frame of every support view; a user outside the roster sees only "not enabled". */
function Frame({ children }: { readonly children: ReactNode }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const person = useStore(isSupportPerson, app.store);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="support.screen">
      <h1 className="text-xl font-semibold">{t('support.area.title')}</h1>
      {person ? (
        children
      ) : (
        <p data-testid="support.area.none" role="status">
          {t('support.area.none')}
        </p>
      )}
    </main>
  );
}

/** A view that is not ready: being checked, ended or failed. */
function NotReady({ view }: { readonly view: Exclude<ViewState, 'ready'> }) {
  const { t, error } = useI18n();
  if (view === 'checking') return <p data-testid="support.checking">{t('support.wg.checking')}</p>;
  if (view === 'gone')
    return (
      <p role="alert" data-testid="support.gone">
        {t('support.wg.gone')}
      </p>
    );
  return (
    <p role="alert" className="text-red-800" data-testid="support.failed" data-code={view.code}>
      {t('support.wg.failed', { reason: error(view.code).title })}
    </p>
  );
}

/** The names of a release: `support.info`, else a sentence (the ID stays in the tooltip, E-19). */
function ReleaseName({ org, wg, testId }: { readonly org: string; readonly wg: string; readonly testId: string }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const info = useStore(
    useMemo(() => selectInfo(wg), [wg]),
    app.store,
  );
  const named = {
    org: info?.orgName ?? t('support.area.unnamed.org'),
    wg: info?.wgName ?? t('support.area.unnamed.wg'),
  };
  return <NoName t={t} text={t('support.area.release', named)} id={`${org} ${wg}`} testId={testId} />;
}

function ReleaseRow({ item: release }: RowProps<SupportRelease>) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const { org_id: org, wg_id: wg } = release;
  useEffect(() => {
    pullSupport(app.tasks, org, wg, { kinds: [INFO_KIND] }).catch(() => 0);
  }, [app, org, wg]);
  const since = formatEventTime(Date.parse(release.granted_at), {
    locale,
    timeZone: localTimeZone(),
    now: app.tasks.now(),
  });
  return (
    <>
      <Link
        className="min-h-11 py-2 font-medium text-blue-800 underline"
        to={`/support/${enc(org)}/${enc(wg)}`}
        data-testid="support.release.open"
      >
        <ReleaseName org={org} wg={wg} testId="support.release.name" />
      </Link>
      <span className="flex flex-wrap gap-x-3 text-sm text-gray-700">
        <span data-testid="support.release.access">{t(`support.area.access.${release.access}`)}</span>
        <span data-testid="support.release.since">{t('support.area.since', { since })}</span>
      </span>
    </>
  );
}

const releaseKey = (release: SupportRelease): string => `${release.org_id} ${release.wg_id}`;
const releaseAttributes = (release: SupportRelease): RowAttributes => ({
  'data-testid': 'support.release',
  'data-wg': release.wg_id,
});

export function SupportScreen() {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const person = useStore(isSupportPerson, app.store);
  const area = useStore((state) => state.support.area, app.store);
  useEffect(() => {
    if (person) void loadReleases(app.tasks);
  }, [app, person]);
  return (
    <Frame>
      <p className="text-sm text-gray-700">{t('support.area.intro')}</p>
      {(area.status === 'idle' || area.status === 'loading') && (
        <p data-testid="support.area.loading">{t('support.area.loading')}</p>
      )}
      {area.status === 'none' && (
        <p role="status" data-testid="support.area.unknown">
          {t('support.area.none')}
        </p>
      )}
      {area.status === 'error' && (
        <p role="alert" className="text-red-800" data-testid="support.area.failed">
          {t('support.area.failed', { reason: error(area.code!).title })}
        </p>
      )}
      {area.status === 'ready' && area.releases.length === 0 && (
        <p data-testid="support.area.empty">{t('support.area.empty')}</p>
      )}
      <WindowedList
        listId="support.releases"
        items={area.releases}
        getKey={releaseKey}
        Row={ReleaseRow}
        newest="start"
        size={WINDOW}
        page={PAGE}
        maxRows={MAX_ROWS}
        className="flex flex-col"
        rowClassName="flex flex-col gap-1 border-b border-gray-200 py-2"
        rowAttributes={releaseAttributes}
        testId="support.releases"
      />
      <button
        type="button"
        className={BUTTON + ' self-start'}
        onClick={() => void loadReleases(app.tasks)}
        data-testid="support.area.reload"
      >
        {t('support.area.reload')}
      </button>
    </Frame>
  );
}

function TaskItem({ item: row }: RowProps<TaskRow>) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const title = useStore(
    useMemo(() => selectTitle(row.wg_id, row.task_id, row.title_event_id), [row]),
    app.store,
  );
  return (
    <Link
      className="flex min-h-11 items-center text-blue-800 underline"
      to={`/support/${enc(row.org_id)}/${enc(row.wg_id)}/${enc(row.task_id)}`}
      data-testid="support.task.open"
    >
      {title.status === 'verified' ? title.text : t('names.task.unknown')}
    </Link>
  );
}

const taskKey = (row: TaskRow): string => row.task_id;
const taskAttributes = (row: TaskRow): RowAttributes => ({ 'data-testid': 'support.task', 'data-task': row.task_id });

/** The workgroup's names and the person's access, shown above a workgroup or task view. */
function Header({ org, wg }: { readonly org: string; readonly wg: string }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const access = useStore((state) => accessOf(state, org, wg), app.store);
  return (
    <>
      <h2 className="text-lg font-medium">
        <ReleaseName org={org} wg={wg} testId="support.wg.title" />
      </h2>
      {access !== null && (
        <p className="text-sm" data-testid="support.wg.access" data-access={access}>
          {t(`support.wg.access.${access}`)}
        </p>
      )}
    </>
  );
}

export function SupportWorkgroupScreen() {
  const { org, wg } = useParams() as { readonly org: string; readonly wg: string };
  const { app } = useAppEnv();
  const { t } = useI18n();
  const person = useStore(isSupportPerson, app.store);
  const rows = useStore(
    useMemo(() => selectListRows(wg, null), [wg]),
    app.store,
  );
  const list = useStore((state) => taskList(state.tasks, wg, null), app.store);
  const view = usePolled(async () => {
    if (!person) return;
    await openOrg(app.tasks, org);
    if (accessOf(app.store.getState(), org, wg) === null) refuse('not_found');
    await pullSupport(app.tasks, org, wg, { kinds: [INFO_KIND, TITLE_KIND] });
    // the first page once, later the first page again (new tasks) keeping what was paged
    const loaded = taskList(app.store.getState().tasks, wg, null).status !== 'idle';
    await loadTasks(app.tasks, wg, null, false, loaded);
  }, [app, org, wg, person]);
  return (
    <Frame>
      <Link className={LINK} to="/support" data-testid="support.back">
        {t('support.wg.back')}
      </Link>
      {view === 'ready' ? (
        <>
          <Header org={org} wg={wg} />
          <h3 className="font-medium">{t('support.wg.tasks')}</h3>
          <WindowedList
            listId="support.tasks"
            items={rows}
            getKey={taskKey}
            Row={TaskItem}
            newest="start"
            size={WINDOW}
            page={PAGE}
            maxRows={MAX_ROWS}
            className="flex flex-col"
            rowAttributes={taskAttributes}
            testId="support.tasks"
            hasMore={list.nextBefore !== null}
            loading={list.status === 'loading'}
            onLoadMore={() => void loadTasks(app.tasks, wg, null, true)}
            empty={<p data-testid="support.tasks.empty">{t('support.wg.empty')}</p>}
          />
        </>
      ) : (
        <NotReady view={view} />
      )}
    </Frame>
  );
}

function Composer({ org, wg, task }: { readonly org: string; readonly wg: string; readonly task: string }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const id = useId();
  const send = async () => {
    setSending(true);
    setFailed(null);
    try {
      await sendSupportPrompt(app.tasks, org, wg, task, text);
      setText('');
    } catch (caught) {
      setFailed(actionErrorCode(caught));
    }
    setSending(false);
  };
  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
      data-testid="support.composer"
    >
      <label htmlFor={id} className="text-sm font-medium">
        {t('support.task.composer.label')}
      </label>
      <textarea
        id={id}
        className="min-h-24 rounded border border-gray-400 p-2"
        value={text}
        onChange={(event) => setText(event.target.value)}
        data-testid="support.composer.text"
      />
      {failed !== null && (
        <p role="alert" className="text-red-800" data-testid="support.composer.failed" data-code={failed}>
          {t('support.task.failed', { reason: error(failed).title })}
        </p>
      )}
      <button
        type="submit"
        className={PRIMARY + ' self-start'}
        disabled={sending || !sendable(text)}
        data-testid="support.composer.send"
      >
        {sending ? t('support.task.sending') : t('support.task.send')}
      </button>
    </form>
  );
}

export function SupportTaskScreen() {
  const { org, wg, task } = useParams() as { readonly org: string; readonly wg: string; readonly task: string };
  const { app } = useAppEnv();
  const { t } = useI18n();
  const person = useStore(isSupportPerson, app.store);
  const items = useStore(
    useMemo(() => selectTimeline(wg, task), [wg, task]),
    app.store,
  );
  const info = useStore(
    useMemo(() => selectInfo(wg), [wg]),
    app.store,
  );
  const marks = useStore((state) => state.support.marks, app.store);
  const access = useStore((state) => accessOf(state, org, wg), app.store);
  const authors = useMemo(
    () => [...new Set(items.flatMap((item) => (item.author === null ? [] : [item.author])))],
    [items],
  );
  useEffect(() => {
    void ensureMarks(app.store, authors);
  }, [app, authors]);
  const author = useMemo(() => (id: string) => info?.members.get(id) ?? marks[id] ?? null, [info, marks]);
  const view = usePolled(async () => {
    if (!person) return;
    await openOrg(app.tasks, org);
    if (accessOf(app.store.getState(), org, wg) === null) refuse('not_found');
    await loadTask(app.tasks, task);
    await pullSupport(app.tasks, org, wg, { kinds: [INFO_KIND] });
    await pullSupport(app.tasks, org, wg, { kinds: CHAT_KINDS, task });
  }, [app, org, wg, task, person]);
  return (
    <Frame>
      <Link className={LINK} to={`/support/${enc(org)}/${enc(wg)}`} data-testid="support.task.back">
        {t('support.task.back')}
      </Link>
      {view === 'ready' ? (
        <>
          <Header org={org} wg={wg} />
          <AuthorNames.Provider value={author}>
            <Timeline listId="task.chat" items={items} empty={t('support.task.empty')} />
          </AuthorNames.Provider>
          {access === 'write' ? (
            <Composer org={org} wg={wg} task={task} />
          ) : (
            <p className="rounded bg-gray-100 p-3 text-sm" data-testid="support.task.read-only">
              {t('support.task.read-only')}
            </p>
          )}
        </>
      ) : (
        <NotReady view={view} />
      )}
    </Frame>
  );
}
