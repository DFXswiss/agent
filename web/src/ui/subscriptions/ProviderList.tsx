// The provider rows of "My subscriptions" (KOR-68, after the page "Subscriptions" of the local dashboard): per provider
// a line with the time to the reset, a bar and the used share of the week as the total of all its accounts ("all 9",
// `totalWindows`: the mean of the accounts that measured it, the next reset of them – never the fullest account) and
// how many accounts are usable now; opened, the 5-hour window, the week and the week per model as such totals, then
// every account with the bar of the window that binds it now (`bindingOf`: the fuller of 5 hours and week, named next
// to it, the other one as a small second value) and the age of its measurement, and the age of the provider's newest
// read. Nothing missing is shown as 0 %. Used by the
// card of the home page and by the full page.
import { useState } from 'react';
import {
  ageParts,
  bindingOf,
  FULLEST,
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
/** How often the shown ages and times to a reset move on. */
export const TICK_MS = 30_000;

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
  const rowName = (row: SubscriptionRow): string =>
    row.name ?? (row.primary ? t('subscriptions.own') : (row.email ?? t('subscriptions.unnamed')));
  return { t, locale, until, age, windowName, rowName };
}

export const percent = (used: number): string => `${Math.round(used * 10) / 10}%`;

export function Bar({
  used,
  label,
  testId,
  window,
}: {
  readonly used: number;
  readonly label: string;
  readonly testId: string;
  /** The window the bar shows, as `data-window`. */
  readonly window?: string;
}) {
  const level = usedLevel(used);
  return (
    <span
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.min(Math.round(used), 100)}
      aria-label={label}
      className="order-last col-span-3 block h-2.5 w-full overflow-hidden rounded bg-gray-200 sm:order-none sm:col-span-1"
      data-testid={testId}
      data-level={level}
      data-window={window}
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
  const { t, age, until, rowName, windowName } = useTexts();
  const found = bindingOf(row);
  const head = found?.binding ?? null;
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
            {row.source === 'operator' ? ` · ${t('subscriptions.by.file')}` : ` · ${t('subscriptions.by.device')}`}
          </span>
        )}
        {found?.other != null && (
          <span
            className="block text-gray-600"
            data-testid="subscriptions.account.other"
            data-window={found.other.window}
          >
            {t('subscriptions.other', {
              window: windowName(found.other.window),
              used: percent(found.other.usedPercent),
            })}
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
          <span className={RESET} data-testid="subscriptions.account.reset">
            {until(head.resetsAt, now)}
          </span>
          <Bar
            used={head.usedPercent}
            label={`${name} – ${windowName(head.window)}`}
            testId="subscriptions.account.bar"
            window={head.window}
          />
          <span className={`${USED} flex flex-col items-end leading-tight`}>
            <span data-testid="subscriptions.account.used">{percent(head.usedPercent)}</span>
            <span
              className="text-xs whitespace-nowrap text-gray-600"
              data-testid="subscriptions.account.window"
              data-window={head.window}
            >
              {windowName(head.window)}
            </span>
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
  // the line stands for every account of the provider, never for one of them (an account may carry its name)
  const total =
    group.rows.length === 1 ? t('subscriptions.total.one') : t('subscriptions.total.many', { n: group.rows.length });
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
        <span className="flex min-w-0 flex-wrap items-center gap-x-2">
          <span aria-hidden="true" className="w-3 shrink-0 text-xs text-gray-600 group-open:rotate-90">
            ▶
          </span>
          <span className="truncate font-semibold">{group.name}</span>
          <span className="shrink-0 text-xs text-gray-600" data-testid="subscriptions.provider.total">
            {total}
          </span>
          <span
            className={`shrink-0 text-xs ${group.usable === 0 ? 'font-semibold text-red-800' : 'text-gray-600'}`}
            data-testid="subscriptions.provider.usable"
            data-usable={group.usable}
          >
            {t('subscriptions.usable', { n: group.usable, total: group.rows.length })}
          </span>
          {group.headlineAt !== null && now - group.headlineAt > OLD_MS && (
            <span className="shrink-0 text-xs font-semibold text-red-800" data-testid="subscriptions.provider.old">
              {t('subscriptions.old', { age: age(group.headlineAt, now) })}
            </span>
          )}
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
            <Bar
              used={group.headline.usedPercent}
              label={`${group.name} – ${total}`}
              testId="subscriptions.provider.bar"
            />
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
