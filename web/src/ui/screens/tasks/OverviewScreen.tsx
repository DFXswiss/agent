// The task dashboard (ADR-101; built on the task overview of KOR-32, route `/o/:org/tasks`, right `org:member`):
// every task the user can read, in every organization of `GET /v1/me` whose log is verified and every workgroup there
// the user can read (from the verified membership logs; the others are never requested) – captured tasks without an
// agent and agent tasks in one list. Filters: organization, workgroup (only those lists are loaded), state, the
// start page's three groups of the open tasks (in progress, waiting for a question, no agent - the hub's one rule over
// the attention records; a captured task that was never started has no agent), status, urgency (the priority), person
// and topic (hub side), "only mine", the title search and the GitHub organisation and repository (in the browser:
// sealed; bounded per request, `scan.ts`). Order: latest activity, newest, or most urgent first. On top the counters
// per group, status and urgency (`TaskCounters.tsx`); a click on one filters. "Capture task"
// records a task without an agent in a writable workgroup of the route's organization (E-41).
// Filters live in the screen's state, not in the URL: a search text in the address would reach the hub on a reload.
import { useId, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { meOrgs } from '../../../core/account/session';
import { ORDERS, type Order } from '../../../core/tasks/filters';
import { readableWorkgroups } from '../../../core/tasks/overview';
import { writableWorkgroups } from '../../../core/tasks/send';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { CaptureForm } from './CaptureForm';
import { useDirectories, useOrgView } from './hooks';
import { TaskCounters, useTaskCounts } from './TaskCounters';
import { StateFilter, TaskTable } from './TaskTable';
import { hubKey, NO_FILTERS, type TicketFilterValues, TicketFilters, ticketFiltered } from './TicketFilters';
import { selectLogs } from './tickets';

const FIELD = 'flex w-full min-w-0 items-center gap-2 sm:w-auto';
const SELECT = 'min-h-11 min-w-0 max-w-full flex-1 rounded border border-gray-300 px-2 sm:flex-none';

interface Place {
  readonly org: string;
  readonly wg: string;
}

export function OverviewScreen() {
  const { org } = useParams() as { readonly org: string };
  const { app } = useAppEnv();
  const { t } = useI18n();
  const orgId = useId();
  const wgId = useId();
  const searchId = useId();
  const orderId = useId();
  const { state, userId } = useOrgView(org);
  const profile = useStore((store) => store.me.profile, app.store);
  // every organization of the user, the route's first
  const allKey = [org, ...meOrgs(profile).flatMap((item) => (item.org_id === org ? [] : [item.org_id]))].join(',');
  const all = useMemo(() => allKey.split(','), [allKey]);
  const selectAll = useMemo(() => selectLogs(allKey), [allKey]);
  const logs = useStore(selectAll, app.store);
  const directories = useDirectories(all);
  const [chosenOrg, setChosenOrg] = useState('');
  const [filter, setFilter] = useState<string | null>(null);
  const [chosen, setChosen] = useState('');
  const [mine, setMine] = useState(false);
  const [search, setSearch] = useState('');
  const [tickets, setTickets] = useState<TicketFilterValues>(NO_FILTERS);
  const [order, setOrder] = useState<Order>('activity');
  // the organizations with a verified log (the others appear once verified); one when chosen
  const verified = all.filter((id) => logs.has(id));
  const shown = verified.includes(chosenOrg) ? [chosenOrg] : verified;
  const places: Place[] =
    userId === null
      ? []
      : shown.flatMap((id) => readableWorkgroups(logs.get(id)!, userId).map((wg) => ({ org: id, wg })));
  // a workgroup that is no longer readable or shown (rights or the organization changed meanwhile) falls back to all
  const place = places.find((item) => item.wg === chosen) ?? null;
  const wgs = place === null ? places.map((item) => item.wg) : [place.wg];
  const listOrgs = place === null ? shown : [place.org];
  const key = hubKey(filter, tickets, order);
  const loaded = useTaskCounts(listOrgs, key, place?.wg ?? null, wgs);
  // until the route's log is verified and the user is known (two checks, KOR-32: the merged coverage of Vitest and
  // the browser counts each condition on its own)
  const loading = (
    <p className="p-4" data-testid="tasks.loading">
      {t('tasks.loading')}
    </p>
  );
  if (state === null) return loading;
  if (userId === null) return loading;
  const several = verified.length > 1;
  // `directories` names every organization of `all`
  const orgName = (id: string) => directories.get(id)!.orgName ?? t('home.org.unnamed');
  const wgName = (id: string, wg: string) => {
    const label = directories.get(id)!.workgroups.get(wg) ?? t('tasks.wg.unnamed');
    return logs.get(id)!.writable(wg) ? label : t('tasks.overview.archived', { name: label });
  };
  const filtered =
    shown.length !== verified.length ||
    filter !== null ||
    place !== null ||
    mine ||
    search.trim() !== '' ||
    ticketFiltered(tickets);
  const reset = () => {
    setChosenOrg('');
    setFilter(null);
    setChosen('');
    setMine(false);
    setSearch('');
    setTickets(NO_FILTERS);
  };
  const writable = writableWorkgroups(state, userId);
  const home = `/o/${encodeURIComponent(org)}`;
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="tasks.overview">
      <div className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold" data-testid="tasks.overview.title">
          {t('tasks.overview.title')}
        </h1>
        <p className="text-sm text-gray-700">{t('tasks.overview.intro')}</p>
      </div>
      <TaskCounters
        loaded={loaded}
        overview={tickets.overview}
        status={tickets.status}
        urgency={tickets.urgency}
        onOverview={(overview) => setTickets({ ...tickets, overview })}
        onStatus={(status) => setTickets({ ...tickets, status })}
        onUrgency={(urgency) => setTickets({ ...tickets, urgency })}
      />
      <CaptureForm org={org} wgs={writable} name={(wg) => wgName(org, wg)} />
      <div className="flex flex-wrap items-end gap-x-4 gap-y-2" data-testid="tasks.filters">
        <div className="flex w-full flex-col gap-1 sm:w-auto">
          <label htmlFor={searchId} className="text-sm">
            {t('tasks.search.label')}
          </label>
          <input
            id={searchId}
            type="search"
            className="min-h-11 rounded border border-gray-300 px-2"
            value={search}
            placeholder={t('tasks.search.placeholder')}
            onChange={(event) => setSearch(event.target.value)}
            data-testid="tasks.search"
          />
        </div>
        <StateFilter value={filter} onChange={setFilter} />
        {several && (
          <div className={FIELD}>
            <label htmlFor={orgId} className="text-sm">
              {t('tasks.filter.org')}
            </label>
            <select
              id={orgId}
              className={SELECT}
              value={shown.length === 1 ? shown[0] : ''}
              onChange={(event) => setChosenOrg(event.target.value)}
              data-testid="tasks.filter.org"
            >
              <option value="">{t('tasks.filter.org.all')}</option>
              {verified.map((id) => (
                <option key={id} value={id}>
                  {orgName(id)}
                </option>
              ))}
            </select>
          </div>
        )}
        <div className={FIELD}>
          <label htmlFor={wgId} className="text-sm">
            {t('tasks.filter.wg')}
          </label>
          <select
            id={wgId}
            className={SELECT}
            value={place?.wg ?? ''}
            onChange={(event) => setChosen(event.target.value)}
            data-testid="tasks.filter.wg"
          >
            <option value="">{t('tasks.filter.wg.all')}</option>
            {places.map((item) => (
              <option key={item.wg} value={item.wg}>
                {several ? `${orgName(item.org)} · ${wgName(item.org, item.wg)}` : wgName(item.org, item.wg)}
              </option>
            ))}
          </select>
        </div>
        <label className="flex min-h-11 items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="h-5 w-5"
            checked={mine}
            onChange={(event) => setMine(event.target.checked)}
            data-testid="tasks.filter.mine"
          />
          {t('tasks.filter.mine')}
        </label>
        <div className={FIELD}>
          <label htmlFor={orderId} className="text-sm">
            {t('tasks.order.label')}
          </label>
          <select
            id={orderId}
            className={SELECT}
            value={order}
            onChange={(event) => setOrder(event.target.value as Order)}
            data-testid="tasks.order"
          >
            {ORDERS.map((value) => (
              <option key={value} value={value}>
                {t(`tasks.order.${value}`)}
              </option>
            ))}
          </select>
        </div>
      </div>
      <TicketFilters org={org} orgs={listOrgs} values={tickets} onChange={setTickets} />
      <TaskTable
        org={org}
        orgs={listOrgs}
        listId="tasks.overview"
        wgs={wgs}
        state={key}
        mine={mine ? userId : null}
        search={search}
        githubOrg={tickets.githubOrg}
        githubRepo={tickets.githubRepo}
        showWorkgroup
        empty={
          filtered ? (
            <div className="flex flex-wrap items-center gap-2 text-gray-700">
              <p>{t('tasks.overview.no-match')}</p>
              <button type="button" className="min-h-11 underline" onClick={reset} data-testid="tasks.filter.reset">
                {t('tasks.filter.reset')}
              </button>
            </div>
          ) : (
            <div className="flex flex-col items-start gap-2 text-gray-700">
              <p>{t('tasks.overview.empty')}</p>
              <p>{t('tasks.overview.how')}</p>
              <Link
                className="flex min-h-11 items-center rounded bg-blue-700 px-4 text-white"
                to={home}
                data-testid="tasks.overview.create"
              >
                {t('tasks.overview.create')}
              </Link>
            </div>
          )
        }
      />
    </main>
  );
}
