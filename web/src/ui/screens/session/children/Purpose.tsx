// The purpose of a subagent (06-web.md sections 4.0a and 5.5, ADR-017): the `purpose` its parent gave in the parameters
// of the `subagent.request` that created it (the child's `origin_operation_id`), decrypted in the browser; unverifiable
// ones are shown with their code. Shown by the session detail's children, the task's sessions tab and the agent tree of
// the situation view.
import { useMemo } from 'react';
import { purposeOf, selectOperation } from '../../../../core/operations/view';
import type { SessionEntry } from '../../../../core/store/slices/tasks';
import { useStore } from '../../../hooks/useStore';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';

/** The operation that created a child session (the row's `origin_operation_id`), or null. */
export function originOf(entry: SessionEntry): string | null {
  const row: Readonly<Record<string, unknown>> = entry.row as unknown as Readonly<Record<string, unknown>>;
  return typeof row.origin_operation_id === 'string' ? row.origin_operation_id : null;
}

export function Purpose({
  wg,
  operation,
  child,
}: {
  readonly wg: string;
  readonly operation: string;
  readonly child: string;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const view = useStore(
    useMemo(() => selectOperation(wg, operation), [wg, operation]),
    app.store,
  );
  const purpose = purposeOf(view, child);
  if (purpose === null) return null;
  if (purpose.status === 'unverifiable')
    return (
      <span className="text-xs text-gray-500" data-testid="children.purpose" data-status="unverifiable">
        {t('children.purpose.unverifiable', { code: purpose.code })}
      </span>
    );
  return (
    <span className="break-words text-sm" data-testid="children.purpose" data-status="verified">
      {t('children.purpose', { purpose: purpose.text })}
    </span>
  );
}
