// The audit view (06-web.md section 9.2), used by the audit screen and the task tab "Audit" (task filter fixed).
// Filters action (an action or a group), actor, workgroup, task, session and time live in the URL query, so a view can
// be linked; they apply on "Apply". Rows are newest first with the time as `HH:MM:SS.mmm` (section 12.4), the actor's
// display name, action, result, the decrypted names of target, workgroup, task and device where the client has them,
// and a link to the referenced event. "Load more" pages with the hub's cursor. Admins verify the chain
// (`GET /v1/orgs/{org}/audit/verify`) and see the result: head, newest checkpoint, ok or not.
import { type FormEvent, useEffect, useId, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { formatEventTime } from '../../../core/i18n';
import { loadAgents } from '../../../core/agents/list';
import { agentName } from '../../../core/store/slices/agents';
import {
  ACTION_GROUPS,
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
import { DEVICE } from '../../../core/log/state';
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
import { selectTitle, TITLE_KIND } from '../../../core/tasks/directory';
import { pullOnce } from '../../../core/tasks/pull';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { BUTTON, PRIMARY } from '../members/ActionDialog';
import { useDirectory, useOrgView } from '../tasks/hooks';

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
  readonly from: string;
  readonly to: string;
}

const draftOf = (filters: AuditFilters): Draft => ({
  action: filters.action ?? '',
  actor: filters.actor ?? '',
  wg: filters.wg ?? '',
  task: filters.task ?? '',
  session: filters.session ?? '',
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
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
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
                    timeZone,
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

function Row(props: {
  readonly org: string;
  readonly row: AuditRow;
  readonly names: AuditNames & { wg: (id: string) => string };
}) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const { row, names, org } = props;
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const wg = row.wg_id;
  const task = row.task_id;
  const title = useStore(selectTitle(wg ?? '', task ?? '', null), app.store);
  useEffect(() => {
    if (wg !== null && task !== null) pullOnce(app.tasks, org, wg, { kinds: [TITLE_KIND], task }).catch(() => 0);
  }, [app, org, wg, task]);
  const actor = actorName(row, names) ?? t(`audit.actor.${row.actor_kind}`, { id: (row.actor_id ?? '').slice(-6) });
  const target =
    row.target_kind === null ? null : (targetName(row, names) ?? `${row.target_kind} ${row.target_id ?? ''}`.trim());
  const link = eventLink(org, row);
  const detail = Object.entries(row.detail);
  const agent = useStore((state) => agentMarker(row, (id) => agentName(state, id)), app.store);
  return (
    <li
      className="flex flex-col gap-1 border-b border-gray-200 py-2"
      data-testid="audit.row"
      data-seq={row.seq}
      data-action={row.action}
    >
      <div className="flex flex-wrap items-center gap-2">
        <time className="text-xs text-gray-600 tabular-nums" dateTime={row.at} data-testid="audit.row.time">
          {formatEventTime(Date.parse(row.at), { locale, timeZone, now: app.tasks.now() })}
        </time>
        <span className="font-medium" data-testid="audit.row.actor">
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
        {wg !== null && <span data-testid="audit.row.wg">{t('audit.row.wg', { name: names.wg(wg) })}</span>}
        {task !== null && (
          <Link
            className="inline-flex min-h-11 items-center underline"
            to={`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(task)}`}
            data-testid="audit.row.task"
          >
            {title.status === 'verified' ? title.text : t('audit.row.task', { id: task.slice(-6) })}
          </Link>
        )}
        {row.device_id !== null && (
          <span data-testid="audit.row.device">
            {t('audit.row.device', { name: names.device(row.device_id) ?? row.device_id.slice(-6) })}
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
    </li>
  );
}

export function AuditView({ org, task }: { readonly org: string; readonly task: string | null }) {
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
  const names = {
    member: (id: string) => directory.members.get(id) ?? null,
    device: (id: string) => labels.get(id) ?? null,
    wg: (id: string) => directory.workgroups.get(id) ?? t('audit.wg.unnamed', { id: id.slice(-6) }),
  };
  const principals = state === null ? [] : [...state.principals.values()].filter((p) => p.removedSeq === null);
  const actors = principals
    .map((p) => ({
      id: p.id,
      name:
        (p.role === DEVICE ? labels.get(p.id) : directory.members.get(p.id)) ??
        t(p.role === DEVICE ? 'audit.actor.device' : 'audit.actor.user', { id: p.id.slice(-6) }),
    }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const workgroups = state === null ? [] : [...state.workgroups.keys()].map((id) => ({ id, name: names.wg(id) }));
  const admin = state !== null && userId !== null && state.isAdmin(userId);
  const rows = visibleRows(view, filters);
  return (
    <div className="flex flex-col gap-4" data-testid="audit.view">
      <Filters filters={filters} fixedTask={task !== null} actors={actors} workgroups={workgroups} onApply={apply} />
      {admin && <Verify org={org} />}
      {view.status === 'error' && (
        <p className="text-red-800" role="alert" data-testid="audit.error">
          {t('audit.failed', { code: view.code! })}
        </p>
      )}
      {rows.length === 0 ? (
        view.status !== 'loading' && (
          <p className="text-gray-600" data-testid="audit.empty">
            {t('audit.empty')}
          </p>
        )
      ) : (
        <ol className="flex flex-col" data-testid="audit.rows">
          {rows.map((row) => (
            <Row key={row.seq} org={org} row={row} names={names} />
          ))}
        </ol>
      )}
      {view.status === 'loading' && (
        <p className="text-gray-600" role="status" data-testid="audit.loading">
          {t('audit.loading')}
        </p>
      )}
      {view.status !== 'loading' && canLoadMore(view, filters) && (
        <button
          type="button"
          className={BUTTON}
          onClick={() => void loadAudit(app.tasks, org, filters, true)}
          data-testid="audit.more"
        >
          {t('audit.more')}
        </button>
      )}
    </div>
  );
}
