// The provider rows of "My subscriptions" (rule E-30, KOR-68, ADR-091 section 3, after the page "Subscriptions" of the
// local dashboard). Everything shows what is LEFT ("free"), coloured by it. Per provider a line with the time to the
// reset, a bar and the share left of the week as the total of all its accounts ("all 9", `totalWindows`: the mean of
// the accounts that measured it, the next reset of them – never the fullest account), how many accounts are usable
// now and the next release; opened, the 5-hour window, the week and the week per model as such totals, then every
// account with the source and age of its measurement and ALL its windows, each with share left, bar and time to the
// reset – the window that binds it now (`bindingOf`) marked, an account with nothing left in it marked "not usable
// now until …" –, and the age of the provider's newest read. Nothing missing is shown as 0 %. Used by the card of the
// home page and by the full page.
import { useState } from 'react';
import { formatClock, localTimeZone } from '../../core/i18n/format';
import {
  ageParts,
  bindingOf,
  exhaustedBy,
  freeLevel,
  freeOf,
  FULLEST,
  type ProviderGroup,
  type QuotaWindow,
  type SubscriptionRow,
  untilParts,
} from '../../core/subscriptions/subscriptions';
import { useI18n } from '../i18n';
import '../../core/i18n/lazy/subscriptions';

/** Older than this, a measurement is marked old. */
export const OLD_MS = 60 * 60 * 1000;
/** How often the shown ages and times to a reset move on. */
export const TICK_MS = 30_000;

const LEVEL_CLASS = { ok: 'bg-green-600', warn: 'bg-amber-500', low: 'bg-red-600' } as const;
// One line: name | time to the reset | bar | share left. On a phone the bar takes a second row of its own below the
// three texts, so nothing is squeezed and nothing overflows; from `sm` on the four sit in one row, the columns of every
// line aligned.
const GRID =
  'grid grid-cols-[minmax(0,1fr)_auto_5.5rem] items-center gap-x-2 gap-y-1 sm:grid-cols-[minmax(7rem,14rem)_6.5rem_minmax(4rem,1fr)_5.5rem] sm:gap-x-3';
/** Lines inside an opened provider start where the name of the provider starts (after the triangle). */
const INDENT = 'pl-5';
/** The windows of an account sit below its name. */
const WINDOW_INDENT = 'pl-9';
/** The text that stands in place of reset, bar and share. */
const NONE = 'col-span-2 text-sm text-gray-700 sm:col-span-3';
const RESET = 'text-sm whitespace-nowrap text-gray-600';
const FREE = 'text-right text-sm whitespace-nowrap tabular-nums';

/** Texts of the area with the time to a reset, the age of a read and the name of a window. */
export function useTexts() {
  const { t, locale } = useI18n();
  const until = (at: number | null, now: number): string => {
    if (at === null) return '';
    const parts = untilParts(at, now);
    return t(`subscriptions.until.${parts.kind}`, { a: parts.a, b: parts.b });
  };
  /** A release time: the time of day (with the date when it is not today) and the time until it. */
  const release = (key: string, at: number, now: number): string =>
    t(key, { at: formatClock(at, { locale, timeZone: localTimeZone(), now }), in: until(at, now) });
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
  /** A share left as text: "82% free". */
  const freeText = (free: number): string => t('subscriptions.free', { p: percent(free) });
  return { t, locale, until, release, age, windowName, rowName, freeText };
}

export const percent = (share: number): string => `${Math.round(share * 10) / 10}%`;

/** A bar of the share LEFT: filled as far as something is left, coloured by it. */
export function Bar({
  free,
  label,
  testId,
}: {
  readonly free: number;
  readonly label: string;
  readonly testId: string;
}) {
  const { freeText } = useTexts();
  const level = freeLevel(free);
  const shown = Math.min(Math.max(free, 0), 100);
  return (
    <span
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(shown)}
      aria-valuetext={freeText(free)}
      aria-label={label}
      className="order-last col-span-3 block h-2.5 w-full overflow-hidden rounded bg-gray-200 sm:order-none sm:col-span-1"
      data-testid={testId}
      data-level={level}
    >
      <span className={`block h-full ${LEVEL_CLASS[level]}`} style={{ width: `${shown}%` }} />
    </span>
  );
}

/** A total window of the provider. */
function Line({
  name,
  free,
  resetsAt,
  now,
  testId,
}: {
  readonly name: string;
  readonly free: number;
  readonly resetsAt: number | null;
  readonly now: number;
  readonly testId: string;
}) {
  const { until, freeText } = useTexts();
  return (
    <div className={`${GRID} py-0.5`} data-testid={testId}>
      <span className={`${INDENT} truncate text-sm`}>{name}</span>
      <span className={RESET}>{until(resetsAt, now)}</span>
      <Bar free={free} label={name} testId={`${testId}.bar`} />
      <span className={FREE}>{freeText(free)}</span>
    </div>
  );
}

/** One window of an account; the binding one marked (and red-tinted when nothing is left in it). */
function AccountWindow({
  account,
  window,
  binding,
  now,
}: {
  readonly account: string;
  readonly window: QuotaWindow;
  readonly binding: boolean;
  readonly now: number;
}) {
  const { t, until, windowName, freeText } = useTexts();
  const free = freeOf(window.usedPercent);
  const name = windowName(window.window);
  const tint = binding ? (free === 0 ? 'bg-red-50' : 'bg-gray-50') : '';
  return (
    <div
      className={`${GRID} rounded py-0.5 ${tint}`}
      data-testid="subscriptions.account.window"
      data-window={window.window}
      data-binding={binding ? 'yes' : 'no'}
    >
      <span className={`${WINDOW_INDENT} flex min-w-0 items-center gap-2 text-sm`}>
        <span className={`truncate ${binding ? 'font-semibold' : ''}`}>{name}</span>
        {binding && (
          <span
            className="shrink-0 rounded bg-gray-200 px-1 text-xs text-gray-800"
            data-testid="subscriptions.account.binding"
          >
            {t('subscriptions.binding')}
          </span>
        )}
      </span>
      <span className={RESET} data-testid="subscriptions.account.reset">
        {until(window.resetsAt, now)}
      </span>
      <Bar free={free} label={`${account} – ${name}`} testId="subscriptions.account.bar" />
      <span className={`${FREE} ${binding ? 'font-semibold' : ''}`} data-testid="subscriptions.account.free">
        {freeText(free)}
      </span>
    </div>
  );
}

function Account({ row, now }: { readonly row: SubscriptionRow; readonly now: number }) {
  const { t, age, release, rowName } = useTexts();
  // the window that binds the account now (none without a reading)
  const binding = bindingOf(row)?.binding.window ?? null;
  const exhausted = exhaustedBy(row);
  const windows = row.reading?.windows ?? [];
  const read = row.reading?.fetchedAt ?? null;
  const name = rowName(row);
  const old = read !== null && now - read > OLD_MS;
  return (
    <li
      className="flex flex-col py-1"
      data-testid="subscriptions.account"
      data-key={row.key}
      data-status={row.status}
      data-exhausted={exhausted === null ? 'no' : 'yes'}
    >
      <div className={`${GRID}`}>
        <span
          className={`${INDENT} min-w-0 text-sm [overflow-wrap:anywhere] ${windows.length === 0 ? '' : 'col-span-full'}`}
        >
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
          {exhausted !== null && (
            <span className="block font-semibold text-red-800" data-testid="subscriptions.account.blocked">
              {exhausted.resetsAt === null
                ? t('subscriptions.blocked.open')
                : release('subscriptions.blocked', exhausted.resetsAt, now)}
            </span>
          )}
        </span>
        {windows.length === 0 && (
          <span className={NONE} data-testid="subscriptions.account.none">
            {row.status === 'logged_in' || row.status === 'measured'
              ? t('subscriptions.not-measured')
              : t(`subscriptions.status.${row.status}`, { code: row.code ?? '' })}
          </span>
        )}
      </div>
      {windows.map((window) => (
        <AccountWindow
          key={window.window}
          account={name}
          window={window}
          binding={window.window === binding}
          now={now}
        />
      ))}
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
  const { t, age, until, release, windowName, freeText } = useTexts();
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
          {group.nextRelease !== null && (
            <span className="shrink-0 text-xs text-gray-600" data-testid="subscriptions.provider.release">
              {release('subscriptions.next-release', group.nextRelease, now)}
            </span>
          )}
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
              free={group.headline.freePercent}
              label={`${group.name} – ${total}`}
              testId="subscriptions.provider.bar"
            />
            <span className={`${FREE} font-semibold`} data-testid="subscriptions.provider.free">
              {freeText(group.headline.freePercent)}
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
              free={window.freePercent}
              resetsAt={window.resetsAt}
              now={now}
              testId={`subscriptions.window.${window.window}`}
            />
          ))}
          <h3 className={`${INDENT} pt-3 pb-1 text-sm font-semibold`}>{t('subscriptions.accounts')}</h3>
          <ul className="flex flex-col divide-y divide-gray-100">
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
