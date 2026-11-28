// Task overview of an organization (KOR-32, route `/o/:org/tasks`, right `org:member`): every task of the workgroups
// the user can read (from the verified membership log; the others are never requested), newest activity first, with
// title, workgroup, state, owner and last activity. Filters: state (hub side), workgroup (only that list is loaded),
// "only mine" and the title search (in the browser: titles are encrypted; bounded per request, `scan.ts`). The empty
// state says how a task is created.
// Filters live in the screen's state, not in the URL: a search text in the address would reach the hub on a reload.
// The one exception is the situation (`?situation=mine|blocked|working|done`, the counters of the situation view link
// here, `core/attention/situation.ts`): chosen, the list shows exactly the tasks its counter counts, as the hub's
// attention list (across the readable workgroups, paged by the hub), and the other filters give way to it.
import { useId, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { isSituation, type Situation, SITUATIONS, situationList } from '../../../core/attention/situation';
import { readableWorkgroups } from '../../../core/tasks/overview';
import { useNow } from '../../countdown/useNow';
import { useI18n } from '../../i18n';
import { AttentionGroup } from '../home/AttentionGroup';
import { useDirectory, useOrgView } from './hooks';
import { StateFilter, TaskTable } from './TaskTable';

/** The situation filter: every task, or the tasks of one counter of the situation view. */
function SituationFilter({
  value,
  onChange,
}: {
  readonly value: Situation | null;
  readonly onChange: (value: Situation | null) => void;
}) {
  const { t } = useI18n();
  const id = useId();
  return (
    <div className="flex w-full min-w-0 items-center gap-2 sm:w-auto">
      <label htmlFor={id} className="text-sm">
        {t('tasks.filter.situation')}
      </label>
      <select
        id={id}
        className="min-h-11 min-w-0 max-w-full flex-1 rounded border border-gray-300 px-2 sm:flex-none"
        value={value ?? ''}
        onChange={(event) => onChange(isSituation(event.target.value) ? event.target.value : null)}
        data-testid="tasks.filter.situation"
      >
        <option value="">{t('tasks.filter.situation.all')}</option>
        {SITUATIONS.map((name) => (
          <option key={name} value={name}>
            {t(`attention.counter.${name}`)}
          </option>
        ))}
      </select>
    </div>
  );
}

export function OverviewScreen() {
  const { org } = useParams() as { readonly org: string };
  const { t } = useI18n();
  const wgId = useId();
  const searchId = useId();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  const now = useNow(true, 30_000);
  const [params, setParams] = useSearchParams();
  const asked = params.get('situation');
  const situation = isSituation(asked) ? asked : null;
  const [filter, setFilter] = useState<string | null>(null);
  const [chosen, setChosen] = useState('');
  const [mine, setMine] = useState(false);
  const [search, setSearch] = useState('');
  const choose = (next: Situation | null) => {
    const changed = new URLSearchParams(params);
    if (next === null) changed.delete('situation');
    else changed.set('situation', next);
    setParams(changed, { replace: true });
  };
  // until the log is verified and the user is known
  const loading = (
    <p className="p-4" data-testid="tasks.loading">
      {t('tasks.loading')}
    </p>
  );
  if (state === null) return loading;
  if (userId === null) return loading;
  const readable = readableWorkgroups(state, userId);
  // a workgroup that is no longer readable (rights changed meanwhile) falls back to all
  const wg = readable.includes(chosen) ? chosen : '';
  const filtered = filter !== null || wg !== '' || mine || search.trim() !== '';
  const reset = () => {
    setFilter(null);
    setChosen('');
    setMine(false);
    setSearch('');
  };
  const name = (id: string) => {
    const label = directory.workgroups.get(id) ?? t('tasks.wg.unnamed');
    return state.writable(id) ? label : t('tasks.overview.archived', { name: label });
  };
  const home = `/o/${encodeURIComponent(org)}`;
  const heading = (
    <div className="flex flex-col gap-1">
      <h1 className="text-xl font-semibold" data-testid="tasks.overview.title">
        {t('tasks.overview.title')}
      </h1>
      <p className="text-sm text-gray-700">{t('tasks.overview.intro')}</p>
    </div>
  );
  if (situation !== null) {
    const list = situationList(situation, userId, now);
    return (
      <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="tasks.overview">
        {heading}
        <div className="flex flex-wrap items-end gap-x-4 gap-y-2" data-testid="tasks.filters">
          <SituationFilter value={situation} onChange={choose} />
        </div>
        <AttentionGroup
          org={org}
          category={list.category}
          filter={list.filter}
          title={t(`attention.counter.${situation}`)}
          empty={t(`tasks.situation.empty.${situation}`)}
          state={state}
          userId={userId}
          directory={directory}
          now={now}
        />
      </main>
    );
  }
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="tasks.overview">
      {heading}
      <div className="flex flex-wrap items-end gap-x-4 gap-y-2" data-testid="tasks.filters">
        <SituationFilter value={null} onChange={choose} />
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
        <div className="flex w-full min-w-0 items-center gap-2 sm:w-auto">
          <label htmlFor={wgId} className="text-sm">
            {t('tasks.filter.wg')}
          </label>
          <select
            id={wgId}
            className="min-h-11 min-w-0 max-w-full flex-1 rounded border border-gray-300 px-2 sm:flex-none"
            value={wg}
            onChange={(event) => setChosen(event.target.value)}
            data-testid="tasks.filter.wg"
          >
            <option value="">{t('tasks.filter.wg.all')}</option>
            {readable.map((id) => (
              <option key={id} value={id}>
                {name(id)}
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
      </div>
      <TaskTable
        org={org}
        listId="tasks.overview"
        wgs={wg === '' ? readable : [wg]}
        state={filter}
        mine={mine ? userId : null}
        search={search}
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
