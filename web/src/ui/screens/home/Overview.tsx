// The overview of the open tasks at the top of the start page (contracts/changes/HOME-COUNTS.md): three numbers over
// every organization of the user and every workgroup they read – "In progress" (an agent works), "Waiting for a
// question" (a person must answer, approve or decide) and "No agent" (nobody works on it and nobody is asked). The hub
// counts them from the attention records (`open_working`, `open_waiting`, `open_no_agent` of the counters); every open
// task counts in exactly one. A number is a button: it shows exactly its tasks (the list below, `?show=`), and names
// the other organizations whose tasks it counts, each a link to the same list there.
//
// The counters of the shown org follow its live channel (`useSummary`, after every batch of `attention` frames and
// every reconnect); those of the other orgs are loaded when shown, after every reconnect and every `OTHER_ORGS_MS`
// (the live channel runs for one org at a time, 03-api.md section 5).
import { useEffect, useMemo } from 'react';
import { Link } from 'react-router-dom';
import { meOrgs } from '../../../core/account/session';
import { refreshSummary } from '../../../core/attention/load';
import { formatNumber } from '../../../core/i18n';
import { type AttentionSummary, type Overview as OverviewName, OVERVIEWS } from '../../../core/store/slices/attention';
import { orgEntry } from '../../../core/store/slices/orgs';
import { useLiveReload } from '../../hooks/useLiveReload';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useDirectory } from '../tasks/hooks';

/** The counters of the other orgs are loaded again this often. */
export const OTHER_ORGS_MS = 30_000;

const FIELD: Readonly<Record<OverviewName, keyof AttentionSummary>> = {
  working: 'open_working',
  waiting: 'open_waiting',
  no_agent: 'open_no_agent',
};

/** The numbers summed over the orgs whose counters are known; null while none is. */
export function overviewTotals(
  orgs: readonly string[],
  summaries: Readonly<Record<string, AttentionSummary>>,
): Readonly<Record<OverviewName, number>> | null {
  const known = orgs.map((org) => summaries[org]).filter((found) => found !== undefined);
  if (known.length === 0) return null;
  const total = { working: 0, waiting: 0, no_agent: 0 };
  for (const summary of known) for (const name of OVERVIEWS) total[name] += summary[FIELD[name]];
  return total;
}

/** Every org of the user, the shown one first. */
function useOrgs(org: string): readonly string[] {
  const { app } = useAppEnv();
  const profile = useStore((state) => state.me.profile, app.store);
  return useMemo(
    () => [org, ...meOrgs(profile).map((item) => item.org_id)].filter((id, at, all) => all.indexOf(id) === at),
    [org, profile],
  );
}

function OrgName({ org }: { readonly org: string }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const verified = useStore((state) => orgEntry(state.orgs, org).log !== null, app.store);
  const directory = useDirectory(org, verified);
  return <>{directory.orgName ?? t('attention.overview.org.unnamed')}</>;
}

export interface OverviewProps {
  /** The shown org (its counters are kept live by the page). */
  readonly org: string;
  /** The number whose tasks are shown, or null. */
  readonly show: OverviewName | null;
  readonly onShow: (show: OverviewName | null) => void;
}

export function Overview({ org, show, onShow }: OverviewProps) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const orgs = useOrgs(org);
  const others = useMemo(() => orgs.slice(1), [orgs]);
  const summaries = useStore((state) => state.attention.summaries, app.store);
  useEffect(() => {
    const load = () => {
      for (const other of others) refreshSummary(app.tasks, other);
    };
    load();
    const timer = setInterval(load, OTHER_ORGS_MS);
    return () => clearInterval(timer);
  }, [app, others]);
  useLiveReload(app.store, () => {
    for (const other of others) refreshSummary(app.tasks, other);
  });
  const totals = overviewTotals(orgs, summaries);
  const elsewhere =
    show === null
      ? []
      : others
          .map((other) => ({ org: other, count: summaries[other]?.[FIELD[show]] ?? 0 }))
          .filter((item) => item.count > 0);
  return (
    <section className="flex flex-col gap-2" aria-labelledby="home-overview-title" data-testid="overview">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <h2 id="home-overview-title" className="text-xl font-semibold">
          {t('attention.overview.label')}
        </h2>
        <p className="text-sm text-gray-700">{t('attention.overview.scope')}</p>
      </div>
      <ul className="grid grid-cols-1 gap-2 sm:grid-cols-3" data-testid="overview.counts">
        {OVERVIEWS.map((name) => {
          const count = totals?.[name] ?? null;
          const pressed = show === name;
          return (
            <li key={name} className="flex">
              <button
                type="button"
                className={`flex min-h-11 w-full flex-row items-center gap-3 rounded border px-3 py-2 text-left sm:flex-col sm:items-start sm:gap-0 ${
                  pressed ? 'border-blue-700 bg-blue-50' : 'border-gray-200'
                }`}
                aria-pressed={pressed}
                onClick={() => onShow(pressed ? null : name)}
                data-testid={`overview.count.${name}`}
                data-count={count ?? ''}
              >
                <span className="text-3xl font-semibold" data-testid="overview.count.value">
                  {count === null ? '–' : formatNumber(count, locale)}
                </span>
                <span className="flex flex-col">
                  <span className="font-medium">{t(`attention.overview.${name}`)}</span>
                  <span className="text-sm text-gray-700">{t(`attention.overview.hint.${name}`)}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {elsewhere.length > 0 && (
        <ul className="flex flex-col text-sm" data-testid="overview.elsewhere">
          {elsewhere.map((item) => (
            <li key={item.org}>
              <Link
                className="flex min-h-11 items-center gap-1 underline"
                to={`/o/${encodeURIComponent(item.org)}?show=${show}`}
                data-testid="overview.elsewhere.org"
                data-org={item.org}
                data-count={item.count}
              >
                {t('attention.overview.other', { count: formatNumber(item.count, locale) })}: <OrgName org={item.org} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
