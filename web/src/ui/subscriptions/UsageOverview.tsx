// The overview "Who used how much" of the page "My subscriptions" (ADR-103, `core/subscriptions/usage.ts`): per
// subscription a table of the people who used it – tokens (input, output, cache read, cache write, total), sessions,
// the share of the tokens and the estimated share of the quota – for the running 5-hour window, the running week or a
// span the user chooses. The share of the quota is always marked as estimated (the provider measures per
// subscription only). The owner sees every person on their subscriptions; on another person's subscription the user
// sees their own row only. People are named by the names of the active organization (an agent as "Agent of …"),
// oneself as "you". Below, the usage through DFX access per person is taken from the organization's billing (ADR-083)
// with a link to the settings section "Credit and usage". Numbers only, no content. The tables scroll sideways on a
// phone; the page itself never does.
import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { loadOrgBilling, type OrgBilling } from '../../core/billing/billing';
import { requestErrorCode } from '../../core/requests/client';
import type { SubscriptionRow } from '../../core/subscriptions/subscriptions';
import { selectDirectory } from '../../core/tasks/directory';
import {
  dayOf,
  formatTokens,
  loadUsage,
  MAX_DAYS,
  PERIODS,
  rangeOf,
  type PersonUsage,
  type SubscriptionUsage,
  type UsageAnswer,
  type UsageFigures,
  type UsagePeriod,
} from '../../core/subscriptions/usage';
import { useStore } from '../hooks/useStore';
import { personName } from '../names/names';
import { useReason } from '../settings/sections/github/reason';
import { useAppEnv } from '../shell/context';
import { PeopleUsage } from '../task-tabs/people';
import { percent, useTexts } from './ProviderList';
import { REFRESH_MS } from './useSubscriptions';
import '../../core/i18n/lazy/subusage';

const DAY_MS = 86_400_000;
const CELL = 'px-2 py-1 text-right tabular-nums whitespace-nowrap';
const COLUMNS = ['input', 'output', 'cache_read', 'cache_write', 'tokens', 'sessions', 'share', 'estimated'] as const;

interface Range {
  readonly from: string;
  readonly to: string;
}

function useUsage(period: UsagePeriod, range: Range) {
  const { app } = useAppEnv();
  const [answer, setAnswer] = useState<{ key: string; value: UsageAnswer } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [round, setRound] = useState(0);
  const span = period === 'range' ? rangeOf(range.from, range.to) : null;
  const since = span?.since ?? null;
  const until = span?.until ?? null;
  const key = `${period}:${since ?? ''}:${until ?? ''}`;
  const invalid = period === 'range' && span === null;
  useEffect(() => {
    if (invalid) return;
    let live = true;
    const read = () =>
      loadUsage(app.tasks, period, since === null || until === null ? undefined : { since, until }).then(
        (value) => {
          if (!live) return;
          setAnswer({ key, value });
          setError(null);
        },
        (failure: unknown) => live && setError(requestErrorCode(failure)),
      );
    void read();
    const timer = setInterval(() => void read(), REFRESH_MS);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [app, period, since, until, key, invalid, round]);
  const reload = useCallback(() => {
    setError(null);
    setRound((value) => value + 1);
  }, []);
  return { answer: answer !== null && answer.key === key ? answer.value : null, error, invalid, reload };
}

function Figures({ row, locale }: { readonly row: UsageFigures; readonly locale: string }) {
  return (
    <>
      <td className={CELL}>{formatTokens(row.inputTokens, locale)}</td>
      <td className={CELL}>{formatTokens(row.outputTokens, locale)}</td>
      <td className={CELL}>{formatTokens(row.cacheReadTokens, locale)}</td>
      <td className={CELL}>{formatTokens(row.cacheWriteTokens, locale)}</td>
      <td className={CELL} data-testid="subusage.tokens">
        {formatTokens(row.tokens, locale)}
      </td>
      <td className={CELL}>{row.sessions}</td>
    </>
  );
}

function PersonRow({ person, name }: { readonly person: PersonUsage; readonly name: string }) {
  const { t, locale } = useTexts();
  return (
    <tr className="border-t border-gray-200" data-testid="subusage.person" data-user={person.userId}>
      <th scope="row" className="px-2 py-1 text-left font-normal whitespace-nowrap">
        {name}
      </th>
      <Figures row={person} locale={locale} />
      <td className={CELL}>{percent(person.tokenShare * 100)}</td>
      <td className={CELL} data-testid="subusage.estimated">
        {person.estimatedPercent === null
          ? '—'
          : t('subusage.estimated', { percent: percent(person.estimatedPercent) })}
      </td>
    </tr>
  );
}

function UsageTable({
  usage,
  title,
  name,
}: {
  readonly usage: SubscriptionUsage;
  readonly title: string;
  readonly name: (id: string) => string;
}) {
  const { t, locale } = useTexts();
  return (
    <div
      className="overflow-x-auto rounded border border-gray-200 bg-white"
      // the table scrolls sideways on a phone: keyboard users reach it, and it is named (axe scrollable-region-focusable)
      tabIndex={0}
      role="region"
      aria-label={title}
      data-testid="subusage.table"
    >
      <table className="w-full min-w-[48rem] text-left text-sm">
        <thead className="bg-gray-100">
          <tr>
            <th scope="col" className="px-2 py-1 font-semibold whitespace-nowrap">
              {t('subusage.col.person')}
            </th>
            {COLUMNS.map((column) => (
              <th key={column} scope="col" className="px-2 py-1 text-right font-semibold whitespace-nowrap">
                {t(`subusage.col.${column}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {usage.people.map((person) => (
            <PersonRow key={person.userId} person={person} name={name(person.userId)} />
          ))}
        </tbody>
        {usage.total !== null && usage.people.length > 1 && (
          <tfoot>
            <tr className="border-t-2 border-gray-300 font-semibold" data-testid="subusage.total">
              <th scope="row" className="px-2 py-1 text-left">
                {t('subusage.total')}
              </th>
              <Figures row={usage.total} locale={locale} />
              <td className={CELL}>{percent(100)}</td>
              <td className={CELL}>
                {usage.measured === null
                  ? '—'
                  : t('subusage.estimated', { percent: percent(usage.measured.usedPercent) })}
              </td>
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  );
}

function SubscriptionBlock({
  usage,
  title,
  name,
  now,
}: {
  readonly usage: SubscriptionUsage;
  readonly title: string;
  readonly name: (id: string) => string;
  readonly now: number;
}) {
  const { t, locale, age } = useTexts();
  const when = (at: number) => new Date(at).toLocaleString(locale, { dateStyle: 'short', timeStyle: 'short' });
  return (
    <section
      className="flex min-w-0 flex-col gap-1"
      data-testid="subusage.subscription"
      data-key={usage.subscriptionId}
      data-own={usage.own ? 'yes' : 'no'}
    >
      <h3 className="font-semibold">{title}</h3>
      <p className="text-sm text-gray-700">
        {t('subusage.span', { since: when(usage.since), until: when(usage.until) })}
      </p>
      <p className="text-sm text-gray-700" data-testid="subusage.measured">
        {usage.measured !== null
          ? t('subusage.measured', {
              percent: percent(usage.measured.usedPercent),
              age: age(usage.measured.fetchedAt, now),
            })
          : t('subusage.measured.none')}
      </p>
      {!usage.own && <p className="text-sm text-gray-700">{t('subusage.foreign.note')}</p>}
      {usage.people.length === 0 ? (
        <p className="text-sm text-gray-700" data-testid="subusage.none">
          {t('subusage.none')}
        </p>
      ) : (
        <UsageTable usage={usage} title={title} name={name} />
      )}
    </section>
  );
}

function DfxAccess({ org }: { readonly org: string | null }) {
  const { app } = useAppEnv();
  const { t } = useTexts();
  const [billing, setBilling] = useState<{ org: string; value: OrgBilling } | null>(null);
  useEffect(() => {
    if (org === null) return;
    let live = true;
    // the billing of an older hub or a refused read leaves the link only
    loadOrgBilling(app.tasks, org).then(
      (value) => live && setBilling({ org, value }),
      () => undefined,
    );
    return () => {
      live = false;
    };
  }, [app, org]);
  const people = billing !== null && billing.org === org ? billing.value.people : null;
  return (
    <section className="flex flex-col gap-1" data-testid="subusage.dfx">
      <h3 className="font-semibold">{t('subusage.dfx.title')}</h3>
      <p className="text-sm text-gray-700">{t('subusage.dfx.intro')}</p>
      {org === null && <p className="text-sm text-gray-700">{t('subusage.dfx.none')}</p>}
      {org !== null && people !== null && <PeopleUsage org={org} rows={people} testId="subusage.dfx.people" />}
      <Link
        className="flex min-h-11 items-center self-start underline"
        to="/settings#billing"
        data-testid="subusage.dfx.link"
      >
        {t('subusage.dfx.link')}
      </Link>
    </section>
  );
}

export function UsageOverview({ rows, now }: { readonly rows: readonly SubscriptionRow[]; readonly now: number }) {
  const { app } = useAppEnv();
  const { t, rowName } = useTexts();
  const reason = useReason();
  const org = useStore((state) => state.session.activeOrg, app.store);
  const me = useStore((state) => state.me.userId, app.store);
  const directory = useStore(selectDirectory(org ?? ''), app.store);
  const [period, setPeriod] = useState<UsagePeriod>('five_hour');
  const [range, setRange] = useState<Range>(() => ({ from: dayOf(Date.now() - 6 * DAY_MS), to: dayOf(Date.now()) }));
  const { answer, error, invalid, reload } = useUsage(period, range);
  const name = (id: string) => (id === me ? t('subusage.you') : personName(t, directory, id));
  const byKey = new Map(rows.map((row) => [row.key, row]));
  const title = (usage: SubscriptionUsage) => {
    const row = byKey.get(usage.subscriptionId);
    if (row !== undefined) return rowName(row);
    return usage.own
      ? t('subscriptions.unnamed')
      : t('subusage.foreign', { name: personName(t, directory, usage.ownerId) });
  };
  return (
    <section className="flex min-w-0 flex-col gap-3" data-testid="subusage.section" aria-labelledby="subusage-title">
      <header className="flex flex-col gap-1">
        <h2 id="subusage-title" className="text-xl font-semibold">
          {t('subusage.title')}
        </h2>
        <p className="text-sm text-gray-700">{t('subusage.intro')}</p>
      </header>
      <fieldset className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <legend className="sr-only">{t('subusage.period')}</legend>
        {PERIODS.map((value) => (
          <label key={value} className="flex min-h-11 items-center gap-2">
            <input
              type="radio"
              name="subusage-period"
              value={value}
              checked={period === value}
              onChange={() => setPeriod(value)}
              className="h-4 w-4"
              data-testid={`subusage.period.${value}`}
            />
            {t(`subusage.period.${value}`)}
          </label>
        ))}
      </fieldset>
      {period === 'range' && (
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <label className="flex min-h-11 items-center gap-2">
            {t('subusage.from')}
            <input
              type="date"
              value={range.from}
              onChange={(event) => setRange({ ...range, from: event.target.value })}
              className="rounded border border-gray-400 px-2 py-1"
              data-testid="subusage.from"
            />
          </label>
          <label className="flex min-h-11 items-center gap-2">
            {t('subusage.to')}
            <input
              type="date"
              value={range.to}
              onChange={(event) => setRange({ ...range, to: event.target.value })}
              className="rounded border border-gray-400 px-2 py-1"
              data-testid="subusage.to"
            />
          </label>
        </div>
      )}
      <p className="text-sm text-gray-700" role="note" data-testid="subusage.estimate-note">
        {period === 'range' ? t('subusage.measured.range') : t('subusage.estimate.note')}
      </p>
      {invalid && (
        <p role="alert" data-testid="subusage.range-invalid">
          {t('subusage.range.invalid', { days: MAX_DAYS })}
        </p>
      )}
      {!invalid && error !== null && (
        <p role="alert" data-testid="subusage.load-failed" data-code={error}>
          {t('subusage.load-failed', { reason: reason(error) })}{' '}
          <button type="button" className="min-h-11 underline" onClick={reload} data-testid="subusage.reload">
            {t('subusage.reload')}
          </button>
        </p>
      )}
      {!invalid && error === null && answer === null && <p data-testid="subusage.loading">{t('subusage.loading')}</p>}
      {!invalid && answer !== null && answer.subscriptions.length === 0 && (
        <p className="text-gray-700" data-testid="subusage.empty">
          {t('subusage.empty')}
        </p>
      )}
      {!invalid &&
        answer !== null &&
        answer.subscriptions.map((usage) => (
          <SubscriptionBlock key={usage.subscriptionId} usage={usage} title={title(usage)} name={name} now={now} />
        ))}
      <DfxAccess org={org} />
    </section>
  );
}
