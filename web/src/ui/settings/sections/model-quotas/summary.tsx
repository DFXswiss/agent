// The summary of an organization's model access (ADR-081 section 6): "Subscriptions: n available · m used up – next
// release 14:05", shown in the section "Model access and quotas" and as the counter of the situation view.
import type { OrgQuotas } from '../../../../core/credentials/orgQuotas';
import { formatClock, localTimeZone } from '../../../../core/i18n';
import { useNow } from '../../../countdown/useNow';
import { useI18n } from '../../../i18n';

/** A release time as `HH:MM` (with the date when it is not today) in the user's zone. */
export function useClock(): (at: number) => string {
  const { locale } = useI18n();
  const now = useNow(false);
  return (at) => formatClock(at, { locale, timeZone: localTimeZone(), now });
}

/** "Subscriptions: n available · m used up", with the next release when there is one. */
export function SummaryText({ summary }: { readonly summary: OrgQuotas['summary'] }) {
  const { t } = useI18n();
  const clock = useClock();
  const counts = { usable: summary.usable, exhausted: summary.exhausted };
  return summary.nextRelease === null
    ? t('model.org.summary', counts)
    : t('model.org.summary.release', { ...counts, time: clock(summary.nextRelease) });
}
