// Settings section "Model access and quotas" of the active organization (ADR-080 section 6, `GET
// /v1/orgs/{org}/quotas`): the summary ("Subscriptions: n available · m used up – next release 14:05"), the user's own
// subscription (its windows, or honestly "not readable"), the operator's subscriptions when the organization may use
// them (state, used share or "quota not readable", release), the last observed limit time of each, and whether DFX
// access over OpenRouter is available. Read when the organization changes and on "Reload".
import { useCallback, useEffect, useState } from 'react';
import {
  type Limited,
  loadOrgQuotas,
  type OperatorSubscription,
  type OrgQuotas,
  SUBSCRIPTION_STATES,
  subscriptionShare,
} from '../../../../core/credentials/orgQuotas';
import { formatPercent } from '../../../../core/monitoring/units';
import { requestErrorCode } from '../../../../core/requests/client';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';
import { SummaryText, useClock } from './summary';

function LimitLine({ limited, testId }: { readonly limited: Limited; readonly testId: string }) {
  const { t } = useI18n();
  const clock = useClock();
  return (
    <span className="text-sm text-gray-600" data-testid={testId}>
      {limited.until === null
        ? t('model.org.limited', { time: clock(limited.at) })
        : t('model.org.limited.until', { time: clock(limited.at), until: clock(limited.until) })}
    </span>
  );
}

function Subscription({ row }: { readonly row: OperatorSubscription }) {
  const { t, locale } = useI18n();
  const clock = useClock();
  const share = subscriptionShare(row);
  const known = (SUBSCRIPTION_STATES as readonly string[]).includes(row.state);
  const release = row.state === 'exhausted' && row.until !== null ? row.until : row.resetsAt;
  return (
    <li className="flex flex-col gap-1" data-testid="model-quotas.subscription" data-state={row.state}>
      <div className="flex flex-wrap justify-between gap-2">
        <span className="font-medium">{row.label}</span>
        <span data-testid="model-quotas.subscription.state">
          {t(known ? `model.org.state.${row.state}` : 'model.org.state.unknown')}
        </span>
      </div>
      {share === null ? (
        <span className="text-sm text-gray-600" data-testid="model-quotas.subscription.unreadable">
          {t('model.org.unreadable')}
        </span>
      ) : (
        <meter
          className="h-3 w-full"
          min={0}
          max={1}
          high={0.8}
          value={share}
          aria-label={t('model.org.used', { percent: formatPercent(share, locale) })}
          data-testid="model-quotas.subscription.meter"
        />
      )}
      {release !== null && (
        <span className="text-sm text-gray-600" data-testid="model-quotas.subscription.release">
          {row.state === 'exhausted'
            ? t('model.org.until', { time: clock(release) })
            : t('model.org.reset', { time: clock(release) })}
        </span>
      )}
      {row.limitedAt !== null && (
        <LimitLine
          limited={{ at: row.limitedAt, until: row.until, readable: row.readable }}
          testId="model-quotas.subscription.limited"
        />
      )}
    </li>
  );
}

function Details({ quotas }: { readonly quotas: OrgQuotas }) {
  const { t, locale } = useI18n();
  const clock = useClock();
  const { own, operator } = quotas;
  return (
    <>
      <p className="font-medium" data-testid="model-quotas.summary">
        <SummaryText summary={quotas.summary} />
      </p>
      <section className="flex flex-col gap-1" data-testid="model-quotas.own">
        <h3 className="font-semibold">{t('model.org.own')}</h3>
        {own.bars.length > 0 && (
          <ul className="flex flex-col gap-1" data-testid="model-quotas.own.bars">
            {own.bars.map((bar) => (
              <li key={bar.window} className="text-sm">
                {bar.model === null
                  ? t(`model.quotas.window.${bar.window}`)
                  : t('model.quotas.window.model', { model: bar.model })}
                : {t('model.org.used', { percent: formatPercent(Math.min(bar.usedPercent, 100) / 100, locale) })}
                {bar.resetsAt !== null && ` · ${t('model.org.reset', { time: clock(bar.resetsAt) })}`}
              </li>
            ))}
          </ul>
        )}
        {own.bars.length === 0 && own.unreadableSince !== null && (
          <p className="text-sm" data-testid="model-quotas.own.unreadable">
            {t('model.org.own.unreadable')}
          </p>
        )}
        {own.bars.length === 0 && own.unreadableSince === null && (
          <p className="text-sm text-gray-600" data-testid="model-quotas.own.none">
            {t('model.org.own.none')}
          </p>
        )}
        {own.limited !== null && <LimitLine limited={own.limited} testId="model-quotas.own.limited" />}
      </section>
      <section className="flex flex-col gap-1" data-testid="model-quotas.operator">
        <h3 className="font-semibold">{t('model.org.operator')}</h3>
        {!operator.granted && (
          <p className="text-sm text-gray-600" data-testid="model-quotas.operator.not-granted">
            {t('model.org.operator.not-granted')}
          </p>
        )}
        {operator.granted && operator.subscriptions.length === 0 && (
          <p className="text-sm text-gray-600" data-testid="model-quotas.operator.empty">
            {t('model.org.operator.empty')}
          </p>
        )}
        {operator.subscriptions.length > 0 && (
          <ul className="flex flex-col gap-3">
            {operator.subscriptions.map((row) => (
              <Subscription key={row.label} row={row} />
            ))}
          </ul>
        )}
      </section>
      <p data-testid="model-quotas.platform" data-available={quotas.platformAvailable ? 'yes' : 'no'}>
        {t(quotas.platformAvailable ? 'model.org.platform.available' : 'model.org.platform.unavailable')}
      </p>
    </>
  );
}

export function ModelQuotasSection() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const org = useStore((state) => state.session.activeOrg, app.store);
  const [quotas, setQuotas] = useState<OrgQuotas | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const load = useCallback(() => {
    if (org === null) return;
    loadOrgQuotas(app.tasks, org).then(setQuotas, (error: unknown) => setLoadError(requestErrorCode(error)));
  }, [app, org]);
  useEffect(load, [load]);
  const reload = () => {
    setLoadError(null);
    load();
  };
  return (
    <div className="flex flex-col gap-3" data-testid="model-quotas.section">
      <p className="text-sm text-gray-600">{t('model.org.intro')}</p>
      {org === null && <p data-testid="model-quotas.no-org">{t('model.org.none')}</p>}
      {loadError !== null && (
        <p role="alert" data-testid="model-quotas.load-failed" data-code={loadError}>
          {t('model.org.load-failed', { reason: reason(loadError) })}
        </p>
      )}
      {org !== null && loadError === null && quotas === null && (
        <p data-testid="model-quotas.loading">{t('model.org.loading')}</p>
      )}
      {quotas !== null && <Details quotas={quotas} />}
      <button
        type="button"
        className="min-h-11 self-start rounded border border-gray-400 px-4"
        onClick={reload}
        data-testid="model-quotas.reload"
      >
        {t('model.org.reload')}
      </button>
    </div>
  );
}

export const section: SettingsSection = {
  id: 'model-quotas',
  order: 65,
  titleKey: 'model.org.title',
  Component: ModelQuotasSection,
};
