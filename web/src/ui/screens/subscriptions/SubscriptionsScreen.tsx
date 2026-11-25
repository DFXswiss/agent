// The page "My subscriptions" (`/subscriptions`, rule E-30, KOR-68, after the page "Subscriptions" of the local
// dashboard): the provider rows – "detailed" opens all of them, "sort by used" orders them by the used share of their
// week – and below the table of every subscription: provider, account, name, status, note, use, listed, plan, used,
// reset, device, checked, changed. Only the user reads it (the data are the user's own, `loadSubscriptions`).
// "Add subscription" and "Log in now" are the places where the login dialogs hang in (KOR-69); until then they name the
// agent tool's command.
import { useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { providerGroups, providerName, type SubscriptionRow } from '../../../core/subscriptions/subscriptions';
import { useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useAppEnv } from '../../shell/context';
import { loginTarget } from '../../shell/guard';
import { useReason } from '../../settings/sections/github/reason';
import { ProviderList, percent, TICK_MS, useTexts } from '../../subscriptions/ProviderList';
import { useSubscriptions } from '../../subscriptions/useSubscriptions';

const COLUMNS = [
  'provider',
  'account',
  'label',
  'status',
  'note',
  'use',
  'listed',
  'plan',
  'used',
  'reset',
  'device',
  'checked',
  'changed',
] as const;

function useText(row: SubscriptionRow): string {
  const { t } = useTexts();
  if (row.use === null) return '—';
  if (row.use === 'own') return t('subscriptions.use.own');
  const parts = [
    ...(row.use.pool ? [t('subscriptions.use.pool')] : []),
    ...(row.use.orgs.length > 0 ? [t('subscriptions.use.orgs', { n: row.use.orgs.length })] : []),
  ];
  return parts.length === 0 ? t('subscriptions.use.measure') : parts.join(', ');
}

function Row({ row, now }: { readonly row: SubscriptionRow; readonly now: number }) {
  const { t, age, until, windowName, rowName } = useTexts();
  const [hint, setHint] = useState(false);
  const windows = row.reading?.windows ?? [];
  const cell = 'px-2 py-1';
  return (
    <tr className="border-t border-gray-200 align-top" data-testid="subscriptions.row" data-key={row.key}>
      <td className={cell}>{providerName(row.provider)}</td>
      <td className={cell}>{row.email ?? '—'}</td>
      <td className={cell}>{rowName(row)}</td>
      <td className={cell} data-testid="subscriptions.row.status">
        {t(`subscriptions.status.${row.status}`, { code: row.code ?? '' })}
        {row.status === 'not_logged_in' && (
          <button
            type="button"
            className="ml-2 underline"
            onClick={() => setHint(!hint)}
            data-testid="subscriptions.row.login"
          >
            {t('subscriptions.login')}
          </button>
        )}
        {hint && (
          <span className="block text-gray-700" data-testid="subscriptions.row.login-hint">
            {t('subscriptions.login.hint', { id: row.key })}
          </span>
        )}
      </td>
      <td className={cell}>{row.note ?? '—'}</td>
      <td className={cell}>{useText(row)}</td>
      <td className={cell}>{t(`subscriptions.source.${row.source}`)}</td>
      <td className={cell}>{row.state ?? '—'}</td>
      <td className={cell} data-testid="subscriptions.row.used">
        {windows.length === 0
          ? '—'
          : windows.map((window) => (
              <span key={window.window} className="block whitespace-nowrap">
                {windowName(window.window)}: {percent(window.usedPercent)}
              </span>
            ))}
      </td>
      <td className={cell}>
        {windows.length === 0
          ? '—'
          : windows.map((window) => (
              <span key={window.window} className="block whitespace-nowrap">
                {windowName(window.window)}: {window.resetsAt === null ? '—' : until(window.resetsAt, now)}
              </span>
            ))}
      </td>
      <td className={`${cell} font-mono`}>{row.reading?.deviceId?.slice(0, 8) ?? '—'}</td>
      <td className={cell}>{row.reading === null ? '—' : age(row.reading.fetchedAt, now)}</td>
      <td className={cell}>{row.changedAt === null ? '—' : age(row.changedAt, now)}</td>
    </tr>
  );
}

function Table({ rows, now }: { readonly rows: readonly SubscriptionRow[]; readonly now: number }) {
  const { t } = useTexts();
  return (
    <div
      className="overflow-x-auto rounded border border-gray-200 bg-white"
      // the table scrolls sideways on a phone: keyboard users reach it, and it is named (axe scrollable-region-focusable)
      tabIndex={0}
      role="region"
      aria-label={t('subscriptions.title')}
      data-testid="subscriptions.table"
    >
      <table className="w-full min-w-[56rem] text-left text-sm">
        <thead className="bg-gray-100">
          <tr>
            {COLUMNS.map((column) => (
              <th key={column} scope="col" className="px-2 py-1 font-semibold whitespace-nowrap">
                {t(`subscriptions.col.${column}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <Row key={row.key} row={row} now={now} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SubscriptionsScreen() {
  const { app } = useAppEnv();
  const { t } = useTexts();
  const reason = useReason();
  const location = useLocation();
  const unlocked = useStore((state) => state.session.phase === 'unlocked', app.store);
  const { rows, error, reload } = useSubscriptions();
  const now = useNow(true, TICK_MS);
  const [detailed, setDetailed] = useState(false);
  const [sorted, setSorted] = useState(false);
  const [adding, setAdding] = useState(false);
  if (!unlocked) return <Navigate to={loginTarget(location.pathname, location.search)} replace />;
  return (
    <main className="mx-auto flex w-full min-w-0 max-w-5xl flex-col gap-4 p-4" data-testid="subscriptions.page">
      <header className="flex flex-col gap-1">
        <h1 className="text-2xl font-semibold">{t('subscriptions.title')}</h1>
        <p className="text-sm text-gray-700">{t('subscriptions.intro')}</p>
      </header>
      {error !== null && (
        <p role="alert" data-testid="subscriptions.load-failed" data-code={error}>
          {t('subscriptions.load-failed', { reason: reason(error) })}{' '}
          <button type="button" className="min-h-11 underline" onClick={reload} data-testid="subscriptions.reload">
            {t('subscriptions.reload')}
          </button>
        </p>
      )}
      {rows === null && error === null && <p data-testid="subscriptions.loading">{t('subscriptions.loading')}</p>}
      {rows !== null && rows.length === 0 && (
        <p className="text-gray-700" data-testid="subscriptions.empty">
          {t('subscriptions.empty')}
        </p>
      )}
      {rows !== null && rows.length > 0 && (
        <>
          <div className="flex flex-wrap gap-x-6">
            <label className="flex min-h-11 items-center gap-2">
              <input
                type="checkbox"
                checked={detailed}
                onChange={(event) => setDetailed(event.target.checked)}
                className="h-4 w-4"
                data-testid="subscriptions.detailed"
              />
              {t('subscriptions.detailed')}
            </label>
            <label className="flex min-h-11 items-center gap-2">
              <input
                type="checkbox"
                checked={sorted}
                onChange={(event) => setSorted(event.target.checked)}
                className="h-4 w-4"
                data-testid="subscriptions.sort"
              />
              {t('subscriptions.sort')}
            </label>
          </div>
          <ProviderList groups={providerGroups(rows, sorted)} now={now} detailed={detailed} />
          <Table rows={rows} now={now} />
        </>
      )}
      {rows !== null && (
        <div className="flex flex-col gap-1">
          <button
            type="button"
            className="min-h-11 self-start rounded border border-gray-400 px-4"
            onClick={() => setAdding(!adding)}
            data-testid="subscriptions.add"
          >
            {t('subscriptions.add')}
          </button>
          {adding && (
            <p className="text-sm text-gray-700" data-testid="subscriptions.add-hint">
              {t('subscriptions.add.hint')}
            </p>
          )}
        </div>
      )}
    </main>
  );
}
