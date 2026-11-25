// The usage strip of a task (06-web.md section 5.2, ADR-010): tokens (input, output, cache) and cost over all sessions
// of the task (main session and subagents), the cost with its basis ("provider" for the cost OpenRouter reports, "list
// price" for API keys, "subscription equivalent" for subscriptions), and the main session's context-window share.
// Counters come from `GET /v1/tasks/{task}` (loaded by the task screen) and the live `session` frames (slice `usage`);
// numbers and money through Intl with the active locale. Rendered by the chat's usage slot (`UsageSlot.tsx`).
import { formatMoney, formatNumber } from '../../../core/i18n';
import { formatPercent } from '../../../core/monitoring/units';
import { taskUsage } from '../../../core/store/slices/usage';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import type { UsageStripProps } from './UsageSlot';
import '../../../core/i18n/lazy/model';

export function UsageStrip({ taskId }: UsageStripProps) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const main = useStore((state) => state.tasks.tasks[taskId]?.main_session_id ?? null, app.store);
  const counters = useStore((state) => state.usage, app.store);
  const usage = taskUsage(counters, taskId, main);
  if (usage === null)
    return (
      <p className="px-4 py-1 text-sm text-gray-600" data-testid="usage.none">
        {t('model.usage.none')}
      </p>
    );
  const number = (value: number) => formatNumber(value, locale);
  return (
    <dl
      className="flex flex-wrap gap-x-4 gap-y-1 px-4 py-1 text-sm text-gray-700"
      aria-label={t('model.usage.label')}
      data-testid="usage.strip"
    >
      <div className="flex gap-1">
        <dt>{t('model.usage.tokens')}</dt>
        <dd data-testid="usage.tokens">
          {t('model.usage.tokens.value', {
            input: number(usage.input),
            output: number(usage.output),
            cache: number(usage.cache),
          })}
        </dd>
      </div>
      <div className="flex gap-1">
        <dt>{t('model.usage.cost')}</dt>
        <dd data-testid="usage.cost" data-basis={usage.bases.join(' ')}>
          {t('model.usage.cost.value', {
            cost: formatMoney(usage.costUsd, locale),
            basis: usage.bases.map((basis) => t(`model.usage.basis.${basis}`)).join(', '),
          })}
        </dd>
      </div>
      <div className="flex gap-1">
        <dt>{t('model.usage.context')}</dt>
        <dd data-testid="usage.context">
          {usage.context === null ? t('model.usage.context.unknown') : formatPercent(usage.context, locale)}
        </dd>
      </div>
    </dl>
  );
}
