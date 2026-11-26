// The audit view (06-web.md section 9.2), used by the audit screen and the task tab "Audit" (task filter fixed).
// Filters action (an action or a group), actor, workgroup, task, session, "On behalf of" (the person whose instruction
// an agent followed) and time live in the URL query, so a view can be linked; they apply on "Apply". Rows are newest
// first with the time as `HH:MM:SS.mmm` (section 12.4), the actor's display name – an agent actor as "Main agent
// <name> #42 on behalf of <person>" / "Subagent #42.1 of <name> #42 on behalf of <person>" (section 4.0a; the name when
// the task's name is in the store, else the handle) –, action, result, the decrypted names of target, workgroup, task and device where the client has them,
// and a link to the referenced event. "Load more" pages with the hub's cursor. Admins verify the chain
// (`GET /v1/orgs/{org}/audit/verify`) and see the result: head, newest checkpoint, ok or not.
import { type FormEvent, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import { loadAgents } from '../../../core/agents/list';
import { type AgentActor, agentActor } from '../../../core/monitoring/actors';
import type { ListPaging } from '../../../core/lists/window';
import { AGENT_NAME_KIND } from '../../../core/agents/naming';
import '../../../core/i18n/lazy/mainagent';
import {
  ACTION_GROUPS,
  type AuditActors,
  actorName,
  agentMarker,
  type AuditFilters,
  type AuditNames,
  type AuditRow,
  AUDIT_ACTIONS,
  eventLink,
  filterQuery,
  isAction,
  isUuid,
  NO_FILTERS,
  parseFilters,
  targetName,
} from '../../../core/monitoring/audit';
import { DEVICE, USER } from '../../../core/log/state';
import { DEVICE_PROFILE, selectDeviceLabels } from '../../../core/org/devices';
import {
  auditView,
  canLoadMore,
  loadAudit,
  verifyAudit,
  verifyState,
  viewKey,
  visibleRows,
} from '../../../core/store/slices/audit';
import { TITLE_KIND } from '../../../core/tasks/directory';
import { titles as pullTitles } from '../../../core/tasks/list';
import { pullOnce } from '../../../core/tasks/pull';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { type RowAttributes, type RowProps, WindowedList } from '../../lists/WindowedList';
import { useAppEnv } from '../../shell/context';
import { BUTTON, PRIMARY } from '../members/ActionDialog';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { knownPersonName, NoName, personName } from '../../names/names';
import {
  agentLabelsSelector,
  agentNamesSelector,
  agentsOf,
  agentTasksOf,
  shallowSame,
  tasksOf,
  titlesSelector,
} from './select';

const pad = (value: number) => String(value).padStart(2, '0');

/** Unix ms as the value of a `datetime-local` input (local time, seconds). */
export function localInput(ms: number | null): string {
  if (ms === null) return '';
  const d = new Date(ms);
  const date = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  return `${date}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** The value of a `datetime-local` input as unix ms (local time), null when empty or invalid. */
export function fromLocalInput(text: string): number | null {
  const ms = text === '' ? Number.NaN : new Date(text).getTime();
  return Number.isNaN(ms) ? null : ms;
}

interface Draft {
  readonly action: string;
  readonly actor: string;
  readonly wg: string;
  readonly task: string;
  readonly session: string;
  readonly on_behalf_of: string;
  readonly from: string;
  readonly to: string;
}

const draftOf = (filters: AuditFilters): Draft => ({
  action: filters.action ?? '',
  actor: filters.actor ?? '',
  wg: filters.wg ?? '',
  task: filters.task ?? '',
  session: filters.session ?? '',
  on_behalf_of: filters.on_behalf_of ?? '',
  from: localInput(filters.from),
  to: localInput(filters.to),
});

/** The typed fields of the draft that are not well-formed (they block "Apply"); a `datetime-local` input only ever
 *  holds a valid time or nothing. */
function invalidFields(draft: Draft): string[] {
  const bad: string[] = [];
  if (draft.action !== '' && !isAction(draft.action)) bad.push('action');
  for (const name of ['task', 'session'] as const) if (draft[name] !== '' && !isUuid(draft[name])) bad.push(name);
  return bad;
}

const filtersOf = (draft: Draft): AuditFilters => ({
  action: draft.action === '' ? null : draft.action,
  actor: draft.actor === '' ? null : draft.actor,
  wg: draft.wg === '' ? null : draft.wg,
  task: draft.task === '' ? null : draft.task,
  session: draft.session === '' ? null : draft.session,
  on_behalf_of: draft.on_behalf_of === '' ? null : draft.on_behalf_of,
  from: fromLocalInput(draft.from),
  // "until" names a whole second: every row within it is included
  to: draft.to === '' ? null : fromLocalInput(draft.to)! + 999,
});

interface Option {
  readonly id: string;
  readonly name: string;
}

function Filters(props: {
  readonly filters: AuditFilters;
  readonly fixedTask: boolean;
  readonly actors: readonly Option[];
  readonly people: readonly Option[];
  readonly workgroups: readonly Option[];
  readonly onApply: (filters: AuditFilters) => void;
}) {
  const { t } = useI18n();
  const [draft, setDraft] = useState(() => draftOf(props.filters));
  const query = filterQuery(props.filters);
  // a new URL from elsewhere (back button, a link) replaces the draft; the form's own "Apply" keeps what is typed
  const applied = useRef(query);
  useEffect(() => {
    if (query !== applied.current) setDraft(draftOf(parseFilters(new URLSearchParams(query))));
    applied.current = query;
  }, [query]);
  const apply = (next: AuditFilters) => {
    applied.current = filterQuery(next);
    props.onApply(next);
  };
  const ids = { list: useId(), base: useId() };
  const bad = invalidFields(draft);
  const field = (name: keyof Draft) => ({
    id: `${ids.base}-${name}`,
    value: draft[name],
    'aria-invalid': bad.includes(name),
    className: `min-h-11 rounded border px-2 ${bad.includes(name) ? 'border-red-700' : 'border-gray-300'}`,
    'data-testid': `audit.filter.${name}`,
    onChange: (event: { target: { value: string } }) => setDraft({ ...draft, [name]: event.target.value }),
  });
  const label = (name: keyof Draft) => (
    <label htmlFor={`${ids.base}-${name}`} className="text-sm text-gray-700">
      {t(`audit.filter.${name}`)}
    </label>
  );
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (bad.length === 0) apply(filtersOf(draft));
  };
  return (
    <form
      className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4"
      onSubmit={submit}
      data-testid="audit.filters"
    >
      <div className="flex flex-col gap-1">
        {label('action')}
        <input {...field('action')} list={ids.list} autoComplete="off" />
        <datalist id={ids.list}>
          {[...ACTION_GROUPS, ...AUDIT_ACTIONS].map((action) => (
            <option key={action} value={action} />
          ))}
        </datalist>
      </div>
      <div className="flex flex-col gap-1">
        {label('actor')}
        <select {...field('actor')}>
          <option value="">{t('audit.filter.anyone')}</option>
          {props.actors.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </div>
      <div className="flex flex-col gap-1">
        {label('on_behalf_of')}
        <select {...field('on_behalf_of')}>
          <option value="">{t('audit.filter.anybody')}</option>
          {props.people.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </div>
      <div className="flex flex-col gap-1">
        {label('wg')}
        <select {...field('wg')}>
          <option value="">{t('audit.filter.all')}</option>
          {props.workgroups.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </div>
      {!props.fixedTask && (
        <div className="flex flex-col gap-1">
          {label('task')}
          <input {...field('task')} autoComplete="off" />
        </div>
      )}
      <div className="flex flex-col gap-1">
        {label('session')}
        <input {...field('session')} autoComplete="off" />
      </div>
      <div className="flex flex-col gap-1">
        {label('from')}
        <input {...field('from')} type="datetime-local" step={1} />
      </div>
      <div className="flex flex-col gap-1">
        {label('to')}
        <input {...field('to')} type="datetime-local" step={1} />
      </div>
      <div className="flex items-end gap-2">
        <button type="submit" className={PRIMARY} disabled={bad.length > 0} data-testid="audit.filter.apply">
          {t('audit.filter.apply')}
        </button>
        <button
          type="button"
          className={BUTTON}
          onClick={() => {
            setDraft(draftOf(NO_FILTERS));
            apply(NO_FILTERS);
          }}
          data-testid="audit.filter.reset"
        >
          {t('audit.filter.reset')}
        </button>
      </div>
      {bad.length > 0 && (
        <p className="text-sm text-red-800 sm:col-span-2 lg:col-span-4" role="alert" data-testid="audit.filter.invalid">
          {t('audit.filter.invalid')}
        </p>
      )}
    </form>
  );
}

function Verify({ org }: { readonly org: string }) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const verify = useStore((state) => verifyState(state.audit, org), app.store);
  const result = verify.result;
  return (
    <div className="flex flex-col gap-2 rounded border border-gray-200 p-3" data-testid="audit.verify">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className={BUTTON}
          disabled={verify.status === 'loading'}
          onClick={() => void verifyAudit(app.tasks, org)}
          data-testid="audit.verify.run"
        >
          {t(verify.status === 'loading' ? 'audit.verify.running' : 'audit.verify.run')}
        </button>
        {verify.status === 'error' && (
          <span className="text-sm text-red-800" role="alert" data-testid="audit.verify.error">
            {t('audit.verify.failed', { code: verify.code! })}
          </span>
        )}
      </div>
      {result !== null && (
        <div
          role="status"
          className="flex flex-col gap-1 text-sm"
          data-testid="audit.verify.result"
          data-ok={String(result.ok)}
        >
          <span className={result.ok ? 'font-medium text-green-800' : 'font-medium text-red-800'}>
            {t(result.ok ? 'audit.verify.ok' : 'audit.verify.broken')}
          </span>
          <span data-testid="audit.verify.head">
            {t('audit.verify.head', { seq: result.head.seq, hash: result.head.hash.slice(0, 16) })}
          </span>
          <span data-testid="audit.verify.checkpoint">
            {result.checkpoint === null
              ? t('audit.verify.no-checkpoint')
              : t('audit.verify.checkpoint', {
                  seq: result.checkpoint.seq,
                  time: formatEventTime(Date.parse(result.checkpoint.published_at), {
                    locale,
                    timeZone: localTimeZone(),
                    now: verify.at!,
                  }),
                  key: result.checkpoint.hub_key_id,
                })}
          </span>
        </div>
      )}
    </div>
  );
}

/** One row with everything it shows resolved by the list (one selector per list, none per row). */
interface AuditItem {
  readonly org: string;
  readonly row: AuditRow;
  /** The actor's display name (an agent actor's text), null when not known. */
  readonly actor: string | null;
  /** The target's display name, null when not known. */
  readonly target: string | null;
  readonly wg: string | null;
  readonly title: string | null;
  readonly device: string | null;
  /** `agentMarker`: undefined without agent, null for an agent that is not the user's. */
  readonly agent: string | null | undefined;
}

/** Rows rendered at first, rows added by a move or a loaded page, the cap (ADR-086 section 1). */
export const WINDOW = 100;
export const PAGE = 50;
export const MAX_ROWS = 200;

const keyOf = (item: AuditItem): string => String(item.row.seq);

const attributes = (item: AuditItem): RowAttributes => ({
  'data-testid': 'audit.row',
  'data-seq': item.row.seq,
  'data-action': item.row.action,
});

type T = (key: string, params?: Record<string, string>) => string;

/**
 * "Main agent <name> #42 on behalf of <person>" / "Subagent #42.1 of <name> #42 …" of an agent actor (`label`: the
 * shown name of its main agent, null when not readable: then the handle alone).
 */
export function agentActorText(t: T, agent: AgentActor, label: string | null, person: (id: string) => string): string {
  const main = label === null ? agent.main : `${label} ${agent.main}`;
  const kind = agent.kind === 'main_agent' ? 'main' : 'subagent';
  const params = { agent: main, handle: agent.handle };
  return agent.onBehalfOf === null
    ? t(`mainagent.audit.${kind}.nobody`, params)
    : t(`mainagent.audit.${kind}`, { ...params, person: person(agent.onBehalfOf) });
}

function Row({ item }: RowProps<AuditItem>) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const { row, org, agent } = item;
  const wg = row.wg_id;
  const task = row.task_id;
  const actor = item.actor ?? t(`audit.actor.${row.actor_kind}`);
  const target = row.target_kind === null ? null : (item.target ?? `${row.target_kind} ${row.target_id ?? ''}`.trim());
  const link = eventLink(org, row);
  const detail = Object.entries(row.detail);
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <time className="text-xs text-gray-600 tabular-nums" dateTime={row.at} data-testid="audit.row.time">
          {formatEventTime(Date.parse(row.at), { locale, timeZone: localTimeZone(), now: app.tasks.now() })}
        </time>
        <span className="font-medium" data-testid="audit.row.actor" data-actor-kind={row.actor_kind}>
          {actor}
        </span>
        {agent !== undefined && (
          <span className="rounded bg-amber-100 px-2 text-sm text-amber-900" data-testid="audit.row.agent">
            {agent === null ? t('audit.row.by-unknown-agent') : t('audit.row.by-agent', { name: agent })}
          </span>
        )}
        <code className="rounded bg-gray-100 px-1 text-sm" data-testid="audit.row.action">
          {row.action}
        </code>
        <span
          className={`rounded px-2 text-sm ${row.result === 'ok' ? 'bg-green-100 text-green-900' : 'bg-red-100 text-red-900'}`}
          data-testid="audit.row.result"
          data-result={row.result}
        >
          {t(`audit.result.${row.result}`)}
        </span>
      </div>
      <div className="flex flex-wrap gap-3 text-sm text-gray-700">
        {target !== null && <span data-testid="audit.row.target">{t('audit.row.target', { name: target })}</span>}
        {wg !== null && (
          <span data-testid="audit.row.wg">{t('audit.row.wg', { name: item.wg ?? t('names.workgroup.unknown') })}</span>
        )}
        {task !== null && (
          <Link
            className="inline-flex min-h-11 items-center underline"
            to={`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(task)}`}
            data-testid="audit.row.task"
          >
            {item.title ?? <NoName t={t} text={t('names.task.unknown')} id={task} />}
          </Link>
        )}
        {row.device_id !== null && (
          <span data-testid="audit.row.device">
            {t('audit.row.device', { name: item.device ?? t('names.device.unknown') })}
          </span>
        )}
        {link !== null && (
          <Link className="inline-flex min-h-11 items-center underline" to={link} data-testid="audit.row.event">
            {t('audit.row.event')}
          </Link>
        )}
      </div>
      {detail.length > 0 && (
        <p className="break-all text-xs text-gray-600" data-testid="audit.row.detail">
          {detail.map(([name, value]) => `${name}=${String(value)}`).join(' · ')}
        </p>
      )}
    </>
  );
}

/**
 * The audit rows of a view: a `WindowedList` newest first (`WINDOW` rows at first, `PAGE` per move, at most `MAX_ROWS`
 * rendered), server paging with the hub's cursor through "Load more". The titles of the rows' tasks are pulled once
 * per list (`core/tasks/list.ts` `titles`, newest `task.meta` pages per workgroup) and read with one selector; so are the names of the agents.
 */
function AuditRows(props: {
  readonly org: string;
  readonly listId: 'audit' | 'task.audit';
  readonly rows: readonly AuditRow[];
  readonly names: AuditNames & { readonly wg: (id: string) => string | null; readonly person: (id: string) => string };
  readonly paging: ListPaging;
  /** The agent actors of the loaded pages (session ID → agent). */
  readonly actors: AuditActors;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const { org, rows, names, paging, actors } = props;
  const tasks = useMemo(() => tasksOf(rows), [rows]);
  const tasksKey = tasks.map((ref) => `${ref.wg}/${ref.task}`).join(' ');
  const titles = useStore(
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `tasksKey` names the tasks
    useMemo(() => titlesSelector(tasks), [tasksKey]),
    app.store,
  );
  useEffect(() => {
    void pullTitles(
      app.tasks,
      tasks.map((ref) => ({ org_id: org, wg_id: ref.wg, task_id: ref.task, title_event_id: null })),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `tasksKey` names the tasks
  }, [app, org, tasksKey]);
  // the main agents named as actors: their names (and titles, for an automatic name) read with one selector
  const agentTasks = useMemo(() => agentTasksOf(rows, actors), [rows, actors]);
  const agentTasksKey = agentTasks.map((ref) => `${ref.wg}/${ref.task}`).join(' ');
  const labels = useStore(
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `agentTasksKey` names the tasks
    useMemo(() => agentLabelsSelector(agentTasks), [agentTasksKey]),
    app.store,
  );
  useEffect(() => {
    for (const ref of agentTasks) {
      for (const kind of [AGENT_NAME_KIND, TITLE_KIND]) {
        pullOnce(app.tasks, org, ref.wg, { kinds: [kind], task: ref.task }).catch(() => 0);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `agentTasksKey` names the tasks
  }, [app, org, agentTasksKey]);
  const agentIds = useMemo(() => agentsOf(rows), [rows]);
  const agentsKey = agentIds.join(' ');
  const agents = useStore(
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `agentsKey` names the agents
    useMemo(() => agentNamesSelector(agentIds), [agentsKey]),
    app.store,
  );
  const actorOf = useMemo(
    () => (row: AuditRow) => {
      const agent = agentActor(row, actors);
      if (agent === null) return actorName(row, names);
      return agentActorText(t, agent, labels.get(agent.task) ?? null, names.person);
    },
    [actors, labels, names, t],
  );
  const items = useMemo(
    () =>
      rows.map((row): AuditItem => ({
        org,
        row,
        actor: actorOf(row),
        target: targetName(row, names),
        wg: row.wg_id === null ? null : names.wg(row.wg_id),
        title: row.task_id === null ? null : (titles.get(row.task_id) ?? null),
        device: row.device_id === null ? null : names.device(row.device_id),
        agent: agentMarker(row, (id) => agents.get(id)!),
      })),
    [org, rows, names, titles, agents, actorOf],
  );
  return (
    <WindowedList
      listId={props.listId}
      items={items}
      getKey={keyOf}
      Row={Row}
      same={shallowSame}
      newest="start"
      size={WINDOW}
      page={PAGE}
      maxRows={MAX_ROWS}
      as="ol"
      className="flex flex-col"
      rowClassName="flex flex-col gap-1 border-b border-gray-200 py-2"
      rowAttributes={attributes}
      testId="audit.rows"
      testIds={{ loadMore: 'audit.more' }}
      hasMore={paging.hasMore}
      loading={paging.loading}
      onLoadMore={paging.loadMore}
      empty={
        // a time filter can leave every loaded row out while older pages may still hold some
        paging.hasMore && !paging.loading ? (
          <button type="button" className={BUTTON} onClick={paging.loadMore} data-testid="audit.more">
            {t('audit.more')}
          </button>
        ) : null
      }
    />
  );
}

export function AuditView({
  org,
  task,
  listId,
}: {
  readonly org: string;
  readonly task: string | null;
  readonly listId: 'audit' | 'task.audit';
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const [search, setSearch] = useSearchParams();
  const { state, userId } = useOrgView(org);
  const ready = state !== null;
  const directory = useDirectory(org, ready);
  const labels = useStore(selectDeviceLabels(org), app.store);
  const parsed = parseFilters(search);
  const filters: AuditFilters = task === null ? parsed : { ...parsed, task };
  const query = filterQuery(filters);
  const key = viewKey(org, filters);
  const view = useStore((s) => auditView(s.audit, key), app.store);
  useEffect(() => {
    if (ready) pullOnce(app.tasks, org, org, { kinds: [DEVICE_PROFILE] }).catch(() => 0);
  }, [app, org, ready]);
  // the names of the user's own agents for the "by agent" marker (11-agent-access.md section 7)
  useEffect(() => {
    if (app.store.getState().agents.status === 'idle') void loadAgents(app.tasks);
  }, [app]);
  useEffect(() => {
    void loadAudit(app.tasks, org, parseFilters(new URLSearchParams(query)));
  }, [app, org, query]);
  const apply = (next: AuditFilters) => {
    const params = new URLSearchParams(filterQuery(task === null ? next : { ...next, task: null }));
    const tab = search.get('tab');
    if (tab !== null) params.set('tab', tab);
    setSearch(params);
  };
  const names = useMemo(
    () => ({
      member: (id: string) => knownPersonName(t, directory, id) ?? null,
      device: (id: string) => labels.get(id) ?? null,
      wg: (id: string) => directory.workgroups.get(id) ?? null,
      person: (id: string) => personName(t, directory, id),
    }),
    [directory, labels, t],
  );
  const wgName = (id: string) => names.wg(id) ?? t('names.workgroup.unknown');
  const principals = state === null ? [] : [...state.principals.values()].filter((p) => p.removedSeq === null);
  const actors = principals
    .map((p) => ({
      id: p.id,
      name:
        (p.role === DEVICE ? labels.get(p.id) : knownPersonName(t, directory, p.id)) ??
        t(p.role === DEVICE ? 'names.device.unknown' : 'names.person.unknown'),
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const workgroups = state === null ? [] : [...state.workgroups.keys()].map((id) => ({ id, name: wgName(id) }));
  const people = principals
    .filter((p) => p.kind === USER)
    .map((p) => ({ id: p.id, name: personName(t, directory, p.id) }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const admin = state !== null && userId !== null && state.isAdmin(userId);
  const rows = useMemo(() => visibleRows(view, filters), [view, query]); // eslint-disable-line react-hooks/exhaustive-deps -- `query` names the filters
  const loading = view.status === 'loading';
  const paging: ListPaging = {
    hasMore: canLoadMore(view, filters),
    loading,
    loadMore: () => void loadAudit(app.tasks, org, filters, true),
  };
  return (
    <div className="flex flex-col gap-4" data-testid="audit.view">
      <Filters
        filters={filters}
        fixedTask={task !== null}
        actors={actors}
        people={people}
        workgroups={workgroups}
        onApply={apply}
      />
      {admin && <Verify org={org} />}
      {view.status === 'error' && (
        <p className="text-red-800" role="alert" data-testid="audit.error">
          {t('audit.failed', { code: view.code! })}
        </p>
      )}
      {rows.length === 0 && !loading && (
        <p className="text-gray-600" data-testid="audit.empty">
          {t('audit.empty')}
        </p>
      )}
      <AuditRows org={org} listId={listId} rows={rows} names={names} paging={paging} actors={view.actors} />
      {rows.length === 0 && loading && (
        <p className="text-gray-600" role="status" data-testid="audit.loading">
          {t('audit.loading')}
        </p>
      )}
    </div>
  );
}
