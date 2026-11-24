// Settings section "Credit and usage" (12-billing.md section 9, ADR-062 section 7): the active organization's balance,
// a clear notice when the credit is used up, the prices in force, the credit history and the consumption per day, per
// workgroup and, when the hub sends it, per person (ADR-083) (`GET /v1/orgs/{org}/billing`). There is no payment and
// no top-up: the operator adds credit.
import { useCallback, useEffect, useState } from 'react';
import {
  formatRuntime,
  formatUsd,
  loadOrgBilling,
  type Consumption,
  type OrgBilling,
} from '../../../../core/billing/billing';
import { requestErrorCode } from '../../../../core/requests/client';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { PeopleUsage } from '../../../task-tabs/people';
import { useReason } from '../github/reason';

function UsageRows({
  rows,
  testid,
}: {
  readonly rows: readonly (Consumption & { readonly label: string })[];
  readonly testid: string;
}) {
  const { t, locale } = useI18n();
  if (rows.length === 0) return <p data-testid={`${testid}.empty`}>{t('billing.usage.empty')}</p>;
  return (
    <ul data-testid={testid}>
      {rows.map((row) => (
        <li key={row.label}>
          {t('billing.usage.row', {
            label: row.label,
            total: formatUsd(row.tokensMicroUsd + row.runtimeMicroUsd, locale),
            tokens: formatUsd(row.tokensMicroUsd, locale),
            runtime: formatUsd(row.runtimeMicroUsd, locale),
          })}
        </li>
      ))}
    </ul>
  );
}

function Details({ billing, org }: { readonly billing: OrgBilling; readonly org: string }) {
  const { t, locale } = useI18n();
  return (
    <>
      <p className="text-xl" data-testid="billing.balance" data-exhausted={billing.exhausted ? 'yes' : 'no'}>
        {t('billing.balance')}: {formatUsd(billing.balanceMicroUsd, locale)}
      </p>
      {billing.exhausted && (
        <p role="alert" className="text-red-700" data-testid="billing.exhausted">
          {t('billing.exhausted')}
        </p>
      )}
      {!billing.active && <p data-testid="billing.inactive">{t('billing.inactive')}</p>}
      <p className="text-sm text-gray-600">{t('billing.no-topup')}</p>
      {billing.version !== null && (
        <section data-testid="billing.prices">
          <h3 className="font-semibold">{t('billing.prices')}</h3>
          <p className="text-sm">{t('billing.prices.version', { version: billing.version })}</p>
          <ul>
            {billing.tokenPrices.map((price) => (
              <li key={price.label}>
                {t('billing.prices.tokens', {
                  model: price.label,
                  input: formatUsd(price.input * 1_000_000, locale),
                  output: formatUsd(price.output * 1_000_000, locale),
                })}
              </li>
            ))}
            {billing.hourly.map((price) => (
              <li key={price.size}>
                {t('billing.prices.hour', { size: price.size, amount: formatUsd(price.usd * 1_000_000, locale) })}
              </li>
            ))}
          </ul>
        </section>
      )}
      <section>
        <h3 className="font-semibold">{t('billing.credits')}</h3>
        {billing.credits.length === 0 ? (
          <p data-testid="billing.credits.empty">{t('billing.credits.empty')}</p>
        ) : (
          <ul data-testid="billing.credits">
            {billing.credits.map((entry) => (
              <li key={`${entry.at}-${entry.kind}-${entry.balanceMicroUsd}`}>
                {new Date(entry.at).toLocaleString(locale)}{' '}
                {t('billing.credit.row', {
                  kind: t(`billing.credit.kind.${entry.kind}`),
                  amount: formatUsd(entry.amountMicroUsd, locale),
                  balance: formatUsd(entry.balanceMicroUsd, locale),
                })}
                {entry.reason !== null && ` – ${entry.reason}`}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section>
        <h3 className="font-semibold">{t('billing.days')}</h3>
        <UsageRows rows={billing.days.map((row) => ({ ...row, label: row.day }))} testid="billing.days" />
      </section>
      <section>
        <h3 className="font-semibold">{t('billing.workgroups')}</h3>
        <UsageRows
          rows={billing.workgroups.map((row) => ({ ...row, label: `${row.wgId} (${formatRuntime(row.runtimeMs)})` }))}
          testid="billing.workgroups"
        />
      </section>
      {billing.people !== null && (
        <section>
          <h3 className="font-semibold">{t('billing.people')}</h3>
          <PeopleUsage org={org} rows={billing.people} testId="billing.people" />
        </section>
      )}
    </>
  );
}

export function BillingSection() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const org = useStore((state) => state.session.activeOrg, app.store);
  const [billing, setBilling] = useState<OrgBilling | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const load = useCallback(() => {
    if (org === null) return;
    loadOrgBilling(app.tasks, org).then(setBilling, (error: unknown) => setLoadError(requestErrorCode(error)));
  }, [app, org]);
  useEffect(load, [load]);
  const reload = () => {
    setLoadError(null);
    load();
  };
  return (
    <div className="flex flex-col gap-3" data-testid="billing.section">
      {loadError !== null && (
        <p role="alert" data-testid="billing.load-failed" data-code={loadError}>
          {t('billing.load-failed', { reason: reason(loadError) })}
        </p>
      )}
      {loadError === null && billing === null && <p data-testid="billing.loading">{t('billing.loading')}</p>}
      {billing !== null && <Details billing={billing} org={org!} />}
      <button
        type="button"
        className="min-h-11 self-start rounded border border-gray-400 px-4"
        onClick={reload}
        data-testid="billing.reload"
      >
        {t('billing.reload')}
      </button>
    </div>
  );
}

export const section: SettingsSection = {
  id: 'billing',
  order: 66,
  titleKey: 'billing.title',
  Component: BillingSection,
};
