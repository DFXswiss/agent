// Settings section "My own subscription" (06-web.md section 8.2, ADR-010; titled apart from the organization's "Model
// access" since KOR-68): the user's own subscription windows the execution device last read
// (`GET /v1/me/quotas`): 5 hours, week and per model, each as a bar of what is left (`freePercent`) and the reset
// time. Without
// rows the quota is "not readable" when the subscription refused the read (KOR-40), else "not available" (no
// subscription, or the usage endpoint did not answer). Spend of API keys and the
// platform is in monitoring.
import { useCallback, useEffect, useState } from 'react';
import { barShare, loadQuotas } from '../../../../core/credentials/quotas';
import { formatEventTime } from '../../../../core/i18n';
import { formatPercent } from '../../../../core/monitoring/units';
import { requestErrorCode } from '../../../../core/requests/client';
import type { QuotaBar } from '../../../../core/store/slices/quotas';
import type { SettingsSection } from '../../../registries/settings-sections';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useNow } from '../../../countdown/useNow';
import { useAppEnv } from '../../../shell/context';
import { useReason } from '../github/reason';
import '../../../../core/i18n/lazy/model';

function Bar({ bar }: { readonly bar: QuotaBar }) {
  const { t, locale } = useI18n();
  const now = useNow(false);
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const label =
    bar.model === null ? t(`model.quotas.window.${bar.window}`) : t('model.quotas.window.model', { model: bar.model });
  const share = barShare(bar, now);
  return (
    <li className="flex flex-col gap-1" data-testid={`quotas.bar.${bar.window}`}>
      <div className="flex flex-wrap justify-between gap-2">
        <span>{label}</span>
        <span data-testid={`quotas.bar.${bar.window}.free`}>
          {t('model.org.free', { percent: formatPercent(share, locale) })}
        </span>
      </div>
      {/* what is left: red at 10 % or less, amber at 30 % or less (ADR-091 section 3) */}
      <meter
        className="h-3 w-full"
        min={0}
        max={1}
        low={0.1}
        high={0.3}
        optimum={1}
        value={share}
        aria-label={label}
        data-testid={`quotas.bar.${bar.window}.meter`}
      />
      <span className="text-sm text-gray-600" data-testid={`quotas.bar.${bar.window}.reset`}>
        {bar.resetsAt === null
          ? t('model.quotas.reset-unknown')
          : t('model.quotas.reset', {
              time: formatEventTime(bar.resetsAt, { locale, timeZone, now }),
            })}
      </span>
    </li>
  );
}

export function QuotasSection() {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const bars = useStore((state) => state.quotas.bars, app.store);
  const unreadable = useStore((state) => state.quotas.unreadableSince, app.store);
  const [loadError, setLoadError] = useState<string | null>(null);
  const load = useCallback(() => {
    loadQuotas(app.tasks).catch((error: unknown) => setLoadError(requestErrorCode(error)));
  }, [app]);
  useEffect(load, [load]);
  const reload = () => {
    setLoadError(null);
    load();
  };
  return (
    <div className="flex flex-col gap-3" data-testid="quotas.section">
      <p className="text-sm text-gray-600" data-testid="quotas.intro">
        {t('model.quotas.intro')}
      </p>
      {loadError !== null && (
        <p role="alert" data-testid="quotas.load-failed" data-code={loadError}>
          {t('model.quotas.load-failed', { reason: reason(loadError) })}
        </p>
      )}
      {loadError === null && bars === null && <p data-testid="quotas.loading">{t('model.quotas.loading')}</p>}
      {bars !== null && bars.length === 0 && unreadable === null && (
        <p data-testid="quotas.unavailable">{t('model.quotas.unavailable')}</p>
      )}
      {bars !== null && bars.length === 0 && unreadable !== null && (
        <p data-testid="quotas.unreadable">{t('model.quotas.unreadable')}</p>
      )}
      {bars !== null && bars.length > 0 && (
        <ul className="flex flex-col gap-3" data-testid="quotas.bars">
          {bars.map((bar) => (
            <Bar key={bar.window} bar={bar} />
          ))}
        </ul>
      )}
      <button
        type="button"
        className="min-h-11 self-start rounded border border-gray-400 px-4"
        onClick={reload}
        data-testid="quotas.reload"
      >
        {t('model.quotas.reload')}
      </button>
    </div>
  );
}

export const section: SettingsSection = {
  id: 'quotas',
  order: 64,
  titleKey: 'model.quotas.title',
  Component: QuotasSection,
};
