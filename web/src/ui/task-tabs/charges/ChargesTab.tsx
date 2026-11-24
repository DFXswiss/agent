// Tab "Costs" of a task (12-billing.md sections 6 and 9, ADR-062 section 7): the charges of the task per session -
// model tokens and VM runtime - and the total (`GET /v1/tasks/{task}/charges`), read when the tab opens.
import { useCallback, useEffect, useState } from 'react';
import { formatRuntime, formatUsd, loadTaskCharges, type TaskCharges } from '../../../core/billing/billing';
import { requestErrorCode } from '../../../core/requests/client';
import type { TaskTabProps } from '../../registries/task-tabs';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useReason } from '../../settings/sections/github/reason';

export function ChargesTab({ taskId }: TaskTabProps) {
  const { app } = useAppEnv();
  const { t, locale } = useI18n();
  const reason = useReason();
  const [charges, setCharges] = useState<TaskCharges | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const load = useCallback(() => {
    loadTaskCharges(app.tasks, taskId).then(setCharges, (error: unknown) => setLoadError(requestErrorCode(error)));
  }, [app, taskId]);
  useEffect(load, [load]);
  const reload = () => {
    setLoadError(null);
    load();
  };
  return (
    <div className="flex flex-col gap-3 p-2" data-testid="charges.tab">
      {loadError !== null && (
        <p role="alert" data-testid="charges.load-failed" data-code={loadError}>
          {t('billing.load-failed', { reason: reason(loadError) })}
        </p>
      )}
      {charges !== null && charges.sessions.length === 0 && (
        <p data-testid="charges.empty">{t('billing.task.empty')}</p>
      )}
      {charges !== null && charges.sessions.length > 0 && (
        <>
          <p className="font-semibold" data-testid="charges.total">
            {t('billing.task.total', { amount: formatUsd(charges.totalMicroUsd, locale) })}
          </p>
          <ul data-testid="charges.sessions">
            {charges.sessions.map((row) => (
              <li key={row.sessionId}>
                {t('billing.task.session', {
                  session: row.sessionId.slice(0, 8),
                  tokens: formatUsd(row.tokensMicroUsd, locale),
                  count: row.tokens.toLocaleString(locale),
                  runtime: formatUsd(row.runtimeMicroUsd, locale),
                  duration: formatRuntime(row.runtimeMs),
                })}
              </li>
            ))}
          </ul>
        </>
      )}
      <button
        type="button"
        className="min-h-11 self-start rounded border border-gray-400 px-4"
        onClick={reload}
        data-testid="charges.reload"
      >
        {t('billing.reload')}
      </button>
    </div>
  );
}
