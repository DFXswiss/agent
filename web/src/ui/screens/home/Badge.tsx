// The badge of the navigation entry Home (06-web.md section 4.0): the number of tasks that wait for a decision of the
// user (`needs_decision_mine` of the counters); nothing while it is zero or unknown. Shown in the sidebar, the
// navigation row and the phone's bottom bar.
import { useSummary } from './hooks';
import { useI18n } from '../../i18n';

export function AttentionBadge({ org }: { readonly org: string }) {
  const { t } = useI18n();
  const summary = useSummary(org);
  const count = summary?.needs_decision_mine ?? 0;
  if (count === 0) return null;
  return (
    <span
      className="ml-1 inline-flex min-w-5 items-center justify-center rounded-full bg-red-700 px-1 text-xs text-white"
      data-testid="shell.badge.attention"
      data-count={count}
    >
      <span aria-hidden="true">{count > 99 ? '99+' : count}</span>
      <span className="sr-only">{t('attention.badge', { count })}</span>
    </span>
  );
}
