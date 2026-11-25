// The provider rows of "My subscriptions" (KOR-68, after the page "Subscriptions" of the local dashboard): per provider
// a line with the time to the reset, a bar and the used share of the week of its most-used account; opened, the
// 5-hour window, the week and the week per model at the highest use of the accounts, then every account with its own
// bar and the age of its measurement, and the age of the provider's newest read. Nothing missing is shown as 0 %.
// Used by the card of the home page and by the full page.
import { useState } from 'react';
import {
  ageParts,
  FULLEST,
  headlineOf,
  type ProviderGroup,
  type QuotaWindow,
  type SubscriptionRow,
  untilParts,
  usedLevel,
} from '../../core/subscriptions/subscriptions';
import { useI18n } from '../i18n';
import '../../core/i18n/lazy/subscriptions';

/** Older than this, a measurement is marked old. */
export const OLD_MS = 60 * 60 * 1000;

const LEVEL_CLASS = { ok: 'bg-green-600', warn: 'bg-amber-500', low: 'bg-red-600' } as const;
// One line: name | time to the reset | bar | percent. On a phone the bar takes a second row of its own below the
// three texts, so nothing is squeezed and nothing overflows; from `sm` on the four sit in one row, the columns of every
// line aligned.
const GRID =
  'grid grid-cols-[minmax(0,1fr)_auto_3.5rem] items-center gap-x-2 gap-y-1 sm:grid-cols-[minmax(7rem,14rem)_6.5rem_minmax(4rem,1fr)_4rem] sm:gap-x-3';
/** Lines inside an opened provider start where the name of the provider starts (after the triangle). */
const INDENT = 'pl-5';
/** The text that stands in place of reset, bar and percent. */
const NONE = 'col-span-2 text-sm text-gray-700 sm:col-span-3';
const RESET = 'text-sm whitespace-nowrap text-gray-600';
const USED = 'text-right text-sm tabular-nums';

/** Texts of the area with the time to a reset, the age of a read and the name of a window. */
export function useTexts() {
  const { t, locale } = useI18n();
  const until = (at: number | null, now: number): string => {
    if (at === null) return '';
    const parts = untilParts(at, now);
    return t(`subscriptions.until.${parts.kind}`, { a: parts.a, b: parts.b });
  };
  const age = (at: number, now: number): string => {
    const parts = ageParts(at, now);
    return t(`subscriptions.age.${parts.unit}`, { n: parts.value });
  };
  const windowName = (window: string): string =>
    window === FULLEST
      ? t('subscriptions.fullest')
      : window.startsWith('weekly:')
        ? t('subscriptions.window.model', { model: window.slice('weekly:'.length) })
        : t(`subscriptions.window.${window}`);
  const rowName = (row: SubscriptionRow): string => row.name ?? (row.primary ? t('subscriptions.own') : row.key);
  return { t, locale, until, age, windowName, rowName };
}

export const percent = (used: number): string => `${Math.round(used * 10) / 10}%`;

export function Bar({
  used,
  label,
  testId,
}: {
  readonly used: number;
  readonly label: string;
  readonly testId: string;
}) {
  const level = usedLevel(used);
  return (
    <span
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(used)}
      aria-label={label}
      className="order-last col-span-3 block h-2.5 w-full overflow-hidden rounded bg-gray-200 sm:order-none sm:col-span-1"
      data-testid={testId}
      data-level={level}
    >
      <span className={`block h-full ${LEVEL_CLASS[level]}`} style={{ width: `${Math.min(used, 100)}%` }} />
    </span>
  );
}

function Line({
  name,
  window,
  now,
  testId,
}: {
  readonly name: string;
  readonly window: QuotaWindow;
  readonly now: number;
  readonly testId: string;
}) {
  const { until } = useTexts();
  return (
    <div className={`${GRID} py-0.5`} data-testid={testId}>
      <span className={`${INDENT} truncate text-sm`}>{name}</span>
      <span className={RESET}>{until(window.resetsAt, now)}</span>
      <Bar used={window.usedPercent} label={name} testId={`${testId}.bar`} />
      <span className={USED}>{percent(window.usedPercent)}</span>
    </div>
  );
}

function Account({ row, now }: { readonly row: SubscriptionRow; readonly now: number }) {
  const { t, age, until, rowName } = useTexts();
  const head = headlineOf(row);
  const read = row.reading?.fetchedAt ?? null;
  const name = rowName(row);
  const old = read !== null && now - read > OLD_MS;
  return (
    <li className={`${GRID} py-1`} data-testid="subscriptions.account" data-key={row.key} data-status={row.status}>
      <span className={`${INDENT} min-w-0 text-sm [overflow-wrap:anywhere]`}>
        <span className="font-medium">{name}</span>
        {row.email !== null && <span className="text-gray-600"> {row.email}</span>}
        {read !== null && (
          <span
            className={`block ${old ? 'font-semibold text-red-800' : 'text-gray-600'}`}
            data-testid="subscriptions.account.age"
            data-old={old ? 'yes' : 'no'}
          >
            {t('subscriptions.recorded', { age: age(read, now) })}
          </span>
        )}
      </span>
      {head === null ? (
        <span className={NONE} data-testid="subscriptions.account.none">
          {row.status === 'logged_in' || row.status === 'measured'
            ? t('subscriptions.not-measured')
            : t(`subscriptions.status.${row.status}`, { code: row.code ?? '' })}
        </span>
      ) : (
        <>
          <span className={RESET}>{until(head.resetsAt, now)}</span>
          <Bar used={head.usedPercent} label={name} testId="subscriptions.account.bar" />
          <span className={USED} data-testid="subscriptions.account.used">
            {percent(head.usedPercent)}
          </span>
        </>
      )}
    </li>
  );
}

function Provider({
  group,
  now,
  detailed,
}: {
  readonly group: ProviderGroup;
  readonly now: number;
  readonly detailed: boolean;
}) {
  const { t, age, until, windowName } = useTexts();
  const [open, setOpen] = useState(false);
  const shown = detailed || open;
  const measured = group.rows.some((row) => row.reading !== null);
  return (
    <details
      open={shown}
      onToggle={(event) => {
        // while "detailed" opens every provider, the user's own choice stays as it was
        if (!detailed) setOpen(event.currentTarget.open);
      }}
      className="group border-b border-gray-200 last:border-b-0"
      data-testid="subscriptions.provider"
      data-provider={group.provider}
    >
      <summary
        className={`${GRID} min-h-11 cursor-pointer list-none py-1.5 [&::-webkit-details-marker]:hidden`}
        data-testid="subscriptions.provider.summary"
      >
        <span className="flex min-w-0 items-center gap-2">
          <span aria-hidden="true" className="w-3 shrink-0 text-xs text-gray-600 group-open:rotate-90">
            ▶
          </span>
          <span className="truncate font-semibold">{group.name}</span>
        </span>
        {group.headline === null ? (
          <span className={NONE} data-testid="subscriptions.provider.none">
            {t(measured ? 'subscriptions.no-data' : 'subscriptions.not-measured')}
          </span>
        ) : (
          <>
            <span className={RESET} data-testid="subscriptions.provider.reset">
              {until(group.headline.resetsAt, now)}
            </span>
            <Bar used={group.headline.usedPercent} label={group.name} testId="subscriptions.provider.bar" />
            <span className={`${USED} font-semibold`} data-testid="subscriptions.provider.used">
              {percent(group.headline.usedPercent)}
            </span>
          </>
        )}
      </summary>
      {shown && (
        <div className="flex flex-col pb-3" data-testid="subscriptions.provider.details">
          {group.windows.map((window) => (
            <Line
              key={window.window}
              name={windowName(window.window)}
              window={window}
              now={now}
              testId={`subscriptions.window.${window.window}`}
            />
          ))}
          <h3 className={`${INDENT} pt-3 pb-1 text-sm font-semibold`}>{t('subscriptions.accounts')}</h3>
          <ul className="flex flex-col">
            {group.rows.map((row) => (
              <Account key={row.key} row={row} now={now} />
            ))}
          </ul>
          {group.newest !== null && (
            <span className={`${INDENT} pt-2 text-sm text-gray-600`} data-testid="subscriptions.provider.recorded">
              {t('subscriptions.recorded', { age: age(group.newest, now) })}
            </span>
          )}
        </div>
      )}
    </details>
  );
}

export function ProviderList({
  groups,
  now,
  detailed = false,
}: {
  readonly groups: readonly ProviderGroup[];
  readonly now: number;
  readonly detailed?: boolean;
}) {
  return (
    <div
      className="flex min-w-0 flex-col rounded border border-gray-200 bg-white px-2 sm:px-3"
      data-testid="subscriptions.providers"
    >
      {groups.map((group) => (
        <Provider key={group.provider} group={group} now={now} detailed={detailed} />
      ))}
    </div>
  );
}
