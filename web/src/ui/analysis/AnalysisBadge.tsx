// The badge "Analysis" of an analysis task (ADR-095 section 6) in task rows, the situation view's entries and the task
// page; nothing for any other task.
import { isAnalysis } from '../../core/analysis/analysis';
import type { TaskRow } from '../../core/store/slices/tasks';
import { useI18n } from '../i18n';

export function AnalysisBadge({ row, testId }: { readonly row: TaskRow; readonly testId: string }) {
  const { t } = useI18n();
  if (!isAnalysis(row)) return null;
  return (
    <span className="self-start rounded bg-purple-100 px-2 text-xs text-purple-900" data-testid={testId}>
      {t('analysis.badge')}
    </span>
  );
}
