// Home of an organization (06-web.md sections 4.0 and 4.1, route `/o/:org`, right `org:member`): the situation view.
//
// - First, at the very top, the overview of the open tasks (`Overview.tsx`, contracts/changes/HOME-COUNTS.md): "In
//   progress", "Waiting for a question", "No agent" over every org and workgroup the user reads. A number shows exactly
//   its tasks of this org right below it (`?show=working|waiting|no_agent`, the lists `working`, `needs_decision` and
//   `no_agent`) instead of the groups further down; "Show everything" brings the groups back.
// - At the top a compact composer (section 4.1): the context line with the organization and the workgroup selector
//   (preselected with the default workgroup; only workgroups with `wg:write`) and the prompt field. The suggestion
//   chips are shown while the organization has no task yet, otherwise behind "Suggestions". A user without write
//   access anywhere sees a short note instead (controls the rights do not allow are not rendered, section 1). Next to
//   it "Optimize processes" (ADR-097 section 6): an analysis of the organization, in the default workgroup unless
//   chosen otherwise.
// - The counters: "Waiting for me", "Stuck", "Running", "Finished today" (since local midnight), and below them the
//   organization's subscriptions ("Subscriptions: n available · m used up – next release 14:05", ADR-081 section 6),
//   then the card "My subscriptions" with every subscription of the user (KOR-68, `ui/subscriptions/`).
// - The four groups in this order: "Needs your decision", "Stuck", "Running", "Recently finished" (the last links the
//   default workgroup's full task list), each a windowed list paged by the hub (`AttentionGroup.tsx`).
// - The switch "By situation | By person" (section 4.0a, ADR-095 section 5; kept in the URL as `?by=person`, no
//   navigation entry of its own): by person the counters stay and the groups give way to one row per person with
//   their main agents (`people/PeopleView.tsx`).
// Live `attention` frames update the entries and the counters (batched in the store, counters at most once a second).
import { useId, useMemo, useState } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { selectNoTask } from '../../../core/attention/view';
import {
  CATEGORIES,
  type AttentionSummary,
  type Overview as OverviewName,
  OVERVIEW_LIST,
  OVERVIEWS,
} from '../../../core/store/slices/attention';
import { formatNumber } from '../../../core/i18n';
import { writableWorkgroups } from '../../../core/tasks/send';
import { useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { Composer } from '../../composer/Composer';
import { AnalysisButton } from '../../analysis/AnalysisButton';
import { useAppEnv } from '../../shell/context';
import { useDirectory, useOrgView } from '../tasks/hooks';
import '../../../core/i18n/lazy/mainagent';
import { AttentionGroup } from './AttentionGroup';
import { Overview } from './Overview';
import { PeopleView } from './people/PeopleView';
import { useSummary } from './hooks';
import { QuotaCounter } from './QuotaCounter';
import { SubscriptionsCard } from '../../subscriptions/SubscriptionsCard';

/** The counters in their order: test ID suffix and field of the summary. */
const COUNTERS: readonly [string, keyof AttentionSummary][] = [
  ['mine', 'needs_decision_mine'],
  ['blocked', 'blocked'],
  ['working', 'working'],
  ['done', 'done_since'],
];

function Counters({ org }: { readonly org: string }) {
  const { t, locale } = useI18n();
  const summary = useSummary(org);
  return (
    <ul
      className="grid grid-cols-2 gap-2 sm:grid-cols-4"
      aria-label={t('attention.counters.label')}
      data-testid="attention.counters"
    >
      {COUNTERS.map(([name, field]) => (
        <li
          key={name}
          className="flex min-h-11 flex-col rounded border border-gray-200 px-3 py-2"
          data-testid={`attention.counter.${name}`}
          data-count={summary === null ? '' : summary[field]}
        >
          <span className="text-2xl font-semibold">
            {summary === null ? '–' : formatNumber(summary[field], locale)}
          </span>
          <span className="text-sm text-gray-700">{t(`attention.counter.${name}`)}</span>
        </li>
      ))}
    </ul>
  );
}

/** The number of the overview whose tasks are shown (`?show=working|waiting|no_agent`; anything else: none). */
export const shownOverview = (value: string | null): OverviewName | null =>
  OVERVIEWS.find((name) => name === value) ?? null;

/** The shown org's count of one number of the overview (null until its counters are known). */
export function overviewCount(summary: AttentionSummary | null, name: OverviewName): number | null {
  if (summary === null) return null;
  return name === 'working' ? summary.open_working : name === 'waiting' ? summary.open_waiting : summary.open_no_agent;
}

/** The grouping of the situation view (`?by=person`; anything else is "by situation"). */
export type GroupBy = 'situation' | 'person';

function GroupSwitch({ by, onChange }: { readonly by: GroupBy; readonly onChange: (by: GroupBy) => void }) {
  const { t } = useI18n();
  return (
    <div
      role="group"
      aria-label={t('mainagent.switch.label')}
      className="flex self-start rounded border border-gray-300"
      data-testid="agents.switch"
      data-by={by}
    >
      {(['situation', 'person'] as const).map((value) => (
        <button
          key={value}
          type="button"
          className={`min-h-11 px-4 text-sm ${by === value ? 'bg-blue-700 text-white' : 'text-gray-800'}`}
          aria-pressed={by === value}
          onClick={() => onChange(value)}
          data-testid={`agents.switch.${value}`}
        >
          {t(`mainagent.switch.${value}`)}
        </button>
      ))}
    </div>
  );
}

export function HomeScreen() {
  const { org } = useParams() as { readonly org: string };
  const { t } = useI18n();
  const selectId = useId();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  const summary = useSummary(org);
  const { app } = useAppEnv();
  const empty = useStore(
    useMemo(() => selectNoTask(org), [org]),
    app.store,
  );
  const now = useNow(true, 30_000);
  const [chosen, setChosen] = useState<string | null>(null);
  const [chips, setChips] = useState(false);
  const [search, setSearch] = useSearchParams();
  const by: GroupBy = search.get('by') === 'person' ? 'person' : 'situation';
  const show = shownOverview(search.get('show'));
  const group = (next: GroupBy) => {
    const params = new URLSearchParams(search);
    if (next === 'person') params.set('by', 'person');
    else params.delete('by');
    setSearch(params);
  };
  const choose = (next: OverviewName | null) => {
    const params = new URLSearchParams(search);
    if (next === null) params.delete('show');
    else params.set('show', next);
    setSearch(params);
  };
  const loading = (
    <p className="p-4" data-testid="home.loading">
      {t('home.loading')}
    </p>
  );
  // two checks (KOR-32): the merged coverage of Vitest and the browser counts each condition on its own
  if (state === null) return loading;
  if (userId === null) return loading;
  const writable = writableWorkgroups(state, userId);
  const wg = chosen !== null && writable.includes(chosen) ? chosen : writable[0];
  const wgName = (id: string) =>
    directory.workgroups.get(id) ?? (id === state.defaultWg ? t('home.wg.default') : t('home.wg.unnamed'));
  const counts: Readonly<Record<string, number | null>> = {
    needs_decision: summary?.needs_decision ?? null,
    blocked: summary?.blocked ?? null,
    working: summary?.working ?? null,
  };
  const allTasks = (
    <Link
      className="flex min-h-11 items-center text-sm underline"
      to={`/o/${encodeURIComponent(org)}/w/${encodeURIComponent(state.defaultWg)}`}
      data-testid="home.all-tasks"
    >
      {t('attention.all-tasks', { wg: wgName(state.defaultWg) })}
    </Link>
  );
  const showAll = (
    <button
      type="button"
      className="flex min-h-11 items-center text-sm underline"
      onClick={() => choose(null)}
      data-testid="overview.all"
    >
      {t('attention.overview.all')}
    </button>
  );
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="home.screen">
      <Overview org={org} show={show} onShow={choose} />
      {show !== null && (
        <AttentionGroup
          key={show}
          org={org}
          category={OVERVIEW_LIST[show]}
          state={state}
          userId={userId}
          directory={directory}
          now={now}
          count={overviewCount(summary, show)}
          extra={showAll}
        />
      )}
      <section className="flex flex-col gap-2" data-testid="attention.compose">
        <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="home.context">
          <span data-testid="home.org">{directory.orgName ?? t('home.org.unnamed')}</span>
          <span aria-hidden="true">/</span>
          {wg !== undefined && (
            <>
              <label htmlFor={selectId} className="sr-only">
                {t('home.wg.label')}
              </label>
              <select
                id={selectId}
                className="min-h-11 rounded border border-gray-300 px-2"
                value={wg}
                onChange={(event) => setChosen(event.target.value)}
                data-testid="home.wg"
              >
                {writable.map((id) => (
                  <option key={id} value={id}>
                    {wgName(id)}
                  </option>
                ))}
              </select>
              {!empty && (
                <button
                  type="button"
                  className="min-h-11 px-2 text-blue-800 underline"
                  aria-expanded={chips}
                  onClick={() => setChips(!chips)}
                  data-testid="home.suggestions"
                >
                  {t('attention.suggestions')}
                </button>
              )}
              <AnalysisButton org={org} scope="org" workgroups={writable} initial={writable[0]!} name={wgName} />
            </>
          )}
        </div>
        {wg === undefined ? (
          <p className="text-gray-700" data-testid="home.read-only">
            {t('home.read-only')}
          </p>
        ) : (
          <Composer org={org} wg={wg} suggestions={empty || chips} />
        )}
      </section>
      <div className="flex flex-col gap-1" data-testid="attention.screen">
        <h1 className="text-xl font-semibold">{t('attention.title')}</h1>
        <p className="text-sm text-gray-700">{t('attention.intro')}</p>
      </div>
      <GroupSwitch by={by} onChange={group} />
      <Counters org={org} />
      <QuotaCounter org={org} />
      <SubscriptionsCard />
      {show === null && by === 'person' && (
        <PeopleView org={org} state={state} userId={userId} directory={directory} now={now} />
      )}
      {show === null &&
        by === 'situation' &&
        CATEGORIES.map((category) => (
          <AttentionGroup
            key={category}
            org={org}
            category={category}
            state={state}
            userId={userId}
            directory={directory}
            now={now}
            {...(category !== 'done' && { count: counts[category] ?? null })}
            {...(category === 'done' && { extra: allTasks })}
          />
        ))}
    </main>
  );
}
