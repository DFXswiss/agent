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
const GRID = 'grid grid-cols-[minmax(5rem,1fr)_5.5rem_minmax(3rem,2fr)_3.5rem] items-center gap-2';

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
      className="block h-2.5 w-full overflow-hidden rounded bg-gray-200"
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
    <div className={GRID} data-testid={testId}>
      <span className="truncate">{name}</span>
      <span className="text-sm text-gray-600">{until(window.resetsAt, now)}</span>
      <Bar used={window.usedPercent} label={name} testId={`${testId}.bar`} />
      <span className="text-right tabular-nums">{percent(window.usedPercent)}</span>
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
    <li className="flex flex-col gap-1" data-testid="subscriptions.account" data-key={row.key} data-status={row.status}>
      <div className={GRID}>
        <span className="truncate">
          <span className="font-medium">{name}</span>
          {row.email !== null && <span className="ml-2 text-sm text-gray-600">{row.email}</span>}
        </span>
        {head === null ? (
          <span className="col-span-3 text-sm text-gray-700" data-testid="subscriptions.account.none">
            {row.status === 'logged_in' || row.status === 'measured'
              ? t('subscriptions.not-measured')
              : t(`subscriptions.status.${row.status}`, { code: row.code ?? '' })}
          </span>
        ) : (
          <>
            <span className="text-sm text-gray-600">{until(head.resetsAt, now)}</span>
            <Bar used={head.usedPercent} label={name} testId="subscriptions.account.bar" />
            <span className="text-right tabular-nums" data-testid="subscriptions.account.used">
              {percent(head.usedPercent)}
            </span>
          </>
        )}
      </div>
      {read !== null && (
        <span
          className={`text-sm ${old ? 'font-semibold text-red-800' : 'text-gray-600'}`}
          data-testid="subscriptions.account.age"
          data-old={old ? 'yes' : 'no'}
        >
          {t('subscriptions.recorded', { age: age(read, now) })}
        </span>
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
      className="border-b border-gray-200 py-1 last:border-b-0"
      data-testid="subscriptions.provider"
      data-provider={group.provider}
    >
      <summary className={`${GRID} min-h-11 cursor-pointer`} data-testid="subscriptions.provider.summary">
        <span className="truncate font-semibold">{group.name}</span>
        {group.headline === null ? (
          <span className="col-span-3 text-sm text-gray-700" data-testid="subscriptions.provider.none">
            {t(measured ? 'subscriptions.no-data' : 'subscriptions.not-measured')}
          </span>
        ) : (
          <>
            <span className="text-sm text-gray-600" data-testid="subscriptions.provider.reset">
              {until(group.headline.resetsAt, now)}
            </span>
            <Bar used={group.headline.usedPercent} label={group.name} testId="subscriptions.provider.bar" />
            <span className="text-right tabular-nums" data-testid="subscriptions.provider.used">
              {percent(group.headline.usedPercent)}
            </span>
          </>
        )}
      </summary>
      {shown && (
        <div className="flex flex-col gap-2 pt-1 pb-2 pl-4 sm:pl-7" data-testid="subscriptions.provider.details">
          {group.windows.map((window) => (
            <Line
              key={window.window}
              name={windowName(window.window)}
              window={window}
              now={now}
              testId={`subscriptions.window.${window.window}`}
            />
          ))}
          <h3 className="font-semibold">{t('subscriptions.accounts')}</h3>
          <ul className="flex flex-col gap-2">
            {group.rows.map((row) => (
              <Account key={row.key} row={row} now={now} />
            ))}
          </ul>
          {group.newest !== null && (
            <span className="text-sm text-gray-600" data-testid="subscriptions.provider.recorded">
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
    <div className="flex flex-col rounded border border-gray-200 bg-white px-3" data-testid="subscriptions.providers">
      {groups.map((group) => (
        <Provider key={group.provider} group={group} now={now} detailed={detailed} />
      ))}
    </div>
  );
}
